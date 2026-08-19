#!/usr/bin/env python3
"""
Daily G4 macro brief builder.

Two independent layers:
  1. FACTS  — spot rates and indicators computed from ECB daily reference rates.
              Deterministic, no model involved, never wrong in an interesting way.
  2. STORY  — one Claude call with web search enabled, asked to explain the moves
              from sources it actually reads, and to reconcile its explanation
              against the price move computed in layer 1.

Writes data/brief.json. Designed to fail soft: if the model layer is
unavailable, the facts layer is still written and the dashboard still works.
"""

import json
import os
import sys
import math
import datetime as dt
import urllib.request

API = "https://api.frankfurter.dev/v1"
SYMBOLS = "EUR,JPY,GBP,CAD,SEK,CHF"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "brief.json")
MODEL = "claude-opus-5"
MAX_CONTINUATIONS = 5

PAIRS = [
    ("dxy", "USD", "DXY (reconstructed)", 2),
    ("eur", "EUR", "EUR/USD", 4),
    ("gbp", "GBP", "GBP/USD", 4),
    ("jpy", "JPY", "USD/JPY", 2),
]


# ----------------------------------------------------------------- facts layer
def fetch_rates():
    end = dt.date.today()
    start = end - dt.timedelta(days=560)
    url = f"{API}/{start}..{end}?base=USD&symbols={SYMBOLS}"
    # Frankfurter 403s the default Python-urllib User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "g4-macro-brief/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def dxy_from(r):
    return (50.14348112
            * (1 / r["EUR"]) ** -0.576
            * r["JPY"] ** 0.136
            * (1 / r["GBP"]) ** -0.119
            * r["CAD"] ** 0.091
            * r["SEK"] ** 0.042
            * r["CHF"] ** 0.036)


def sma(s, n):
    return sum(s[-n:]) / n if len(s) >= n else None


def rsi(s, n=14):
    """Wilder's smoothing — the standard 14-day RSI."""
    if len(s) < n + 1:
        return None
    ch = [s[i] - s[i - 1] for i in range(1, len(s))]
    gain = sum(max(c, 0) for c in ch[:n]) / n
    loss = sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        gain = (gain * (n - 1) + max(c, 0)) / n
        loss = (loss * (n - 1) + max(-c, 0)) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def pct(s, back):
    return (s[-1] / s[-1 - back] - 1) * 100 if len(s) > back else None


def realised_vol(s, n=20):
    if len(s) < n + 1:
        return None
    r = [math.log(s[i] / s[i - 1]) for i in range(len(s) - n, len(s))]
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var * 252) * 100


def build_facts(payload):
    rates = payload["rates"]
    dates = sorted(rates)
    series = {"dxy": [], "eur": [], "gbp": [], "jpy": []}
    for d in dates:
        r = rates[d]
        if not all(k in r for k in ("EUR", "JPY", "GBP", "CAD", "SEK", "CHF")):
            continue
        series["dxy"].append(dxy_from(r))
        series["eur"].append(1 / r["EUR"])
        series["gbp"].append(1 / r["GBP"])
        series["jpy"].append(r["JPY"])

    facts = {}
    for pid, code, label, dp in PAIRS:
        s = series[pid]
        s200, last = sma(s, 200), s[-1]
        dist = (last / s200 - 1) * 100 if s200 else None
        loc = "unknown" if dist is None else (
            "testing" if abs(dist) < 0.5 else ("above" if dist > 0 else "below"))
        facts[pid] = {
            "code": code, "label": label, "dp": dp,
            "last": round(last, dp),
            "chg_1d_pct": round(pct(s, 1), 3) if pct(s, 1) is not None else None,
            "chg_5d_pct": round(pct(s, 5), 3) if pct(s, 5) is not None else None,
            "chg_20d_pct": round(pct(s, 20), 3) if pct(s, 20) is not None else None,
            "rsi_14": round(rsi(s), 1) if rsi(s) is not None else None,
            "sma_200": round(s200, dp) if s200 else None,
            "sma_50": round(sma(s, 50), dp) if sma(s, 50) else None,
            "dist_from_200sma_pct": round(dist, 2) if dist is not None else None,
            "sma_location": loc,
            "realised_vol_20d_pct": round(realised_vol(s), 1) if realised_vol(s) else None,
            "range_20d": [round(min(s[-20:]), dp), round(max(s[-20:]), dp)],
        }
    return dates[-1], facts


# ----------------------------------------------------------------- story layer
SCHEMA = {
    "type": "object",
    "properties": {
        "market_summary": {"type": "string"},
        "regime_note": {"type": "string"},
        "currencies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": ["USD", "EUR", "GBP", "JPY"]},
                    "what_happened": {"type": "string"},
                    "why": {"type": "string"},
                    "drivers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "factor": {"type": "string"},
                                "direction": {"type": "string",
                                              "enum": ["supportive", "negative", "mixed"]},
                                "weight": {"type": "string",
                                           "enum": ["high", "medium", "low"]},
                            },
                            "required": ["factor", "direction", "weight"],
                            "additionalProperties": False,
                        },
                    },
                    "scenarios": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "condition": {"type": "string"},
                                "implication": {"type": "string"},
                                "likelihood": {"type": "string",
                                               "enum": ["more likely", "possible", "less likely"]},
                            },
                            "required": ["condition", "implication", "likelihood"],
                            "additionalProperties": False,
                        },
                    },
                    "tape_check": {"type": "string",
                                   "enum": ["consistent", "contradicts", "insufficient evidence"]},
                    "tape_check_note": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "confidence_reason": {"type": "string"},
                },
                "required": ["code", "what_happened", "why", "drivers", "scenarios",
                             "tape_check", "tape_check_note", "confidence", "confidence_reason"],
                "additionalProperties": False,
            },
        },
        "data_expectations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "release": {"type": "string"},
                    "when": {"type": "string"},
                    "evidence_so_far": {"type": "string"},
                    "what_would_surprise": {"type": "string"},
                },
                "required": ["release", "when", "evidence_so_far", "what_would_surprise"],
                "additionalProperties": False,
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "publisher": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "publisher", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_summary", "regime_note", "currencies",
                 "data_expectations", "sources"],
    "additionalProperties": False,
}

