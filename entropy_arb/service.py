"""Single-owner persistent service. Production is read-only; fake execution is opt-in.

Commands are serialized by one background worker. HTTP GET reads a cached
projection and cannot drive strategy evaluation or touch account secrets.
"""
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import uuid

import aiohttp

from .execution import OfflineExecutor
from .ownership import OwnerLock
from .read_only import PublicAccountReader
from .recovery import RecoveryCoordinator
from .risk import RiskEngine, RiskRejected
from .storage import Ledger, LegSpec, RecoveryRequired, IdempotencyConflict, TERMINAL
from .strategy import candidate


class ServiceError(RuntimeError): pass


class PublicCollector:
    def __init__(self, root, config=None):
        self.root=Path(root); self.engine=None; self.task=None
        self.config=config
        self.flag=self.root/".web"/"collector.pause"

    @property
    def books(self):
        if self.engine is None or not self.engine.markets_ready: return {}
        return {"entropy":self.engine.entropy.book,"lighter":self.engine.hedge.book}

    async def start(self):
        if self.task and not self.task.done(): return
        from .config import load_config
        from .engine import Engine
        self.flag.unlink(missing_ok=True)
        config=self.root/"config.yaml"
        if not config.exists(): config=self.root/"config.example.yaml"
        cfg=self.config or load_config(str(config),None,symbol="SNDK",hedge_venue="lighter")
        cfg.recorder_csv=str(self.root/"logs"/"minutes.csv")
        self.engine=Engine(cfg,record_only=True)
        self.engine.record_pause_file=str(self.flag)
        self.task=asyncio.create_task(self.engine.run(),name="public-collector")

    def pause(self, enabled):
        if enabled:
            self.flag.parent.mkdir(parents=True,exist_ok=True); self.flag.touch()
        else: self.flag.unlink(missing_ok=True)

    async def close(self):
        if not self.task: return
        if self.engine: self.engine.request_stop()
        try: await asyncio.wait_for(asyncio.shield(self.task),15)
        except (Exception,asyncio.CancelledError):
            self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True)
        self.task=None


