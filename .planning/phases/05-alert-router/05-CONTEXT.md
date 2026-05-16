# Phase 5: Alert Router — Context

**Gathered:** 2026-05-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the Alert Router that enforces daily budget caps, runs slot competition with deterministic tiebreaking, and returns typed routing decisions — one tuple per input signal. The router is pure logic with no DB side effects: it reads the current delivered count from the DB via `count_delivered_today()` and returns `(signal, routing_status, demoted_from)` tuples. The Phase 6 job calls `insert_signal()` and `email_sender` after routing. MONITORING signals (Discovery Phase A, parse-failure fallback) bypass the router entirely — agents insert them directly.

Phase 5 ends when `route_signals()` is importable, returns correct routing decisions, and 87+ tests pass. Job wiring (insert + email + digest) is Phase 6.

</domain>

<decisions>
## Implementation Decisions

### Hard Budget Caps (ROUT-01)

- **D-01:** Daily caps are hard-coded constants (not configurable via config):
  - `ACTION_REQUIRED`: max **1** per day
  - `INFORMATIONAL`: max **3** per day
  - `MONITORING`: unlimited — agents insert directly, router never handles them

- **D-02:** Budget applies **both agents combined** per day. There is no per-agent budget.

### Slot Competition Model (ROUT-02, ROUT-05)

- **D-03:** Competition is **intra-batch only**. Within a single `route_signals()` call, all input signals compete for available slots. Cross-run behavior is covered by D-06 (no eviction).

- **D-04:** Intra-batch allocation order is **severity-first, score-ranked within severity**:
  1. Sort all AR signals by score descending, alphabetical ticker tiebreak.
  2. Allocate available AR budget (0 or 1 remaining) to the top-ranked AR signal(s).
  3. Sort all INFO signals by score descending, alphabetical ticker tiebreak.
  4. Allocate available INFO budget (0–3 remaining) to the top-ranked INFO signal(s).
  5. Any signal that doesn't get a slot → SUPPRESSED.

- **D-05:** Tiebreak is deterministic: descending score, then ascending ticker alphabetical. This matches ROUT-05 and existing convention from Phase 4 `_rank_values()`.

### Cross-Run Behavior (ROUT-03)

- **D-06:** **No eviction.** Once a signal is DELIVERED, it is never retroactively changed. If the budget is full when a new signal arrives (from a later job run on the same day), the newcomer is SUPPRESSED regardless of its score. First-come-first-served across job runs.

- **D-07:** The router reads `repository.count_delivered_today()` at the start of each call to get the current DELIVERED counts. This is the only way cross-run budget awareness is achieved — no in-memory state. `count_delivered_today()` returns `{"ACTION_REQUIRED": N, "INFORMATIONAL": N}`.

### Budget Reset (ROUT-04)

- **D-08:** Budget resets at `America/New_York` midnight. `count_delivered_today()` already uses ET timezone with `LIKE 'YYYY-MM-DD%'` date prefix matching — this convention is inherited. The router does not reimplement timezone logic.

### demoted_from Field (ROUT-02)

- **D-09:** Add a `demoted_from` column to the `signals` table via `_ensure_column(cursor, "signals", "demoted_from", "TEXT")` in `init_db()`. Default NULL (DELIVERED and MONITORING signals have no reason code).

- **D-10:** Valid `demoted_from` reason codes (**typed, not free-form**):
  - `"budget_cap_ar"` — ACTION_REQUIRED slot already full (from DB read or intra-batch allocation)
  - `"budget_cap_info"` — INFORMATIONAL slot(s) already full
  - `"outscored"` — beaten by a higher-scored signal of the same severity within the same batch

- **D-11:** `demoted_from` is set **at insert time** — insert-only, no UPDATE path. Since `insert_signal()` uses `INSERT OR IGNORE`, this is the only write opportunity.

### Router Public API

- **D-12:** Router lives at `src/signal_system/router/alert_router.py` with a package `__init__.py` exporting `route_signals`.

- **D-13:** Public function signature:
  ```python
  def route_signals(signals: list[Signal]) -> list[tuple[Signal, str, str | None]]:
      """Route a batch of signals against today's delivery budget.

      Returns a list of (signal, routing_status, demoted_from) tuples,
      one per input signal. routing_status is 'DELIVERED' or 'SUPPRESSED'.
      demoted_from is None for DELIVERED signals.

      Reads count_delivered_today() once at start. Does NOT insert to DB.
      Caller (Phase 6 job) handles insert_signal() and email.
      """
  ```

- **D-14:** The router does **not** call `insert_signal()`, `email_sender`, or any other DB write. It is pure logic: reads one DB query (`count_delivered_today()`), runs competition, returns tuples.

- **D-15:** Input signals must have `severity` in `{"ACTION_REQUIRED", "INFORMATIONAL"}`. Any signal with `severity == "MONITORING"` passed to `route_signals()` should raise `ValueError` — MONITORING signals are agent-inserted directly and never go through the router.

### MONITORING Signal Bypass

- **D-16:** MONITORING signals bypass the router entirely. Two sources:
  1. Discovery Phase A: `discovery_agent.py` calls `insert_signal(signal, routing_status="MONITORING")` directly.
  2. News Classifier parse-failure: calls `insert_signal(signal, routing_status="MONITORING")` directly.
  The router has zero knowledge of these paths.

### Phase 5 Boundary

- **D-17:** Phase 5 delivers `route_signals()` — routing decisions only. Phase 6 is responsible for:
  - Calling `insert_signal(signal, routing_status=rs, demoted_from=dmf)` for each tuple
  - Calling `email_sender.send_email()` for each DELIVERED signal
  - Constructing and sending the zero-alert digest email

- **D-18:** `insert_signal()` must gain a `demoted_from: str | None = None` keyword argument (backward-compatible, similar to how `routing_status` was added in Phase 4). Planner must include this as a Wave 0 prerequisite.

### Test Strategy Notes

- **D-19:** Key test scenarios:
  - 5 AR signals in one batch → 1 DELIVERED (highest score, alphabetical tiebreak), 4 SUPPRESSED with `"outscored"` or `"budget_cap_ar"`
  - Mixed batch (2 AR + 5 INFO) → 1 AR DELIVERED + 3 INFO DELIVERED + 3 SUPPRESSED
  - Second job run same day: DB already has 1 AR DELIVERED → new AR signal → `"budget_cap_ar"`
  - Equal scores, different tickers → alphabetical winner is deterministic across reruns
  - ET midnight reset: signal at 23:59 ET vs signal at 00:01 ET next day → different budget windows
  - Empty input → returns `[]`

- **D-20:** `count_delivered_today()` must be monkeypatched in tests (real DB or patched). Tests should not touch the real `state/signals.db`.

</decisions>

<canonical_refs>
## Canonical References

- `src/signal_system/state/repository.py` — `count_delivered_today()`, `insert_signal()`, `_ensure_column()`
- `src/signal_system/models.py` — `Signal` dataclass, `Severity` type
- `src/signal_system/discovery/discovery_agent.py` — structural template for new agent module
- `.planning/REQUIREMENTS.md` — ROUT-01 through ROUT-05
- `.planning/phases/04-discovery-agent/04-CONTEXT.md` — D-10 Phase A direct-insert pattern
- `src/signal_system/data/finnhub_client.py` — NOT used by router (no Finnhub calls)
</canonical_refs>

<deferred>
## Deferred Ideas

None from this discussion.
</deferred>
