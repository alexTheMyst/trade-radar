---
phase: "02"
slug: data-layer
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-16
---

# Phase 02 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Finnhub API → finnhub_client.py | All HTTP responses (including error codes and response bodies) cross here; response data is untrusted | Quote dicts, news headline lists, HTTP status codes |
| Ticker symbols → finnhub_client.py | Ticker strings originate from operator-controlled `universe.csv` but pass through to API call params | Short ASCII strings (e.g. "AAPL") |
| Finnhub headlines → caller | Raw headline text returned from `fetch_company_news` is untrusted; sanitization is Phase 3 (CLFY-01) responsibility, NOT this module | Arbitrary news strings from third-party sources |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-02-01 | Tampering | Ticker symbols passed to `quote()` and `company_news()` | accept | See Accepted Risks Log | closed |
| T-02-02 | Denial of Service | Rate-limit exhaustion — 429 storm consuming retries | mitigate | `wait_exponential(min=2, max=60)` + `stop_after_attempt(5)` bounds per-ticker retry cost to ~240s worst case; token bucket enforces ≤55 calls/min preemptively via `_acquire_slot()` with `threading.Lock`; 11 tests including 429-retry path | closed |
| T-02-03 | Information Disclosure | `FINNHUB_API_KEY` leaked in error log messages | mitigate | All `logger.*` call sites use only `ticker` and `exc.status_code` — never `config.FINNHUB_API_KEY`; validated by code review of all logger call sites in T3/T5 | closed |
| T-02-04 | Elevation of Privilege | Paid-tier endpoint silent pass-through returning zero scores | mitigate | `PAID_TIER_STATUS_CODES = frozenset({403, 404})` catches both status codes; logs WARNING; returns `None` or `[]`; caller (Discovery Agent, DISC-02) enforces no-score-on-None policy; T4 tests verify both 403 and 404 paths | closed |
| T-02-05 | Tampering | Headline content containing prompt injection characters | transfer | Raw headlines returned as-is from `fetch_company_news`; sanitization (strip control chars, 500-char cap, `<headline>` delimiters) is CLFY-01's responsibility in Phase 3; transfer documented in both Phase 2 and Phase 3 PLAN.md trust boundaries | closed |
| T-02-SC | Tampering | Supply chain — `tenacity` PyPI package | mitigate | T1 verified PyPI JSON confirms `jd/tenacity` as source repo before `uv add`; package has >10-year history (2016+), high download count, well-known in ecosystem; pinned to `tenacity==9.1.4` in `pyproject.toml` | closed |

*Status: open · closed*
*Disposition: mitigate (implementation verified) · accept (documented risk) · transfer (third-party / next phase)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-02-01 | T-02-01 | Ticker symbols originate from operator-maintained `universe.csv`, not user input. Finnhub SDK encodes all params via `requests` URL encoding. No additional validation added — the operator controls the universe file and tickers have no execution surface. Blast radius of a malformed ticker is a Finnhub API error, not a system compromise. | operator | 2026-05-16 |

---

## Transferred Threats

| Transfer ID | Threat Ref | Transferred To | Owner | Status |
|-------------|------------|----------------|-------|--------|
| TR-02-01 | T-02-05 | Phase 03 (CLFY-01) | news_classifier.py `_sanitize_headline()` | resolved — Phase 03 SECURITY.md T-03-01/T-03-02 cover full sanitization + delimiter injection |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-16 | 6 | 6 | 0 | gsd-secure-phase (short-circuit: register_authored_at_plan_time=true, all dispositions present) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] Transferred threats documented with resolution status
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-16
