---
phase: "02"
slug: data-layer
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-16
---

# Phase 02 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 (via uv) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_smoke.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~3 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_smoke.py -q`
- **After every plan wave:** Run `uv run pytest -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~3 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 02-T1 | 01 | 1 | — | T-02-SC | tenacity PyPI identity verified before install | manual | `curl -s https://pypi.org/pypi/tenacity/json \| jq '.info.project_urls'` | ✅ | ✅ green |
| 02-T2 | 01 | 1 | DATA-01, DATA-02, DATA-03 | T-02-02, T-02-03, T-02-04 | Token bucket enforces rate limit; 429 retried; 403/404 returned None without retry | unit | `uv run pytest tests/test_smoke.py -k "token_bucket or fetch_quotes or retry_on_429 or no_retry_on_403" -q` | ✅ | ✅ green |
| 02-T3 | 01 | 1 | DATA-01, DATA-02, DATA-03 | T-02-02, T-02-03, T-02-04 | _acquire_slot, _fetch_single_quote, fetch_quotes implemented and green | unit | `uv run pytest tests/test_smoke.py -k "token_bucket or fetch_quotes or retry" -q` | ✅ | ✅ green |
| 02-T4 | 01 | 1 | DATA-03, DATA-04 | T-02-04, T-02-05 | 404 paid-tier returns None; fetch_company_news returns list with headline+source | unit | `uv run pytest tests/test_smoke.py -k "paid_tier or company_news" -q` | ✅ | ✅ green |
| 02-T5 | 01 | 1 | DATA-04 | T-02-05 | fetch_company_news returns [] on empty, 403/404, exhausted retry; passes dates as YYYY-MM-DD | unit | `uv run pytest tests/test_smoke.py -k "company_news" -q` | ✅ | ✅ green |
| 02-T6 | 01 | 1 | DATA-01..04 | all | All Phase 2 public names importable; PAID_TIER_STATUS_CODES contains 403+404 | integration | `uv run pytest tests/test_smoke.py -k "phase2_public_api" -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Coverage Summary

| Requirement | Description | Tests | Status |
|-------------|-------------|-------|--------|
| DATA-01 | Bulk quote fetch with preemptive token bucket (≤55 calls/min) | `test_token_bucket_calls_sleep`, `test_fetch_quotes_returns_dict`, `test_fetch_quotes_none_on_zero_price` | ✅ COVERED |
| DATA-02 | Retry 429 via tenacity up to 5 attempts, exponential backoff | `test_retry_on_429` (asserts `quote` called 5× on exhaustion) | ✅ COVERED |
| DATA-03 | 403/404 → log warning, return None/[], caller skips | `test_no_retry_on_403`, `test_paid_tier_404_returns_none`, `test_company_news_returns_empty_on_paid_tier` | ✅ COVERED |
| DATA-04 | fetch_company_news: headlines + source, returns [] on empty or 403/404 | `test_company_news_returns_list`, `test_company_news_empty_on_no_results`, `test_company_news_passes_dates_as_strings` | ✅ COVERED |

**28 tests total | 0 failures | 0 gaps**

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. All tests live in `tests/test_smoke.py` under the `# Phase 2: T2` and `# Phase 2: T4` sections. No new test files required.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| tenacity PyPI package identity | T-02-SC | Supply chain check must precede `uv add` — a one-time pre-install gate, not a repeatable test | `curl -s https://pypi.org/pypi/tenacity/json \| jq '.info.project_urls'` — confirm `jd/tenacity` GitHub URL |
| Live 429 rate-limit backoff timing | DATA-02 | Requires real Finnhub API key and hitting the actual rate limit — cannot safely automate in CI | Run manually with `FINNHUB_API_KEY` set; observe `tenacity` WARNING log lines and exponential delay |
| Paid-tier endpoint discrimination (live) | DATA-03 | Requires real API key where 403/404 triggers naturally — mocked in tests but runtime behavior must be confirmed on first production run | First live run: check logs for WARNING containing `paid-tier` on any 403/404 response |

---

## Validation Sign-Off

- [x] All tasks have automated verify or manual-only justification
- [x] Sampling continuity: every task has a verify command
- [x] Wave 0 covers all requirements (pre-existing infra)
- [x] No watch-mode flags
- [x] Feedback latency < 3s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-05-16
