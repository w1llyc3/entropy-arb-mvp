"""Re-evaluate evidence; publishing this report does not enable real trading."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from entropy_arb.release_gate import evaluate


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--soak',required=True);parser.add_argument('--resume',required=True)
    parser.add_argument('--readonly',default=str(ROOT/'.web/prelive-validation/readonly.json'))
    args=parser.parse_args()
    artifacts={k:str(Path(getattr(args,k)).resolve()) for k in ('soak','resume','readonly')}
    result=evaluate(ROOT,artifacts)
    report=dict(result,artifacts=artifacts,evaluated_at=time.time())
    output=ROOT/'.web/prelive-validation/release.json';output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_suffix('.tmp');temporary.write_text(json.dumps(report,indent=2),encoding='utf-8');temporary.replace(output)
    print(json.dumps(result))
    raise SystemExit(0 if result['manual_entry_evidence_ready'] else 2)