SYSTEM = """You are a senior FX strategist writing the morning note for a systematic macro desk.

HOW YOU WRITE
- You explain what moved and why, from sources you have actually read today. Every causal
  claim must trace to a source you cite.
- You never state that a currency will rise or fall. You write conditionally: what would have
  to be true, and what follows if it is. "If X, then Y becomes more likely" — never "Y will happen".
- Forbidden: "will rise", "will fall", price targets, buy/sell calls, guarantees, "certainly",
  "definitely". A reader must never be able to quote you as having predicted a level.
- When the sources do not explain a move, say so plainly and set tape_check to
  "insufficient evidence". An honest gap is worth more than a plausible story.

THE TAPE CHECK — this is the part that matters most
You are given the actual price move, computed from published closes before you searched.
After you form your explanation, compare it against that move:
  - "consistent"           — the narrative you found matches the direction of the move.
  - "contradicts"          — the narrative points the opposite way to what price did.
                             Say so loudly. A story that contradicts the tape is usually
                             either stale, already priced, or wrong.
  - "insufficient evidence"— you could not find sources that explain this move.
Never bend the explanation to fit the move. Report the mismatch instead.

ON UPCOMING DATA
For data_expectations, reason like an analyst about releases due in the next ~10 days: what
the evidence so far leans toward, and specifically what result would be a surprise. Do not
give a point forecast. Frame it as "evidence leans toward X; a print above Y would be the
surprise that changes the picture".

Be concise and concrete. Levels, dates, named sources. No filler, no hedged mush — conditional
is not the same as vague."""


def call_claude(as_of, facts):
    try:
        import anthropic
    except ImportError:
        return None, "anthropic SDK not installed (pip install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no ANTHROPIC_API_KEY in environment"

    client = anthropic.Anthropic()
    today = dt.date.today().isoformat()

    table = "\n".join(
        f"  {f['label']:<22} {f['last']:<10} "
        f"1d {f['chg_1d_pct']:+.2f}%  5d {f['chg_5d_pct']:+.2f}%  20d {f['chg_20d_pct']:+.2f}%  "
        f"RSI14 {f['rsi_14']}  200SMA {f['sma_200']} ({f['dist_from_200sma_pct']:+.2f}%, {f['sma_location']})"
        for f in facts.values()
    )

    prompt = f"""Today is {today}. Latest published close: {as_of}.

These are the ACTUAL moves, computed from ECB daily reference rates before any searching.
Treat them as ground truth — your job is to explain them, not to restate them:

{table}

Research today's G4 FX session using web search. Cover:
  - What actually drove these moves in the last 24-48 hours (central bank communication,
    data releases, energy, geopolitics, positioning).
  - Which way each driver cuts for each currency.
  - The conditions that would change the picture from here.
  - Any high-impact data due in the next ~10 days, and what current evidence leans toward.

Search for recent, dated material. Prefer primary sources (central bank statements, official
data releases) and established financial press. Ignore anything undated or older than a week
unless it is explicitly background.

Return one entry per currency: USD, EUR, GBP, JPY. Populate every field of the schema, and
run the tape check honestly against the numbers above."""

    tools = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ]

    messages = [{"role": "user", "content": prompt}]
    resp = None
    for attempt in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=messages,
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if resp.stop_reason != "pause_turn":
            break
        # Server-side tool loop hit its iteration cap. Re-send with the paused
        # assistant turn appended; the API resumes on its own — do not add text.
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": resp.content},
        ]
    else:
        return None, f"still paused after {MAX_CONTINUATIONS} continuations"

    if resp.stop_reason == "refusal":
        return None, f"model declined: {getattr(resp, 'stop_details', None)}"

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return None, "no text block in response"

    usage = resp.usage
    story = json.loads(text)
    story["_usage"] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "model": MODEL,
    }
    return story, None


# ----------------------------------------------------------------------- main
def main():
    facts_only = "--facts-only" in sys.argv

    print("fetching rates…", flush=True)
    as_of, facts = build_facts(fetch_rates())
    print(f"  as of {as_of}", flush=True)
    for f in facts.values():
        print(f"  {f['label']:<22} {f['last']:<10} 1d {f['chg_1d_pct']:+.2f}%  "
              f"RSI {f['rsi_14']}  {f['sma_location']}", flush=True)

    story, err = (None, "skipped (--facts-only)") if facts_only else call_claude(as_of, facts)
    if err:
        print(f"story layer unavailable: {err}", file=sys.stderr, flush=True)
    else:
        print(f"story layer ok — {story['_usage']['input_tokens']} in / "
              f"{story['_usage']['output_tokens']} out tokens", flush=True)

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "data_as_of": as_of,
        "facts": facts,
        "story": story,
        "story_error": err,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"wrote {os.path.relpath(OUT)}", flush=True)
    # Never fail the workflow just because the model layer was unavailable —
    # the facts layer alone still makes the dashboard useful.
    return 0


if __name__ == "__main__":
    sys.exit(main())
