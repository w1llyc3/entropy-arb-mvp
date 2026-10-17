"""Durable, explicitly simulated IOC execution. Never imports live transports.

Retained fake exchange truth is distinct from confirmed fill evidence, allowing
lost acknowledgements/restarts to be queried without replaying a send. This is
a depth snapshot model, not a claim about real queue position or fill probability.
"""
from decimal import Decimal, ROUND_DOWN
import hashlib
import math

from .workbench import encode, SessionConflict
from .session_reconciliation import reconcile

D = Decimal
VENUES = ("entropy", "lighter")
FINAL = ("FILLED", "CANCELED", "REJECTED", "NOT_SENT")


class PaperSession:
    def __init__(self, store, *, faults=None, hook=None):
        self.store, self.db = store, store.db
        self.faults = faults or {}
        self.hook = hook or (lambda stage, value: None)

    def orders(self, sid):
        return [dict(r) for r in self.db.execute("SELECT * FROM paper_orders WHERE session_id=? ORDER BY created,id", (sid,))]

    def fills(self, sid):
        return [dict(r) for r in self.db.execute("SELECT * FROM paper_fills WHERE session_id=? AND confirmed=1 ORDER BY observed,order_id", (sid,))]

    def positions(self, sid, *, truth=False):
        positions = {v:D(0) for v in VENUES}
        query="SELECT f.*,o.side FROM paper_fills f JOIN paper_orders o ON f.order_id=o.id WHERE f.session_id=?"
        if not truth:
            query += " AND f.confirmed=1"
        for fill in self.db.execute(query,(sid,)):
            positions[fill["venue"]] += D(fill["qty"]) * (1 if fill["side"] else -1)
        return positions

    def can_finish(self, sid):
        return (all(D(q)==0 for q in self.positions(sid,truth=True).values())
                and all(o["state"] in FINAL for o in self.orders(sid))
                and self.projection(sid)["reconciliation"]["complete"])

    def projection(self, sid):
        with self.store.lock:
            return self._projection(sid)

    def _projection(self, sid):
        orders, fills = self.orders(sid), self.fills(sid)
        unknown = any(o["state"] not in FINAL for o in orders)
        positions = self.positions(sid)
        cash=D(0); fees=D(0); histogram={};price_histogram={};turnover=D(0)
        running={v:D(0) for v in VENUES};peak={v:D(0) for v in VENUES};held_since=None
        price_width=D(fills[0]["price"])*D(".001") if fills else D(1)
        for f in fills:
            side=self.db.execute("SELECT side,edge_bps,reduce_only FROM paper_orders WHERE id=?",(f["order_id"],)).fetchone()
            cash += D(f["qty"])*D(f["price"])*(-1 if side["side"] else 1)
            fees += D(f["fee"])
            turnover += D(f["qty"])*D(f["price"])
            if not any(running.values()): held_since=f["observed"]
            running[f["venue"]]+=D(f["qty"])*(1 if side["side"] else -1)
            peak[f["venue"]]=max(peak[f["venue"]],abs(running[f["venue"]]))
            if not any(running.values()): held_since=None
            bucket=str((D(side["edge_bps"])/5).to_integral_value(rounding="ROUND_FLOOR")*5)
            phase="reduce" if side["reduce_only"] else "open"
            item=histogram.setdefault((phase,bucket),dict(intent=phase,lower_bps=bucket,upper_bps=str(D(bucket)+5),fill_count=0,notional_usd="0"))
            item["fill_count"]+=1
            item["notional_usd"]=str(D(item["notional_usd"])+D(f["qty"])*D(f["price"]))
            price_bucket=(D(f["price"])/price_width).to_integral_value(rounding="ROUND_FLOOR")*price_width
            price_key=(f["venue"],str(price_bucket))
            point=price_histogram.setdefault(price_key,dict(venue=f["venue"],lower_usd=str(price_bucket),upper_usd=str(price_bucket+price_width),fill_count=0,base_qty="0"))
            point["fill_count"]+=1
            point["base_qty"]=str(D(point["base_qty"])+D(f["qty"]))
        flat=not unknown and all(q==0 for q in positions.values())
        status=self.db.execute("SELECT status,reason FROM paper_status WHERE session_id=?",(sid,)).fetchone()
        signal=self.db.execute("SELECT payload FROM paper_signals WHERE session_id=?",(sid,)).fetchone()
        import json
        claims=[dict(venue=o["venue"],account_domain="paper:"+sid,symbol=o["symbol"],client_id=o["id"],side=o["side"],qty=o["qty"],reduce_only=o["reduce_only"]) for o in orders]
        reports=[dict(claim,state=o["state"],filled=str(next((D(f["qty"]) for f in fills if f["order_id"]==o["id"]),D(0)))) for claim,o in zip(claims,orders)]
        trade_rows=[]
        for fill in fills:
            order=next(o for o in orders if o["id"]==fill["order_id"])
            trade_rows.append(dict(venue=fill["venue"],account_domain="paper:"+sid,symbol=order["symbol"],client_id=order["id"],side=order["side"],trade_id=fill["order_id"]+":fill",qty=fill["qty"],price=fill["price"]))
        reconciliation=reconcile(claims,reports,trade_rows,positions=self.positions(sid,truth=True),baseline={v:0 for v in VENUES},orders_complete=True)
        unknown=unknown or not reconciliation["complete"]
        flat=not unknown and all(q==0 for q in positions.values())
        return dict(mode="PAPER_SIMULATION",positions={v:str(q) if not unknown else None for v,q in positions.items()},
                    fees_usd=str(fees), realized_pnl_before_funding_usd=str(cash-fees) if flat else None,
                    cash_flow_before_funding_usd=str(cash-fees), funding_verified=False,
                    recovery_required=unknown, flat=flat, orders=[{k:v for k,v in o.items() if k!="truth_state"} for o in orders], fills=fills,
                    signal=json.loads(signal["payload"]) if signal else None,
                    fill_count=len(fills), turnover_usd=str(turnover),
                    peak_inventory_qty={v:str(q) for v,q in peak.items()},
                    inventory_held_sec=max(0,self.store.now()-held_since) if held_since is not None else 0,
                    oldest_unknown_age_sec=max((self.store.now()-o["created"] for o in orders if o["state"] not in FINAL),default=None),
                    fill_edge_distribution=sorted(histogram.values(),key=lambda x:D(x["lower_bps"])),
                    fill_price_distribution=list(price_histogram.values()),
                    price_bucket_rule="0.1% of the first confirmed simulated fill price",
                    status=status["status"] if status else "IDLE", reason=status["reason"] if status else "",
                    actual_exchange_orders_sent=0,
                    reconciliation=reconciliation,
                    model="IOC depth snapshot; configured fees/slippage; no latency, queue or actual funding model")

    def status(self, sid, status, reason):
        self.db.execute("INSERT INTO paper_status VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET status=excluded.status,reason=excluded.reason",
                        (sid,status,reason))

    def reconcile(self, sid):
        """Query retained fake truth, never resubmit or infer no order from silence."""
        with self.store.transaction():
            self.query_truth(sid)
        return self.projection(sid)

    def query_truth(self, sid):
        if self.store._session(sid)["mode"]!="PAPER_SIMULATION":
            raise SessionConflict("paper Session required")
        for order in self.orders(sid):
            if order["state"] in ("SUBMITTING", "UNKNOWN"):
                if order["truth_state"] in FINAL:
                    self.db.execute("UPDATE paper_orders SET state=truth_state WHERE id=?",(order["id"],))
                    self.db.execute("UPDATE paper_fills SET confirmed=1 WHERE order_id=?",(order["id"],))
                else:
                    self.status(sid,"UNKNOWN","simulated exchange outcome still unknown")
        self.store.event(sid,"PAPER_ORDERS_QUERIED_NO_RESEND")

    @staticmethod
    def valid_books(books, now):
        if set(books)!=set(VENUES):
            return False
        try:
            times=[]
            for book in books.values():
                times.append(book.last_update_ts)
                if not book.is_fresh(5) or not 0<=now-book.last_update_ts<=5:
                    return False
                for levels in (book.bids,book.asks):
                    if not levels or any(not math.isfinite(px) or not math.isfinite(qty) or px<=0 or qty<=0 for px,qty in levels.items()):
                        return False
                if book.best_bid()>=book.best_ask():
                    return False
            return abs(times[0]-times[1])<=2
        except (TypeError,ValueError,AttributeError):
            return False

    def _execute(self, sid, books, rules):
        for order in self.orders(sid):
            if order["state"]!="PREPARED":
                continue
            if not order["reduce_only"] and self.store._session(sid)["state"]!="OBSERVING":
                with self.store.transaction():
                    self.db.execute("UPDATE paper_orders SET state='NOT_SENT',truth_state='NOT_SENT' WHERE id=? AND state='PREPARED'",(order["id"],))
                continue
            with self.store.transaction():
                self.db.execute("UPDATE paper_orders SET state='SUBMITTING' WHERE id=? AND state='PREPARED'",(order["id"],))
            self.hook("after_claim",order)
            # A SUBMITTING order is never replayed on restart. Its absence in
            # fake truth is UNKNOWN, matching the production uncertainty rule.
            book=books[order["venue"]]
            levels=sorted((book.asks if order["side"] else book.bids).items(),reverse=not bool(order["side"]))
            remaining=D(order["qty"]); filled=D(0); value=D(0)
            reducible=abs(self.positions(sid,truth=True)[order["venue"]]) if order["reduce_only"] else remaining
            if self.faults.get(order["venue"])=="reject":
                levels=[]
            for px,depth in levels:
                px,depth=D(str(px)),D(str(depth))
                if (order["side"] and px>D(order["limit_px"])) or (not order["side"] and px<D(order["limit_px"])):
                    break
                amount=min(remaining,depth,reducible)
                if self.faults.get(order["venue"])=="partial":
                    amount=min(amount,D(order["qty"])/2)
                # Model adverse execution within the configured price cap.
                slipped=px*(1+(D(str(rules["slippage_bps"]))/10000)*(1 if order["side"] else -1))
                slipped=min(slipped,D(order["limit_px"])) if order["side"] else max(slipped,D(order["limit_px"]))
                filled+=amount;value+=amount*slipped;remaining-=amount;reducible-=amount
                if remaining<=0 or self.faults.get(order["venue"])=="partial":
                    break
            truth="REJECTED" if self.faults.get(order["venue"])=="reject" else "FILLED" if remaining==0 else "CANCELED"
            lost=self.faults.get(order["venue"])=="lose_ack"
            with self.store.transaction():
                self.db.execute("UPDATE paper_orders SET state=?,truth_state=? WHERE id=?",("UNKNOWN" if lost else truth,truth,order["id"]))
                if filled:
                    fee=value*D(str(rules["fee_"+order["venue"]+"_bps"]))/10000
                    self.db.execute("INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?)",(order["id"],sid,order["venue"],str(filled),str(value/filled),str(fee),self.store.now(),int(not lost)))
                self.store.event(sid,"PAPER_IOC_RESULT",dict(order_id=order["id"],state="UNKNOWN" if lost else truth))
            self.hook("after_accept",order)

    def tick(self, sid, books):
        with self.store.lock:
            return self._tick(sid,books)

    def _tick(self, sid, books):
        session=self.store._session(sid)
        if session["mode"]!="PAPER_SIMULATION" or session["state"] not in ("OBSERVING","PAUSED","DRAINING"):
            return
        if not self.valid_books(books,self.store.now()):
            with self.store.transaction(): self.status(sid,"WAITING_FOR_FRESH_BOOKS","requires fresh paired depth with <=2s skew")
            return
        rules=session["rules"]
        # A previous committed intent is processed before any new decision.
        if any(o["state"]=="PREPARED" for o in self.orders(sid)):
            self._execute(sid,books,rules)
        if self.projection(sid)["recovery_required"]:
            with self.store.transaction():
                self.status(sid,"UNKNOWN","query paper orders before any new execution")
                self.db.execute("UPDATE sessions SET state='RECOVERY_REQUIRED',version=version+1 WHERE id=? AND state!='RECOVERY_REQUIRED'",(sid,))
            return
        positions=self.positions(sid)
        long="entropy" if rules["direction"]=="BUY_ENTROPY" else "lighter"
        short="lighter" if long=="entropy" else "entropy"
        entry=(D(str(books[short].best_bid()))/D(str(books[long].best_ask()))-1)*10000
        exit_edge=(D(str(books[short].best_ask()))/D(str(books[long].best_bid()))-1)*10000
        costs=2*(D(str(rules["fee_entropy_bps"]))+D(str(rules["fee_lighter_bps"])))+4*D(str(rules["slippage_bps"]))+D(str(rules["funding_buffer_bps"]))
        residual=abs(sum(positions.values()))
        marked=sum(q*D(str(books[v].best_bid() if q>=0 else books[v].best_ask())) for v,q in positions.items())
        equity=D(self.projection(sid)["cash_flow_before_funding_usd"])+marked
        with self.store.transaction():
            self.db.execute("INSERT INTO paper_signals VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload",(sid,encode(dict(entry_gross_bps=str(entry),entry_net_bps=str(entry-costs),exit_residual_bps=str(exit_edge),round_trip_cost_buffer_bps=str(costs),marked_pnl_before_funding_usd=str(equity),observed_at=self.store.now()))))
        has_inventory=any(q!=0 for q in positions.values())
        if equity<=-D(str(rules["max_loss_usd"])) and session["state"]!="DRAINING":
            with self.store.transaction():
                self.db.execute("UPDATE sessions SET state='PAUSED',version=version+1 WHERE id=? AND state='OBSERVING'",(sid,))
                self.status(sid,"LOSS_LIMIT","paper loss limit reached; new entries blocked")
            session["state"]="PAUSED"
        reducing=has_inventory and (session["state"]=="DRAINING" or residual>0 or equity<=-D(str(rules["max_loss_usd"])) or exit_edge<=D(str(rules["exit_bps"])))
        if session["state"]=="DRAINING" and not has_inventory:
            self.finish(sid)
            return
        if not reducing and (session["state"]!="OBSERVING" or entry-costs<D(str(rules["entry_bps"]))):
            with self.store.transaction(): self.status(sid,"WAITING","no permitted entry/exit signal")
            return
        specs=[]
        qty=(D(str(rules["order_usd"]))/max(D(str(b.best_ask())) for b in books.values())/(1+D(str(rules["slippage_bps"]))/10000)).quantize(D(".000001"),rounding=ROUND_DOWN)
        for venue in VENUES:
            side=positions[venue]<0 if reducing else venue==long
            amount=abs(positions[venue]) if reducing else qty
            if amount==0:
                continue
            top=D(str(books[venue].best_ask() if side else books[venue].best_bid()))
            limit=top*(1+D(str(rules["slippage_bps"]))/10000*(1 if side else -1))
            if not reducing and (amount*limit>D(str(rules["order_usd"])) or (abs(positions[venue])+amount)*limit>D(str(rules["max_inventory_usd"]))):
                with self.store.transaction(): self.status(sid,"RISK_BLOCKED","per-leg inventory/order limit")
                return
            if not reducing and amount*limit>D(str(rules["max_unhedged_usd"])):
                with self.store.transaction(): self.status(sid,"RISK_BLOCKED","worst single-leg fill exceeds unhedged limit")
                return
            specs.append((venue,side,amount,limit))
        if not specs:
            return
        sample=encode(dict(t=[books[v].last_update_ts for v in VENUES],reduce=reducing))
        batch=hashlib.sha256((sid+sample).encode()).hexdigest()
        with self.store.transaction():
            if self.db.execute("SELECT 1 FROM paper_orders WHERE session_id=? AND batch=?",(sid,batch)).fetchone():
                return
            for venue,side,amount,limit in specs:
                oid=hashlib.sha256((sid+batch+venue).encode()).hexdigest()[:32]
                self.db.execute("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(oid,sid,batch,venue,rules["symbol"],int(side),str(amount),str(limit),int(reducing),"PREPARED","PREPARED",self.store.now(),str(exit_edge if reducing else entry)))
            self.store.event(sid,"PAPER_INTENTS_COMMITTED",dict(batch=batch,reduce_only=reducing,entry_net_bps=str(entry-costs)))
            self.status(sid,"INTENTS_COMMITTED","simulated IOC intents persisted before processing")
        self.hook("after_prepare",batch)
        self._execute(sid,books,rules)
        if self.projection(sid)["recovery_required"]:
            with self.store.transaction():
                self.db.execute("UPDATE sessions SET state='RECOVERY_REQUIRED',version=version+1 WHERE id=?",(sid,))
                self.status(sid,"UNKNOWN","simulated acknowledgement lost; query retained truth")
        elif session["state"]=="DRAINING" and self.can_finish(sid):
            self.finish(sid)

    def finish(self, sid):
        with self.store.transaction():
            if not self.can_finish(sid):
                raise SessionConflict("paper orders/inventory remain")
            self.db.execute("UPDATE sessions SET state='ENDED',ended=?,version=version+1 WHERE id=?",(self.store.now(),sid))
            self.store.event(sid,"PAPER_DRAIN_COMPLETED")
            self.db.execute("INSERT OR IGNORE INTO reports VALUES(?,?)",(sid,encode(self.store._report(sid))))
