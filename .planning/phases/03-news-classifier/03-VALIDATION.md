---
phase: "03"
slug: news-classifier
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-16
---

# Phase 03 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 (via uv) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_smoke.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_smoke.py -q`
- **After every plan wave:** Run `uv run pytest -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 03-T1 | 01 | 1 | CLFY-02, TAX-04 | T-03-12 | Signal.model_version + thesis_version_hash fields exist; alert_id unchanged | unit | `uv run pytest tests/test_smoke.py -k "model_version or thesis_version" -q` | ✅ | ✅ green |
| 03-T2 | 01 | 1 | CLFY-02, TAX-04 | T-03-12 | Signal fields wired through insert_signal; backwards-compat alert_id preserved | unit | `uv run pytest tests/test_smoke.py -k "model_version or thesis_version" -q` | ✅ | ✅ green |
| 03-T3 | 01 | 1 | — | T-03-09 | insert_llm_call importable; keyword-only signature | unit | `uv run pytest tests/test_smoke.py -k "insert_llm_call" -q` | ✅ | ✅ green |
| 03-T4 | 01 | 1 | — | T-03-09 | insert_llm_call records all 4 token counts; keyword-only enforcement | unit | `uv run pytest tests/test_smoke.py -k "insert_llm_call" -q` | ✅ | ✅ green |
| 03-T5 | 01 | 1 | CLFY-01 | T-03-01, T-03-02 | _sanitize_headline: strips ANSI + Cc-category, HTML-escapes, truncates at 500, wraps in delimiters | unit | `uv run pytest tests/test_smoke.py -k "sanitize" -q` | ✅ | ✅ green |
| 03-T6 | 01 | 1 | CLFY-01, CLFY-05 | T-03-01, T-03-02, T-03-03 | classifier package importable; sanitized text reaches API; no email_sender/heartbeat imports | unit + grep | `uv run pytest tests/test_smoke.py -k "sanitize or import" -q` | ✅ | ✅ green |
| 03-T7 | 01 | 1 | CLFY-02, CLFY-03 | T-03-04 | temperature=0.0; pinned model; output_format=ClassificationResult; cache_control ephemeral present | unit | `uv run pytest tests/test_smoke.py -k "temperature or output_format or cache_control" -q` | ✅ | ✅ green |
| 03-T8 | 01 | 1 | CLFY-02, CLFY-03 | T-03-04, T-03-09 | classify_headline calls API with correct kwargs; logs all 4 token counts via insert_llm_call; stamps model_version + thesis_version_hash | unit | `uv run pytest tests/test_smoke.py -k "classify_headline" -q` | ✅ | ✅ green |
| 03-T9 | 01 | 1 | CLFY-04 | T-03-07, T-03-08 | ValidationError triggers 1 retry then MONITORING signal with raw_response in body | unit | `uv run pytest tests/test_smoke.py -k "parse_failure or retry" -q` | ✅ | ✅ green |
| 03-T10 | 01 | 1 | CLFY-04 | T-03-06, T-03-07, T-03-08 | None parsed_output → immediate MONITORING (no retry); [parse_failure] literal in source | unit | `uv run pytest tests/test_smoke.py -k "monitoring or parsed_output" -q` | ✅ | ✅ green |
| 03-T11 | 01 | 1 | CLFY-06 | T-03-11 | in-memory dedup skips duplicate headlines; whitespace normalization catches near-dupes; alert_id stable across runs | unit | `uv run pytest tests/test_smoke.py -k "dedup or alert_id_stable" -q` | ✅ | ✅ green |
| 03-T12 | 01 | 1 | CLFY-06 | T-03-11 | classify_headlines builds system prompt once per batch; returns list[Signal]; idempotent DB insert via INSERT OR IGNORE | unit | `uv run pytest tests/test_smoke.py -k "classify_headlines" -q` | ✅ | ✅ green |
| 03-T13 | 01 | 1 | CLFY-01..06 | all | Full integration smoke: all Phase 3 public surfaces importable; end-to-end classify_headlines → insert_signal → llm_calls round-trip | integration | `uv run pytest tests/test_smoke.py -k "phase3" -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Coverage Summary

| Requirement | Description | Tests | Status |
|-------------|-------------|-------|--------|
| CLFY-01 | Headline sanitization: strip ANSI + Cc-category, HTML-escape `<`/`>`, truncate at 500 chars, wrap in `<headline>` delimiters | `test_sanitize_headline_*` (8 tests), `test_classify_user_message_has_sanitized_headline` | ✅ COVERED |
| CLFY-02 | Deterministic classification: `temperature=0.0`, pinned `config.ANTHROPIC_MODEL`, `output_format=ClassificationResult`, `model_version` stamped on Signal | `test_classify_uses_temperature_zero`, `test_classify_passes_output_format`, grep gates: `temperature=0.0`, `model=config.ANTHROPIC_MODEL` | ✅ COVERED |
| CLFY-03 | Prompt caching: `cache_control` ephemeral block on system prompt; `cache_read_input_tokens` + `cache_creation_input_tokens` logged to `llm_calls` | `test_system_includes_cache_control`, T13 cache token round-trip (`cache_read_input_tokens=800`) | ✅ COVERED |
| CLFY-04 | Parse-failure recovery: `ValidationError` → 1 retry → `messages.create()` captures raw text → MONITORING signal; `parsed_output is None` → immediate MONITORING; neither silently dropped | `test_parse_failure_retries_once_then_monitoring`, `test_empty_parsed_output_emits_monitoring` | ✅ COVERED |
| CLFY-05 | Isolation: classifier imports only `anthropic`, `pydantic`, `tenacity`, `signal_system.{models,config,state.repository,data.thesis_loader}` — no `email_sender`, `heartbeat`, `router` | grep gate: `grep -c "email_sender" src/signal_system/classifier/` returns 0; `test_phase3_public_api_importable` | ✅ COVERED |
| CLFY-06 | Dedup: in-memory `dedup_seen` SHA-256 set short-circuits before API; `alert_id` stable across re-runs; `INSERT OR IGNORE` at DB layer | `test_classify_headlines_dedup_skips_duplicate`, `test_classify_headlines_dedup_normalizes_whitespace`, `test_classify_headlines_alert_id_stable_across_runs`, T13 INSERT OR IGNORE round-trip | ✅ COVERED |

**69 tests total | 0 failures | 0 gaps**

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. All Phase 3 tests live in `tests/test_smoke.py` under TDD RED→GREEN task sections (T1–T13). No new test files required.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Prompt cache activation on live API | CLFY-03 | Cache activation requires ~1,024+ token system prompt with a real Anthropic API call — cannot verify in mocked tests (R-03-A1 risk) | First `news-morning` run: `sqlite3 state/signals.db "SELECT cache_read_input_tokens FROM llm_calls ORDER BY id DESC LIMIT 10;"` — if all 0, expand `thesis.yaml` until rendered system prompt exceeds Anthropic's minimum token threshold |
| `thesis_version_hash` matches SHA-256 of thesis.yaml on live run | CLFY-02, TAX-04 | Tests monkeypatch a known hash; live value depends on actual `thesis.yaml` content read at runtime | First run: log `thesis_version_hash` from a returned Signal and cross-check against `sha256sum thesis.yaml` |

---

## Validation Sign-Off

- [x] All tasks have automated verify or manual-only justification
- [x] Sampling continuity: every task has a verify command (13 tasks, no gaps)
- [x] Wave 0 covers all requirements (pre-existing infra)
- [x] No watch-mode flags
- [x] Feedback latency < 5s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-05-16
