---
phase: 6
slug: job-orchestration
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-17
---

# Phase 6 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Finnhub API → news_morning.py | External news headlines fetched for core holdings | Untrusted headline text; timestamps from external source |
| news_morning.py → Claude API | Headlines embedded in classification prompts | User-visible ticker/headline content (control-char stripped, capped at ~500 chars) |
| Claude API → repository.py | LLM classification output persisted as signals | Signal severity, body, title |
| repository.py → SQLite | All state reads/writes including run dates and signals | Internal state; ET-timezone ISO strings |
| jobs/common.py → Gmail SMTP | Digest email delivery | Signal summaries; zero-alert confirmation text |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-6-01 | Tampering | `repository.py`, `news_morning.py` | mitigate | Window lower bound derived from `get_latest_successful_run_date("daily-close")` → `_previous_close_datetime()` pins to 4:00 PM ET on that date. `RuntimeError` raised if no prior successful run exists — no Finnhub calls or emails proceed. | closed |
| T-6-02 | Denial of Service | `jobs/common.py`, `news_morning.py` | mitigate | `render_digest()` always appends `"Scanned N tickers, 0 alerts"` line when `DELIVERED == 0`. `validate_digest_payload()` raises `RuntimeError` if that line is absent on zero-delivery runs — digest send is blocked. | closed |
| T-6-03 | Information Disclosure | `news_morning.py` | mitigate | `_dedupe_and_cap_headlines()` returns both kept and overflow slices. Every overflow item is persisted via `_persist_monitoring_signal(_make_overflow_monitoring_signal(...))` with `routing_status="MONITORING"` before classification begins — no headlines are silently dropped. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

No accepted risks.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-17 | 3 | 3 | 0 | gsd-secure-phase (Copilot) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-17
