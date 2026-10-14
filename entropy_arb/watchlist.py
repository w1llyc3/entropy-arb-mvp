"""Fixed research watchlist; never grants execution permissions."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchAsset:
    symbol: str
    label: str
    lighter_symbol: str

    @property
    def coin(self): return "io:"+self.symbol

    @property
    def csv_rel(self):
        return "logs/minutes.csv" if self.symbol=="SNDK" else f"logs/record-only/{self.symbol}/minutes.csv"


ASSETS=tuple(ResearchAsset(*row) for row in (
    ("SNDK","Sandisk","SNDK"), ("ANTH","Anthropic","ANTHROPIC"),
    ("DRAM","DRAM","DRAM"), ("OAI","OpenAI","OPENAI"),
    ("NBIS","Nebius","NBIS"), ("EWY","EWY","EWY"),
    ("XIAOMI","Xiaomi","XIAOMI"), ("IONQ","IONQ","IONQ"),
    ("GPRO","GoPro","GPRO"), ("TCNT","Tencent","TENCENT")))
BY_SYMBOL={a.symbol:a for a in ASSETS}


def asset_for(symbol):
    try: return BY_SYMBOL[symbol]
    except (KeyError,TypeError): raise ValueError("unknown research asset") from None


def resolve_catalog(hl_meta,lighter_meta):
    if not isinstance(hl_meta.get("universe"),list) or not isinstance(lighter_meta.get("order_books"),list):
        raise ValueError("incomplete public market catalog")
    result=[]
    for asset in ASSETS:
        entropy=[m for m in hl_meta["universe"] if m.get("name")==asset.coin and not m.get("isDelisted")]
        lighter=[m for m in lighter_meta["order_books"] if m.get("symbol")==asset.lighter_symbol
                 and m.get("status")=="active" and m.get("market_type")=="perp"]
        paired=len(entropy)==len(lighter)==1
        result.append(dict(symbol=asset.symbol,label=asset.label,entropy_coin=asset.coin,
            lighter_symbol=asset.lighter_symbol,market_id=int(lighter[0]["market_id"]) if paired else None,
            state="PAIRED" if paired else "ENTROPY_ONLY" if len(entropy)==1 else "UNAVAILABLE",
            csv_path=asset.csv_rel,record_only=True,execution_enabled=False,
            contract_terms_verified=False))
    return result
