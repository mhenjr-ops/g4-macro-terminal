#!/usr/bin/env python3
"""
Forward-looking G4 event forecaster.

The point of this script is PRE-event, not post-event. It answers:
  "CPI prints in 38 hours. Consensus is 2.9%. What does the evidence available
   right now suggest, what is already priced, and what likely follows either way?"

Three layers:
  1. FACTS    — spot levels and indicators from ECB reference rates (deterministic).
  2. CALENDAR — upcoming high/medium-impact events with consensus + previous,
                from the ForexFactory weekly feed.
  3. FORECAST — one Claude call with web search: for each upcoming event, weigh the
                evidence available NOW against consensus, state a lean with explicit
                confidence, note what is already priced, and map reactions.

It also keeps an honest scoreboard. Every lean is written to data/ledger.json when
made. Once the event has passed, a later run looks up what actually printed and marks
the call hit / miss / unclear. Without that, forecasts are just confident noise.

Writes data/forecast.json and data/ledger.json.
"""

import json
import os
import sys
import math
import datetime as dt
import urllib.request

RATES_API = "https://api.frankfurter.dev/v1"
SYMBOLS = "EUR,JPY,GBP,CAD,SEK,CHF"
CAL_FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UA = "g4-macro-terminal/1.0"

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "..", "data", "forecast.json")
LEDGER = os.path.join(HERE, "..", "data", "ledger.json")

MODEL = "claude-opus-5"
MAX_CONTINUATIONS = 5
MAX_EVENTS = int(os.environ.get("MAX_EVENTS") or 6)   # each forecast costs real output tokens
FOCUS = (os.environ.get("FOCUS") or "").strip()       # e.g. "Non-Farm" for a pre-NFP run
MAX_TOKENS = 32000        # streaming, so a long structured answer is not truncated
G4 = {"USD", "EUR", "GBP", "JPY"}

PAIRS = [
    ("dxy", "USD", "DXY (reconstructed)", 2),
    ("eur", "EUR", "EUR/USD", 4),
    ("gbp", "GBP", "GBP/USD", 4),
    ("jpy", "JPY", "USD/JPY", 2),
]


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ----------------------------------------------------------------- facts layer
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
    if len(s) < n + 1:
        return None
    ch = [s[i] - s[i - 1] for i in range(1, len(s))]
    gain = sum(max(c, 0) for c in ch[:n]) / n
    loss = sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        gain = (gain * (n - 1) + max(c, 0)) / n
        loss = (loss * (n - 1) + max(-c, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def pctchg(s, back):
    return (s[-1] / s[-1 - back] - 1) * 100 if len(s) > back else None


def realised_vol(s, n=20):
    if len(s) < n + 1:
        return None
    r = [math.log(s[i] / s[i - 1]) for i in range(len(s) - n, len(s))]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1) * 252) * 100


def build_facts():
    end = dt.date.today()
    start = end - dt.timedelta(days=560)
    payload = get_json(f"{RATES_API}/{start}..{end}?base=USD&symbols={SYMBOLS}")
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
        vol = realised_vol(s)
        facts[pid] = {
            "code": code, "label": label, "dp": dp,
            "last": round(last, dp),
            "chg_1d_pct": round(pctchg(s, 1), 3),
            "chg_5d_pct": round(pctchg(s, 5), 3),
            "chg_20d_pct": round(pctchg(s, 20), 3),
            "rsi_14": round(rsi(s), 1),
            "sma_200": round(s200, dp) if s200 else None,
            "dist_from_200sma_pct": round(dist, 2) if dist is not None else None,
            "sma_location": loc,
            "realised_vol_20d_pct": round(vol, 1) if vol else None,
            "daily_range_1sd_pct": round(vol / math.sqrt(252), 2) if vol else None,
            "range_20d": [round(min(s[-20:]), dp), round(max(s[-20:]), dp)],
        }
    return dates[-1], facts


