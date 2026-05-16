---
phase: "05"
slug: alert-router
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-16
---

# Phase 05 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Signal input | `route_signals()` receives `list[Signal]` from caller | severity, score, ticker — all untrusted until validated |
| Local DB | `repository.insert_signal()` writes routing decisions to SQLite | routing_status, demoted_from, signal fields |
| Concurrent jobs | SQLite WAL mode handles concurrent Task Scheduler job writes | routing state shared across job invocations |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-05-01 | Elevation of Privilege | `alert_router.py:route_signals()` | mitigate | `ValueError` raised immediately if any input signal has `severity == "MONITORING"` — router has zero knowledge of the MONITORING path | closed |
| T-05-02 | Tampering | `alert_router.py` sort key | mitigate | `score or 0.0` coerces `None`/`NaN` to `0.0`; highest explicit score always wins slot competition deterministically | closed |
| T-05-03 | Information Disclosure | `alert_router.py` constants | mitigate | `demoted_from` typed as `Literal["budget_cap_ar","budget_cap_info","outscored"]` — enforced at assignment; SQL INSERT parameterised | closed |
| T-05-04 | Tampering | `repository.py:insert_signal` | mitigate | All DB writes via `insert_signal()` with parameterised `?` placeholders — `routing_status` and `demoted_from` never interpolated into SQL | closed |
| T-05-05 | Denial of Service | `repository.py` WAL + INSERT OR IGNORE | mitigate | SQLite WAL mode + `INSERT OR IGNORE` makes simultaneous double-insert safe; no budget eviction — first write wins | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

No accepted risks.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-16 | 5 | 5 | 0 | gsd-secure-phase (plan-time register, short-circuit rule applied; T-05-01 verified by UAT T-AR-06) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-16
