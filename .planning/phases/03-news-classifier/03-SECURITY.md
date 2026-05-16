---
phase: "03"
slug: news-classifier
status: verified
threats_open: 0
asvs_level: 1
created: 2026-05-16
---

# Phase 03 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Finnhub headline data → classifier | Untrusted text from arbitrary news sources via Finnhub API | Raw headline strings (arbitrary length, arbitrary content) |
| Sanitized headline → Anthropic API | Defense layer 1: control-char strip, HTML-escape, `<headline>` delimiters; defense layer 2: system prompt instructs model to treat headline as untrusted | Sanitized, bounded, delimited headline string (≤500 chars) |
| Anthropic API response → ClassificationResult | Pydantic schema validation via `messages.parse()`; ValidationError on schema mismatch | Structured JSON → typed `ClassificationResult` instance |
| ClassificationResult → Signal | Severity assigned from confidence band; `pillar_name` is freeform `str | None` (limited blast radius — downstream router enforces budget) | Signal dataclass fields |
| Signal → SQLite signals table | `INSERT OR IGNORE` on `alert_id`; idempotent reruns | Signal row insert |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-03-01 | Tampering | Headline text containing control chars / prompt injection | mitigate | `_sanitize_headline()` strips Unicode C-category chars (Cc/Cf/Cs/Co/Cn), HTML-escapes `<`/`>`, truncates to 500 chars with `…`, wraps in `<headline>…</headline>` delimiters; verified by T5/T6 tests (CLFY-01) | closed |
| T-03-02 | Tampering | Embedded `</headline>` injection breaking delimiter | mitigate | HTML-escape (`<`→`&lt;`, `>`→`&gt;`) runs BEFORE delimiter wrapping; nested-delimiter attack from RESEARCH Pitfall 5 defeated; system prompt also instructs model to treat headline as untrusted | closed |
| T-03-03 | Information Disclosure | `ANTHROPIC_API_KEY` leaked in logs | mitigate | Logger calls use only ticker, status, token counts — never `config.ANTHROPIC_API_KEY`; grep gate in T13 verifies no `os.environ` reads in classifier module | closed |
| T-03-04 | Denial of Service | Cost runaway from infinite retry on ValidationError | mitigate | `tenacity.stop_after_attempt(2)` caps at 1 retry max; classifier processes ≤50 headlines per run (Phase 6 cap from JOBS-04); per-headline worst case is 2 `messages.parse()` + 1 `messages.create()` on failure | closed |
| T-03-05 | Tampering | Schema spoofing — model returns valid JSON with malicious `pillar_name` | accept | See Accepted Risks Log | closed |
| T-03-06 | Denial of Service / Tampering | Model returns refusal or empty content (`parsed_output is None`) | mitigate | T10 detects `parsed is None`, emits MONITORING Signal — never silently dropped (CLFY-04); no retry on this branch (model intentionally returned no text) | closed |
| T-03-07 | Tampering | Silent parse-failure swallow (CLFY-04 violation) | mitigate | T10 catches `pydantic.ValidationError`, retries once, then calls `messages.create()` to capture raw text into Signal.body — every parse failure emits a MONITORING Signal with `raw_response`; T9 tests assert this; grep gate verifies `[parse_failure]` literal present in source | closed |
| T-03-08 | Tampering | Structured-output schema drift (model returns valid JSON missing required field) | mitigate | Same path as T-03-07 — `ValidationError` triggers retry then MONITORING; operator sees schema drift in Signal.body and can update `ClassificationResult` / system prompt | closed |
| T-03-09 | Information Disclosure | Cost telemetry insufficient — operator cannot detect cost runaway | mitigate | `insert_llm_call()` records all 4 token counts on every API call (success AND failure recovery); operator queries `SELECT SUM(input_tokens), SUM(cache_read_input_tokens) FROM llm_calls WHERE job='news_classifier'` for budget visibility | closed |
| T-03-10 | Tampering | Cache miss because system prompt below Anthropic activation minimum | accept | See Accepted Risks Log | closed |
| T-03-11 | Tampering | Signal duplicates created by re-running classifier on same headline | mitigate | Two-layer dedup: in-memory `dedup_seen` set (SHA-256 key: `ticker:ET-date:normalized-headline`) short-circuits before API call (T12); `alert_id` collision falls through to `INSERT OR IGNORE` at DB layer; T11 + T13 verify both layers | closed |
| T-03-12 | Spoofing | `model_version` / `thesis_version_hash` missing on signals (Phase 1 schema gap) | mitigate | T2 extends `Signal` dataclass; fields threaded through `insert_signal()`; T1 / T8 / T13 verify stamping on all signal paths | closed |
| T-03-SC | Tampering | Supply chain — `anthropic`, `pydantic`, `tenacity` PyPI packages | mitigate | All three packages pinned in `pyproject.toml` (Phase 1 / Phase 2); `anthropic` is Anthropic's official SDK; `pydantic` is FastAPI-ecosystem mainstream; `tenacity` PyPI legitimacy verified in Phase 2 T1 | closed |

*Status: open · closed*
*Disposition: mitigate (implementation verified) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-03-01 | T-03-05 | `pillar_name: str | None` is freeform in `ClassificationResult`. Downstream severity uses confidence band, not pillar identity; Phase 5 Alert Router enforces delivery budget cap regardless. Blast radius is one miscategorized signal — not a system compromise. | operator | 2026-05-16 |
| AR-03-02 | T-03-10 | Cache miss if thesis.yaml system prompt is below Anthropic prompt-caching activation minimum (~1,024 tokens). Functional behavior is unaffected; only cost optimization is lost. `insert_llm_call` will show `cache_read_input_tokens=0` across runs, making the gap visible to operator. Listed in Risk Register R-03-A1 for empirical validation on first real run. | operator | 2026-05-16 |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-16 | 13 | 13 | 0 | gsd-secure-phase (short-circuit: register_authored_at_plan_time=true, all dispositions present) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-16