# -------------------------------------------------------------- calendar layer
def event_id(e):
    return f"{e['country']}|{e['title']}|{e['date'][:16]}"


def build_calendar(hours_ahead=192):
    """Upcoming events only. Past events are not this tool's business."""
    try:
        feed = get_json(CAL_FEED, timeout=45)
    except Exception as err:
        return [], f"calendar feed unavailable: {err}"

    now = dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(hours=hours_ahead)
    out = []
    for e in feed:
        try:
            when = dt.datetime.fromisoformat(e["date"])
        except (ValueError, KeyError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if not (now <= when <= horizon):
            continue
        if e.get("impact") not in ("High", "Medium"):
            continue
        out.append({
            "id": event_id(e),
            "title": e["title"],
            "currency": e["country"],
            "when_utc": when.astimezone(dt.timezone.utc).isoformat(timespec="minutes"),
            "hours_until": round((when - now).total_seconds() / 3600, 1),
            "impact": e["impact"],
            "consensus": e.get("forecast") or None,
            "previous": e.get("previous") or None,
            "is_g4": e["country"] in G4,
        })
    out.sort(key=lambda x: x["when_utc"])
    return out, None


# ------------------------------------------------------------------ the ledger
def load_ledger():
    try:
        with open(LEDGER) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": []}


def pending_resolution(ledger):
    """Calls whose event has passed but which have never been scored."""
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for e in ledger["entries"]:
        if e.get("outcome"):
            continue
        try:
            when = dt.datetime.fromisoformat(e["when_utc"])
        except ValueError:
            continue
        # Give the print two hours to be reported before trying to score it.
        if now > when + dt.timedelta(hours=2):
            out.append(e)
    return out[:8]


def select_events(calendar):
    """Which events this run forecasts.

    High impact before Medium, capped at MAX_EVENTS — eleven full write-ups is what
    overran the output budget on the first live run. When FOCUS is set (a manual
    pre-NFP style run) the whole budget goes to matching events instead.
    """
    g4 = [e for e in calendar if e["is_g4"]]
    if FOCUS:
        needle = FOCUS.lower()
        matched = [e for e in g4
                   if needle in e["title"].lower() or needle == e["currency"].lower()]
        if matched:
            g4 = matched
        else:
            print(f"  focus '{FOCUS}' matched nothing — forecasting the full week instead",
                  file=sys.stderr, flush=True)
    return ([e for e in g4 if e["impact"] == "High"]
            + [e for e in g4 if e["impact"] != "High"])[:MAX_EVENTS]


# ---------------------------------------------------------------- claude layer
FORECAST_SCHEMA = {
    "type": "object",
    "properties": {
        "session_note": {"type": "string"},
        "forecasts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "event": {"type": "string"},
                    "currency": {"type": "string"},
                    "consensus": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "fact": {"type": "string"},
                                "points": {"type": "string",
                                           "enum": ["above consensus", "below consensus", "neutral"]},
                                "weight": {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                            "required": ["fact", "points", "weight"],
                            "additionalProperties": False,
                        },
                    },
                    "lean": {"type": "string",
                             "enum": ["above consensus", "in line", "below consensus", "no clear lean"]},
                    "lean_rationale": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "confidence_reason": {"type": "string"},
                    "whats_priced": {"type": "string"},
                    "asymmetry": {"type": "string"},
                    "reactions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "if_outcome": {"type": "string"},
                                "then_currency": {"type": "string"},
                                "likelihood": {"type": "string",
                                               "enum": ["more likely", "possible", "less likely"]},
                            },
                            "required": ["if_outcome", "then_currency", "likelihood"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["event_id", "event", "currency", "consensus", "evidence", "lean",
                             "lean_rationale", "confidence", "confidence_reason", "whats_priced",
                             "asymmetry", "reactions"],
                "additionalProperties": False,
            },
        },
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "actual": {"type": "string"},
                    "outcome": {"type": "string",
                                "enum": ["hit", "miss", "unclear", "not found"]},
                    "note": {"type": "string"},
                },
                "required": ["event_id", "actual", "outcome", "note"],
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
    "required": ["session_note", "forecasts", "resolutions", "sources"],
    "additionalProperties": False,
}

