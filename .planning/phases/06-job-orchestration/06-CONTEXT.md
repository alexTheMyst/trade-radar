# Phase 6: Job Orchestration - Context

**Gathered:** 2026-05-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the runnable `news-morning` and `discovery` jobs that connect the completed classifier, discovery, router, repository, heartbeat, and email components into end-to-end operator workflows. This phase also delivers the scoped measurement and operations handoff already defined in the roadmap: operator feedback fields, deferred outcome backfill code (inactive until ~30 days post-go-live), and Windows Task Scheduler setup artifacts.

This phase does **not** redesign agent logic, router rules, thesis taxonomy, or discovery scoring. It owns orchestration, persistence of routing decisions, digest delivery, and go-live documentation.

</domain>

<decisions>
## Implementation Decisions

### Job Entry Points and Lifecycle
- **D-01:** Phase 6 adds two runnable jobs exposed from `src/signal_system/__main__.py`: `news-morning` and `discovery`. `daily-close` stays intact.
- **D-02:** Both new jobs follow the existing MVP job contract: `repository.insert_run()` first, all work inside `with heartbeat.heartbeat():`, and `repository.update_run(run_id, "success")` stays inside the heartbeat block so DB failures still trip `/fail`.
- **D-03:** The router remains pure. Phase 6 is responsible for taking `(signal, routing_status, demoted_from)` tuples from `route_signals()` and persisting them with `repository.insert_signal(...)`.

### News Job Scope and Window
- **D-04:** `news-morning` scans **core holdings only** by reusing the existing universe data source and selecting the `core_holding=1` set.
- **D-05:** The headline window for each `news-morning` run is **since previous market close** through run time, not rolling 24 hours or 7 calendar days.

### Digest Delivery Model
- **D-06:** Email delivery is **one digest per job run**, not one email per delivered alert.
- **D-07:** Digests show **delivered alerts in detail** and summarize non-delivered results by **count only**. Suppressed and MONITORING items are not listed individually in email.
- **D-08:** Zero-alert confirmation is explicit. `news-morning` must send a digest containing the required "Scanned N tickers, 0 alerts" confirmation, and live `discovery` runs do the same when nothing is delivered.

### Discovery Mode Behavior
- **D-09:** In Discovery **Phase A**, the job is strict calibration mode: it writes MONITORING rows to SQLite and sends **no email**.
- **D-10:** In Discovery **Phase B**, the job routes returned signals, persists router decisions, and sends the same digest-style email pattern as `news-morning`.

### Headline Cap and Overflow Handling
- **D-11:** `news-morning` applies the 50-headline cap **after deduplication**, then processes the **newest 50** headlines.
- **D-12:** Every headline skipped because of the 50-headline cap is written as its **own MONITORING signal** with a volume-cap note, not collapsed into one aggregate row.

### Measurement and Operations Handoff
- **D-13:** `MEAS-02` is implemented in this phase as code only, but remains **inactive until ~30 days post-go-live**. Docs must say activation is deferred.
- **D-14:** Windows runner support stays **Task Scheduler only**. Phase 6 must ship both prose setup guidance and an exported `.xml` task reference artifact for the operator.

### the agent's Discretion
- Exact digest subject lines and section ordering are flexible as long as they preserve the required zero-alert confirmation, detailed delivered alerts, and count-only summaries for non-delivered results.
- Internal module split is flexible: helper functions may live in new job modules or nearby utilities if they preserve the existing repository/heartbeat/email patterns and keep DB access inside `state/repository.py`.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Planning and milestone requirements
- `.planning/ROADMAP.md` — Phase 6 goal, dependencies, and success criteria for `news-morning`, `discovery`, measurement, and ops.
- `.planning/REQUIREMENTS.md` — JOBS-01 through OPS-02 requirement definitions.
- `.planning/v1.0-v1.0-MILESTONE-AUDIT.md` — current integration blockers this phase is expected to close.

