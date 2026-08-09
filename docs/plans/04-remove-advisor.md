# Plan 04 — Remove the Advisor Agent

**Type:** Remove · **Priority:** P2 · **Est. effort:** ~0.5 day

## Rationale

The Advisor agent is carrying cost with no signal:

- **Dormant.** Last `advisor` run in the `runs` table: **2026-06-02**. Total runs ever:
  **3**. It is not on any active schedule (`ops/tasks/` contains only `daily-close`,
  `discovery`, `news-morning`).
- **Shadow-only.** `ADVISOR_SHADOW_MODE` defaults `true`; it was never validated at an IC
  review and flipped live. It has never influenced a real decision.
- **Redundant.** Its verdict/rationale layer duplicates judgment already provided by the
  News Classifier (thesis-relevant deltas) and by Schwab's own position tooling. The system
  is explicitly **alert-only**; a prescriptive "advisor" sits awkwardly against that
  charter.
- **Heavy.** It pulls in `advisor_agent.py`, `rationale.py`, `verdict_engine.py`, a
  `jobs/advisor.py` entry, the `advice` table (21 rows, all shadow), advice-outcome backfill
  paths, and it is the **sole consumer** of the `wash_sale` ledger (see Plan 05).

Removing it shrinks surface area, deletes a config flag, and unblocks retiring the unused
wash-sale ledger.

> Decision gate: if you have a concrete near-term intent to take the Advisor **live**
> (flip shadow off after an IC review), do **not** remove it — instead schedule it and put
> it under the Plan 01 watchdog. This plan assumes the answer is "no, cut it." Confirm
> before executing.

## Dependency map (what references the advisor)

From a repo scan (`grep -rl`), the Advisor touches:

```
src/signal_system/advisor/advisor_agent.py     ← delete
src/signal_system/advisor/rationale.py         ← delete
src/signal_system/advisor/verdict_engine.py    ← delete (exports WASH_SALE_WINDOW_DAYS)
src/signal_system/advisor/__init__.py          ← delete
src/signal_system/jobs/advisor.py              ← delete
src/signal_system/__main__.py                  ← imports run_advisor, advise_ticker,
                                                 WASH_SALE_WINDOW_DAYS; JOBS["advisor"];
                                                 `advise` + `log-trade` command wiring
src/signal_system/state/repository.py          ← advice + wash_sale fns (see Plan 05)
src/signal_system/data/holdings.py             ← holdings are shared infra; KEEP (used by
                                                 news-morning + reconciliation). Only remove
                                                 any advisor-specific import if present.
tests/test_advisor_agent.py                    ← delete
tests/test_advisor_job.py                      ← delete
tests/test_verdict_engine.py                   ← delete
tests/test_rationale.py                        ← delete
tests/test_repository_advisor.py               ← delete
```

`holdings.py` is **not** advisor-owned — it is core data used by `news-morning` and Plan 03.
Keep it. Only the `advice`/`wash_sale` repository functions and the `advisor/` package go.

## Execution steps

1. **Confirm the decision gate** (above). If cutting:
2. Delete the `src/signal_system/advisor/` package and `src/signal_system/jobs/advisor.py`.
3. In `__main__.py`:
   - Remove imports: `run_advisor`, `advise_ticker`, `WASH_SALE_WINDOW_DAYS`.
   - Remove `"advisor": run_advisor` from `JOBS`.
   - Remove the `advise <TICKER>` command branch and its usage-banner text.
   - Remove `log-trade` wiring **only as part of Plan 05** (it feeds wash-sale). If doing 04
     alone first, leave `log-trade` but note it becomes a no-op input; prefer doing 04+05
     together.
4. In `config.py`: remove `ADVISOR_SHADOW_MODE`.
5. In `repository.py`: remove advice-specific functions (`insert_advice`,
   `list_advice_backfill_candidates`, `update_advice_outcomes`, `AdviceBackfillCandidate`).
   Handle wash-sale functions in Plan 05.
6. Delete the advisor test files listed above.
7. **Data:** the `advice` table can be left in place (harmless) or dropped. Recommended:
   leave the table, stop writing to it — a migration to drop it adds risk for no benefit,
   and the weekly backup already snapshots it. Document that `advice` is deprecated.
8. Update docs: `CLAUDE.md` (remove Advisor from architecture + "What's built"), `README.md`,
   `roadmap.md`, `architecture.md`. Note the removal in the changelog/commit.
9. `uv run pytest` — expect the deleted tests gone and the rest green. Fix any stray import.
10. `uv run python -m signal_system` (no args) — verify the usage banner no longer lists
    `advisor` / `advise` and nothing errors on import.

## What explicitly stays

- `holdings.py`, `universe.py`, `finnhub_client`, `repository` core — shared infra.
- The `advice` **table rows** (historical shadow output) — retained read-only for the
  record; no new writes.
- News Classifier, Discovery, Exit Sentinel, Alert Router — untouched.

## Testing

- Full suite passes with advisor tests removed.
- `test_cli_commands.py` — update/remove any case asserting `advise` / `advisor` exist;
  add an assertion that an unknown `advisor` command prints the "Unknown command" path.
- Import smoke: `python -c "import signal_system.__main__"` succeeds with no `advisor`
  references.

## Risks & mitigations

- **Latent import** of `WASH_SALE_WINDOW_DAYS` or advisor symbols elsewhere → the dependency
  scan above is the checklist; `grep -rn "advisor\|advise\|verdict_engine\|WASH_SALE"` after
  deletion must return only docs/plan hits.
- **Regret** (you later want advice) → it is in git history; resurrection is a revert. The
  shadow rows remain in `advice` for reference.
- **Coupling with Plan 05** → do them in one PR to avoid a dangling `log-trade` → wash-sale
  path.

## Acceptance criteria

- `grep -rn "advisor" src/` returns nothing (docs may still reference it historically).
- `uv run pytest` green; `JOBS` no longer contains `advisor`.
- Usage banner and `CLAUDE.md` architecture section no longer mention the Advisor.
