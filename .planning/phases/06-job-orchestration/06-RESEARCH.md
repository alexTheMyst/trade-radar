# Phase 6: Job Orchestration - Research

**Researched:** 2026-05-16  
**Domain:** Python job orchestration, digest delivery, SQLite-backed run wiring, Windows Task Scheduler handoff  
**Confidence:** MEDIUM

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Phase 6 adds two runnable jobs exposed from `src/signal_system/__main__.py`: `news-morning` and `discovery`. `daily-close` stays intact.
- **D-02:** Both new jobs follow the existing MVP job contract: `repository.insert_run()` first, all work inside `with heartbeat.heartbeat():`, and `repository.update_run(run_id, "success")` stays inside the heartbeat block so DB failures still trip `/fail`.
- **D-03:** The router remains pure. Phase 6 persists `(signal, routing_status, demoted_from)` tuples returned by `route_signals()`.
- **D-04:** `news-morning` scans core holdings only.
- **D-05:** `news-morning` uses a window from previous market close through run time.
- **D-06:** Email delivery is one digest per job run.
- **D-07:** Digests show delivered alerts in detail and summarize suppressed / MONITORING outcomes by count only.
- **D-08:** Zero-alert confirmation is explicit for `news-morning` and for live Discovery Phase B.
- **D-09:** Discovery Phase A is DB-only and sends no email.
- **D-10:** Discovery Phase B routes returned signals, persists router decisions, and sends a digest.
- **D-11:** The 50-headline cap is applied after deduplication, newest-first.
- **D-12:** Each skipped headline beyond the cap becomes its own MONITORING signal with a volume-cap note.
- **D-13:** `MEAS-02` is implemented now but remains inactive until ~30 days post-go-live.
- **D-14:** Windows runner support stays Task Scheduler only and requires prose docs plus a scrubbed `.xml` reference artifact.

### the agent's Discretion
- Exact digest subject lines and section ordering are flexible if they preserve the required zero-alert confirmation, delivered-alert detail, and count-only summaries.
- Internal helper placement is flexible as long as DB access stays in `state/repository.py` and the existing heartbeat/email patterns remain intact.

## Phase Requirements

| ID | Description | Research Support |
|---|---|---|
| JOBS-01 | `news-morning` runs fetch -> classify -> route -> digest inside heartbeat | The codebase already has thesis loading, Finnhub news fetch, classifier, router, repository, heartbeat, and email pieces; only orchestration is missing. |
| JOBS-02 | `discovery` runs universe -> score -> route/log inside heartbeat | `score_universe()` already handles Phase A/B semantics; Phase 6 must wrap it in a job and branch on config. |
| JOBS-03 | Always send zero-alert digest | A shared digest renderer should be used by `news-morning` and Discovery Phase B. |
| JOBS-04 | 50-headline cap, overflow to MONITORING rows | This must happen before classification, after deduplication, newest-first. |
| MEAS-01 | Operator feedback fields/manual workflow | The fields already exist in `signals`; the deliverable is docs + verification. |
| MEAS-02 | Deferred idempotent outcome backfill | Add code now but do not schedule it yet. |
| OPS-01 | Task Scheduler guide + XML artifact | Requires prose plus a scrubbed XML reference with StartWhenAvailable and single-instance settings. |
| OPS-02 | Gmail filter + Healthchecks non-email setup | Requires an operator setup checklist. |

## Summary

The hard parts of the system are already built in isolation: thesis loading, news classification, discovery scoring, routing, run persistence, heartbeat, and email sending. Phase 6 should stay thin and focus on orchestration-only glue: two job modules, a small amount of shared digest/persistence code, one repository helper for the most recent successful `daily-close`, and the measurement/ops handoff docs.

The best planning assumption is to anchor `news-morning` to the most recent successful `daily-close` rather than naïve date subtraction, and to reuse one plain-text digest builder for `news-morning` and Discovery Phase B.

## Standard Stack

| Library / Module | Purpose | Why Standard |
|---|---|---|
| `sqlite3` | signal/run state | Existing persistence layer; no ORM allowed. |
| `zoneinfo` + `tzdata` | ET timestamps | Existing code standardizes on ET timestamps. |
| `finnhub-python` | quotes + company news | Existing data layer already wraps this SDK. |
| `anthropic` | news classification | Existing classifier already depends on it. |
| `pydantic` | thesis + classifier schemas | Already used by thesis loader and classifier. |
| `tenacity` | retry behavior | Reuse existing retry paths; do not add ad hoc loops. |
| `httpx` | heartbeat pings | Already used in `heartbeat.py`. |
| `smtplib` + `email.message` | digest delivery | Existing plain-text email path. |