### Prior phase handoffs
- `.planning/phases/04-discovery-agent/04-CONTEXT.md` — Discovery Phase A/B behavior, score thresholds, and caller responsibilities.
- `.planning/phases/04-discovery-agent/04-01-SUMMARY.md` — Discovery outputs, repository updates, and signal-body expectations.
- `.planning/phases/05-alert-router/05-CONTEXT.md` — router purity contract and caller-owned persistence/email responsibilities.
- `.planning/phases/05-alert-router/05-01-SUMMARY.md` — explicit Phase 6 handoff for persisting routed signals and emailing only DELIVERED results.
- `.planning/phases/03-news-classifier/03-SUMMARY.md` — classifier surface, dedup behavior, MONITORING fallback, and token telemetry wiring.

### Existing code integration points
- `src/signal_system/__main__.py` — job dispatcher to extend with new runnable commands.
- `src/signal_system/jobs/daily_close.py` — canonical job structure: run insert, heartbeat boundary, signal insert, email send, run update.
- `src/signal_system/classifier/news_classifier.py` — `classify_headlines()` surface and parse-failure MONITORING behavior.
- `src/signal_system/discovery/discovery_agent.py` — `score_universe()` Phase A/B contract and run-count updates.
- `src/signal_system/router/alert_router.py` — `route_signals()` API and routing tuple contract.
- `src/signal_system/state/repository.py` — all DB writes, schema fields, run lifecycle helpers, and feedback columns.
- `src/signal_system/data/universe.py` — reusable source for core-holding ticker selection.
- `src/signal_system/data/thesis_loader.py` — thesis load/version-hash/stale-review gate used by the news job.
- `src/signal_system/delivery/email_sender.py` — plain-text Gmail SMTP delivery helper.
- `src/signal_system/monitoring/heartbeat.py` — required heartbeat wrapper for all jobs.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `jobs/daily_close.py`: the exact run/heartbeat/email skeleton to copy for both Phase 6 jobs.
- `classifier/news_classifier.py`: returns typed `Signal` objects and already handles dedup + MONITORING fallback for parse failures.
- `discovery/discovery_agent.py`: already owns Phase A direct inserts, Phase B signal return, and run-count updates.
- `router/alert_router.py`: pure routing function ready to consume batches and return persistence-ready tuples.
- `state/repository.py`: already has `insert_signal`, `insert_run`, `update_run`, `update_run_counts`, `insert_llm_call`, and the schema fields Phase 6 needs.
- `data/universe.py`: existing source for the core-holdings subset needed by `news-morning`.

### Established Patterns
- Every job initializes a run row before entering the heartbeat context and marks success from inside the context block.
- All timestamps use `ZoneInfo("America/New_York")`; date-prefix queries and daily-budget behavior depend on ET timestamps.
- DB writes happen only through `state/repository.py`; jobs orchestrate, they do not write raw SQL.
- MONITORING is a bypass path: discovery Phase A and classifier parse-failure outputs are inserted directly, not routed.
- Email delivery is plain-text through a single helper; no templating system exists today.

### Integration Points
- `__main__.py` `JOBS` dict is the entrypoint for making `news-morning` and `discovery` runnable.
- `news-morning` must connect thesis loading, ticker selection, Finnhub news fetch, classification, routing, signal persistence, and digest delivery.
- `discovery` must connect ticker selection, scoring, optional routing (Phase B only), signal persistence, and digest/no-digest behavior by phase.
- Measurement and ops outputs must land in phase-owned docs/artifacts without changing the alert-only system boundary.

</code_context>

<specifics>
## Specific Ideas

- `news-morning` should treat the **core holdings** set as its scan list, not the broader discovery rotation.
- The news fetch window is **since previous market close**, which lets the morning run focus on overnight and pre-open developments instead of arbitrary calendar windows.
- Both live jobs should be **digest-first**. Delivered alerts are readable in the email body; suppressed and monitoring outcomes are summarized by count only.
- Discovery **Phase A** stays inbox-silent by design; Discovery **Phase B** sends a zero-alert confirmation digest when nothing is routed.
- Headline cap behavior is explicit: **dedup first, newest 50 win**, and every skipped headline becomes its own MONITORING row with a volume-cap note.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 6-Job Orchestration*
*Context gathered: 2026-05-16*