class TradingService:
    def __init__(self, root, *, public_accounts=None, venues=None, books=None,
                 simulation=False, collector=None, interval=.2, reconcile_interval=3,
                 persist_sec=3, lock_dir=None, in_rth=None):
        self.root=Path(root).resolve()
        self.public_accounts=dict(public_accounts or {})
        address=self.public_accounts.get("entropy")
        if isinstance(address,str): self.public_accounts["entropy"]=address.lower()
        index=self.public_accounts.get("lighter")
        if index is not None and str(index).isdigit(): self.public_accounts["lighter"]=str(int(index))
        self.simulation=simulation
        if venues and not simulation: raise ValueError("injected execution venues require simulation mode")
        if simulation and (not venues or any(getattr(v,"is_simulation",False) is not True for v in venues.values())):
            raise ValueError("simulation requires fake venues")
        self.venues=venues or {}
        self.accounts=({n:v.account for n,v in venues.items()} if venues else
                       {n:str(self.public_accounts.get(n) or "unconfigured") for n in ("entropy","lighter")})
        if set(self.accounts)!={"entropy","lighter"}: raise ValueError("SNDK pair required")
        self.owner=OwnerLock(dict(root=str(self.root)),lock_dir)
        self.account_owner=(OwnerLock(dict(market="SNDK",deployment="io/lighter-mainnet",accounts=self.accounts),lock_dir)
                            if all(a!="unconfigured" for a in self.accounts.values()) else None)
        self.collector=collector or (None if simulation else PublicCollector(self.root))
        self._books=books
        self.interval,self.reconcile_interval=interval,reconcile_interval
        self.persist_sec=persist_sec if simulation else 3
        self.in_rth=in_rth
        self.queue=asyncio.Queue()
        self.worker=None; self.http=None; self.ledger=None; self.monitor=None
        self.recording=False; self.paused=False; self.closing=False
        self.recovered=False; self.reason="not initialized"; self.state="BOOTING"
        self.task_spec=None; self.armed=None; self._cache={}
        self.last_recovery=0; self.heartbeat=0; self.last_order=None
        self._collector_retry=0

    @property
    def books(self): return self._books if self._books is not None else (self.collector.books if self.collector else {})

    async def start(self):
        if self.worker: raise ServiceError("service already started")
        if (self.root/".web"/"recorder.pid").exists():
            raise ServiceError("legacy recorder metadata present: verify and stop legacy collector manually")
        if not self.owner.acquire(): raise ServiceError("another service owns this account/market")
        try:
            if self.account_owner and not self.account_owner.acquire():
                raise ServiceError("another service owns these accounts in another workspace")
            (self.root/".web").mkdir(parents=True,exist_ok=True)
            self.ledger=Ledger(self.root/".web"/"ledger.sqlite")
            domain=dict(mode="SIMULATION" if self.simulation else "RECORD_ONLY",accounts=self.accounts)
            old=self.ledger.get_setting("account_domain")
            if old is not None and old!=domain:
                binding=(old["mode"]==domain["mode"]=="RECORD_ONLY" and not self.ledger.all_operations()
                         and all(old["accounts"].get(n) in ("unconfigured",self.accounts[n]) for n in self.accounts))
                if not binding: raise ServiceError("ledger account/domain mismatch")
            self.ledger.set_setting("account_domain",domain)
            if not self.simulation:
                self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
                self.venues={n:PublicAccountReader(n,self.public_accounts.get(n),self.http) for n in self.accounts}
            self.recovery=RecoveryCoordinator(self.ledger,self.venues,self.accounts)
            self.risk=RiskEngine()
            self.executor=OfflineExecutor(self.ledger,self.venues,allow_submit=True) if self.simulation else None
            self.task_spec=self.ledger.get_setting("task")
            self.recording=bool(self.ledger.get_setting("recording",False))
            # Do not inherit LIVE/paused/armed/confirmation acknowledgements on restart.
            if self.task_spec:
                self.task_spec["mode"]="record"
                self.ledger.set_setting("task",self.task_spec)
            self.ledger.cancel_proposal()
            self.state="RECOVERING"
            await self._reconcile(full=True)
            self.worker=asyncio.create_task(self._run(),name="persistent-trading-service")
            self.monitor=asyncio.create_task(self._monitor(),name="service-heartbeat")
            self._project()
        except BaseException:
            if self.http: await self.http.close()
            if self.ledger: self.ledger.close(); self.ledger=None
            if self.account_owner: self.account_owner.close()
            self.owner.close(); raise

    async def call(self, command, payload=None):
        if self.closing or not self.worker or self.worker.done(): raise ServiceError("service unavailable")
        future=asyncio.get_running_loop().create_future()
        # A disconnected caller cannot cancel execution; consume later errors
        # even when that caller no longer awaits the response.
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        await self.queue.put((command,payload or {},future))
        return await asyncio.shield(future)

    async def _run(self):
        while not self.closing or not self.queue.empty():
            try: item=await asyncio.wait_for(self.queue.get(),self.interval)
            except asyncio.TimeoutError: item=None
            if item:
                command,payload,future=item
                try:
                    result=await self._handle(command,payload)
                    self._project()
                    if not future.done(): future.set_result(result)
                except asyncio.CancelledError:
                    if not future.done(): future.set_exception(ServiceError("service stopped; query operation before retry"))
                    raise
                except Exception as exc:
                    if not isinstance(exc,(ServiceError,RiskRejected,ValueError,IdempotencyConflict)):
                        self.recovered=False; self.state="HALTED"; self.reason=type(exc).__name__
                    if not future.done(): future.set_exception(exc)
                finally: self.queue.task_done()
            if not self.closing:
                try: await self._tick()
                except Exception as exc:
                    self.recovered=False; self.state="HALTED"; self.reason=type(exc).__name__
            self._project()

    async def _monitor(self):
        while not self.closing:
            self.heartbeat=time.time()
            self._cache["heartbeat"]=self.heartbeat
            self._cache["service_state"]=self.state
            self._cache["trading_ready"]=(self.state=="SIMULATION_READY" and self.recovered
                                           and self.recording and not self.paused)
            if self.worker and self.worker.done():
                self._cache.update(service_state="HALTED",trading_ready=False,recovery_reason="worker exited")
            await asyncio.sleep(.5)

    async def _reconcile(self, *, full=False):
        self.state="RECONCILING"
        self.recovered=False
        result=await self.recovery.run(full=full)
        self.recovered=result["ok"]; self.reason=result["reason"]
        self.last_recovery=time.monotonic()
        self.state=("PAUSED" if self.paused else "SIMULATION_READY" if self.recovered and self.simulation else
                    "RECORD_ONLY" if not self.simulation else "RECOVERING")
        return result

    async def _handle(self, command, payload):
        from web.probe import normalize_task, decision_defaults
        if command=="task":
            spec=normalize_task(payload)
            if spec["mode"]!="record": raise ServiceError("LIVE disabled: public read-only service")
            if self.ledger.pending_operations(): raise ServiceError("resolve existing operations before editing task")
            self.task_spec=spec; self.armed=None
            self.ledger.cancel_proposal(); self.ledger.set_setting("task",spec)
            return dict(ok=True,task=spec)
        if command=="clear":
            if self.recording or self.ledger.pending_operations(): raise ServiceError("stop and reconcile first")
            self.task_spec=None; self.ledger.cancel_proposal(); self.ledger.set_setting("task",None)
            return dict(ok=True)
        if command=="start":
            if payload.get("symbol","SNDK")!="SNDK" or payload.get("hedge","lighter")!="lighter":
                raise ServiceError("Phase 1 supports SNDK Entropy/Lighter mainnet only")
            if self.recording and not self.paused: raise ServiceError("already running")
            if self.task_spec is None:
                self.task_spec=decision_defaults(); self.ledger.set_setting("task",self.task_spec)
            self.paused=False; self.recording=True
            self.ledger.set_setting("recording",True)
            if self.collector:
                await self.collector.start(); self.collector.pause(False)
            await self._reconcile()
            return dict(ok=True,running=True,record_only=True,live_armed=False,resumed=True)
        if command=="pause":
            if not self.recording: raise ServiceError("nothing running")
            self.paused=True; self.armed=None; self.state="PAUSED"
            self.ledger.cancel_proposal()
            if self.collector: self.collector.pause(True)
            return dict(ok=True,paused=True)
        if command=="stop":
            self.state="DRAINING"; self.recording=False; self.paused=False; self.armed=None
            self.ledger.set_setting("recording",False); self.ledger.cancel_proposal()
            if self.collector: await self.collector.close()
            self.state="STOPPED"
            return dict(ok=True,stopped=True,running=False)
        if command=="reconcile": return await self._reconcile(full=True)
        if command=="cancel":
            self.ledger.cancel_proposal(); self.armed=None
            return dict(ok=True,skipped=True,sent=False)
        if command=="confirm": return await self._confirm(payload)
        if command=="flatten": return await self._flatten(payload)
        raise ServiceError("unknown service command")

    def _specs(self, proposal):
        return [LegSpec(p["venue"],p["account"],Decimal(p["qty"]),Decimal(p["limit_px"]),
                        p["is_buy"],p["reduce_only"]) for p in proposal["specs"]]

    async def _confirm(self, payload):
        if payload.get("confirm") is not True: raise ServiceError("explicit confirmation required")
        if not payload.get("proposal_id"): raise ServiceError("stable proposal_id required")
        proposal=self.ledger.proposal(payload.get("proposal_id"))
        if not proposal: raise ServiceError("proposal unavailable")
        key=payload.get("idempotency_key") or "confirm:"+proposal["proposal_id"]
        specs=self._specs(proposal)
        if proposal["operation_id"]:
            op=self.ledger.create_intent(key,proposal["proposal_id"],proposal["intent"],specs)
            return dict(ok=True,replayed=True,sent=False,operation=self.ledger.operation(op),operation_id=op)
        if not self.simulation: raise ServiceError("RECORD_ONLY: real execution remains disabled")
        if not self.recording or self.paused: raise ServiceError("start or resume before confirming")
        if proposal["status"]!="PENDING" or time.time()>proposal["expires"]: raise ServiceError("proposal expired or canceled")
        await self._reconcile()
        reduction=proposal["intent"] in ("reduce","close")
        if reduction and not self._owned_reduction(): raise RiskRejected("unresolved orders or external positions")
        if self.task_spec.get("rth_only",True) and not self._in_rth():
            ack="force_ack:"+proposal["proposal_id"]
            if payload.get("force_confirm") is not True:
                self.ledger.set_setting(ack,True)
                return dict(ok=False,sent=False,needs_force_confirm=True,gap="非 RTH：需再次强制确认")
            if self.ledger.get_setting(ack) is not True: raise RiskRejected("first risk acknowledgement required")
        if not reduction and self._fee_mismatch(): raise RiskRejected("confirmed fee differs from assumption")
        self._check_send_budget()
        self.risk.check(specs,self.recovery.snapshots,self.books,self.task_spec,
                        recovered=self.recovered,reduction=reduction)
        # Preserve minimum-size/grid/cap checks on the actual approved IOC bounds.
        op=self.ledger.create_intent(key,proposal["proposal_id"],proposal["intent"],specs,
                                     baselines=self.recovery.snapshots)
        self.recovered=False; self.state="EXECUTING"
        self.risk.record_send()
        try: result=await asyncio.wait_for(self.executor.execute(op),10)
        finally: self.armed=None
        self.last_order=dict(confirm_id=key,operation_id=op,sent=True,status=result["state"])
        await self._reconcile()
        if proposal["intent"]=="open":
            actual=self._entry_edge(op)
            if actual is not None: self.ledger.set_setting("entry",dict(operation_id=op,pre_fee_edge_bps=actual))
        elif self.recovered and all(s.position==0 for s in self.recovery.snapshots.values()):
            self.ledger.set_setting("entry",{})
        return dict(ok=True,sent=True,operation_id=op,operation=self.ledger.operation(op))

    def _owned_reduction(self):
        baseline=self.ledger.get_setting("managed_baseline")
        if not baseline or set(self.recovery.snapshots)!={"entropy","lighter"}: return False
        expected={n:Decimal(baseline[n]["position"]) for n in self.accounts}
        for op in self.ledger.all_operations():
            for leg in op["legs"]:
                if leg["outcome"] not in TERMINAL or leg["account"]!=self.accounts.get(leg["venue"]): return False
                if self.ledger.fill_total(leg["id"])!=Decimal(leg["filled"]): return False
                expected[leg["venue"]]+=Decimal(leg["filled"])*(1 if leg["is_buy"] else -1)
        with self.ledger.lock:
            boundary=self.ledger.db.execute("SELECT max(created) FROM events WHERE kind='SEND_BOUNDARY_COMMITTED'").fetchone()[0]
        return all(s.valid(time.time()) and not s.open_orders and s.position==expected[n]
                   and (not boundary or (s.settled_through is not None and s.settled_through>=boundary))
                   for n,s in self.recovery.snapshots.items())

    async def _flatten(self, payload):
        if not self.simulation: raise ServiceError("RECORD_ONLY: manual flatten cannot submit real orders")
        if payload.get("confirm") is not True: raise ServiceError("explicit confirmation required")
        if not payload.get("idempotency_key"): raise ServiceError("stable idempotency_key required")
        # A retried flatten returns the same operation, never a new quantity.
        key=payload["idempotency_key"]
        with self.ledger.lock:
            row=self.ledger.db.execute("SELECT operation_id FROM commands WHERE key=?",(key,)).fetchone()
        if not payload.get("proposal_id"): raise ServiceError("flatten proposal_id required")
        if row:
            operation=self.ledger.operation(row[0])
            if operation["proposal"]!="flatten:"+payload["proposal_id"] or operation["intent"]!="reduce":
                raise IdempotencyConflict("flatten key belongs to a different request")
            return dict(ok=True,replayed=True,sent=False,operation_id=row[0],operation=operation)
        await self._reconcile()
        if not self._owned_reduction(): raise RiskRejected("cannot flatten external or unverified positions")
        offer=self.flatten_offer()
        if not offer or payload["proposal_id"]!=offer["proposal_id"]: raise RiskRejected("flatten positions changed")
        specs=[]
        for n,s in self.recovery.snapshots.items():
            if not s.position: continue
            book=self.books.get(n)
            if book is None: raise RiskRejected("no book")
            buy=s.position<0; px=book.best_ask() if buy else book.best_bid()
            if px is None: raise RiskRejected("no executable price")
            specs.append(LegSpec(n,self.accounts[n],abs(s.position),Decimal(str(px)),buy,True))
        if not specs: raise ServiceError("already flat")
        self._check_send_budget()
        self.risk.check(specs,self.recovery.snapshots,self.books,self.task_spec or {},recovered=False,reduction=True)
        op=self.ledger.create_intent(key,"flatten:"+payload["proposal_id"],"reduce",specs,baselines=self.recovery.snapshots)
        self.risk.record_send(); self.recovered=False
        await asyncio.wait_for(self.executor.execute(op),10)
        await self._reconcile()
        return dict(ok=True,sent=True,operation_id=op,operation=self.ledger.operation(op))

    def _in_rth(self):
        if self.in_rth is not None: return self.in_rth()
        from datetime import datetime,timezone
        from web.probe import in_us_rth
        return in_us_rth(datetime.now(timezone.utc))

    def _check_send_budget(self):
        with self.ledger.lock:
            sends=self.ledger.db.execute("SELECT count(*) FROM events WHERE kind='SEND_BOUNDARY_COMMITTED' AND created>=?",
                                         (time.time()-60,)).fetchone()[0]
        if sends>=30: raise RiskRejected("persistent send budget exhausted")

    def _entry_edge(self, op):
        prices={}
        for leg in self.ledger.operation(op)["legs"]:
            with self.ledger.lock:
                rows=list(self.ledger.db.execute("SELECT qty,price FROM fills WHERE leg_id=?",(leg["id"],)))
            qty=sum((Decimal(r[0]) for r in rows),Decimal(0))
            if not qty: return None
            prices["buy" if leg["is_buy"] else "sell"]=sum((Decimal(r[0])*Decimal(r[1]) for r in rows),Decimal(0))/qty
        return float((prices["sell"]/prices["buy"]-1)*10000) if len(prices)==2 else None

    def _measured_fee(self):
        with self.ledger.lock:
            rows=list(self.ledger.db.execute("SELECT f.qty,f.price,f.fee FROM fills f JOIN legs l ON l.id=f.leg_id WHERE l.venue='entropy'"))
        quote=sum((Decimal(r[0])*Decimal(r[1]) for r in rows),Decimal(0))
        return float(sum((Decimal(r[2]) for r in rows),Decimal(0))/quote*10000) if quote else None

    def _fee_mismatch(self):
        actual=self._measured_fee()
        return actual is not None and abs(actual-.9)>.05

    def _daily(self):
        from datetime import datetime,timezone
        from web.probe import trading_day
        today=trading_day(datetime.now(timezone.utc)); count=0; notional=0.0
        for op in self.ledger.all_operations():
            if trading_day(datetime.fromtimestamp(op["created"],timezone.utc))!=today: continue
            with self.ledger.lock:
                sent=self.ledger.db.execute("SELECT 1 FROM events WHERE operation_id=? AND kind='SEND_BOUNDARY_COMMITTED'",(op["id"],)).fetchone()
            if sent:
                count+=1
                notional+=max(float(Decimal(l["qty"])*Decimal(l["limit_px"])) for l in op["legs"])
        return count,notional

    async def _tick(self):
        if time.monotonic()-self.last_recovery>=self.reconcile_interval: await self._reconcile()
        if self.collector and self.recording:
            task=self.collector.task
            if task is None or task.done():
                self.reason="public collector disconnected; retrying"
                if time.monotonic()>=self._collector_retry:
                    await self.collector.close(); await self.collector.start()
                    self.collector.pause(self.paused); self._collector_retry=time.monotonic()+5
        if not self.task_spec or not self.recording or self.paused or not self.recovered:
            self.armed=None; return
        offer=candidate(self.task_spec,self.books,self.recovery.snapshots,self.accounts)
        if not offer: self.armed=None; self.ledger.cancel_proposal(); return
        direction=(offer["intent"],offer["direction"])
        if not self.armed or self.armed[0]!=direction:
            self.ledger.cancel_proposal()
            self.armed=(direction,time.monotonic()); return
        if time.monotonic()-self.armed[1]<self.persist_sec: return
        current=self.ledger.proposal()
        if not current or current["expires"]<time.time():
            current=dict(proposal_id=uuid.uuid4().hex,intent=offer["intent"],direction=offer["direction"],
                         pre_fee_edge_bps=offer["pre_fee"],net_edge_bps=offer["net"],
                         specs=[s.payload() for s in offer["specs"]],created=time.time(),expires=time.time()+5)
            self.ledger.put_proposal(current)
        if self.simulation and self.task_spec.get("auto_confirm") and self._in_rth():
            # Unknown or incomparable funding periods never qualify for automation.
            periods={getattr(b,"funding_period_sec",None) for b in self.books.values()}
            funding_ok=(len(periods)==1 and None not in periods and all(
                b.funding is not None and b.funding_observed_ts is not None and
                0<=time.time()-b.funding_observed_ts<=90 for b in self.books.values()))
            if not funding_ok: return
            from web.probe import evaluate_auto_gates
            entry=self.ledger.get_setting("entry",{})
            count,notional=self._daily()
            gate=evaluate_auto_gates(intent=current["intent"],pre_fee_edge_bps=current["pre_fee_edge_bps"],
                entry_pre_fee_bps=entry.get("pre_fee_edge_bps"),measured_fee_bps=self._measured_fee(),
                notional_usd=self.task_spec["order_notional_usd"],
                auto_confirm_max_usd=self.task_spec["auto_confirm_max_usd"],fresh=True,halted=False,rth=True,
                funding_entropy=self.books["entropy"].funding,funding_hedge=self.books["lighter"].funding,
                available_entropy=float(self.recovery.snapshots["entropy"].available),
                available_lighter=float(self.recovery.snapshots["lighter"].available),
                available_need_entropy=self.task_spec["order_notional_usd"],available_need_lighter=self.task_spec["order_notional_usd"],
                daily_notional=notional,daily_count=count,daily_max_notional=self.task_spec.get("auto_daily_max_notional_usd"),
                daily_max_count=self.task_spec.get("auto_daily_max_count"))
            if not gate["ok"]: return
            try: await self._confirm(dict(confirm=True,proposal_id=current["proposal_id"]))
            except (ServiceError,RiskRejected): pass

    def _project(self):
        self.heartbeat=time.time()
        snapshots=self.recovery.snapshots if hasattr(self,"recovery") else {}
        accounts={n:(snapshots[n].payload() if n in snapshots else dict(position=None,available=None)) for n in self.accounts}
        accounts.update(creds=dict(entropy=False,lighter=False),note="公开账户查询；不读取私钥。缺失与未验证数据不会视为零仓位。")
        metrics=self.ledger.metrics() if self.ledger else {}
        proposal=self.ledger.proposal() if self.ledger else None
        if proposal:
            from web.probe import build_confirm_payload
            legs=[dict(venue="Entropy" if p["venue"]=="entropy" else "Lighter",
                       direction="BUY" if p["is_buy"] else "SELL",notional_usd=float(Decimal(p["qty"])*Decimal(p["limit_px"])),
                       available=accounts[p["venue"]].get("available"),isolated=None) for p in proposal["specs"]]
            display=dict(proposal,legs=legs,reduce_only=proposal["intent"]!="open")
            proposal=build_confirm_payload(self.task_spec,display,rth=self._in_rth(),tail=None)
            proposal.update(confirm_enabled=self.simulation and self.recovered and not self.paused,
                            block_reason=None if self.simulation else "RECORD_ONLY：真实交易未开放",
                            force_required=bool(self.task_spec.get("rth_only",True) and not self._in_rth()),
                            force_ack=bool(self.ledger.get_setting("force_ack:"+proposal["proposal_id"],False)),
                            idempotency_key="confirm:"+proposal["proposal_id"])
        task=dict(self.task_spec,mode_label="模拟" if self.simulation else "只记录") if self.task_spec else None
        self._cache=dict(service_state=self.state,heartbeat=self.heartbeat,mode="SIMULATION" if self.simulation else "RECORD_ONLY",
            trading_ready=self.simulation and self.recovered and self.recording and not self.paused,
            recovery_reason=self.reason,metrics=metrics,running=self.recording,paused=self.paused,pid=os.getpid(),
            status_label="暂停" if self.paused else "记录中" if self.recording else "已停止",
            task=task,proposal=proposal if self.recording and not self.paused else None,accounts=accounts,
            live_armed=False,halted=not self.recovered,net_base=(self.ledger.get_setting("last_reconcile",{}) or {}).get("net_base"),
            reconcile=self.ledger.get_setting("last_reconcile") if self.ledger else None,last_order=self.last_order,
            warnings=[] if self.recovered else [self.reason],flatten=self.flatten_offer(),rth=self._in_rth(),
            fee_check=dict(assumed_bps=.9,actual_bps=self._measured_fee() if self.ledger else None,
                           mismatch=self._fee_mismatch() if self.ledger else False),edge=self._edge())

    def _edge(self):
        e,h=self.books.get("entropy"),self.books.get("lighter")
        mid=(self.task_spec or {}).get("midline_bps",0)
        if not e or not h or not e.mid() or not h.mid(): return dict(midline_bps=mid)
        sell=(e.best_bid()/h.best_ask()-1)*10000; buy=(h.best_bid()/e.best_ask()-1)*10000
        return dict(sell_pre_bps=sell,buy_pre_bps=buy,net_sell_bps=sell-.9,net_buy_bps=buy-.9,
                    deviation_bps=(e.mid()/h.mid()-1)*10000-mid,midline_bps=mid)

    def flatten_offer(self):
        if not hasattr(self,"recovery") or not self._owned_reduction(): return None
        from web.probe import flatten_plan
        s=self.recovery.snapshots
        plan=flatten_plan(float(s["entropy"].position),float(s["lighter"].position))
        if not plan: return None
        import hashlib
        history=self.ledger.all_operations()
        signature=json.dumps(dict(positions={n:str(p.position) for n,p in s.items()},
                                  operation=history[-1]["id"] if history else None),sort_keys=True)
        legs=[]
        for leg in plan["legs"]:
            name=leg["venue"]
            name="entropy" if name.lower()=="entropy" else "lighter"
            reference=self.books.get(name)
            mid=reference.mid() if reference else None
            legs.append(dict(leg,base_qty=leg["close_qty"],
                             notional_usd=leg["close_qty"]*mid if mid else None))
        return dict(plan,legs=legs,available=True,busy=False,
                    proposal_id=hashlib.sha256(signature.encode()).hexdigest(),
                    confirm_enabled=self.simulation,block_reason=None if self.simulation else "RECORD_ONLY")

    def snapshot(self): return json.loads(json.dumps(self._cache))

    async def close(self):
        if self.closing: return
        self.closing=True
        self.state="DRAINING"
        if self.worker:
            try: await asyncio.wait_for(asyncio.shield(self.worker),12)
            except (Exception,asyncio.CancelledError):
                self.worker.cancel(); await asyncio.gather(self.worker,return_exceptions=True)
        if self.monitor:
            self.monitor.cancel(); await asyncio.gather(self.monitor,return_exceptions=True)
        while not self.queue.empty():
            _,_,future=self.queue.get_nowait()
            if not future.done(): future.set_exception(ServiceError("service stopped before command processing"))
            self.queue.task_done()
        try:
            if self.collector: await self.collector.close()
        finally:
            try:
                if self.http: await self.http.close()
            finally:
                try:
                    if self.ledger: self.ledger.close(); self.ledger=None
                finally:
                    if self.account_owner: self.account_owner.close()
                    self.owner.close(); self.state="STOPPED"