**Installation:** No new dependencies are required for Phase 6.

## Concrete Orchestration Plan

### `news-morning` job
1. `run_id = repository.insert_run("news-morning")` before entering the heartbeat context.
2. Inside `with heartbeat.heartbeat():`, call `load_thesis(config.THESIS_PATH)` exactly once so stale or missing thesis aborts the job before classification.
3. Read **core holdings only** from the universe CSV; adding `get_core_holdings()` beside `get_todays_universe()` is the cleanest code shape.
4. Derive the lower news-window bound from the most recent successful `daily-close` run in `runs`, not from simple calendar subtraction.
5. Fetch company news per core ticker with `fetch_company_news(ticker, from_date, to_date)`, then filter client-side to `previous_close <= item.datetime <= now`.
6. Deduplicate **before** the cap using the same normalization rule the classifier already uses; extract or reuse that helper instead of duplicating logic.
7. Sort deduped items by `datetime` descending, keep the newest 50, and convert every remainder into its own MONITORING signal with a "volume cap reached" note.
8. Group the kept items by ticker and call `classify_headlines()` only on those items. Parse-failure MONITORING outputs should be inserted directly with `routing_status="MONITORING"`.
9. Send non-MONITORING classifier outputs to `route_signals()`, then persist every `(signal, routing_status, demoted_from)` tuple via `repository.insert_signal(...)`.
10. Build one plain-text digest: delivered alerts detailed, suppressed count only, monitoring count only, and an explicit zero-alert line when `DELIVERED == 0`.
11. Call `email_sender.send_email(...)` once, then `repository.update_run(run_id, "success")` inside the heartbeat block.

### `discovery` job
1. `run_id = repository.insert_run("discovery")`, then enter heartbeat.
2. `tickers = get_todays_universe()` and `date_iso = today_et.date().isoformat()`.
3. Call `score_universe(tickers, run_id, date_iso)`; the agent already updates `tickers_scanned` / `tickers_signaled`.
4. Branch on `config.DISCOVERY_PHASE`, **not** on whether `score_universe()` returned `[]`, because Phase A also returns `[]`.
5. Phase A: do nothing else; the agent already inserted MONITORING rows and no email should be sent.
6. Phase B: route returned signals, persist router decisions, and send one digest even if nothing was delivered.

### Deferred outcome backfill (`MEAS-02`)
- Implement as code now, but leave it unscheduled and clearly documented as deferred until ~30 days post-go-live.
- Keep it thin: select rows where `acted IS NOT NULL` and the corresponding outcome field is still null, then fill `outcome_price_30d` / `outcome_price_90d` once the row age crosses the threshold.
- Prefer existing quote access unless later evidence proves exact historical closes are required.

## Existing Patterns to Reuse

| Source | Reuse Pattern | Why |
|---|---|---|
| `jobs/daily_close.py` | insert run -> heartbeat -> work -> email -> update success in heartbeat -> failed in `except` | This is the locked job contract. |
| `state/repository.py` | all DB writes centralized here | Phase 6 must not add raw SQL in jobs. |
| `delivery/email_sender.py` | single plain-text helper | Digest should stay plain text; no templating system exists. |
| `monitoring/heartbeat.py` | `/start`, success, `/fail` wrapper | Required for both new jobs. |
| `discovery/discovery_agent.py` | Phase A inserts MONITORING directly; Phase B returns signals | Phase 6 must respect this split. |
| `router/alert_router.py` | pure route function returning tuples | Persistence remains caller-owned. |
| `classifier/news_classifier.py` | parse failures become MONITORING signals; shared dedup model exists | Needed for news job wiring. |

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---|---|---|---|
| DB access in jobs | inline SQL | `state/repository.py` helpers | Repo enforces one access layer. |
| Retry loops | custom sleep/retry logic | existing `tenacity`-wrapped helpers | Retry semantics already exist. |
| Routing logic | second budget implementation | `route_signals()` | Avoid budget drift and audit mismatch. |
| HTML email templating | custom formatter system | plain-text digest builder | Existing delivery path is plain text only. |
| Holiday calendar dependency | new calendar package | last successful `daily-close` anchor in DB | Reuses current runtime state and avoids new dependencies. |

