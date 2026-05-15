# Features Research — Investment Signal System

## Table Stakes

Features without which the system fails its core promise of never missing a material thesis-relevant event.

### News Classifier

| Feature | Notes | Confidence |
|---------|-------|-----------|
| Structured output via `messages.parse()` / `tool_choice` forcing | Forces Claude to return typed JSON schema — not free-text parsing | HIGH |
| Per-pillar confidence scores in classification schema | Raw LLM confidence per thesis pillar, not just binary match | HIGH |
| thesis.yaml `review_due` gate — refuse-to-run on stale taxonomy | Exception (not warning) propagates through heartbeat, trips /fail | HIGH |
| Headline sanitization before LLM calls | `<headline>` delimiters, control-char strip, 500-char cap | HIGH |
| Headline deduplication within trading day | Hash + near-duplicate detection; avoids double-alerting on same story | HIGH |

### Alert Router

| Feature | Notes | Confidence |
|---------|-------|-----------|
| Slot competition with score-based ranking and demotion path | Higher score wins ACTION_REQUIRED; loser demotes to INFORMATIONAL with reason code | HIGH |
| No-signal-day digest — always send, even zero alerts | "Scanned N tickers, 0 alerts" — silence with confirmation | HIGH |
| Per-ticker cooldown window stored in SQLite | Prevents re-alerting same ticker within N hours | MEDIUM |
| Daily budget from DB query (not in-memory) | Both news-morning and discovery can run same day; in-memory state breaks | HIGH |
| `routing_status` column (DELIVERED / MONITORING / SUPPRESSED) | Router must never mutate `severity` — only `routing_status` | HIGH |

### Discovery Agent

| Feature | Notes | Confidence |
|---------|-------|-----------|
| 1/3 universe rotation with core-holdings override | `hashlib.md5(symbol) % 3` — deterministic across days/restarts | HIGH |
| Rate-limit gating on all Finnhub calls | Token bucket or sleep-based; 60 calls/min hard limit | HIGH |
| Discovery Agent Phase A logs-only flag | Config flag disables router delivery for 2-week calibration | HIGH |
| Score-floor / missing-data guard | Skip tickers where required data points are unavailable rather than score them artificially low | HIGH |
| K-1 ETF exclusion at universe-builder level | USO, UNG, DBC, GSG — filter here, not at alert time | HIGH |

### Measurement Infrastructure

| Feature | Notes | Confidence |
|---------|-------|-----------|
| Signal idempotency key | Prevents duplicate rows on job retry | HIGH |
| Operator feedback fields (`acted`, `acted_at`, `user_note`) | Filled within 7 days; prerequisite for outcome measurement | HIGH |
| 30d/90d outcome backfill cron (idempotent) | `outcome_price_30d` / `outcome_price_90d` via Finnhub; must be idempotent | HIGH |
| Wash sale table with `account` column from day one | 4 accounts: schwab_main, schwab_secondary, roth_ira, hsa | HIGH |

---

## Differentiators

Signal quality edge features — valuable but not blocking core operation.

| Feature | Notes | When to Build |
|---------|-------|--------------|
| Per-factor sub-score retained alongside total | Explainability — operator can see what drove the score | Phase A calibration |
| Weight version stamp on each signal row | IC stays interpretable after weight changes | Discovery Phase A |
| Score normalization within universe (percentile rank) | Raw scores vary by factor availability; percentile rank compares apples-to-apples | After 2-week Phase A data |
| Deterministic tie-breaking (alphabetic secondary sort) | Reproducible ordering when scores are equal | Router implementation |
| Information Coefficient tracking per signal type | Spearman rank correlation between score and outcome; per-agent-type only, never aggregate | ~30 days post go-live |
| Hit-rate vs base-rate comparison | Is signal better than random? | ~30 days post go-live |
| Calibration tracking (confidence → outcome) | Long-horizon; flagged as preliminary | ~90 days post go-live |
| Source quality tier / whitelist | Down-weight low-quality Finnhub sources | After baseline established |
| thesis.yaml hot-reload on job start | Operator updates thesis; no restart needed | News Classifier v1 |
| Pillar delta vs absolute-level distinction | Alert when pillar trend changes direction, not just when it's high | News Classifier v2 |

---

## Anti-Features

Explicitly out of scope — do not add without explicit operator decision.

| Feature | Reason |
|---------|--------|
| Automated trade execution | System is alert-only by design; execution would change risk profile entirely |
| Self-learning / adaptive scoring | Operator adapts weights manually at quarterly review |
| Real-time intraday tick streaming | Finnhub free tier + Windows Task Scheduler; not an intraday system |
| Position sizing / portfolio optimization | Out of scope; covered by operator judgment |
| Raw-sentiment-as-signal (without pillar mapping) | Sentiment without thesis context is noise |
| Aggregate IC across signal types | Must always be per-agent-type; aggregate IC is misleading |
| Earnings Setup agent | Covered natively by Schwab |
| Portfolio Drift agent | Covered natively by Schwab |
| Regime classifier | Subsumed into news classifier pillar deltas |
| Multi-user / multi-tenant | Solo-operator system |
| UI dashboard | Email delivery is the interface |
| GitHub Actions runner | Windows Task Scheduler only |

---

## Feature Dependency Graph

```
thesis.yaml + thesis_loader.py
        │
        ▼
news_classifier.py ──────────────────┐
                                     │
finnhub_client.py (bulk) ──► universe.py ──► discovery_agent.py
                                     │
        ┌────────────────────────────┘
        ▼
alert_router.py (requires: models.py, repository.py routing_status column)
        │
        ▼
email_sender.py (existing — untouched by router)
        │
        ▼
Signal outcome backfill cron (requires: signals rows exist with acted field)
```

---

## MVP Recommendation for Current Milestone

**Build order:**

1. `models.py` — canonical `Signal` dataclass (unblocks everything else)
2. `thesis.yaml` + `thesis_loader.py` — operator taxonomy with review_due gate
3. Universe builder (`data/universe.py`) + Finnhub bulk fetch extensions
4. DB schema additions (`routing_status`, `wash_sale` table)
5. News Classifier agent (Claude API, thesis.yaml-driven)
6. Discovery Agent Phase A (logs-only, no router connection)
7. Alert Router (after models + repository additions)
8. `news_morning.py` and `discovery.py` jobs (thin orchestrators)
9. Signal outcome backfill cron

**Defer until data exists (~30 days post go-live):**
- Information Coefficient tracking
- Calibration tracking
- Score normalization / percentile rank
