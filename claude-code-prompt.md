# Claude Code Bootstrap Prompt

Paste this (or a trimmed version) as the first message to Claude Code when starting the project.

---

## Context

I'm building a personal investment signal system. The full design is in this directory:

- `README.md` — project overview and goals
- `architecture.md` — the system design
- `mvp-week1.md` — what we're building first (tracer bullet, plumbing only)
- `roadmap.md` — weeks 2+
- `signal-log-schema.md` — data model and measurement approach
- `thesis.example.yaml` — config template for the news classifier
- `risks-and-open-items.md` — things to validate and watch out for

**Read all of these before writing code.** They reflect ~90 minutes of design grilling; don't re-litigate decisions that are already settled.

## My Background

- Experienced Java developer transitioning to Python for this project (deliberate choice — finance ecosystem is Python-first)
- Comfortable with type systems; I'll appreciate type hints and `mypy` discipline
- I run trades manually on Schwab; this system is alert-only, never executes
- I want brief, direct responses; ask clarifying questions before assuming
- I'll be running this on a Windows machine I own

## What I Want From You (Claude Code)

### Step 1: Validate before building

Per `risks-and-open-items.md`, the most important pre-work is validating Finnhub free tier coverage. **Before scaffolding any application code, write a small validation script** (a single Python file or just `curl` commands) that confirms which Finnhub endpoints and symbols work on the free tier. Specifically:

- What symbol returns S&P 500 close on free tier?
- Same for VIX, oil
- Is `/scan/technical-indicator` on free tier?
- Is `/stock/insider-sentiment` on free tier?
- Is `/stock/insider-transactions` on free tier?

Surface the results to me. If something critical is paid-only, we discuss before proceeding.

### Step 2: Propose project structure

Once Finnhub is validated, propose a Python project structure that supports the Week 1 MVP and leaves room for weeks 2+ without rework. Suggested layout is in `mvp-week1.md` but feel free to improve.

Ask me before choosing:
- Dependency management: `uv` (recommended) vs `pip + venv` vs `poetry`
- Type checking strictness: `mypy --strict`, `pyright`, or none
- Lint/format: `ruff` is the default unless I push back
- Project layout if you'd diverge from the suggestion

### Step 3: Build the tracer bullet

Implement the Week 1 MVP per the acceptance criteria in `mvp-week1.md`. **All five criteria must pass.** Do not skip the failure-mode test (criterion 5).

Commit incrementally; one commit per meaningful unit.

### Step 4: Document any deviations

If you find the design needs to deviate from the docs (e.g., Finnhub API quirk forces a different approach), update the relevant doc file in the same commit. The docs should stay in sync with the code.

## Hard Rules

- **No automated trade execution.** Anywhere. Ever. This is not negotiable.
- **No committing secrets.** `.env` must be in `.gitignore` from the first commit. Commit `.env.example` as a template.
- **No silent error handling.** Every `except` block either re-raises or pings `/fail` on Healthchecks. Bare `except: pass` is forbidden.
- **No `None` in the database for required fields.** Use schema `NOT NULL` constraints to enforce this.
- **Type hints on all function signatures.** This is a long-lived system; future-you will thank present-you.

## What to Ask Before Starting

Before writing any code, confirm with me:

1. Finnhub API key — do I have one yet? (Free tier signup needed if not.)
2. Healthchecks.io account — do I have a check UUID provisioned?
3. Gmail app password — do I have one set up?
4. Anthropic API key — do I have one? (Needed week 2, not week 1, but good to know.)
5. Where on disk should the project live, and where should `state/signals.db` go?

Don't write code until you have answers to 1, 2, 3, and 5. Confirming step 4 can wait until week 2.
