# Plan 07 — News Subject-Ticker Attribution

**Type:** Fix · **Priority:** P0 · **Est. effort:** ~2 days · **Blocked by:** Plan 03 (holdings loader)

## Problem

A News Classifier signal's `ticker` is the ticker the system **queried Finnhub with**, not
the company the story is about. The classifier is never asked what the story is about.

### The mechanism

`jobs/news_morning.py`:

```python
headlines_by_ticker: dict[str | None, list[dict]] = defaultdict(list)
for ticker, item, _ in kept_items:
    headlines_by_ticker[ticker].append(item)      # ticker == the Finnhub query symbol
```

`classifier/news_classifier.py` passes it straight through to the `Signal` (lines 437, 469).
`ClassificationResult` has four fields — `pillar_name`, `confidence`, `direction`,
`rationale` — and no ticker. The model reads the headline, decides a pillar, and the
query symbol is stapled on afterwards.

Finnhub's company-news endpoint returns loosely-related coverage, so the query symbol is
frequently not the subject. Compounding it, `_dedupe_headlines()` sorts globally by recency
and keeps the **first** ticker an article appears under, so an article surfacing under both
NVDA and MCHP is assigned whichever the loop reached first.

### Evidence — 2026-08-10, delivered

| Delivered as | Actual subject | Held? |
|---|---|---|
| `NVDA — INFORMATIONAL — HELD`<br>*"Microchip's Data Center Revenue Grew 98% Last Quarter…"* | Microchip Technology (MCHP) | **No** |

### Evidence — same run, suppressed

| Tagged | Headline | Actual subject |
|---|---|---|
| NVDA | *"Why Micron Stock Plummeted 28.7% in July…"* | MU — **held, and not in the scan set** |
| NVDA | *"Anthropic Locks In $10B European Compute Bet…"* | Anthropic (private) |
| NVDA | *"Firmus raises $2bn to accelerate Project Southgate rollout"* | Firmus (private) |
| NVDA | *"Iran Tensions Highlight Defense Supply Gaps…"* | macro / defense sector |
| GOOGL | *"Why Is TSM Stock Rising Premarket?"* | TSM |
| GOOGL | *"JPMorgan Strategists Raise S&P 500 Target…"* | macro |
| AVGO | *"Cold Jobs Report Warms Up Wall Street, Airbnb Steals the Show"* | macro / ABNB |

### Why it matters more than it looks

An alert-only system's entire product is the operator's attention. A wrong ticker on a
confidently-worded alert does three things:

1. Sends attention to the wrong position.
2. Corrupts measurement — `outcome_price_30d` gets backfilled against NVDA for a story about
   Microchip, so the News Classifier's measured hit rate is partly noise.
3. Corrupts the MONITORING corpus that `weekly-synthesis` mines for new pillar candidates,
   so the error propagates into thesis evolution.

## Related defect — `HELD` is not a check

```python
# jobs/news_morning.py
# news-morning only ever scans core holdings, so `tickers` IS the held set.
held_tickers = set(tickers)
```

The comment is false. `tickers` comes from `get_core_holdings()`, which reads
`core_holding=TRUE` from `universe.csv` — the **universe**, not `holdings.csv`. Two
consequences:

- `NEM`, `RTX` and `SMH` are core-scanned but absent from `holdings.csv`; they would print
  `HELD` falsely.
- Every company-news signal is `HELD` by construction, so the tag conveys no information
  even when it is accidentally right.

## Goals

1. `Signal.ticker` names the company the story is about, or is `NULL` for macro.
2. `HELD` is computed against `holdings.csv`.
3. Classification quality does not regress on the golden set.

## Non-goals

- Fixing *which* tickers get scanned. That is Plan 03.
- Fixing the volume cap. That is Plan 09.
- Re-attributing historical signals. Do not backfill (see §6).

---

## Design

### 7a. Extend `ClassificationResult`

```python
class ClassificationResult(BaseModel):
    """Structured output from the Anthropic classifier call."""
    pillar_name: str | None = Field(description="Matched thesis pillar name, or null if off-thesis")
    subject_ticker: str | None = Field(
        default=None,
        description=(
            "Ticker of the company the headline is primarily about, uppercase, "
            "or null if the headline is macro/sector-level or about a private company. "
            "Do NOT return the ticker whose news feed surfaced this headline unless "
            "that company is genuinely the subject."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Classification confidence 0-1")
    direction: Literal["positive", "negative", "neutral"] = Field(description="Signal direction relative to the pillar")
    rationale: str = Field(description="One-sentence rationale for the classification")
```

`messages.parse()` generates the JSON schema from the model, so this is the whole change on
the SDK side. The `default=None` keeps older cached responses parseable.

The prompt must state the distinction explicitly — the failure mode to guard against is the
model echoing the query ticker because it appears in context. Consider omitting the query
ticker from the prompt entirely; the classifier does not need it to judge pillar or
direction.

