"""Explicit, bounded manual validation dispatcher; never a strategy entry point."""
from decimal import Decimal
import re

from .execution import DurableDispatcher
from .live_adapter import HyperliquidAdapter,LighterAdapter
from .storage import RecoveryRequired


LIVE_ACK='I_REVIEWED_THIS_COMMIT_AND_ACCEPT_REAL_ORDERS'


class ManualLiveExecutor(DurableDispatcher):
    def __init__(self,ledger,venues,*,reviewed_commit=None,current_commit=None,ack=None,enabled=False):
        if (enabled is not True or ack!=LIVE_ACK or not isinstance(reviewed_commit,str)
            or not re.fullmatch('[0-9a-f]{40}',reviewed_commit) or reviewed_commit!=current_commit):
            raise RecoveryRequired('manual live validation requires reviewed commit and explicit opt-in')
        if (set(venues)!={'entropy','lighter'} or not isinstance(venues['entropy'],HyperliquidAdapter)
            or not isinstance(venues['lighter'],LighterAdapter)):
            raise ValueError('reviewed SNDK adapters required')
        super().__init__(ledger,venues,allow_submit=True)

    async def execute(self,operation_id):
        operation=self.ledger.operation(operation_id)
        if operation['state']!='INTENT_COMMITTED':
            return operation # No transport on replay, even if the exchange accepted.
        operations=self.ledger.all_operations()
        # Lifetime test budget: one entry, at most four separately verified exits.
        if len(operations)>5 or sum(o['intent']=='open' for o in operations)>1:
            raise RecoveryRequired('manual test budget exhausted')
        for leg in operation['legs']:
            intent=self.intent(leg)
            adapter=self.venues[leg['venue']]
            if leg['market']!='SNDK' or leg['account']!=adapter.account:
                raise RecoveryRequired('test order identity mismatch')
            adapter.rules.validate(intent)
            if intent.qty*intent.limit_px>Decimal(25):
                raise RecoveryRequired('manual test hard cap is $25 per leg')
            if operation['intent']!='open' and not intent.reduce_only:
                raise RecoveryRequired('test exit must be reduce-only')
        return await super().execute(operation_id)
