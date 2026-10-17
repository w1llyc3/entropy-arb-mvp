import copy
import pytest
from entropy_arb.session_reconciliation import reconcile


def evidence():
    intent=dict(venue="entropy",account_domain="account-a",symbol="SNDK",client_id="stable-id",side=1,qty="2",reduce_only=0)
    order=dict(intent,state="FILLED",filled="2")
    fill=dict(venue="entropy",account_domain="account-a",symbol="SNDK",client_id="stable-id",side=1,qty="2",price="100",trade_id="trade-1")
    return intent,order,fill


def run(intent,order,fills,**kw):
    defaults=dict(positions=dict(entropy=2,lighter=0),baseline=dict(entropy=0,lighter=0),orders_complete=True)
    defaults.update(kw)
    return reconcile([intent],[order],fills,**defaults)


def test_exact_owned_inventory_and_replayed_fills_are_idempotent():
    i,o,f=evidence()
    result=run(i,o,[f,f,copy.deepcopy(f)])
    assert result["complete"] and result["owned_inventory"]["entropy"]=="2"
    assert not result["external_activity_adopted"]


@pytest.mark.parametrize("field,value",[("account_domain","account-b"),("symbol","NBIS"),("client_id","foreign"),("side",0),("qty","3"),("reduce_only",1)])
def test_same_size_foreign_or_conflicting_order_never_adopted(field,value):
    i,o,f=evidence();o[field]=value
    result=run(i,o,[f])
    assert not result["complete"] and result["owned_inventory"] is None


def test_unknown_and_missing_order_coverage_are_not_no_orders():
    i,o,f=evidence();o["state"]="UNKNOWN"
    assert not run(i,o,[f])["complete"]
    o["state"]="FILLED"
    assert not run(i,o,[f],orders_complete=False)["complete"]
    assert not reconcile([i],[],[],positions=dict(entropy=0,lighter=0),baseline=dict(entropy=0,lighter=0),orders_complete=True)["complete"]


def test_external_inventory_and_conflicting_trade_never_masked_by_netting():
    i,o,f=evidence()
    assert not run(i,o,[f],positions=dict(entropy=3,lighter=0))["complete"]
    bad=dict(f,qty="1")
    assert not run(i,o,[f,bad])["complete"]
    assert not run(i,o,[dict(f,qty="3")])["complete"]


def test_filled_order_cannot_be_accepted_with_zero_missing_fills():
    i,o,f=evidence();o['filled']='0'
    assert not run(i,o,[],positions=dict(entropy=0,lighter=0))['complete']
