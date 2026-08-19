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
2. The workflow runs **Monday at 06:00 UTC**, covering the week ahead. The ForexFactory feed
   runs Sunday→Friday and rolls over the weekend, so Monday morning is the first moment the whole
   week is visible — and it publishes before the EU (07:00–09:00 UTC) and US (12:30 UTC) windows.

### Manual runs — the pre-NFP pattern

The Monday call is made with Monday's evidence. For a Friday release that misses ADP, claims and
ISM services. So the day before a big print, run it again from **Actions → Run workflow** with:

- **focus** — `Non-Farm`, `CPI`, `FOMC`, or a currency like `USD`. Narrows the run to matching
  events so the whole research budget goes to the one you care about. Blank = the whole week.
- **max_events** — fewer events, more depth each, lower cost.

Re-forecasting an event already called **supersedes** the earlier lean and records both, so the
page shows how the view moved as evidence arrived. A call can never be revised after the event has
printed or been scored — otherwise the track record would be a lie.

### Cost

Observed on a full 6-event week: ~211k input / ~26k output tokens ≈ **$1.71**. On the weekly
schedule that is about **$7/month**, plus roughly **$0.30–0.60** for each focused manual run.
Web search dominates the input side. Further levers: lower `MAX_EVENTS`, or
`output_config={"effort": "medium"}` in the call.

### Known limits

The ForexFactory feed carries the **current week only** — there is no next-week endpoint — so the
horizon is at most Monday to Friday. Nothing further out can be forecast.

A Monday lean on a Friday release is made without the week's own evidence. That is what the
focused manual run is for; use it, or treat late-week Monday calls as provisional.

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
