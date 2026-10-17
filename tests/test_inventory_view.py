import asyncio
import copy
import json

import pytest

from entropy_arb.inventory_view import InventoryView, hl_positions, lighter_positions, public_identity


def books():
    return dict(code=200, order_books=[
        dict(symbol="ANTHROPIC",market_id=7,status="active",market_type="perp"),
        dict(symbol="OPENAI",market_id=8,status="active",market_type="perp"),
        dict(symbol="TENCENT",market_id=9,status="active",market_type="perp")])


def account():
    return dict(code=200,accounts=[dict(account_index=10,available_balance="50",positions=[
        dict(market_id=7,position="2",sign=-1)])])


def test_public_identity_never_passes_private_or_token_fields_to_parser(tmp_path,monkeypatch):
    import entropy_arb.inventory_view as module
    from dotenv import dotenv_values
    path=tmp_path/"operator.env"
    path.write_text('HL_ACCOUNT_ADDRESS=0x'+'1'*40+'\nLIGHTER_ACCOUNT_INDEX=0010\n'
                    'HL_PRIVATE_KEY=must-not-parse\nLIGHTER_READ_TOKEN=must-not-parse\n'
                    'LIGHTER_API_PRIVATE_KEY=must-not-parse\n',encoding="utf-8")
    def parse(*args,**kwargs):
        stream=kwargs["stream"]
        text=stream.read()
        assert "must-not-parse" not in text
        assert "PRIVATE" not in text and "TOKEN" not in text
        stream.seek(0)
        return dotenv_values(*args,**kwargs)
    monkeypatch.setattr(module,"dotenv_values",parse)
    assert public_identity(path)==dict(entropy="0x"+"1"*40,lighter="10")
    path.write_text('HL_ACCOUNT_ADDRESS=0x'+'1'*40+'\nENTROPY_HL_ACCOUNT=0x'+'2'*40,encoding="utf-8")
    with pytest.raises(ValueError,match="duplicate"):
        public_identity(path)


def test_exact_aliases_missing_market_is_unknown_and_orders_are_unverified():
    rows=lighter_positions(books(),account(),"10")
    assert rows["ANTH"]["position_qty"]=="-2"
    assert rows["OAI"]["position_qty"]=="0"
    assert rows["TCNT"]["position_qty"]=="0"
    assert "SNDK" not in rows
    assert all(r["open_order_count"] is None and not r["open_orders_verified"] for r in rows.values())


@pytest.mark.parametrize("mutation",["identity","duplicate-position","duplicate-market","sign","negative","nonfinite","error-code","missing-positions"])
def test_ambiguous_or_incomplete_lighter_account_is_not_zero(mutation):
    data=account(); metadata=books()
    if mutation=="identity": data["accounts"][0]["account_index"]=11
    if mutation=="duplicate-position": data["accounts"][0]["positions"]*=2
    if mutation=="duplicate-market": metadata["order_books"].append(copy.deepcopy(metadata["order_books"][0]))
    if mutation=="sign": data["accounts"][0]["positions"][0]["sign"]=0
    if mutation=="negative": data["accounts"][0]["positions"][0]["position"]="-2"
    if mutation=="nonfinite": data["accounts"][0]["available_balance"]="NaN"
    if mutation=="error-code": data["code"]=400
    if mutation=="missing-positions": del data["accounts"][0]["positions"]
    with pytest.raises(ValueError):
        lighter_positions(metadata,data,"10")


def test_hl_dex_identity_and_duplicate_detection():
    data=dict(withdrawable="20",assetPositions=[dict(position=dict(coin="io:OAI",szi="3")),
                                               dict(position=dict(coin="xyz:OAI",szi="999"))])
    orders=[dict(coin="io:OAI",oid=1)]
    rows=hl_positions(data,orders)
    assert rows["OAI"]["position_qty"]=="3" and rows["OAI"]["open_order_count"]==1
    assert rows["SNDK"]["position_qty"]=="0"
    with pytest.raises(ValueError,match="duplicate"):
        hl_positions(data,orders*2)
    data["assetPositions"]*=2
    with pytest.raises(ValueError,match="duplicate"):
        hl_positions(data,orders)


def test_snapshot_ages_without_get_refresh_and_never_nets_contracts():
    clock=[100]
    view=InventoryView(dict(entropy="private-public-identity",lighter="10"),now=lambda:clock[0])
    view.rows["lighter"]=lighter_positions(books(),account(),"10")
    view.observed["lighter"]=100
    first=view.snapshot("ANTH")
    assert first["legs"]["lighter"]["position_qty"]=="-2"
    assert first["legs"]["entropy"]["position_qty"] is None
    assert first["net_exposure_usd"] is None and not first["execution_ready"]
    assert "private-public-identity" not in json.dumps(first)
    clock[0]=131
    stale=view.snapshot("ANTH")["legs"]["lighter"]
    assert stale["observed_at"]==100
    assert stale["status"]=="UNKNOWN" and stale["position_qty"] is None


def test_fetch_failure_invalidates_previous_success_without_leaking_exception():
    class Failure:
        def get(self,*args,**kwargs):
            raise RuntimeError("sensitive response must not escape")
    view=InventoryView(dict(lighter="10"))
    view.rows["lighter"]=lighter_positions(books(),account(),"10")
    view.observed["lighter"]=view.now()
    asyncio.run(view.refresh(Failure()))
    data=view.snapshot("ANTH")
    assert data["legs"]["lighter"]["position_qty"] is None
    assert "sensitive" not in json.dumps(data)
    assert not hasattr(view,"submit")
