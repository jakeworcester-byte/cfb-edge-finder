#!/usr/bin/env python3
"""
Pre-bet news, QB/injury, opt-out, and weather guardrail using Claude + web search.

Checks the top upcoming candidate games (within 72 hours of kickoff) before the
Top 5 card locks. Results are cached in data/news_checks.json for 18 hours so
repeat runs in the same day do not re-query the API.

Never fails the build: missing API key, missing package, network error, or bad
output logs the reason and leaves the game unflagged.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MODEL = os.environ.get("NEWS_CHECK_MODEL", "claude-opus-5")
EFFORT = "low"
MAX_TOKENS = 1200
MAX_SEARCH_USES = 2
CACHE_TTL_HOURS = 18
LOOKAHEAD_HOURS = 72
MAX_CANDIDATES = 10

SYSTEM = """You are a college football pre-bet news and weather checker.
Given one upcoming matchup and the model's candidate bet(s), use web search to
check ONLY concrete, current facts affecting this game right now:
1. Starting quarterback availability, key starter injuries, suspensions, bowl
   opt-outs, or sudden coaching changes announced this week.
2. Game-day weather forecast at the stadium (especially sustained wind >= 15 mph
   or heavy rain/snow when a total bet is under consideration).

Classify severity strictly:
- "avoid": Confirmed starting QB is out/doubtful/opted out/transferred, multiple
  key starters or head coach are out and not priced into season-long ratings, OR
  severe weather (>= 20 mph sustained wind or heavy rain/snow) directly undercuts
  an Over play.
- "caution": Starting QB or key skill/line starter is questionable/game-time
  decision, minor coaching/roster disruption, or moderate wind (13-19 mph).
- "clear": No material injury, opt-out, or weather concern found.

