"""Offline SDK import/signature/native-library check. No keys or account clients."""
import inspect
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from entropy_arb.live_adapter import HL_REVISION,LIGHTER_REVISION
from entropy_arb.live_transport import verify_sdk_pins


def main():
    verify_sdk_pins()
    from hyperliquid.utils.signing import sign_l1_action
    from lighter import SignerClient
    from lighter.signer_client import get_signer
    assert {'wallet','action','active_pool','nonce','expires_after','is_mainnet'} <= set(inspect.signature(sign_l1_action).parameters)
    assert {'market_index','client_order_index','base_amount','price','is_ask','order_type','time_in_force','reduce_only'} <= set(inspect.signature(SignerClient.create_order).parameters)
    assert {'market_index','order_index'} <= set(inspect.signature(SignerClient.cancel_order).parameters)
    library=get_signer()
    assert all(hasattr(library,name) for name in ('SignCreateOrder','SignCancelOrder','CreateAuthToken'))
    print(json.dumps(dict(passed=True,platform=platform.system(),architecture=platform.machine(),
        hyperliquid_revision=HL_REVISION,lighter_revision=LIGHTER_REVISION,native_signer_loaded=True,
        private_keys_loaded=False,account_clients_created=False,real_requests_sent=False)))


if __name__=='__main__': main()
