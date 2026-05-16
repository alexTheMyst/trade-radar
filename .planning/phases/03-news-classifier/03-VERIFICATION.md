---
phase: 03-news-classifier
verified: 2026-05-16T06:18:00-06:00
status: passed
score: 6/6
overrides_applied: 0
re_verification: false
---

# Phase 3: News Classifier — Verification Report

**Phase Goal:** Implement a headline-based news classifier using Anthropic `messages.parse()` at temperature=0 with ephemeral prompt caching, parse-failure MONITORING recovery, in-memory dedup via SHA-256, and full LLM token telemetry via `insert_llm_call`.

**Verified:** 2026-05-16T06:18:00-06:00
**Status:** PASSED
**Re-verification:** No — initial verification
**Test suite:** 69/69 tests pass (`uv run pytest tests/ -x -q`)

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `_sanitize_headline()` strips ANSI escapes + Cc-category control chars, HTML-escapes `<`/`>`, truncates at 500 chars, wraps in `<headline>` delimiters | VERIFIED | `test_sanitize_headline_*` (8 tests); ANSI pre-pass bug fixed (`re.sub` before Cc filter) in af6fa1b |
| 2 | Classification calls use `temperature=0.0`, pinned `config.ANTHROPIC_MODEL`, `output_format=ClassificationResult`; `model_version` stamped on Signal | VERIFIED | `test_classify_uses_temperature_zero`, `test_classify_passes_output_format`; grep gates confirm literals in source |
| 3 | System prompt uses `cache_control` ephemeral block; `cache_read_input_tokens` and `cache_creation_input_tokens` logged to `llm_calls` table | VERIFIED | `test_system_includes_cache_control`; T13 cache token round-trip asserts `cache_read_input_tokens=800` logged |
| 4 | `ValidationError` after 1 retry → MONITORING signal; `parsed_output is None` → immediate MONITORING signal; neither silently dropped | VERIFIED | `test_parse_failure_retries_once_then_monitoring`, `test_empty_parsed_output_emits_monitoring` |
| 5 | Classifier imports only permitted modules — no `email_sender`, `heartbeat`, or `router` | VERIFIED | `grep -c "email_sender" src/signal_system/classifier/` returns 0; `test_phase3_public_api_importable` |
| 6 | In-memory dedup via SHA-256 key (ticker:ET-date:normalized-headline) short-circuits before API; `alert_id` stable across re-runs; `INSERT OR IGNORE` at DB layer | VERIFIED | `test_classify_headlines_dedup_skips_duplicate`, `test_classify_headlines_dedup_normalizes_whitespace`, `test_classify_headlines_alert_id_stable_across_runs` |

