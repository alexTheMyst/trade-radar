# Phase 6: Job Orchestration - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-16
**Phase:** 06-job-orchestration
**Areas discussed:** News scan scope and lookback window, Email and digest shape, Discovery behavior in Phase A vs Phase B, Headline cap and overflow policy

---

## News scan scope and lookback window

| Option | Description | Selected |
|--------|-------------|----------|
| Core holdings only | Use the existing `core_holding=1` set from the universe file | ✓ |
| Today's rotation universe | Classify the same broader set Discovery scans that day | |
| All universe tickers | Widest coverage, highest runtime and noise | |

**User's choice:** `core_holdings_only`
**Notes:** `news-morning` should fetch headlines only for core holdings, using the window since the previous market close.

| Option | Description | Selected |
|--------|-------------|----------|
| Since previous market close | Overnight/pre-open window through run time | ✓ |
| Last 24 hours | Rolling 24-hour window | |
| Last 7 calendar days | Rolling weekly window | |

**User's choice:** `since_previous_market_close`
**Notes:** The job should not use a rolling calendar window.

---

## Email and digest shape

| Option | Description | Selected |
|--------|-------------|----------|
| One digest per job run | Includes all delivered alerts in one message | ✓ |
| One email per delivered alert | Highest immediacy, most inbox noise | |
| Per-alert emails plus summary | Most verbose | |

**User's choice:** `single_digest_per_job`
**Notes:** Delivery should stay digest-first across the live jobs.

| Option | Description | Selected |
|--------|-------------|----------|
| Counts only | Show delivered alerts in detail, summarize everything else by count | ✓ |
| Full sections | List suppressed and monitoring items in the digest | |
| Delivered only | Hide suppressed/monitoring results from email | |

**User's choice:** `counts_only`
**Notes:** Non-delivered outcomes stay visible only as counts, not full itemized sections.

---

## Discovery behavior in Phase A vs Phase B

| Option | Description | Selected |
|--------|-------------|----------|
| No email | Strict logs-only calibration, inspect SQLite when needed | ✓ |
| Summary digest only | Send counts and run stats, no full monitoring list | |
| Full monitoring digest | Email all candidate results even in Phase A | |

**User's choice:** `no_email_db_only`
**Notes:** Discovery Phase A should remain inbox-silent and DB-only.

| Option | Description | Selected |
|--------|-------------|----------|
| Send zero-alert digest | Explicit confirmation even when router delivers nothing | ✓ |
| Silent if no delivery | No email if nothing is delivered | |

**User's choice:** `send_zero_alert_digest`
**Notes:** Discovery Phase B should mirror the explicit zero-alert confirmation behavior instead of staying silent.

---

## Headline cap and overflow policy

| Option | Description | Selected |
|--------|-------------|----------|
| Dedup first, newest 50 | Process the newest 50 unique headlines | ✓ |
| Raw feed first 50 | Keep the first 50 items exactly as Finnhub returns them | |
| Dedup then feed-order 50 | Deduplicate first, preserve provider order | |

**User's choice:** `dedup_then_newest_50`
**Notes:** The cap should be applied after deduplication, favoring recency.

| Option | Description | Selected |
|--------|-------------|----------|
| One MONITORING signal per skipped headline | Preserve per-headline auditability with a volume-cap note | ✓ |
| Single aggregate MONITORING signal | One summary row for all skipped items | |

**User's choice:** `one_monitoring_signal_per_skipped_headline`
**Notes:** Overflow should remain auditable at headline granularity.

---

## the agent's Discretion

- Exact digest subject/body formatting
- Internal helper layout between job modules and supporting utilities

## Deferred Ideas

None.
