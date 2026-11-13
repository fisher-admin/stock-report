"""fisher-quant: progressive-refactor target skeleton for the A-share reco system.

Modules:
- quant.datahub      read-only access to the gold SQLite warehouse + calendar helpers
- quant.backtest_v2  honest backtest engine (next-open entry, limit-up skip, costs)
- quant.verify       T+1/T+3/T+5 verification of selection_history snapshots
"""

__version__ = "0.2.0"
