# G4 Macro Terminal

A single-page FX desk dashboard for USD, EUR, GBP and JPY. Two layers, deliberately separated:

**Facts** — spot rates and indicators computed in the browser from ECB daily reference rates
(via the Frankfurter API). Wilder RSI(14), SMA(200) trend location, 20-day and 52-week ranges,
realised volatility, and a dollar index reconstructed from its six ICE components. No key, no
server, no model. Refresh the page and it recomputes from the latest published closes.

**Calendar** — upcoming High and Medium impact events with **consensus and previous**, from the
ForexFactory weekly feed. Free, no key.

**Forecast** — the point of the whole thing, and it runs *before* the print, not after.
`scripts/build_forecast.py` calls the Claude API with web search and, for each upcoming release,
weighs the evidence available right now against consensus: a lean (above / in line / below /
no clear lean), the evidence behind it with weights, **what is already priced**, where the
**asymmetry** sits, and conditional currency reactions. Writes `data/forecast.json`.

Language is enforced probabilistic — "evidence leans", "likely", "risk is skewed toward".
Never "will rise", never price targets. "No clear lean" is a legitimate and frequent answer;
manufacturing a view to look useful is the failure mode this is written against.

**Track record** — every lean is written to `data/ledger.json` when made. Once the event has
passed, a later run looks up what actually printed and scores the call hit / miss / unclear.
Without that, forecasts are just confident noise. The record shows on the dashboard.

The layers are separate on purpose: facts never depend on the model, so if the forecast layer
fails the dashboard still works.

## Setup

The dashboard alone needs nothing — open `index.html`.

For the daily brief:

1. Add an Anthropic API key at **Settings → Secrets and variables → Actions** as `ANTHROPIC_API_KEY`.
2. The workflow runs **Tuesday and Thursday at 10:00 UTC** — deliberately ahead of the
   12:30/13:30 UTC US data window, so calls publish before the prints they forecast. Run it on
   demand any time from the **Actions** tab.

### Cost

Observed: ~211k input / ~26k output tokens per run ≈ **$1.71** at Claude Opus 5 pricing, so about
**$14/month** on the Tue/Thu schedule. Web search dominates the input side. Levers if you want it
cheaper: fewer runs, lower `MAX_EVENTS`, or `output_config={"effort": "medium"}` in the call.

### Coverage gap worth knowing

The ForexFactory feed carries the **current week only**, so a Thursday run can see at most through
the weekend. Events landing on a **Monday** are not forecast by either run — Monday is usually the
lightest day for G4 data (NFP is Friday, CPI and FOMC mid-week), but it is a real gap. Add a
Monday run, or shift to Mon/Wed, if that matters to you.

Run it locally:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/build_forecast.py         # add --facts-only to skip the model layer
```

## What this is not

Not investment advice. It forecasts *data releases relative to consensus*, not currency
direction — and even those are leans with stated confidence, scored openly so you can see how
often they are wrong. ECB reference rates are one price per business day, so this is a daily
planning tool, not an execution screen. Verify every level against your own broker feed.

## Layout

```
index.html              the dashboard (Daily Brief / Risk Matrix / Narrative tabs)
scripts/build_forecast.py  facts + calendar + pre-event forecasts
data/forecast.json         regenerated daily by the workflow
data/ledger.json           append-only record of every call and how it scored
archive/                the earlier static prototypes
```
