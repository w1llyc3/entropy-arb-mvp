"""Explicit operator-only SDK construction; no imports/signing on normal startup."""
import json
from importlib import metadata
from pathlib import Path

import aiohttp

from .live_adapter import HL_REVISION,LIGHTER_REVISION


def verify_sdk_pins():
    for distribution,revision in [('hyperliquid-python-sdk',HL_REVISION),('lighter-sdk',LIGHTER_REVISION)]:
        try:
            direct=json.loads(metadata.distribution(distribution).read_text('direct_url.json') or '{}')
        except (metadata.PackageNotFoundError,ValueError):
            raise RuntimeError('pinned signing SDK not installed') from None
        if direct.get('vcs_info',{}).get('commit_id')!=revision:
            raise RuntimeError('signing SDK revision differs from reviewed pin')


def read_operator_env(path,*,secrets=False,lighter_auth=False):
    """Read only the named file, never search parents or mutate process env.

    Read-only mode skips credential values before parsing. Errors never echo
    file contents or values. Existing OS environment is intentionally ignored.
    """
    public={'ENTROPY_HL_ACCOUNT','LIGHTER_ACCOUNT_INDEX','LIGHTER_READ_TOKEN',
            'LIGHTER_TRADE_FEE_SCALE','ENTROPY_LIVE_TEST','ENTROPY_REVIEWED_COMMIT'}
    private={'ENTROPY_HL_PRIVATE_KEY','LIGHTER_API_PRIVATE_KEY','LIGHTER_API_KEY_INDEX'}
    from dotenv import dotenv_values
    import io
    selected=[]
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        key=line.partition('=')[0].strip()
        if key in public or (secrets and key in private) or (lighter_auth and key in {'LIGHTER_API_PRIVATE_KEY','LIGHTER_API_KEY_INDEX'}):
            selected.append(line)
    values=dotenv_values(stream=io.StringIO('\n'.join(selected)),interpolate=False)
    return {k:v for k,v in values.items() if v is not None}


class RestTransport:
    def __init__(self,session,token=None): self.session,self.token=session,token

    async def _json(self,method,url,**kw):
        # HTTP exception bodies can contain auth material. Expose categories only.
        try:
            async with self.session.request(method,url,timeout=aiohttp.ClientTimeout(total=5),**kw) as response:
                if response.status!=200: raise RuntimeError('exchange HTTP request failed')
                return await response.json()
        except Exception:
            raise RuntimeError('exchange request unavailable') from None

    async def info(self,payload):
        return await self._json('POST','https://api.hyperliquid.xyz/info',json=payload)

    async def exchange(self,payload):
        return await self._json('POST','https://api.hyperliquid.xyz/exchange',json=payload)

    async def lighter(self,path,params):
        allowed={'orderBooks','account','accountActiveOrders','accountInactiveOrders','trades','orderBookOrders'}
        if path not in allowed: raise ValueError('read endpoint not allowed')
        headers={'authorization':self.token} if self.token else {}
        return await self._json('GET','https://mainnet.zklighter.elliot.ai/api/v1/'+path,params=params,headers=headers)


async def make_signers(values):
    verify_sdk_pins()
    from eth_account import Account
    from hyperliquid.utils.signing import sign_l1_action
    try:
        wallet=Account.from_key(values['ENTROPY_HL_PRIVATE_KEY'])
        signer=await make_read_signer(values)
        return wallet,sign_l1_action,signer
    except Exception:
        raise RuntimeError('signer configuration invalid') from None


async def make_read_signer(values):
    """Construct an auth-token signer only; caller never receives a send wrapper."""
    verify_sdk_pins()
    from lighter import SignerClient
    try:
        return SignerClient(url='https://mainnet.zklighter.elliot.ai',
            account_index=int(values['LIGHTER_ACCOUNT_INDEX']),
            api_private_keys={int(values['LIGHTER_API_KEY_INDEX']):values['LIGHTER_API_PRIVATE_KEY']},chain_id=304)
    except Exception:
        raise RuntimeError('read authentication configuration invalid') from None