Return JSON matching the schema. Keep summary to one concise sentence under 160
characters. Do not use em dashes or en dashes."""

SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "severity": {
                "type": "string",
                "enum": ["clear", "caution", "avoid"],
            },
            "summary": {"type": "string"},
            "flags": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["severity", "summary", "flags"],
        "additionalProperties": False,
    },
}

DASHES = re.compile(r"\s*[—–]\s*")
LAST_ERROR = None


def _parse_iso(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_cache(path):
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("games"), dict):
                return data
        except Exception as e:
            print(f"news_check: could not read cache ({e})", file=sys.stderr)
    return {"games": {}, "lastError": None}


def save_cache(path, cache):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _check_one_game(client, game, bets, now):
    bet_desc = ", ".join(f"{b['pick']} ({b['type']})" for b in bets)
    kick = game.get("startDate") or "upcoming"
    venue = game.get("venue") or "unknown venue"
    prompt = (
        f"Matchup: {game['awayTeam']} at {game['homeTeam']}\n"
        f"Kickoff (UTC): {kick}\n"
        f"Venue: {venue}\n"
        f"Current date (UTC): {now.strftime('%Y-%m-%d')}\n"
        f"Candidate model play(s): {bet_desc}\n\n"
        "Search current injury reports, starting QB status, opt-outs, and "
        "game-day weather forecast for this matchup, then return the JSON verdict."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": MAX_SEARCH_USES,
        }],
        output_config={"effort": EFFORT, "format": SCHEMA},
        messages=[{"role": "user", "content": prompt}],
    )
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text" and getattr(b, "text", None)]
    if not texts:
        raise ValueError(f"no text block (stop_reason={response.stop_reason})")
    out = _extract_json(texts[-1])
    sev = str(out.get("severity", "clear")).lower()
    if sev not in ("clear", "caution", "avoid"):
        sev = "clear"
    summary = DASHES.sub(", ", str(out.get("summary", "")).strip())[:180]
    flags = [DASHES.sub(", ", str(f).strip())[:32] for f in (out.get("flags") or []) if str(f).strip()][:3]
    return {
        "severity": sev,
        "summary": summary or "No material injury or weather flags found.",
        "flags": flags,
        "checkedAt": now.isoformat(),
        "source": "claude",
    }


def apply_news_checks(evaluated, wkey, cache_path, now):
    """Run cached news/weather guardrails on top eligible candidates.

    Mutates eligible bets on 'avoid' games to eligible=False and attaches
    g['newsCheck'] onto checked games. Returns (checked_count, avoided_count).
    """
    global LAST_ERROR
    LAST_ERROR = None

    cache = load_cache(cache_path)
    games_cache = cache.setdefault("games", {})

    # Identify eligible candidates within the lookahead window, ranked by max EV
    cands = []
    for g in evaluated:
        elig = [b for b in g.get("bets", []) if b.get("eligible") and b.get("ev", 0) > 0]
        if not elig:
            continue
        st = _parse_iso(g.get("startDate"))
        if st is None:
            continue
        hrs = (st - now).total_seconds() / 3600.0
        if hrs < 0 or hrs > LOOKAHEAD_HOURS:
            continue
        best_ev = max(b["ev"] for b in elig)
        cands.append((best_ev, g, elig))

    cands.sort(key=lambda x: x[0], reverse=True)
    targets = cands[:MAX_CANDIDATES]

    # Attach any already-fresh cached checks even if beyond MAX_CANDIDATES
    for g in evaluated:
        ckey = f"{wkey}:{g['id']}"
        cached = games_cache.get(ckey)
        if cached:
            at = _parse_iso(cached.get("checkedAt"))
            if at and (now - at).total_seconds() / 3600.0 <= CACHE_TTL_HOURS:
                g["newsCheck"] = {
                    "severity": cached["severity"],
                    "summary": cached["summary"],
                    "flags": cached.get("flags") or [],
                    "checkedAt": cached["checkedAt"],
                }

    need_api = []
    for _, g, elig in targets:
        if g.get("newsCheck"):
            continue
        need_api.append((g, elig))

    if need_api:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            LAST_ERROR = "no ANTHROPIC_API_KEY"
            print(f"news_check: skipped ({LAST_ERROR})", file=sys.stderr)
        else:
            try:
                import anthropic
                client = anthropic.Anthropic()
                for g, elig in need_api:
                    try:
                        res = _check_one_game(client, g, elig, now)
                        ckey = f"{wkey}:{g['id']}"
                        games_cache[ckey] = {
                            "weekKey": wkey,
                            "matchup": f"{g['awayTeam']} @ {g['homeTeam']}",
                            **res,
                        }
                        g["newsCheck"] = {
                            "severity": res["severity"],
                            "summary": res["summary"],
                            "flags": res["flags"],
                            "checkedAt": res["checkedAt"],
                        }
                    except Exception as e:  # noqa: BLE001
                        LAST_ERROR = f"{type(e).__name__}: {e}"
                        print(f"news_check: failed on {g['awayTeam']} @ {g['homeTeam']} ({LAST_ERROR})",
                              file=sys.stderr)
                        break  # stop hitting API on quota/auth/schema errors
            except ImportError:
                LAST_ERROR = "anthropic package not installed"
                print(f"news_check: skipped ({LAST_ERROR})", file=sys.stderr)

    # Apply 'avoid' downgrades across all checked games
    checked = 0
    avoided = 0
    for g in evaluated:
        nc = g.get("newsCheck")
        if not nc:
            continue
        checked += 1
        if nc.get("severity") == "avoid":
            for b in g.get("bets", []):
                if b.get("eligible"):
                    b["eligible"] = False
                    b["detail"] = f"{b['detail']} (filtered: news guardrail - {nc['summary']})"
                    avoided += 1

    # Prune stale cache entries older than 14 days
    cutoff = now.timestamp() - 14 * 86400
    cache["games"] = {
        k: v for k, v in games_cache.items()
        if (_parse_iso(v.get("checkedAt")) or now).timestamp() >= cutoff
    }
    cache["lastError"] = LAST_ERROR
    cache["updatedAt"] = now.isoformat()
    save_cache(cache_path, cache)
    return checked, avoided