### 7b. Resolution precedence

Compute the final ticker in `news_classifier.py`, not in the job:

1. `subject_ticker` if present **and** it resolves against `universe.csv` or
   `holdings.csv` → use it.
2. `subject_ticker` present but unrecognised (private company, foreign listing, typo) →
   `ticker = None`, and record the raw model string in `body` for later inspection. Do not
   invent a symbol.
3. `subject_ticker` null → `ticker = None` (macro).
4. Never fall back to the query ticker. Falling back silently reintroduces the bug and is
   indistinguishable from it in the data.

Keep the query ticker — it is genuinely useful provenance. Add a `source_ticker` column to
`signals` so "which feed surfaced this" remains answerable and the two are never conflated
again.

### 7c. Real `HELD` computation

Replace `held_tickers = set(tickers)` with a holdings-derived set:

```python
from signal_system.data.holdings import load_holdings

held_tickers = {h.ticker for h in load_holdings()}
```

Depends on Plan 03's fix to the `load_holdings(path: Path = HOLDINGS_PATH)` default-argument
binding — do 03's §3a first, or do it here and let 03 consume it. One of the two, not both.

Digest rendering rules:

- `HELD` only when the resolved subject ticker is in the holdings set.
- Nothing at all when it is not. Do not print `NOT HELD` — absence is quieter and the
  digest's budget is attention.
- Macro signals (`ticker IS NULL`) print `MACRO`, as today.

### 7d. Alert ID stability

`compute_alert_id(ticker, date, rule, agent)` takes the ticker. Changing which ticker is
passed changes the hash, so a story could deliver twice across the cutover — once under the
old scheme, once under the new.

Mitigation: keep `compute_alert_id` keyed on the **query** ticker and the headline hash, as
today. Attribution changes the `Signal.ticker` field, not identity. This also keeps the
existing dedup and `INSERT OR IGNORE` semantics intact — worth stating in the code comment,
because it is the kind of thing that looks like an oversight later.

---

## 5. Evaluation — mandatory gate

`CLAUDE.md`: *"`eval-classifier` — golden-set readout before any model change."* This plan
changes the output schema and the prompt, so the gate applies.

1. Run `python -m signal_system eval-classifier` on `main`; record the baseline.
2. Extend the golden set with **attribution cases** — it currently tests pillar and
   direction, not subject. Add at minimum:
   - A headline surfaced under NVDA that is about MCHP → expect `MCHP`.
   - A macro headline surfaced under a company feed → expect `None`.
   - A private-company headline (Anthropic, Firmus) → expect `None`, not a guess.
   - A headline genuinely about the query ticker → expect the query ticker.
   - A headline naming two companies → expect the primary subject, documented.
3. Re-run post-change. **Do not merge on a worse pillar/direction score.** Attribution
   accuracy is a new metric with no baseline; record it as the new floor.

Attach both readouts to the phase summary.

## 6. Migration

**Do not backfill.** 5,206 existing `news_classifier` rows carry query-ticker attribution.
Re-attributing them means re-running the LLM over the corpus, and re-writing historical
signals would destroy the audit trail the signal log exists to provide.

Instead, stamp the change:

- Add `attribution_version` to `signals` (e.g. `subject-v1`). NULL = query-ticker legacy.
- `review-report` and `replay-weights` must segment on it, exactly as they segment on
  `weight_version`. Mixing a query-ticker cohort with a subject-ticker cohort produces a
  meaningless hit rate.
- Note the cutover date in `signal-log-schema.md`.

## 7. Testing

- **Unit:** resolution precedence — each of the four branches in §7b, including the
  no-fallback rule.
- **Unit:** unrecognised `subject_ticker` yields `ticker=None` and preserves the raw string.
- **Unit:** `HELD` is true for a holdings ticker, absent for a core-but-unheld ticker
  (`NEM`, `RTX`, `SMH` are the live cases).
- **Unit:** `compute_alert_id` output is unchanged for a fixed input across the change —
  guards the dedup regression in §7d.
- **Golden set:** §5.
- **Integration:** re-classify the 2026-08-10 delivered set and assert the Microchip story
  no longer carries `NVDA — HELD`. This is the acceptance artifact.

## 8. Acceptance criteria

- [ ] No delivered alert names a ticker the headline is not about, verified over one week
      of live runs by manual spot-check of all delivered alerts (≤20/week — tractable).
- [ ] `HELD` appears only for tickers present in `holdings.csv`.
- [ ] Macro and private-company stories resolve to `MACRO`, never to a guessed symbol.
- [ ] `eval-classifier` pillar/direction score is no worse than baseline; attribution
      accuracy recorded as the new floor.
- [ ] `source_ticker` and `attribution_version` populated on every new signal.
- [ ] `signal-log-schema.md` documents the cutover.
