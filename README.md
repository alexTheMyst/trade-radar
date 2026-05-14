# Portfolio Signal System

A rules-based, alert-only investment signal system. Two agents emit ranked opportunities and thesis-relevant news deltas; all trade execution is manual on Schwab.

---

## Goals

1. **Offload market monitoring and analysis** — surface what matters, suppress noise.
2. **Be measurable** — every signal logged with timestamp, action, and outcome. Quarterly review against benchmarks.

**Not a goal:** beat SPY by some margin in year one. The realistic year-one win is *structural* — fewer missed catalysts, a maintained thesis doc, and a feedback loop on discretionary judgment.

## Non-Goals

- ❌ Automated trade execution. Everything is alert-only.
- ❌ Self-learning / adaptive scoring. The system stays static; the operator adapts based on quarterly review.
- ❌ Replace the human. The system is an outside check, not a decision-maker.

---

## System at a Glance

```
       Finnhub API          Claude API
            │                   │
            ▼                   ▼
   ┌──────────────────┐  ┌──────────────────┐
   │ Discovery Agent  │  │ News Classifier  │
   │   (daily 4:15PM) │  │   (3× daily)     │
   └────────┬─────────┘  └────────┬─────────┘
            │                     │
            └──────────┬──────────┘
                       ▼
              ┌─────────────────┐
              │   Alert Router  │
              │ (budget + slot  │
              │   competition)  │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  SQLite log +   │
              │  Email digest   │
              └─────────────────┘

Schwab price alerts handle mechanical triggers separately — no agent needed.
```

## What This Replaces

| Task | Was | Now |
|---|---|---|
| Watching S&P close vs trigger levels | Manual / mental | Schwab native price alerts |
| Finding new tickers worth a look | Ad-hoc scrolling | Discovery Agent (wide scan, capped sizing) |
| Tracking thesis-relevant news | Reading WSJ / Seeking Alpha | News Classifier (config-driven) |
| Earnings setup, portfolio drift | (cut from scope) | Schwab's earnings tab + monthly eyeball |

---

## Where to Start

1. Read `architecture.md` — the full system design
2. Read `mvp-week1.md` — the tracer-bullet acceptance criteria
3. Read `risks-and-open-items.md` — things to validate before writing real code
4. Skim `roadmap.md` — what comes after MVP

## Tech Stack

- **Language:** Python 3.12+
- **Package manager:** [uv](https://github.com/astral-sh/uv) (fast, modern; falls back to `pip + venv` if preferred)
- **Runner:** Windows machine + Windows Task Scheduler
- **State:** SQLite via stdlib `sqlite3` (single file, version-controlled via backup script)
- **Heartbeat:** [healthchecks.io](https://healthchecks.io) free tier
- **Data:** Finnhub free tier (60 calls/min) via `finnhub-python` SDK
- **LLM:** Anthropic Claude API via official `anthropic` Python SDK (`claude-sonnet-4-5-20250929` or current Sonnet)
- **Alert delivery:** Gmail SMTP via stdlib `smtplib` (MVP); revisit for push later

**Why Python over Java:** the finance/quant ecosystem is Python-first. Stack Overflow answers, GitHub examples, pandas for the inevitable backtest harness, and reference-quality LLM SDKs all point the same direction. Operator is a Java native — there's a small learning tax on day one but a large compounding payoff after.

## Operator Responsibilities

Uptime is yours. Specifically:
- Keep the Windows machine awake during scheduled run windows
- Maintain `thesis.yaml` quarterly (see `roadmap.md`)
- Fill in the `acted` field in the signal log after each alert
- Quarterly review of hit rates (see `signal-log-schema.md`)