## Measurement / Docs Deliverables Without Scope Creep

1. **MEAS-01:** Treat as docs + verification, not a schema project. `signals` already contains `acted`, `acted_at`, and `user_note`.
2. **MEAS-02:** Add code plus repository helpers, but do not add a live schedule yet.
3. **OPS-01:** Ship one scrubbed Task Scheduler XML reference template plus a prose guide with the required settings.
4. **OPS-02:** Add a short setup checklist for the Gmail filter and Healthchecks notifications; do not expand into a broader runbook.

## Common Pitfalls

### 1. Miscomputing "previous market close"
Simple "now minus 1 day" breaks on Mondays and market holidays. Prefer the most recent successful `daily-close` run as the lower-bound anchor.

### 2. Applying the 50-headline cap too late
If the cap happens after classification, you waste LLM calls and cannot produce correct overflow MONITORING rows.

### 3. Branching Discovery on returned signals instead of phase
`score_universe()` returns `[]` both for Phase A monitoring mode and for Phase B when nothing signaled.

### 4. Re-implementing classifier dedup in the job
The classifier already defines normalization and dedup semantics; copying a second version invites drift.

### 5. Task Scheduler `S4U` logon type
`S4U` stores no password and cannot access the network or encrypted files, which is incompatible with Finnhub, Anthropic, Gmail SMTP, and Healthchecks. Use a password-backed logon type for the real scheduled task.

### 6. Thesis stale-date timezone drift
`load_thesis()` claims ET semantics in comments but currently uses `date.today()` without `ZoneInfo("America/New_York")`, so the stale-thesis gate can be off by one day around midnight on non-ET machines.

### 7. Duplicate digest emails on reruns
`insert_signal()` is idempotent, but email sending is not. A rerun after partial failure can resend a digest. Requirements do not currently force Phase 6 to solve this.

## Recommended Plan Decomposition

### Wave 0 — Shared prerequisites
- Add `get_core_holdings()` in `data/universe.py`.
- Add a repository helper to fetch the most recent successful run timestamp for a named job, likely `daily-close`.
- Add shared digest rendering + routed-signal persistence helpers in `jobs/` or a nearby utility module.
- Decide and document cold-start behavior when no successful `daily-close` exists yet.

### Wave 1 — `news-morning`
- Implement the end-to-end job module.
- Add overflow MONITORING signal creation.
- Add zero-alert digest behavior.
- Add focused tests for cap ordering, parse-failure persistence, and digest contents.

### Wave 2 — `discovery`
- Implement the end-to-end job module.
- Respect Phase A no-email behavior.
- Reuse the shared digest path for Phase B.

### Wave 3 — Measurement + ops
- Add deferred outcome backfill code.
- Add the Task Scheduler guide + XML template.
- Add the Gmail filter + Healthchecks setup checklist.

### Wave 4 — Audit closeout
- Add missing `04-VERIFICATION.md` and `05-VERIFICATION.md` or equivalent milestone evidence, because the current audit still treats Phases 4 and 5 as unverified.
- Refresh Phase 6 traceability / summary artifacts so the milestone matrix clears.

## Environment Availability

| Dependency | Required By | Available | Status / Fallback |
|---|---|---|---|
| `uv` | tests / local dev workflow | yes | Present in repo workflow |
| SQLite | state inspection | yes | Existing stdlib path |
| `thesis.yaml` | `news-morning` | no | Create from `thesis.example.yaml` first |
| `.env` | live jobs | yes | Present locally; values not inspected |
| Windows Task Scheduler | OPS-01 validation | no | Manual validation required on the target Windows host |
| live API credentials | Finnhub / Anthropic / Gmail / Healthchecks | unknown | Not validated in this session |

## Validation Architecture

| Property | Value |
|---|---|
| Framework | `pytest 9.0.3` |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run | `uv run pytest tests/test_job_orchestration.py -q` |
| Full suite | `uv run pytest -q` |

**Current baseline:** full suite passes with `101 passed`.

