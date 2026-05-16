---
phase: "03"
plan: "01"
subsystem: news-classifier
tags: [classifier, anthropic, pydantic, tdd, signal, llm-telemetry]
dependency_graph:
  requires: [signal_system.models, signal_system.state.repository, signal_system.data.thesis_loader, signal_system.config]
  provides: [signal_system.classifier.classify_headlines, signal_system.classifier.ClassificationResult, signal_system.state.repository.insert_llm_call]
  affects: [signal_system.models.Signal, signal_system.state.repository.insert_signal]
tech_stack:
  added: [anthropic, pydantic, tenacity]
  patterns: [TDD red-green, structured-output via messages.parse(), prompt caching with ephemeral cache_control, in-memory dedup, MONITORING signal recovery]
key_files:
  created:
    - src/signal_system/classifier/__init__.py
    - src/signal_system/classifier/news_classifier.py
  modified:
    - src/signal_system/models.py
    - src/signal_system/state/repository.py
    - tests/test_smoke.py
decisions:
  - "ANSI escape stripping (re.sub) before Unicode Cc category filter — prevents [31m leakage after ESC removal"
  - "MONITORING signal on both ValidationError (after retry) and None parsed_output — no exception propagation from batch loop"
  - "insert_llm_call is keyword-only — prevents positional-arg drift when columns change"
  - "Dedup key = SHA-256(ticker:ET-date:normalized-headline) — deterministic, date-scoped, ticker-scoped"
  - "System prompt uses cache_control ephemeral block — Anthropic prompt caching for the fixed system prompt"
metrics:
  duration: "~45 min"
  completed_date: "2026-05-16"
  tasks_completed: 10
  files_modified: 5
---

# Phase 3 Plan 01: News Classifier Summary

**One-liner:** News headline classifier using Anthropic `messages.parse()` at temperature=0 with ephemeral prompt caching, parse-failure MONITORING recovery, in-memory dedup, and full LLM token telemetry via `insert_llm_call`.

## What Was Built

### New Files
- **`src/signal_system/classifier/__init__.py`** — Package init exporting `classify_headlines` and `ClassificationResult`
- **`src/signal_system/classifier/news_classifier.py`** — Full classifier implementation:
  - `_sanitize_headline()` — strips ANSI escapes, Unicode Cc control chars (preserving `\n`/`\t`), HTML-escapes `<`/`>`, truncates at 500 chars with `…`, wraps in `<headline>...</headline>`
  - `_build_system_prompt()` — deterministic prompt from `Thesis` pillars, structured for prompt caching
  - `ClassificationResult` — pydantic model (`pillar_name`, `confidence`, `direction`, `rationale`)
  - `classify_headline()` — single headline: calls `_call_with_retry()`, handles `ValidationError` with raw-text recovery, handles `None` parsed_output — both emit MONITORING signals
  - `classify_headlines()` — batch: builds system prompt once, deduplicates via SHA-256 key, returns `list[Signal]`

### Modified Files
- **`src/signal_system/models.py`** — Added `model_version: str | None = None` and `thesis_version_hash: str | None = None` to `Signal` dataclass
- **`src/signal_system/state/repository.py`** — Wired `signal.model_version`/`signal.thesis_version_hash` into `insert_signal()`; added `insert_llm_call()` keyword-only function

## Test Count

**Before:** 28 tests | **After:** 69 tests (+41 new tests)

## Key Decisions Made

1. **ANSI escape pre-stripping** — `re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)` before Cc-category filter. Discovered during T6 greenification: stripping `\x1b` alone leaves `[31m` in output. Added `import re` and pre-pass step. [Rule 1 - Bug]

2. **`insert_llm_call` keyword-only** — All 6 params are keyword-only (`*` sentinel). Prevents silent positional-arg errors if column order changes.

3. **Dedup key scope** — `SHA-256(ticker:ET-date:normalized-headline)`. Scoped to ticker+date prevents cross-ticker suppression; normalization (lowercase, collapse whitespace, strip trailing punctuation) catches near-duplicates from Finnhub re-publishing.

4. **No retry on `None` parsed_output** — `parsed_output is None` means the model returned a non-text block (refusal). No retry makes sense; MONITORING signal emitted immediately.

5. **System prompt passed as parameter** — `classify_headline()` accepts `system_prompt` as a parameter (not building it internally). This means `classify_headlines()` builds it once per batch for cache efficiency.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] ANSI escape sequence leakage in `_sanitize_headline`**
- **Found during:** T6 GREEN (test_sanitize_headline_strips_control_chars)
- **Issue:** Input `"Apple\x00 reports\x07 earnings\x1b[31m"` produced `"<headline>Apple reports earnings[31m</headline>"` — the ESC `\x1b` was stripped (Cc category), but `[31m` remained
- **Fix:** Added `re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)` as a pre-pass before the character-by-character Cc filter; added `import re`
- **Files modified:** `src/signal_system/classifier/news_classifier.py`
- **Commit:** af6fa1b (fix applied within T6 GREEN commit)

**2. [Rule 1 - Bug] `insert_llm_call` edit consumed `count_delivered_today` function definition**
- **Found during:** T4 GREEN verification (`uv run pytest -x -q`)
- **Issue:** Edit tool replaced the line `def count_delivered_today() -> dict[str, int]:` as part of the anchor string, leaving the function body as dead code under `insert_llm_call`
- **Fix:** Restored `def count_delivered_today() -> dict[str, int]:` function definition in a follow-up edit
- **Files modified:** `src/signal_system/state/repository.py`
- **Commit:** d95f244 (fix applied within T4 GREEN commit)

## Known Stubs

None — all implemented functionality is wired end-to-end. The classifier calls `_get_client()` which requires `ANTHROPIC_API_KEY` in the environment; tests monkeypatch `_get_client` to avoid real API calls.

## Threat Flags

| Flag | File | Description |
|------|------|-------------|
| threat_flag: prompt-injection | src/signal_system/classifier/news_classifier.py | Untrusted headline text embedded in LLM prompt — mitigated by `<headline>` delimiter wrapping, HTML-escaping `<`/`>`, and explicit Security Note in system prompt |

## Self-Check: PASSED

Files created:
- FOUND: src/signal_system/classifier/__init__.py
- FOUND: src/signal_system/classifier/news_classifier.py

Commits exist:
- f97ada4: test(03): RED — Signal.model_version + thesis_version_hash fields
- 14dbe94: feat(03): extend Signal with model_version + thesis_version_hash; wire into insert_signal
- dc6edde: test(03): RED — repository.insert_llm_call helper
- d95f244: feat(03): add repository.insert_llm_call for token telemetry
- 3d86e18: test(03): RED — _sanitize_headline + _build_system_prompt
- af6fa1b: feat(03): create classifier package skeleton; sanitization + system prompt builder
- 881ba18: test(03): RED — classify_headline API kwargs, llm_calls logging, signal stamping
- b7d51e3: test(03): RED — parse-failure retry + MONITORING signal + raw_response capture
- d62f669: test(03): RED — classify_headlines batch, dedup, shared-set, idempotency
- 1fb81cc: test(03): phase integration smoke — all Phase 3 surfaces verified end-to-end

Test suite: 69 passed, 0 failed
