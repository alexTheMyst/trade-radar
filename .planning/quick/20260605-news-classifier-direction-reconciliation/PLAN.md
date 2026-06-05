---
slug: news-classifier-direction-reconciliation
date: 2026-06-05
status: in-progress
---

# Add same-day direction reconciliation to news-classifier pipeline

Prevent contradictory same-pillar signals for the same ticker from both being DELIVERED in the same digest (observed: AVGO 2026-06-05 got DELIVERED -0.78 AND +0.72).

## Tasks

1. Add `pillar: str | None` field to Signal model (models.py)
2. Add `pillar` column to signals table via `_ensure_column` in repository.py; update `insert_signal` SQL
3. Set `pillar=parsed.pillar_name` in news_classifier.py Signal construction
4. Create `src/signal_system/reconciler.py` — pure `reconcile_directions(routable) -> (winners, losers)`
5. Wire reconciliation into news_morning.py between `_classify_kept_headlines` and `route_signals`; persist losers as MONITORING/reconciled
6. Write `tests/test_reconciler.py` — TDD coverage for all 5 cases
7. Run `uv run pytest -q` — confirm green (136+ tests)
8. Commit atomically; update STATE.md