SYSTEM = """You are a senior macro strategist writing the PRE-EVENT note for an FX desk.

WHAT THIS NOTE IS
You are writing BEFORE the data prints, not after. Nobody needs another explanation of
yesterday's move. Your job is: given the evidence available right now, what is this release
likely to show relative to consensus, what is already priced, and what plausibly follows.
Never explain a past move except where it establishes what is already priced in.

HOW YOU REASON ABOUT A RELEASE
1. Start from consensus — you are given it. Your lean is always RELATIVE to consensus, never
   an absolute forecast. "Evidence leans above consensus" is a claim; "CPI will be 3.1%" is not
   your job.
2. Build the lean from leading and correlated evidence you can actually find: earlier
   sub-components, related prints from the same economy, survey and PMI detail, the same
   release in comparable economies, official commentary, base effects, and the recent record
   of this series versus consensus.
3. Weight the evidence explicitly. Two weak signals do not make a strong lean.
4. If the evidence does not separate from consensus, say "no clear lean" and set confidence
   low. That is a legitimate and frequent answer. Manufacturing a lean to look useful is the
   single worst thing you can do here.

WHAT'S ALREADY PRICED — do not skip this
A hot print that the market already expects moves nothing. Establish what the forwards, recent
price action and commentary imply is already discounted, and judge the surprise against THAT,
not against consensus alone. This distinction is most of the value of the note.

ASYMMETRY
State where the larger move sits. Often the risk is lopsided: if a miss is well priced but a
beat is not, the beat produces the bigger move even at lower probability.

LANGUAGE — strictly enforced
- Always probabilistic and conditional: "evidence leans", "likely", "more likely than not",
  "the risk is skewed toward". Never "will rise", "will fall", "is going to", "guaranteed".
- Currency implications are conditional on the outcome, never standalone directional calls.
  Correct: "a print above consensus would likely support the dollar, with EUR/USD pressured
  toward 1.1550." Wrong: "the dollar will rise this week."
- No price targets stated as expectations. Levels may only appear as reference points that
  already exist on the chart (the 200-day, a range boundary you are given).

RESOLUTIONS
For each past call listed, search for what actually printed. Mark "hit" if the actual landed on
the side of consensus the lean called, "miss" if the opposite, "unclear" if it printed in line
or the lean was "no clear lean", "not found" if you cannot confirm the number. Report honestly —
a scoreboard that flatters the forecaster is worthless. Never guess an actual you did not find.

Be concrete and brief. Real numbers, real dates, named sources."""


