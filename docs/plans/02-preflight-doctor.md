# Plan 02 — Preflight / Doctor Command (`preflight`)

**Type:** Add · **Priority:** P1 · **Est. effort:** ~1 day

## Problem

The two-month outage had two proximate causes that were each checkable in seconds but that
no single command checked:

- The pinned `ANTHROPIC_MODEL` rejected `temperature=0.0` (a `400` on the first classify
  call). No preflight ever exercised the model.
- 19 of 35 real holdings are not `core_holding` in `universe.csv`, so `news-morning` is
  blind to them even when green. No command surfaced this.

Additionally, several latent config traps exist: a past `thesis.yaml` `review_due` makes
`news-morning` refuse to run; Finnhub free-tier endpoints (`^GSPC`, `^VIX`, insider
sentiment) may be paid-only; missing secrets fail deep inside a job rather than up front.

## Goal

One operator command — `uv run python -m signal_system preflight` — that runs a battery of
cheap, read-only checks and prints a clear PASS/WARN/FAIL report. It is the "does this
machine work right now" button. It makes **no writes** and sends **no alerts** (it is not a
scheduled job; it is a human-invoked diagnostic).

## Checks (grouped)

### A. Secrets & config
- All `_require`d env vars present and non-empty: `FINNHUB_API_KEY`, `HEALTHCHECKS_UUID`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`.
  (`config` already raises on missing — preflight catches and reports rather than
  crashing.)
- Per-job `HEALTHCHECKS_UUID_*` present for every scheduled job → **WARN** if any fall back
  to the shared UUID (this is the masking risk from Plan 01).
- `ANTHROPIC_MODEL` is not a floating alias (no `-latest`); print the pinned value.

### B. Anthropic reachability + model validity  ← would have caught the outage
- Issue **one** minimal `messages.parse()` call with the real `ANTHROPIC_MODEL` and the
  exact parameters the classifier uses (so a deprecated `temperature`, unsupported
  `output_format`, or retired model is caught).
- Report: PASS (200), FAIL with the API error text (e.g. `temperature is deprecated for
  this model`), and the four token counts from `usage`.
- Cost is ~1 cheap call; acceptable for a manual command.

### C. Finnhub reachability + endpoint availability
- `fetch_spy_close()` (used by `daily-close`) → PASS/FAIL.
- `fetch_quote("AAPL")` → PASS/FAIL.
- `fetch_company_news("AAPL", yesterday, today)` returns a list → PASS/FAIL.
- `fetch_general_news()` → PASS/WARN (WARN if `[]`, since free tier may gate it).
- Explicitly probe the CLAUDE.md "known risk" endpoints and report which are paid-gated so
  scoring assumptions stay honest.

### D. Thesis health
- `load_thesis(config.THESIS_PATH)` parses → PASS/FAIL (surfaces `ThesisStaleError` with the
  `review_due` date). A past `review_due` is **FAIL** — it will halt `news-morning`.
- Print `review_due` and days remaining; **WARN** if < 14 days out.

### E. Data coverage / drift  ← reuses Plan 03
- Holdings load without error (surfaces the `HOLDINGS_PATH` default-arg bug if present).
- `holdings_universe_drift()` → **WARN** listing held tickers not scanned (the 19).
- Universe non-empty; `get_core_holdings()` non-empty.
- K-1 denylist intact (no `USO/UNG/DBC/GSG` leaked into the discovery universe).

### F. State integrity
- DB opens, WAL mode on, `count_signals()` returns.
- Latest `backup` run succeeded within 8 days (WARN otherwise).
- Print last success datetime per scheduled job (a lightweight, on-demand version of the
  Plan 01 watchdog view).

## Output format

Human-readable, aligned, color-optional (respect `NO_COLOR`). Exit code encodes worst
status so it is CI/scriptable:

```
SIGNAL SYSTEM PREFLIGHT — 2026-08-09 14:30 ET

[A] Secrets & config
  PASS  all required secrets present
  WARN  per-job health checks unset: news-morning, discovery, sentinel (masking risk)
[B] Anthropic
  FAIL  model 'claude-sonnet-5' rejected params: `temperature` is deprecated for this model
[C] Finnhub
  PASS  spy_close=641.20  quote(AAPL)=...  company_news=12 items
  WARN  general_news returned [] (free-tier gated?)
[D] Thesis
  PASS  review_due 2026-09-30 (52 days)
[E] Coverage
  WARN  19 held tickers not scanned: AAPL, CAN, CDE, CMC, ESEA, ETH, FCX, JNUG, MARA, MU...
[F] State
  PASS  6702 signals, WAL on, backup 0 days ago

RESULT: FAIL (1 fail, 3 warn)   exit=2
```

Exit codes: `0` all pass, `1` warnings only, `2` any failure.

## Files touched

| File | Change |
|------|--------|
| `src/signal_system/preflight.py` | **New.** Each check a small function returning `(status, message)`; a runner aggregates and formats. |
| `src/signal_system/__main__.py` | Add `elif cmd == "preflight": _cmd_preflight(sys.argv[2:])`; add to the usage banner. |
| `src/signal_system/data/holdings.py` | Fix the `load_holdings(path=HOLDINGS_PATH)` default-arg binding so preflight (and tests) can point it at a probe path. See Plan 03. |
| `tests/test_preflight.py` | **New** — mock each external boundary; assert status classification and exit codes. |
| `ops/operator-setup-checklist.md` | Add "run `preflight` after any `.env` or model change." |

## Design constraints

- **Read-only.** No `runs` row, no `signals` write, no health-check ping. It must be safe to
  run anytime, including mid-incident.
- **Never crash.** Each check is wrapped; a check that throws is reported as FAIL with the
  exception, not an uncaught traceback (the whole point is to *replace* deep tracebacks).
- **No new dependencies.** Reuse existing clients (`finnhub_client`, the Anthropic client
  factory, `thesis_loader`, `universe`, `repository`).
- **Fast.** Target < 10s wall time; the Anthropic + Finnhub probes dominate.

## Testing

- Each check function unit-tested with mocked boundaries for PASS / WARN / FAIL.
- `test_preflight_exit_code_worst_status` — mix of statuses → correct aggregate exit code.
- `test_preflight_anthropic_400_reported_not_raised` — simulate `BadRequestError` → FAIL row,
  no traceback, process exits 2.
- `test_preflight_readonly` — run against a temp DB and assert row counts unchanged and no
  `runs` inserted.

## Rollout

1. Land with the `holdings.py` default-arg fix (shared with Plan 03).
2. Document in the operator checklist: run before enabling a schedule and after any model or
   `.env` change.
3. Optional follow-up: a `--json` flag so the Plan 01 watchdog or CI can consume it.

## Risks

- **Cost creep** if run in a loop — it makes live API calls. Keep it human-invoked; do not
  schedule it. (The scheduled liveness role belongs to Plan 01.)
- **Finnhub rate limit** — preflight makes ~4 calls; well under the 60/min bucket.

## Acceptance criteria

- Running `preflight` against today's real config reproduces the two findings from the
  review (model param FAIL when a bad model is pinned; 19-ticker drift WARN) without any
  code path throwing.
- Exit code is non-zero when any FAIL is present, enabling `preflight && enable-task`
  gating.