**Score:** 6/6 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/signal_system/classifier/__init__.py` | Exports `classify_headlines`, `ClassificationResult` | VERIFIED | Package init created; both names importable |
| `src/signal_system/classifier/news_classifier.py` | Full classifier: sanitize, system prompt, classify_headline, classify_headlines, dedup | VERIFIED | All functions implemented; ANSI pre-pass bug fixed in af6fa1b |
| `src/signal_system/models.py` | `model_version: str | None` and `thesis_version_hash: str | None` added to Signal | VERIFIED | Fields added with `None` defaults; frozen dataclass constraint maintained |
| `src/signal_system/state/repository.py` | `insert_llm_call()` keyword-only; `insert_signal()` wired for `model_version`/`thesis_version_hash` | VERIFIED | Keyword-only enforced via `*` sentinel; `count_delivered_today` function restored after edit collision bug (d95f244) |
| `tests/test_smoke.py` | 41 new Phase 3 tests (TDD RED→GREEN) | VERIFIED | 69 total tests passing (+41 from Phase 2 baseline of 28) |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `classify_headlines(tickers, headlines)` | `classify_headline()` per headline | System prompt built once; `dedup_seen` set shared across batch | WIRED | O(1) dedup check before any API call |
| `classify_headline()` | `anthropic.messages.parse()` | `_call_with_retry()` with tenacity; `temperature=0.0`; `output_format=ClassificationResult` | WIRED | Parse-failure path → 1 retry → MONITORING signal |
| `insert_llm_call()` | `llm_calls` SQLite table | Keyword-only params: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `job`, `model_version` | WIRED | All 4 token counts from `response.usage` logged per call |
| Signal | `repository.insert_signal()` | `model_version` + `thesis_version_hash` bound from Signal fields | WIRED | Columns wired; Phase 3 is the first consumer to write these fields |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| 69 tests pass end-to-end | `uv run pytest tests/ -x -q` | `69 passed` | PASS |
| Sanitization strips ANSI + Cc | `test_sanitize_headline_strips_control_chars` | ANSI sequences and Cc chars removed; `\n`/`\t` preserved | PASS |
| Temperature locked to 0.0 | `test_classify_uses_temperature_zero` | `temperature=0.0` asserted in API call kwargs | PASS |
| Cache control on system prompt | `test_system_includes_cache_control` | `cache_control` ephemeral block present | PASS |
| Parse failure → MONITORING | `test_parse_failure_retries_once_then_monitoring` | 1 retry then MONITORING signal emitted | PASS |
| None parsed_output → MONITORING | `test_empty_parsed_output_emits_monitoring` | Immediate MONITORING without retry | PASS |
| Dedup short-circuits API | `test_classify_headlines_dedup_skips_duplicate` | API not called for duplicate headline | PASS |
| alert_id stable across re-runs | `test_classify_headlines_alert_id_stable_across_runs` | Identical SHA-256 on second call | PASS |
| No forbidden imports | grep gate | 0 matches for email_sender, heartbeat, router in classifier/ | PASS |

---

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|-------------|-------------|--------|----------|
| CLFY-01 | Headline sanitization: strip ANSI + Cc-category, HTML-escape `<`/`>`, truncate at 500 chars, wrap in `<headline>` delimiters | SATISFIED | `_sanitize_headline()` with ANSI pre-pass; 8 sanitization tests |
| CLFY-02 | Deterministic classification: `temperature=0.0`, pinned `config.ANTHROPIC_MODEL`, `output_format=ClassificationResult`, `model_version` stamped on Signal | SATISFIED | `test_classify_uses_temperature_zero`, `test_classify_passes_output_format`, grep gates |
| CLFY-03 | Prompt caching: `cache_control` ephemeral block on system prompt; cache token counts logged to `llm_calls` | SATISFIED | `test_system_includes_cache_control`; T13 cache token round-trip test |
| CLFY-04 | Parse-failure recovery: `ValidationError` → 1 retry → MONITORING; `parsed_output is None` → immediate MONITORING; neither silently dropped | SATISFIED | `test_parse_failure_retries_once_then_monitoring`, `test_empty_parsed_output_emits_monitoring` |
| CLFY-05 | Isolation: classifier imports only `anthropic`, `pydantic`, `tenacity`, permitted `signal_system` modules — no delivery or router imports | SATISFIED | grep gate returns 0 for forbidden imports; `test_phase3_public_api_importable` |
| CLFY-06 | Dedup: in-memory SHA-256 `dedup_seen` set short-circuits before API; `alert_id` stable across re-runs; `INSERT OR IGNORE` at DB layer | SATISFIED | `test_classify_headlines_dedup_skips_duplicate`, `test_classify_headlines_dedup_normalizes_whitespace`, `test_classify_headlines_alert_id_stable_across_runs` |

---

### Deviations Noted

| Deviation | Impact | Resolution |
|-----------|--------|------------|
| ANSI escape leakage in `_sanitize_headline` (T6): stripping `\x1b` left `[31m` in output | Bug — prompt injection risk if not fixed | Auto-fixed: added `re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)` pre-pass; committed in af6fa1b |
| `insert_llm_call` edit consumed `count_delivered_today` function definition (T4) | Bug — `count_delivered_today` became unreachable dead code | Auto-fixed: function definition restored in follow-up edit; committed in d95f244 |

---

### Human Verification Required

One manual gate deferred — prompt cache activation requires a thesis.yaml that exceeds the Anthropic minimum token threshold (~1,024 tokens). The unit tests mock this behavior; empirical validation (confirming `cache_read_input_tokens > 0` on second call with a real thesis.yaml) deferred to first live run per VALIDATION.md.

---

## Gaps Summary

No gaps. All 6 CLFY requirements satisfied. 69 tests passing. Two auto-fixed bugs documented with commits; neither left residual risk.

---

*Verified: 2026-05-16T06:18:00-06:00*
*Verifier: Claude (gsd-verifier)*