def call_claude(as_of, facts, calendar, pending):
    try:
        import anthropic
    except ImportError:
        return None, "anthropic SDK not installed (pip install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no ANTHROPIC_API_KEY in environment"

    focus = select_events(calendar)
    if not focus and not pending:
        return None, "no upcoming G4 events in the horizon and nothing to resolve"

    cal_txt = "\n".join(
        f"  [{e['id']}]\n"
        f"    {e['currency']} {e['title']} — {e['impact']} impact\n"
        f"    due {e['when_utc']} (in {e['hours_until']}h) | consensus {e['consensus'] or 'n/a'} | previous {e['previous'] or 'n/a'}"
        for e in focus) or "  (none)"

    lvl_txt = "\n".join(
        f"  {f['label']:<22} {f['last']:<9} RSI14 {f['rsi_14']:<5} "
        f"200SMA {f['sma_200']} ({f['dist_from_200sma_pct']:+.2f}%, {f['sma_location']}) | "
        f"20d range {f['range_20d'][0]}–{f['range_20d'][1]} | 1SD day ±{f['daily_range_1sd_pct']}%"
        for f in facts.values())

    pend_txt = "\n".join(
        f"  [{p['event_id']}] {p['event']} ({p['currency']}) due {p['when_utc']} — "
        f"consensus was {p.get('consensus')}, our lean was '{p['lean']}' at {p['confidence']} confidence"
        for p in pending) or "  (nothing awaiting resolution)"

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    focus_line = (f"\nFOCUSED RUN — the desk asked specifically about: {FOCUS}. Spend your research "
                  f"budget on the event(s) below rather than surveying the week.\n" if FOCUS else "")
    prompt = f"""Current time: {now}. Latest FX close: {as_of}.{focus_line}

UPCOMING G4 EVENTS — these are what you are forecasting. Use the exact event_id in your output:

{cal_txt}

CURRENT LEVEL STRUCTURE (computed from published closes — use for reference levels and for
judging whether a move would be significant against normal daily range):

{lvl_txt}

PAST CALLS AWAITING RESOLUTION — search for what actually printed and score each one:

{pend_txt}

For every upcoming event above, research what the evidence available RIGHT NOW suggests about
the release relative to consensus. Search for leading indicators, sub-components, related
prints, survey detail, official commentary, and how this series has recently run versus
consensus. Prefer primary sources (statistical agencies, central banks) and dated material from
the past two weeks.

Then produce a forecast entry per event. Where evidence is thin, say "no clear lean" with low
confidence rather than inventing a view."""

    tools = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ]

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(MAX_CONTINUATIONS + 1):
        # Streaming: web research runs for minutes and the structured answer is long.
        # A non-streaming call risks both an HTTP timeout and a truncated body.
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=messages,
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": FORECAST_SCHEMA}},
        ) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason != "pause_turn":
            break
        # Server-side tool loop hit its cap; resend with the paused turn appended.
        # The API resumes on its own — deliberately no extra user text.
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": resp.content}]
    else:
        return None, f"still paused after {MAX_CONTINUATIONS} continuations"

    if resp.stop_reason == "refusal":
        return None, f"model declined: {getattr(resp, 'stop_details', None)}"

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        return None, "no text block in response"

    # 3. Fail with a diagnosis rather than an opaque JSONDecodeError. Truncation at
    #    the token ceiling is the one way this JSON can be malformed.
    if resp.stop_reason == "max_tokens":
        return None, (f"output truncated at max_tokens={MAX_TOKENS} "
                      f"({resp.usage.output_tokens} tokens, {len(text)} chars) — "
                      f"lower MAX_EVENTS or raise MAX_TOKENS")
    try:
        out = json.loads(text)
    except json.JSONDecodeError as err:
        return None, f"model returned malformed JSON ({err}); stop_reason={resp.stop_reason}"
    out["_usage"] = {"input_tokens": resp.usage.input_tokens,
                     "output_tokens": resp.usage.output_tokens, "model": MODEL}
    return out, None


