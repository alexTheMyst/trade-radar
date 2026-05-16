---
phase: "04"
slug: discovery-agent
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-16
---

# Phase 04 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| External API | Finnhub REST API → `fetch_quote()` / `_fetch_single_quote()` | Ticker strings from universe file; quote data returned |
| Local DB | `repository.update_run_counts()` writes to SQLite `runs` table | run_id (UUID), integer counts |
| Config | `DISCOVERY_PHASE` env var read inside `score_universe()` | Phase A/B switch; controls routing_status value |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-04-01 | Tampering | `fetch_quote()`, `_fetch_single_quote()` | mitigate | Finnhub SDK escapes ticker in URL construction; no string interpolation into SQL | closed |
| T-04-02 | Tampering | `discovery_agent.py` scoring loop | mitigate | `h==l` guard sets `range_position=0.0` before ranking — no NaN produced | closed |
| T-04-03 | Elevation of Privilege | `discovery_agent.py:score_universe` | mitigate | `routing_status` is hardcoded literal `"MONITORING"`, never derived from external input | closed |
| T-04-04 | Tampering | `repository.py:update_run_counts` | mitigate | Parameterised `?` placeholder in `UPDATE runs SET` — ticker never reaches SQL | closed |
| T-04-05 | Denial of Service | `discovery_agent.py:_rank_values` | mitigate | Explicit `n==1` early return → all ranks `0.5`; no division by zero possible | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

No accepted risks.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-16 | 5 | 5 | 0 | gsd-secure-phase (plan-time register, short-circuit rule applied) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-16
