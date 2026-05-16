---
status: complete
phase: 03-news-classifier
source: 03-SUMMARY.md
started: 2026-05-16T12:03:21-07:00
updated: 2026-05-16T12:14:40-07:00
---

## Current Test

[testing complete]

## Tests

### 1. Package importable
expected: Run `python -c "from signal_system.classifier import classify_headlines, ClassificationResult; print('OK')"` — prints `OK` with no errors
result: pass

### 2. Signal fields extended
expected: Run `python -c "from signal_system.models import Signal; f = Signal.__dataclass_fields__; print('model_version' in f, 'thesis_version_hash' in f)"` — prints `True True`
result: pass

### 3. insert_llm_call importable
expected: Run `python -c "from signal_system.state.repository import insert_llm_call; print('OK')"` — prints `OK` with no errors
result: pass

### 4. Full test suite green
expected: Run `uv run pytest -q` — output shows `69 passed` (or more), `0 failed`, `0 errors`
result: pass

### 5. llm_calls table in DB
expected: Run `python -m signal_system daily-close` (or just `python -c "from signal_system.state import repository; repository.init_db()"`) then `sqlite3 state/signals.db ".schema llm_calls"` — output shows CREATE TABLE with columns: `id`, `run_id`, `agent`, `model`, `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `timestamp`
result: pass

### 6. Headline sanitizer strips control chars and ANSI sequences
expected: Run via temp script — prints `<headline>Apple reports earnings</headline>` (no ANSI codes, no null bytes, wrapped in delimiters)
result: pass

### 7. Headline sanitizer truncates at 500 chars
expected: Run `python /tmp/test_truncate.py` — prints `True True`
result: pass

## Summary

total: 7
passed: 7
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none yet]
