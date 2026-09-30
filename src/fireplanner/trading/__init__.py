"""Trade management: sizing, stops, targets, stock-type buckets and the two-strike rule.

The analysis half of FirePlanner says what the market is doing. This half
manages positions once you act on it:

``rules``      the account's trading policy, from ``config.yaml``
``markets``    US / London / Hong Kong / Singapore / Tokyo: lots, ticks, pence
``sizing``     $5,000 slots in whole lots, the stop and the target
``buckets``    Steady / Core / Volatile by money and by daily swing
``strikes``    two stop-outs in a row lock a stock for 10 trading days
``gate``       the pre-trade check behind every dashboard buy
``journal``    SQLite memory of every trade, exit and alert
``sync``       keeps the journal in step with the broker
``guardian``   no position without a stop; stops only move up
``service``    the loop that runs all of it against one broker connection
``server``     the live dashboard
``sim``        an in-memory broker for tests, demos and rehearsal
``ib_broker``  TWS / IB Gateway via ``ib_async``
"""

from .buckets import BucketView, Holding, bucket_mix
from .gate import GateResult, check_entry
from .guardian import plan_protection
from .journal import Journal
from .markets import MARKETS, Instrument, parse_symbol
from .rules import TradingRules, load_rules, load_settings
from .service import TradingService, import_watchlist
from .sim import SimBroker
from .sizing import EntryPlan, plan_entry
from .strikes import strike_status

__all__ = [
    "BucketView", "Holding", "bucket_mix", "GateResult", "check_entry", "plan_protection", "Journal",
    "MARKETS", "Instrument", "parse_symbol", "TradingRules", "load_rules", "load_settings",
    "TradingService", "import_watchlist", "SimBroker", "EntryPlan", "plan_entry", "strike_status",
]