# ------------------------------------------------------------------ ledger I/O
def update_ledger(ledger, story, calendar):
    """Record new calls; apply resolutions to old ones. Append-only by event_id."""
    if not story:
        return ledger, 0, 0

    by_id = {e["id"]: e for e in calendar}
    idx0 = {e["event_id"]: e for e in ledger["entries"]}
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    added = revised = 0

    for f in story.get("forecasts", []):
        ev = by_id.get(f["event_id"], {})
        prior = idx0.get(f["event_id"])

        if prior is None:
            ledger["entries"].append({
                "event_id": f["event_id"],
                "event": f["event"],
                "currency": f["currency"],
                "when_utc": ev.get("when_utc", ""),
                "consensus": f.get("consensus"),
                "lean": f["lean"],
                "confidence": f["confidence"],
                "called_at": stamp,
                "revisions": [{"lean": f["lean"], "confidence": f["confidence"], "at": stamp}],
                "outcome": None, "actual": None, "note": None,
            })
            added += 1
            continue

        # A later run re-forecasting the same event supersedes the earlier call —
        # that is the point of a pre-NFP run. But NEVER after the event has printed:
        # revising a call once the answer is known would make the scoreboard a lie.
        try:
            when = dt.datetime.fromisoformat(prior.get("when_utc") or ev.get("when_utc", ""))
        except ValueError:
            when = None
        if prior.get("outcome") or (when and now >= when):
            continue

        prior.setdefault("revisions", [{"lean": prior["lean"],
                                        "confidence": prior["confidence"],
                                        "at": prior.get("called_at", "")}])
        if (f["lean"], f["confidence"]) != (prior["lean"], prior["confidence"]):
            prior["revisions"].append({"lean": f["lean"], "confidence": f["confidence"], "at": stamp})
            prior["lean"] = f["lean"]
            prior["confidence"] = f["confidence"]
            prior["consensus"] = f.get("consensus", prior.get("consensus"))
            revised += 1

    resolved = 0
    idx = {e["event_id"]: e for e in ledger["entries"]}
    for r in story.get("resolutions", []):
        e = idx.get(r["event_id"])
        if not e or e.get("outcome"):
            continue
        if r["outcome"] == "not found":
            continue          # leave pending; a later run may find it
        e["outcome"] = r["outcome"]
        e["actual"] = r["actual"]
        e["note"] = r["note"]
        e["resolved_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        resolved += 1

    scored = [e for e in ledger["entries"] if e.get("outcome") in ("hit", "miss")]
    ledger["record"] = {
        "scored": len(scored),
        "hits": sum(1 for e in scored if e["outcome"] == "hit"),
        "misses": sum(1 for e in scored if e["outcome"] == "miss"),
        "unclear": sum(1 for e in ledger["entries"] if e.get("outcome") == "unclear"),
        "pending": sum(1 for e in ledger["entries"] if not e.get("outcome")),
    }
    return ledger, added, resolved, revised


def main():
    facts_only = "--facts-only" in sys.argv

    if FOCUS:
        print(f"FOCUSED RUN — '{FOCUS}' (max {MAX_EVENTS} events)", flush=True)
    print("fetching rates…", flush=True)
    as_of, facts = build_facts()
    print(f"  as of {as_of}", flush=True)

    print("fetching calendar…", flush=True)
    calendar, cal_err = build_calendar()
    if cal_err:
        print(f"  {cal_err}", file=sys.stderr, flush=True)
    g4 = [e for e in calendar if e["is_g4"]]
    print(f"  {len(calendar)} upcoming events, {len(g4)} G4", flush=True)
    for e in g4[:6]:
        print(f"    +{e['hours_until']:>5.1f}h  {e['currency']} {e['title'][:44]:<44} "
              f"cons {e['consensus'] or '—'}", flush=True)

    ledger = load_ledger()
    pending = pending_resolution(ledger)
    print(f"  {len(pending)} past call(s) awaiting resolution", flush=True)

    story, err = ((None, "skipped (--facts-only)") if facts_only
                  else call_claude(as_of, facts, calendar, pending))
    if err:
        print(f"forecast layer unavailable: {err}", file=sys.stderr, flush=True)
    else:
        u = story["_usage"]
        print(f"forecast layer ok — {len(story['forecasts'])} forecasts, "
              f"{len(story['resolutions'])} resolutions, "
              f"{u['input_tokens']} in / {u['output_tokens']} out", flush=True)

    ledger, added, resolved, revised = update_ledger(ledger, story, calendar)
    if story:
        print(f"ledger: +{added} new, {revised} revised, {resolved} resolved, "
              f"record {ledger.get('record')}", flush=True)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "data_as_of": as_of,
            "facts": facts,
            "calendar": calendar,
            "calendar_error": cal_err,
            "forecast": story,
            "forecast_error": err,
            "record": ledger.get("record"),
            "focus": FOCUS or None,
        }, fh, indent=2)
    with open(LEDGER, "w") as fh:
        json.dump(ledger, fh, indent=2)
    print(f"wrote {os.path.relpath(OUT)} and {os.path.relpath(LEDGER)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
