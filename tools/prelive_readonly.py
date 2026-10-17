"""Refresh sanitized SDK/preflight/history evidence. Never creates order intents.

Uses only Lighter's local read-auth token signer, not the HL transaction key.
"""
import argparse
from argparse import Namespace
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from entropy_arb.live_check import run
from entropy_arb.release_gate import candidate_digest,read_artifact


async def collect(env_file):
    digest=candidate_digest(ROOT)
    sdk=subprocess.run([sys.executable,str(ROOT/'tools/check_live_sdk.py')],cwd=ROOT,capture_output=True,text=True,timeout=30)
    if sdk.returncode:raise RuntimeError('SDK check unavailable')
    sdk=json.loads(sdk.stdout)
    started=time.time()
    def args(command):return Namespace(command=command,env_file=env_file,state_dir=str(ROOT/'.web/manual-live-test'),authenticated_read=True)
    preflight=await run(args('preflight'))
    history=await run(args('history-audit'))
    if candidate_digest(ROOT)!=digest:raise RuntimeError('candidate changed during checks')
    return dict(mode='PRELIVE_READONLY_EVIDENCE',observed_at=started,candidate_sha256=digest,account_domain=preflight['account_domain'],
                sdk=sdk,preflight=preflight,history=history,real_orders_sent=0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',required=True)
    parser.add_argument('--output',default=str(ROOT/'.web/prelive-validation/readonly.json'))
    args=parser.parse_args()
    # Do not expose SDK exception bodies, auth material, credentials or account rows.
    import logging
    logging.disable(logging.CRITICAL)
    try:
        output=read_artifact(ROOT,args.output)
        result=asyncio.run(collect(args.env_file))
        output.parent.mkdir(parents=True,exist_ok=True)
        temporary=output.with_suffix('.tmp');temporary.write_text(json.dumps(result,indent=2),encoding='utf-8');temporary.replace(output)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(dict(ok=False,message='read-only evidence refresh failed; no orders sent')))
        return 1


if __name__=='__main__':raise SystemExit(main())
