# Change Plans

Comprehensive implementation plans for the next development cycle, derived from a
full-system state review on 2026-08-09. The review surfaced one theme: the system's
weakest link is **silent failure and unused surface area**, not missing capability.
The News Classifier job was dead for two months (2026-06-08 → 2026-08-09) and nothing
alerted, because a dead job is indistinguishable from a quiet day and per-job
health checks were never wired up.

> **Re-sequenced 2026-08-10.** A live verification run added five plans (06–10) and
> re-ordered the whole backlog. **Read [00-MASTER-PLAN.md](00-MASTER-PLAN.md) first** — it
> carries the evidence, the phasing, and the open decisions. The table below is the current
> ordering; the sequencing notes from 2026-08-09 are preserved at the bottom.

| # | Plan | Type | Priority | Effort | Phase |
|---|------|------|----------|--------|-------|
| 06 | [Cross-agent budget arbitration](06-cross-agent-budget.md) | Fix | **P0** | ~1.5 day | 1 |
| 01 | [Meta-monitor job](01-meta-monitor.md) | Add | **P0** | ~0.5 day | 0 — *code done, never scheduled* |
| 08 | [Digest integrity](08-digest-integrity.md) | Fix | **P0** | ~1 day | 1 |
| 07 | [News subject-ticker attribution](07-news-attribution.md) | Fix | **P0** | ~2 day | 1 |
| 02 | [Preflight / doctor command](02-preflight-doctor.md) | Add | P1 | ~1 day | 4 |
| 03 | [Holdings ↔ universe reconciliation](03-holdings-universe-reconciliation.md) | Add | P1 | ~1 day | 2 |
| 09 | [News coverage & volume cap](09-news-coverage-volume-cap.md) | Fix | P1 | ~1 day | 2 |
| 10 | [Measurement repair](10-measurement-repair.md) | Fix | P1 | ~1.5 day | 3 |
| 04 | [Remove the Advisor agent](04-remove-advisor.md) | Remove | P2 | ~0.5 day | 4 |
| 05 | [Retire the wash-sale ledger](05-retire-wash-sale-ledger.md) | Remove | P2 | ~0.5 day | 4 |

## Sequencing notes

**Added 2026-08-10:**

- **Phase 0 is operational, not development.** `jobs/watchdog.py` and `jobs/sentinel.py` are
  both implemented and registered in `__main__.JOBS`, and neither has ever run. Schedule
  what already exists before building anything new.
- **06 outranks 01.** Plan 01 detects a job that *fails*. Nothing detects a job that
  *succeeds and delivers nothing because another job consumed the daily budget first* —
  which is Discovery's state since 2026-08-09.
- **07 depends on 03.** Both touch the holdings/universe split. Build the holdings loader
  fix (03 §3a) once and have 07 consume it.
- **08 before 07.** Fix the counting before changing what is counted, or the attribution
  rollout has no trustworthy baseline.
- **09 pairs with 03.** Raising the cap does not help a ticker that is never scanned.

**From 2026-08-09 (still valid):**

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
- Any change to classifier prompts or output schema requires `eval-classifier` against the
  golden set before rollout (`CLAUDE.md`). Only Plan 07 trips this.
- Implementation is entered through a GSD command (`/gsd-quick`, `/gsd-debug`,
  `/gsd-execute-phase`). These documents are plans, not authorization to edit source.
