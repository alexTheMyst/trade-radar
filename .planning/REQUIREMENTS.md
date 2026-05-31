# Requirements — v1.1 Go-Live & Calibration

**Milestone:** v1.1 Go-Live & Calibration
**Goal:** Get the v1.0 system running in production and establish the feedback loop before Discovery Phase B routing.
**Note:** v1.1 is primarily operational (evidence items/runbooks). Code changes, if any, should be limited to go-live bug fixes.
---

## Active Requirements

### Operations — Deployment

- [ ] **OPS-01**: Operator imports Task Scheduler XML task files on the Windows runner machine and validates that `daily-close`, `news-morning`, and `discovery` tasks are listed and enabled → *Phase 7*
- [ ] **OPS-02**: Operator confirms Telegram delivery works with production `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (message received in chat from a live job run) and confirms healthchecks.io receives start/success pings from at least one job run → *Phase 7*

### Jobs — Live Validation

- [ ] **JOBS-01**: All three jobs (`daily-close`, `news-morning`, `discovery`) run end-to-end against live Finnhub and Anthropic APIs with no unhandled exceptions; Telegram digest message delivered from `news-morning`; `runs` table shows `status=success` for all three → *Phase 7*

### Measurement

- [ ] **MEAS-01**: Operator fills `acted`, `acted_at`, and `user_note` fields within 7 days for every signal where a trade decision was made; at least one acted signal confirmed in DB after first live week → *Phase 8*

### Discovery Calibration

- [ ] **DISCOVERY-B**: Phase A → B transition criteria documented (minimum calibration days, signal volume threshold, score distribution review); switch procedure (`DISCOVERY_PHASE=B` in `.env`) documented in runbook; operator confirms criteria before flipping → *Phase 8*

---

## Future Requirements (deferred post-v1.1)

- Telegram delivery migration (approved spec in `docs/superpowers/specs/2026-05-17-telegram-delivery-design.md`)
- Outcome backfill activation (`outcome_backfill.py` coded — activate ~30 days post go-live)
- Discovery Phase B live routing (depends on Phase A calibration window closing)
- Quarterly thesis review workflow (first review due 2026-11-01 per `thesis.example.yaml`)
- Discovery scoring weight adjustment after calibration data available

---

## Out of Scope (v1.1)

- Code changes of any kind — implementation is complete at v1.0
- Automated trade execution — alert-only by design
- Regime classifier, Portfolio Drift agent, Earnings Setup agent — excluded by design
- GitHub Actions / Linux cron — Windows Task Scheduler only

---

## Traceability

| REQ-ID | Phase | Plan |
|--------|-------|------|
| OPS-01 | Phase 7 | — |
| OPS-02 | Phase 7 | — |
| JOBS-01 | Phase 7 | — |
| MEAS-01 | Phase 8 | — |
| DISCOVERY-B | Phase 8 | — |
