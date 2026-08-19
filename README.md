# G4 Macro Terminal

A single-page FX desk dashboard for USD, EUR, GBP and JPY. Two layers, deliberately separated:

**Facts** — spot rates and indicators computed in the browser from ECB daily reference rates
(via the Frankfurter API). Wilder RSI(14), SMA(200) trend location, 20-day and 52-week ranges,
realised volatility, and a dollar index reconstructed from its six ICE components. No key, no
server, no model. Refresh the page and it recomputes from the latest published closes.

**Narrative** — a daily brief explaining *why* things moved, generated once a day by
`scripts/build_brief.py`, which calls the Claude API with web search enabled and writes
`data/brief.json`. Every causal claim is expected to trace to a cited source.

The two layers are kept apart on purpose. The model is given the price move *before* it
searches, and must reconcile its explanation against it — reporting `contradicts` or
`insufficient evidence` rather than inventing a story that fits. Facts never depend on the
model; if the narrative layer fails, the dashboard still works.

## Setup

The dashboard alone needs nothing — open `index.html`.

For the daily brief:

1. Add an Anthropic API key at **Settings → Secrets and variables → Actions** as `ANTHROPIC_API_KEY`.
2. The workflow runs weekdays at 11:30 UTC, or on demand from the **Actions** tab.

Run it locally:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/build_brief.py            # add --facts-only to skip the model layer
```

## What this is not

Not investment advice, and not a prediction engine. The brief is written to be conditional —
"if X, then Y becomes more likely" — never directional calls or price targets. ECB reference
rates are one price per business day, so this is a daily planning tool, not an execution screen.
Verify every level against your own broker feed.

## Layout

```
index.html              the dashboard (Daily Brief / Risk Matrix / Narrative tabs)
scripts/build_brief.py  facts + story builder, writes data/brief.json
data/brief.json         regenerated daily by the workflow
archive/                the earlier static prototypes
```
