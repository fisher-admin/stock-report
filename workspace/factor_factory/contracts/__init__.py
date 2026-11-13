"""factor_factory provider contract layer (Wave1 W1-4/W1-5/W1-6/W1-11 base).

STATUS: DORMANT (休眠). Adopted verbatim from research spike
`2026-07-github-stock-systems/spikes/spike3_provider` plus the four R-3
spec revisions (PHASE3_SPIKES_REPORT.md §R-3):

  R-3.1 pre_close per-source capability table (contracts.yaml `capabilities`,
        enforced fail-closed in fetchers)
  R-3.2 contracts.yaml is calibrated against the RUNTIME env (norm)
  R-3.3 source health (ok-rate) is a separate verdict from contract
        compliance (harness all_green) — core.assess_source_health
  R-3.4 name reconciliation compares after NFKC + status-affix normalization;
        raw mismatch list still lands in the manifest in full

No production script imports this package yet — the consumer-side switch is
W1-3 scope (see research/2026-07-github-stock-systems/wave1/W1-3-切换设计.md).

Self-check harness (three interface families, offline by default):

    cd /Users/fisher/.openclaw/workspace/factor_factory
    /Users/fisher/.openclaw/venv/bin/python -m contracts            # offline
    /Users/fisher/.openclaw/venv/bin/python -m contracts --live     # real pulls

Unit tests (no pytest in venv; bare-assert script):

    /Users/fisher/.openclaw/venv/bin/python -m contracts.tests.test_contracts
"""

__version__ = "1.0.0"

from .core import (  # noqa: F401
    ContractViolation,
    ErrorType,
    FailureRecord,
    FetchMeta,
    HarnessReport,
    RawPayload,
    ReconcileBreach,
    SourceHealth,
    StrictQuery,
    StrictRow,
    TETFetcher,
    assess_source_health,
    run_harness,
)
from .models import (  # noqa: F401
    DailyBarV1,
    EarningsPreannounceV1,
    MarketContext,
    SecurityMetaV1,
    limit_exemption,
    validate_daily_row_invariants,
)
from .pit import assert_pit_clean, pit_filter  # noqa: F401
from .reconcile import (  # noqa: F401
    names_equivalent,
    normalize_security_name,
    reconcile_daily,
    reconcile_names,
)
