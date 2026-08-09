# Change Plans

Comprehensive implementation plans for the next development cycle, derived from a
full-system state review on 2026-08-09. The review surfaced one theme: the system's
weakest link is **silent failure and unused surface area**, not missing capability.
The News Classifier job was dead for two months (2026-06-08 → 2026-08-09) and nothing
alerted, because a dead job is indistinguishable from a quiet day and per-job
health checks were never wired up.

These plans are ordered by priority. The first three *add honesty and coverage*; the
last two *remove carrying cost*.

| # | Plan | Type | Priority | Effort |
|---|------|------|----------|--------|
| 01 | [Meta-monitor job](01-meta-monitor.md) | Add | P0 | ~0.5 day |
| 02 | [Preflight / doctor command](02-preflight-doctor.md) | Add | P1 | ~1 day |
| 03 | [Holdings ↔ universe reconciliation](03-holdings-universe-reconciliation.md) | Add | P1 | ~1 day |
| 04 | [Remove the Advisor agent](04-remove-advisor.md) | Remove | P2 | ~0.5 day |
| 05 | [Retire the wash-sale ledger](05-retire-wash-sale-ledger.md) | Remove | P2 | ~0.5 day |

## Sequencing notes

- **01 first, always.** Until the meta-monitor exists, every other change ships blind —
  you won't know if it broke a scheduled job.
- **04 and 05 are coupled.** The `wash_sale` ledger exists only to feed the Advisor's
  verdict engine. Do 04 before or with 05; doing 05 alone leaves dead references.
- **02 and 03 share code.** The drift check in 03 is one of the checks 02 runs. Build the
  drift function once (03) and have preflight (02) call it.

## Cross-cutting conventions (all plans honor these)

- Python 3.12+, `uv`, stdlib `sqlite3` (WAL), no new dependencies unless stated.
- All DB access through `state/repository.py` — no raw SQL elsewhere.
- Scheduled jobs wrap in `heartbeat.heartbeat(job=...)` and write a `runs` row via
  `repository.insert_run()` / `update_run()`.
- Secrets via `config._require` / `_optional`; never commit real values.
- Every job that can fail silently must fail **loud** — surface to Telegram and/or its
  own health check.
