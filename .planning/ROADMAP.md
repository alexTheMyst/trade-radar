# Roadmap: Signal System — Rules-Based Investment Alert Engine

## Milestones

### ✅ v1.0 MVP — Shipped 2026-05-17

Two AI agents (News Classifier + Discovery Agent), Alert Router with daily budget enforcement, and `news-morning` + `discovery` job orchestrators — implementation-complete, 44/44 requirements satisfied, 120 tests passing. See [`.planning/milestones/v1.0-ROADMAP.md`](.planning/milestones/v1.0-ROADMAP.md) for full archive.

---

## v1.1 Phases — Go-Live & Calibration

**Milestone goal:** Get the v1.0 system running in production and establish the feedback loop before Discovery Phase B routing.
**Phases:** 2 | **Requirements:** 5 | **Coverage:** 100%

| # | Phase | Goal | Requirements | Success Criteria |
|---|-------|------|--------------|------------------|
| 7 | Deployment & Live Validation | System running in production with all credentials verified | OPS-01, OPS-02, JOBS-01 | 3 |
| 8 | Measurement & Phase B Prep | Feedback loop confirmed; Phase B transition documented | MEAS-01, DISCOVERY-B | 3 |

---

### Phase 7: Deployment & Live Validation

**Goal:** System running in production with all credentials verified and all three jobs completing successfully against live APIs.

**Requirements:**
- OPS-01: Task Scheduler XML tasks imported and enabled on runner machine
- OPS-02: Telegram delivery confirmed + healthchecks.io pings verified live
- JOBS-01: All 3 jobs run end-to-end against live Finnhub + Anthropic APIs

**Plans:** 3 plans

Plans:
- [ ] 07-01-PLAN.md — Pre-flight: credential validation (Telegram, Finnhub, healthchecks.io) and DB initialization
- [ ] 07-02-PLAN.md — Task Scheduler import and on-demand test run of daily-close
- [ ] 07-03-PLAN.md — Full end-to-end live run of all three jobs with DB and delivery audit

**Success Criteria:**
1. `daily-close`, `news-morning`, and `discovery` tasks appear in Windows Task Scheduler and show Last Run Result = 0 (success)
2. A Telegram message is delivered from a live `news-morning` run to `TELEGRAM_CHAT_ID`
3. healthchecks.io dashboard shows the check green within 24 hours of Task Scheduler activation

**Runbook reference:** `E2E-TEST-PLAN.md` at repo root — follow sections 0–8 in order.

---

### Phase 8: Measurement & Phase B Prep

**Goal:** Operator feedback loop confirmed over first live week; Discovery Phase A → B transition criteria documented.

**Requirements:**
- MEAS-01: acted/user_note workflow confirmed over first 7 days
- DISCOVERY-B: Phase A → B transition criteria and switch procedure documented

**Success Criteria:**
1. At least one signal has `acted=1`, `acted_at` timestamp, and non-empty `user_note` in the `signals` table after the first live week
2. A written runbook exists describing Phase B transition criteria (minimum days, signal volume, score distribution review checklist)
3. Operator confirms criteria are understood and will be applied before setting `DISCOVERY_PHASE=B`

**Deliverable:** `docs/runbooks/phase-b-transition.md` — Phase A calibration review checklist and `.env` flip procedure.

---

*v1.1 roadmap created: 2026-05-17*