### Wave 0 Gaps
- `tests/test_job_orchestration.py` is missing and should cover `news-morning`, `discovery`, zero-alert digests, and dispatcher wiring.
- `tests/test_outcome_backfill.py` is missing and should cover idempotent fill logic and deferred activation semantics.
- Optional CLI smoke coverage can be added for `python -m signal_system news-morning` and `discovery`.

## Security Domain

| ASVS Category | Applies | Standard Control |
|---|---|---|
| V2 Authentication | no | — |
| V3 Session Management | no | — |
| V4 Access Control | no | — |
| V5 Input Validation | yes | Preserve existing Pydantic thesis/classifier schemas and headline sanitization. |
| V6 Cryptography | no | Existing SHA-256 alert IDs only; no new crypto work. |

### Phase-specific threats
- Prompt-injection risk remains in news headlines; Phase 6 must preserve the existing classifier boundary and not concatenate raw unbounded text into email subjects.
- Do not commit a live exported Task Scheduler XML containing personal usernames, real absolute home paths, or other operator identifiers; use a scrubbed template artifact.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|---|---|---|
| A1 | Use the last successful `daily-close` run as the authoritative "previous market close" anchor. | Concrete Orchestration Plan | News window may need a different source if operator expectations differ. |
| A2 | Add `get_core_holdings()` rather than reusing `get_todays_universe()` plus filtering elsewhere. | News job / Wave 0 | Minor module-shape drift only. |
| A3 | Prefer `TASK_INSTANCES_IGNORE_NEW` for single-instance enforcement. | OPS-01 | If queueing is preferred, scheduler behavior changes. |
| A4 | Deferred outcome backfill can use current quote fetches rather than new historical endpoints. | MEAS-02 | If exact 30th/90th-day closes are required, scope expands. |
| A5 | Digest rerun idempotence does not need to be solved in Phase 6. | Pitfalls | Manual reruns may resend emails. |

## Open Questions

1. **Cold start for `news-morning`: what if no successful `daily-close` exists yet?**  
   Recommendation: require one manual `daily-close` before enabling `news-morning`, or explicitly document the fallback.

2. **Should deferred outcome backfill be exposed through `__main__`?**  
   Context decisions only require two new public jobs, so keeping backfill internal is cleaner unless the user explicitly wants a third public entrypoint.

3. **What exact Windows command line should the XML template use?**  
   This depends on whether the operator schedules `uv run ...` or a dedicated interpreter path on the Windows host.

## Sources

- `src/signal_system/__main__.py`
- `src/signal_system/jobs/daily_close.py`
- `src/signal_system/discovery/discovery_agent.py`
- `src/signal_system/router/alert_router.py`
- `src/signal_system/classifier/news_classifier.py`
- `src/signal_system/state/repository.py`
- `src/signal_system/monitoring/heartbeat.py`
- `src/signal_system/delivery/email_sender.py`
- `src/signal_system/data/universe.py`
- `src/signal_system/data/thesis_loader.py`
- `.planning/phases/06-job-orchestration/06-CONTEXT.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP.md`
- `.planning/STATE.md`
- `.planning/v1.0-v1.0-MILESTONE-AUDIT.md`
- Finnhub Company News docs: <https://finnhub.io/docs/api#company-news>
- Microsoft Task Scheduler docs:
  - <https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-startwhenavailable>
  - <https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-multipleinstances>
  - <https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-logontype-principaltype-element>
  - <https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create>
- Gmail filter docs: <https://support.google.com/mail/answer/6579?hl=en>
- Healthchecks docs: <https://healthchecks.io/docs/>

### Confidence Assessment

| Area | Level | Reason |
|---|---|---|
| Standard Stack | HIGH | Existing code and dependencies are already present. |
| Architecture | MEDIUM | Main job wiring is clear, but previous-close anchoring and deferred backfill invocation still need planning decisions. |
| Pitfalls | HIGH | Most risks come directly from current code, locked context, and official Task Scheduler docs. |

### Key Findings
- The repo is missing only the orchestration layer; all major subsystems already exist separately.
- `news-morning` should anchor its window off the last successful `daily-close`, not naïve date math.
- Discovery Phase A/B behavior is already implemented inside `score_universe()`; Phase 6 must branch on config, not return shape.
- No new dependencies are required for Phase 6.
- `thesis.yaml` is currently missing locally, and Windows Task Scheduler cannot be validated on this Darwin host.

Ready for planning.
