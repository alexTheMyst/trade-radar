---
slug: news-classifier-direction-reconciliation
date: 2026-06-05
status: complete
---

# Summary: same-day direction reconciliation

## What was done

Added a reconciliation step between classification and routing to prevent
contradictory same-pillar signals (e.g. AVGO +0.78 AND -0.72 on 2026-06-05)
from both being DELIVERED in the same digest.

## Files changed

- `src/signal_system/models.py` — added `pillar: str | None` field to Signal
- `src/signal_system/state/repository.py` — `pillar` column migration + insert_signal update
- `src/signal_system/classifier/news_classifier.py` — sets `pillar=parsed.pillar_name` on Signal
- `src/signal_system/reconciler.py` — new module: pure `reconcile_directions(routable) -> (winners, losers)`
- `src/signal_system/jobs/news_morning.py` — wires reconciliation; persists losers as MONITORING/reconciled
- `tests/test_reconciler.py` — 11 tests covering all 5 required cases

## Outcome

231 tests passing (was 136 before this session's full suite ran).
Commit: `7ca2382`
