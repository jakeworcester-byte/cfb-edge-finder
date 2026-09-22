#!/usr/bin/env python3
"""
CFB Edge Finder - data pipeline.

Pulls the current week's FBS schedule, betting lines, and computer ratings
from the CollegeFootballData.com (CFBD) API, runs an ensemble prediction
model, scores every available bet for expected value, and writes
site/data.json for the static site.

Ensemble components (see MODEL.md):
  SP+  (Bill Connelly)  - 35%
  FPI  (ESPN)           - 30%
  Elo  (CFBD)           - 20%
  SRS  (simple rating)  - 15%

Requires env var CFBD_API_KEY. Without it, writes demo data so the site
still renders (with a banner) until the key is configured.
"""

import hashlib
import json
import math
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.collegefootballdata.com"
API_KEY = os.environ.get("CFBD_API_KEY", "").strip()

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "site" / "data.json"
LEDGER_PATH = ROOT / "data" / "picks_history.json"
BOARDS_PATH = ROOT / "data" / "boards.json"
RESULTS_PATH = ROOT / "site" / "results.json"
LASTWEEK_PATH = ROOT / "site" / "lastweek.json"
RECORD_PATH = ROOT / "site" / "modelrecord.json"
RECAP_PATH = ROOT / "data" / "recap.json"
STAKE = 10.0                 # flat bet size for the season P/L tally

# --- model constants -------------------------------------------------------
WEIGHTS = {"sp": 0.35, "fpi": 0.30, "elo": 0.20, "srs": 0.15}
HOME_FIELD_PTS = 2.3
ELO_PTS_PER_POINT = 28.0     # ~28 Elo points per scoreboard point
SIGMA_ATS = 13.5             # std dev of margin vs spread (CFB)
SIGMA_ML = 15.5              # std dev used for outright win probability
FCS_DEFAULT_RATING = -24.0   # placeholder power rating for FCS opponents
ASSUMED_SPREAD_PRICE = -110  # CFBD does not carry spread/total prices
MIN_EDGE_SPREAD = 2.0        # pts of edge required to be pick-eligible
MIN_EDGE_TOTAL = 3.5
MIN_EDGE_ML_PROB = 0.05      # model prob must beat implied prob by this
ML_MIN_PROB = 0.35           # no longshot MLs: normal-tail probs are unreliable
ML_MAX_PRICE = 300           # ignore moneylines longer than +300 / shorter than -300
TOP_N = 5

# EV tiers used to break the season record down by how strong the model
# thought each play was. Answers "does a higher modeled edge actually win more?"
EV_TIERS = [(0.20, "20%+"), (0.10, "10-20%"), (0.05, "5-10%"), (-9.9, "0-5%")]
# Calibration buckets on the model's own win probability.
CAL_BUCKETS = [(0.70, 1.01, "70%+"), (0.65, 0.70, "65-70%"), (0.60, 0.65, "60-65%"),
               (0.55, 0.60, "55-60%"), (0.50, 0.55, "50-55%"),
               (0.00, 0.50, "Under 50%")]

BOOK_PRIORITY = ["consensus", "DraftKings", "ESPN Bet", "Bovada", "Caesars"]


def api_get(path, **params):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API_BASE}{path}?{qs}" if qs else f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "cfb-edge-finder/1.0",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick(d, *keys, default=None):
    """Read the first present key - tolerates CFBD camelCase/snake_case drift."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def american_payout(odds):
    """Profit per 1 unit staked at American odds."""
    return odds / 100.0 if odds > 0 else 100.0 / abs(odds)


def implied_prob(odds):
    return 100.0 / (odds + 100.0) if odds > 0 else abs(odds) / (abs(odds) + 100.0)


def ev_per_unit(p_win, odds):
    """Expected profit per 1 unit staked (pushes ignored)."""
    return p_win * american_payout(odds) - (1.0 - p_win)


# --- season / week detection ------------------------------------------------

def current_season(now):
    # Jan bowl games belong to the previous calendar year's season
    return now.year if now.month >= 6 else now.year - 1


def parse_iso(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def calendar_weeks(year):
    """All calendar weeks as (start, end, seasonType, week), ordered by start."""
    try:
        cal = api_get("/calendar", year=year)
    except Exception as e:
        print(f"calendar fetch failed ({e}); defaulting to regular wk 1", file=sys.stderr)
        return []
    weeks = []
    for wk in cal:
        end = parse_iso(pick(wk, "endDate", "lastGameStart", "end_date"))
        if end is None:
            continue
        start = parse_iso(pick(wk, "startDate", "firstGameStart", "start_date")) or end
        weeks.append((start, end,
                      pick(wk, "seasonType", "season_type", default="regular"),
                      int(pick(wk, "week", default=1))))
    weeks.sort(key=lambda w: w[0])
    return weeks


def is_upcoming(game, now):
    """True if the game hasn't been played and hasn't kicked off yet."""
    if pick(game, "completed", default=False):
        return False
    start = parse_iso(pick(game, "startDate", "start_date"))
    return start is None or start >= now


def fetch_upcoming_week(year, now, weeks):
    """Find the next week with games still to play.

    Walks the calendar forward from today and returns
    (seasonType, week, upcoming_games, previous_week). A week whose games
    have all been played (e.g. it's Sunday and the calendar week hasn't
    rolled over yet) is skipped in favor of the next one. previous_week is
    the (seasonType, week) immediately before the chosen one, or None.
    If the season is over, returns the final week's full slate as played.
    """
    if not weeks:
        return "regular", 1, api_get("/games", year=year, week=1,
                                     seasonType="regular", classification="fbs"), None

    def prev_of(stype, wk):
        idx = next((i for i, w in enumerate(weeks) if (w[2], w[3]) == (stype, wk)), 0)
        return (weeks[idx - 1][2], weeks[idx - 1][3]) if idx > 0 else None

    candidates = [w for w in weeks if w[1] >= now] or weeks[-1:]
    last_fetch = None
    for _, _, season_type, week in candidates[:3]:
        games = api_get("/games", year=year, week=week,
                        seasonType=season_type, classification="fbs")
        last_fetch = (season_type, week, games, prev_of(season_type, week))
        upcoming = [g for g in games if is_upcoming(g, now)]
        if upcoming:
            return season_type, week, upcoming, prev_of(season_type, week)
    # nothing upcoming (season over / long dead period): show last week as played
    return last_fetch


# --- ratings ----------------------------------------------------------------

def fetch_ratings(year):
    """Returns {team: {sp, sp_off, sp_def, fpi, elo, srs}} plus league SP+ means."""
    ratings = {}

    def slot(team):
        return ratings.setdefault(team, {})

    try:
        for r in api_get("/ratings/sp", year=year):
            team = pick(r, "team")
            if not team or team == "nationalAverages":
                continue
            s = slot(team)
            s["sp"] = pick(r, "rating")
            off = pick(r, "offense", default={}) or {}
            dfn = pick(r, "defense", default={}) or {}
            s["sp_off"] = pick(off, "rating")
            s["sp_def"] = pick(dfn, "rating")
    except Exception as e:
        print(f"SP+ fetch failed: {e}", file=sys.stderr)

    try:
        for r in api_get("/ratings/fpi", year=year):
            team = pick(r, "team")
            if team:
                slot(team)["fpi"] = pick(r, "fpi", "rating")
    except Exception as e:
        print(f"FPI fetch failed: {e}", file=sys.stderr)

    try:
        for r in api_get("/ratings/elo", year=year):
            team = pick(r, "team")
            if team:
                slot(team)["elo"] = pick(r, "elo", "rating")
    except Exception as e:
        print(f"Elo fetch failed: {e}", file=sys.stderr)

    try:
        for r in api_get("/ratings/srs", year=year):
            team = pick(r, "team")
            if team:
                slot(team)["srs"] = pick(r, "rating")
    except Exception as e:
        print(f"SRS fetch failed: {e}", file=sys.stderr)

    elos = [v["elo"] for v in ratings.values() if v.get("elo") is not None]
    elo_mean = statistics.mean(elos) if elos else 1500.0
    offs = [v["sp_off"] for v in ratings.values() if v.get("sp_off") is not None]
    defs = [v["sp_def"] for v in ratings.values() if v.get("sp_def") is not None]
    sp_means = {
        "off": statistics.mean(offs) if offs else 30.0,
        "def": statistics.mean(defs) if defs else 27.0,
    }
    return ratings, elo_mean, sp_means


def fetch_records(year):
    rec = {}
    try:
        for r in api_get("/records", year=year):
            team = pick(r, "team")
            total = pick(r, "total", default={}) or {}
            if team:
                rec[team] = f"{pick(total, 'wins', default=0)}-{pick(total, 'losses', default=0)}"
    except Exception as e:
        print(f"records fetch failed: {e}", file=sys.stderr)
    return rec


def power_rating(team_ratings, elo_mean):
    """Blend available sources into one points-scale power rating.

    Returns (rating, sources_used). Missing sources drop out and the
    remaining weights renormalize.
    """
    comps, used = [], []
    r = team_ratings or {}
    if r.get("sp") is not None:
        comps.append(("sp", r["sp"]))
    if r.get("fpi") is not None:
        comps.append(("fpi", r["fpi"]))
    if r.get("elo") is not None:
        comps.append(("elo", (r["elo"] - elo_mean) / ELO_PTS_PER_POINT))
    if r.get("srs") is not None:
        comps.append(("srs", r["srs"]))
    if not comps:
        return None, []
    wsum = sum(WEIGHTS[k] for k, _ in comps)
    rating = sum(WEIGHTS[k] * v for k, v in comps) / wsum
    used = [k for k, _ in comps]
    return rating, used


def expected_points(off_rating, opp_def_rating, sp_means):
    """SP+-derived expected points for one side, shrunk 50% toward average."""
    league_avg = (sp_means["off"] + sp_means["def"]) / 2.0
    if off_rating is None or opp_def_rating is None:
        return league_avg
    return league_avg + 0.5 * (off_rating - sp_means["off"]) + 0.5 * (opp_def_rating - sp_means["def"])


# --- lines ------------------------------------------------------------------

def fetch_lines(year, week, season_type):
    """Returns {gameId: {books: [...], spread, total, ml_home, ml_away}}."""
    out = {}
    try:
        rows = api_get("/lines", year=year, week=week, seasonType=season_type)
    except Exception as e:
        print(f"lines fetch failed: {e}", file=sys.stderr)
        return out
    for row in rows:
        gid = pick(row, "id", "gameId")
        books = []
        for ln in pick(row, "lines", default=[]) or []:
            books.append({
                "provider": pick(ln, "provider", default="?"),
                "spread": pick(ln, "spread"),
                "overUnder": pick(ln, "overUnder", "over_under"),
                "homeMoneyline": pick(ln, "homeMoneyline", "home_moneyline"),
                "awayMoneyline": pick(ln, "awayMoneyline", "away_moneyline"),
                "formattedSpread": pick(ln, "formattedSpread", "formatted_spread"),
            })
        if not books:
            continue

        def consensus(field):
            vals = [b[field] for b in books if isinstance(b.get(field), (int, float))]
            # median of several books can land on .25/.75 - snap to a bettable half-point
            return round(statistics.median(vals) * 2) / 2 if vals else None

        def best_price(field):
            vals = [b[field] for b in books if isinstance(b.get(field), (int, float))]
            if not vals:
                return None
            return max(vals, key=american_payout)

        books.sort(key=lambda b: BOOK_PRIORITY.index(b["provider"])
                   if b["provider"] in BOOK_PRIORITY else 99)
        out[gid] = {
            "books": books,
            "spread": consensus("spread"),          # home spread (negative = home favored)
            "total": consensus("overUnder"),
            "mlHome": best_price("homeMoneyline"),
            "mlAway": best_price("awayMoneyline"),
        }
    return out


# --- game evaluation ---------------------------------------------------------

def evaluate_game(game, ratings, elo_mean, sp_means, records, lines):
    home = pick(game, "homeTeam", "home_team")
    away = pick(game, "awayTeam", "away_team")
    gid = pick(game, "id")
    neutral = bool(pick(game, "neutralSite", "neutral_site", default=False))
    home_class = (pick(game, "homeClassification", "home_division", default="") or "").lower()
    away_class = (pick(game, "awayClassification", "away_division", default="") or "").lower()

    hr = ratings.get(home, {})
    ar = ratings.get(away, {})
    h_rating, h_sources = power_rating(hr, elo_mean)
    a_rating, a_sources = power_rating(ar, elo_mean)

    low_confidence = False
    if h_rating is None:
        h_rating, h_sources, low_confidence = FCS_DEFAULT_RATING, ["fcs-default"], True
    if a_rating is None:
        a_rating, a_sources, low_confidence = FCS_DEFAULT_RATING, ["fcs-default"], True
    if away_class and away_class != "fbs":
        low_confidence = True
    if home_class and home_class != "fbs":
        low_confidence = True

    hfa = 0.0 if neutral else HOME_FIELD_PTS
    model_margin = (h_rating - a_rating) + hfa  # positive = home favored

    exp_home = expected_points(hr.get("sp_off"), ar.get("sp_def"), sp_means)
    exp_away = expected_points(ar.get("sp_off"), hr.get("sp_def"), sp_means)
    model_total = exp_home + exp_away
    # re-split scores so they stay consistent with the ensemble margin
    home_pts = (model_total + model_margin) / 2.0
    away_pts = (model_total - model_margin) / 2.0

    p_home_win = norm_cdf(model_margin / SIGMA_ML)

    entry = {
        "id": gid,
        "startDate": pick(game, "startDate", "start_date"),
        "homeTeam": home,
        "awayTeam": away,
        "homeConference": pick(game, "homeConference", "home_conference"),
        "awayConference": pick(game, "awayConference", "away_conference"),
        "homeRecord": records.get(home, ""),
        "awayRecord": records.get(away, ""),
        "neutralSite": neutral,
        "tv": pick(game, "tv", "outlet"),
        "venue": pick(game, "venue"),
        "model": {
            "homeRating": round(h_rating, 1),
            "awayRating": round(a_rating, 1),
            "sources": {"home": h_sources, "away": a_sources},
            "margin": round(model_margin, 1),          # home - away
            "spread": round(-model_margin, 1),          # model's fair home spread
            "total": round(model_total, 1),
            "homeScore": round(home_pts, 1),
            "awayScore": round(away_pts, 1),
            "pHomeWin": round(p_home_win, 3),
            "lowConfidence": low_confidence,
        },
        "lines": lines.get(gid),
        "bets": [],
    }

    ln = lines.get(gid)
    if not ln or low_confidence:
        return entry

    # --- spread ---
    if isinstance(ln.get("spread"), (int, float)):
        vegas_margin = -ln["spread"]                    # implied home margin
        edge = model_margin - vegas_margin              # + = home side value
        side = "home" if edge > 0 else "away"
        team = home if side == "home" else away
        line_for_side = ln["spread"] if side == "home" else -ln["spread"]
        p_cover = norm_cdf(abs(edge) / SIGMA_ATS)
        ev = ev_per_unit(p_cover, ASSUMED_SPREAD_PRICE)
        entry["bets"].append({
            "type": "spread",
            "pick": f"{team} {line_for_side:+g}",
            "team": team,
            "side": side,
            "line": line_for_side,
            "edgePts": round(abs(edge), 1),
            "prob": round(p_cover, 3),
            "price": ASSUMED_SPREAD_PRICE,
            "ev": round(ev, 4),
            "eligible": abs(edge) >= MIN_EDGE_SPREAD,
            "detail": f"Model line: {home} {-model_margin:+.1f} vs market {ln['spread']:+.1f}",
        })

    # --- total ---
    if isinstance(ln.get("total"), (int, float)):
        edge_t = model_total - ln["total"]
        direction = "Over" if edge_t > 0 else "Under"
        p_hit = norm_cdf(abs(edge_t) / SIGMA_ATS)
        ev = ev_per_unit(p_hit, ASSUMED_SPREAD_PRICE)
        entry["bets"].append({
            "type": "total",
            "pick": f"{direction} {ln['total']:g}",
            "team": None,
            "side": direction.lower(),
            "line": ln["total"],
            "edgePts": round(abs(edge_t), 1),
            "prob": round(p_hit, 3),
            "price": ASSUMED_SPREAD_PRICE,
            "ev": round(ev, 4),
            "eligible": abs(edge_t) >= MIN_EDGE_TOTAL,
            "detail": f"Model total {model_total:.1f} vs market {ln['total']:g}",
        })

    # --- moneylines ---
    for side, ml, p_win in (("home", ln.get("mlHome"), p_home_win),
                            ("away", ln.get("mlAway"), 1.0 - p_home_win)):
        if not isinstance(ml, (int, float)) or ml == 0:
            continue
        team = home if side == "home" else away
        imp = implied_prob(ml)
        gap = p_win - imp
        ev = ev_per_unit(p_win, ml)
        entry["bets"].append({
            "type": "moneyline",
            "pick": f"{team} ML {ml:+g}",
            "team": team,
            "side": side,
            "line": None,
            "edgePts": round(gap * 100, 1),             # prob gap in pct pts
            "prob": round(p_win, 3),
            "price": ml,
            "ev": round(ev, 4),
            "eligible": (gap >= MIN_EDGE_ML_PROB and ev > 0
                         and p_win >= ML_MIN_PROB and abs(ml) <= ML_MAX_PRICE),
            "detail": f"Model win prob {p_win:.0%} vs implied {imp:.0%}",
        })

    return entry


def top_picks(games):
    cands = []
    for g in games:
        for b in g["bets"]:
            if b["eligible"] and b["ev"] > 0:
                cands.append({
                    "gameId": g["id"],
                    "matchup": f"{g['awayTeam']} @ {g['homeTeam']}",
                    "homeTeam": g["homeTeam"],
                    "awayTeam": g["awayTeam"],
                    "startDate": g["startDate"],
                    **b,
                })
    cands.sort(key=lambda b: b["ev"], reverse=True)
    # at most one pick per game so the card isn't 3 angles on one matchup
    seen, out = set(), []
    for c in cands:
        if c["gameId"] in seen:
            continue
        seen.add(c["gameId"])
        out.append(c)
        if len(out) == TOP_N:
            break
    return out


# --- pick ledger and grading ---------------------------------------------------

def load_ledger():
    if LEDGER_PATH.exists():
        try:
            data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("picks"), list):
                return data
        except Exception as e:
            print(f"could not read ledger ({e}); starting fresh", file=sys.stderr)
    return {"picks": []}


def pick_started(p, now):
    st = parse_iso(p.get("startDate"))
    return st is not None and st <= now


def grade_one(p, home_pts, away_pts):
    """Return a result dict for a pick given the final score."""
    t, side, line = p["type"], p.get("side"), p.get("line")
    if t == "total":
        actual = home_pts + away_pts
        if actual == line:
            outcome = "push"
        elif (actual > line) == (side == "over"):
            outcome = "win"
        else:
            outcome = "loss"
    else:
        team_pts, opp_pts = (home_pts, away_pts) if side == "home" else (away_pts, home_pts)
        if t == "spread":
            adj = team_pts + (line or 0)
            outcome = "push" if adj == opp_pts else ("win" if adj > opp_pts else "loss")
        else:  # moneyline
            outcome = ("win" if team_pts > opp_pts
                       else "push" if team_pts == opp_pts else "loss")
    profit = (0.0 if outcome == "push"
              else STAKE * american_payout(p["price"]) if outcome == "win"
              else -STAKE)
    return {"homeScore": home_pts, "awayScore": away_pts,
            "outcome": outcome, "profit": round(profit, 2)}


def fetch_scores(week_keys):
    """Final scores for completed games: {gameId: (home_pts, away_pts)}."""
    scores = {}
    for season, stype, wk in sorted(week_keys):
        try:
            for g in api_get("/games", year=season, week=wk, seasonType=stype):
                if pick(g, "completed", default=False):
                    hp = pick(g, "homePoints", "home_points")
                    ap = pick(g, "awayPoints", "away_points")
                    if hp is not None and ap is not None:
                        scores[pick(g, "id")] = (hp, ap)
        except Exception as e:
            print(f"score fetch failed ({season} {stype} wk {wk}): {e}", file=sys.stderr)
    return scores


def grade_picks(ledger, scores, now):
    """Grade ungraded ledger picks whose games have kicked off."""
    graded = 0
    for p in ledger["picks"]:
        if not p.get("result") and pick_started(p, now) and p["gameId"] in scores:
            p["result"] = grade_one(p, *scores[p["gameId"]])
            graded += 1
    return graded


# --- full-board snapshots (every game, locked at kickoff) ------------------------

def week_key(season, stype, week):
    return f"{season}-{stype}-{week}"


def load_boards():
    if BOARDS_PATH.exists():
        try:
            data = json.loads(BOARDS_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("weeks"), dict):
                return data
        except Exception as e:
            print(f"could not read boards ({e}); starting fresh", file=sys.stderr)
    return {"weeks": {}}


def line_snapshot(lines, now):
    """The bettable numbers from one run, for the line-movement history."""
    if not lines:
        return None
    return {
        "at": now.isoformat(),
        "spread": lines.get("spread"),
        "total": lines.get("total"),
        "mlHome": lines.get("mlHome"),
        "mlAway": lines.get("mlAway"),
    }


def same_line(a, b):
    if not a or not b:
        return False
    return all(a.get(k) == b.get(k) for k in ("spread", "total", "mlHome", "mlAway"))


def record_board(boards, evaluated, season, stype, week, now, backfilled=False):
    """Snapshot the model's call on every game. Games already kicked off or
    graded keep their earlier snapshot; the rest refresh to this run.

    Every run appends to the game's lineHistory (when the numbers actually
    moved), including the run that locks it, so we keep the opening number
    the site published alongside the last one seen before kickoff.
    """
    wk = boards["weeks"].setdefault(week_key(season, stype, week), {
        "season": season, "seasonType": stype, "week": week,
        "backfilled": backfilled, "games": {}})
    for e in evaluated:
        gid = str(e["id"])
        old = wk["games"].get(gid)
        history = list((old or {}).get("lineHistory") or [])
        snapshot = line_snapshot(e.get("lines"), now)
        if snapshot and not same_line(snapshot, history[-1] if history else None):
            history.append(snapshot)

        if old and (old.get("result") or pick_started(old, now)):
            # already locked: the model call stays frozen, but a line seen
            # in this run is still the freshest read on where it closed
            if history:
                old["lineHistory"] = history
            continue

        snap = dict(e)
        snap["result"] = None
        snap["lockedAt"] = now.isoformat()
        snap["lineHistory"] = history
        wk["games"][gid] = snap


def value_bets(g):
    """The plays the model actually flagged on this game. Most games have
    none, and that is the point: no edge, no play."""
    out = []
    for b in g.get("bets", []):
        if b.get("eligible") and b.get("ev", 0) > 0:
            out.append(b)
    return out


def attach_line_history(boards, evaluated, season, stype, week):
    """Copy each game's line history onto the published board so the site can
    show how the number moved since the week opened."""
    wk = boards["weeks"].get(week_key(season, stype, week))
    if not wk:
        return
    for e in evaluated:
        snap = wk["games"].get(str(e["id"]))
        if snap:
            e["lineHistory"] = snap.get("lineHistory") or []


def grade_game(g, home_pts, away_pts):
    """Grade every value play the model flagged on this game.

    Games where the model found no edge grade to an empty call list - they
    are scored, but they are a no-play and never count in the record.
    """
    calls = []
    for b in value_bets(g):
        res = grade_one(b, home_pts, away_pts)
        calls.append({
            "type": b["type"], "pick": b["pick"], "side": b.get("side"),
            "line": b.get("line"), "price": b["price"], "prob": b["prob"],
            "edgePts": b.get("edgePts"), "ev": b["ev"],
            "outcome": res["outcome"], "profit": res["profit"],
        })
    return {"homeScore": home_pts, "awayScore": away_pts, "calls": calls}


def regrade_boards(boards):
    """Re-derive the graded calls on every finished game from its locked
    snapshot. Idempotent, and it keeps older weeks consistent whenever the
    value thresholds or the grading rules change."""
    changed = 0
    for wk in boards["weeks"].values():
        for g in wk["games"].values():
            res = g.get("result")
            if not res or res.get("homeScore") is None:
                continue
            fresh = grade_game(g, res["homeScore"], res["awayScore"])
            if fresh != res:
                g["result"] = fresh
                changed += 1
    return changed


def grade_boards(boards, scores, now):
    graded = 0
    for wk in boards["weeks"].values():
        for gid, g in wk["games"].items():
            if not g.get("result") and pick_started(g, now) and g["id"] in scores:
                g["result"] = grade_game(g, *scores[g["id"]])
                graded += 1
    return graded


def pending_week_keys(ledger, boards, now):
    keys = {(p["season"], p["seasonType"], p["week"]) for p in ledger["picks"]
            if not p.get("result") and pick_started(p, now)}
    for wk in boards["weeks"].values():
        if any(not g.get("result") and pick_started(g, now) for g in wk["games"].values()):
            keys.add((wk["season"], wk["seasonType"], wk["week"]))
    return keys


def backfill_previous_week(boards, prev, year, now, ratings, elo_mean, sp_means, records):
    """One-time snapshot of a week that was played before this run ever saw
    it (only happens the first week after this feature ships). Uses current
    ratings, which already reflect those results, so it's flagged."""
    if not prev:
        return
    stype, week = prev
    if week_key(year, stype, week) in boards["weeks"]:
        return
    games = api_get("/games", year=year, week=week, seasonType=stype, classification="fbs")
    lines = fetch_lines(year, week, stype)
    evaluated = [evaluate_game(g, ratings, elo_mean, sp_means, records, lines) for g in games]
    evaluated = [e for e in evaluated if e["homeTeam"] and e["awayTeam"]]
    record_board(boards, evaluated, year, stype, week, now, backfilled=True)
    print(f"Backfilled board for {stype} week {week}: {len(evaluated)} games")


# --- line movement and closing line value ------------------------------------

def opening_line(g):
    hist = g.get("lineHistory") or []
    return hist[0] if hist else None


def closing_line(g):
    """The last line seen before kickoff. Runs land Tue/Thu/Sat morning, so
    this is the final number the site saw, not a true post-close capture."""
    hist = g.get("lineHistory") or []
    if hist:
        return hist[-1]
    ln = g.get("lines") or {}
    if not ln:
        return None
    return {"at": g.get("lockedAt"), "spread": ln.get("spread"), "total": ln.get("total"),
            "mlHome": ln.get("mlHome"), "mlAway": ln.get("mlAway")}


def clv_for(bet_type, side, line, price, close):
    """Points (or cents of win probability) gained or given up between the
    number we posted and the last number seen. Positive means the market
    moved toward our side after we published the play."""
    if not close:
        return None
    if bet_type == "spread":
        cs = close.get("spread")
        if not isinstance(cs, (int, float)) or not isinstance(line, (int, float)):
            return None
        close_side = cs if side == "home" else -cs
        return {"pts": round(line - close_side, 1), "closeLine": close_side,
                "at": close.get("at")}
    if bet_type == "total":
        ct = close.get("total")
        if not isinstance(ct, (int, float)) or not isinstance(line, (int, float)):
            return None
        pts = (ct - line) if side == "over" else (line - ct)
        return {"pts": round(pts, 1), "closeLine": ct, "at": close.get("at")}
    if bet_type == "moneyline":
        cp = close.get("mlHome") if side == "home" else close.get("mlAway")
        if not isinstance(cp, (int, float)) or not cp or not price:
            return None
        return {"probPts": round((implied_prob(cp) - implied_prob(price)) * 100, 1),
                "closePrice": cp, "at": close.get("at")}
    return None


def board_index(boards):
    return {str(g["id"]): g for wk in boards["weeks"].values() for g in wk["games"].values()}


def attach_clv(ledger, boards):
    """Score each posted pick against the last line seen before kickoff."""
    games = board_index(boards)
    for p in ledger["picks"]:
        g = games.get(str(p["gameId"]))
        if not g:
            continue
        line = p.get("openLine", p.get("line"))
        price = p.get("openPrice", p.get("price"))
        p["clv"] = clv_for(p["type"], p.get("side"), line, price, closing_line(g))


def clv_value(clv):
    """One comparable number per pick: points for spreads and totals,
    probability points for moneylines. Different units, but the sign is
    what matters."""
    if not clv:
        return None
    return clv.get("pts") if clv.get("pts") is not None else clv.get("probPts")


def clv_summary(items):
    vals = [clv_value(i.get("clv")) for i in items]
    vals = [v for v in vals if v is not None]
    moved = [v for v in vals if v != 0]
    if not vals:
        return {"n": 0, "moved": 0, "beat": 0, "beatRate": None, "avgPts": None}
    beat = sum(1 for v in moved if v > 0)
    return {"n": len(vals), "moved": len(moved), "beat": beat,
            "beatRate": round(beat / len(moved), 4) if moved else None,
            "avgPts": round(sum(vals) / len(vals), 2)}


# --- season record across every value play -----------------------------------

def new_tally(label=None):
    t = {"plays": 0, "wins": 0, "losses": 0, "pushes": 0,
         "profit": 0.0, "staked": 0.0, "_prob": 0.0}
    if label:
        t["label"] = label
    return t


def add_play(t, c):
    key = {"win": "wins", "loss": "losses", "push": "pushes"}[c["outcome"]]
    t["plays"] += 1
    t[key] += 1
    t["profit"] += c["profit"]
    t["staked"] += STAKE
    t["_prob"] += c.get("prob") or 0.0


def close_tally(t):
    decided = t["wins"] + t["losses"]
    t["winRate"] = round(t["wins"] / decided, 4) if decided else None
    t["expected"] = round(t["_prob"] / t["plays"], 4) if t["plays"] else None
    t["profit"] = round(t["profit"], 2)
    t["roi"] = round(t["profit"] / t["staked"], 4) if t["staked"] else None
    t["units"] = round(t["profit"] / STAKE, 2)
    t.pop("_prob", None)
    return t


def ev_tier(ev):
    for floor, label in EV_TIERS:
        if ev >= floor:
            return label
    return EV_TIERS[-1][1]


def graded_weeks(boards):
    """Weeks with at least one finished game, oldest first."""
    def order(wk):
        return (wk["season"], 0 if wk["seasonType"] == "regular" else 1, wk["week"])
    done = [wk for wk in boards["weeks"].values()
            if any(g.get("result") for g in wk["games"].values())]
    return sorted(done, key=order)


def week_plays(wk):
    """(graded call, its game) for every value play in a finished week."""
    out = []
    for g in sorted(wk["games"].values(), key=lambda x: (x.get("startDate") or "")):
        res = g.get("result")
        if not res:
            continue
        for c in res.get("calls", []):
            out.append((c, g))
    return out


def build_model_record(boards, ledger, now, demo=False):
    """Every value play the model has made this season, graded.

    This is the wide sample: 15-20 plays a week across the whole board
    versus 5 for the published Top 5. Games where the model found no edge
    count as no-plays and never enter the record.
    """
    overall = new_tally()
    hindsight = new_tally()
    by_type = {t: new_tally(t) for t in ("spread", "total", "moneyline")}
    by_ev = {label: new_tally(label) for _, label in EV_TIERS}
    cal = {label: new_tally(label) for _, _, label in CAL_BUCKETS}
    weeks = []

    for wk in graded_weeks(boards):
        backfilled = bool(wk.get("backfilled"))
        wt = new_tally()
        finished = [g for g in wk["games"].values() if g.get("result")]
        plays = week_plays(wk)
        for c, g in plays:
            add_play(wt, c)
            if backfilled:
                add_play(hindsight, c)
                continue
            add_play(overall, c)
            add_play(by_type[c["type"]], c)
            add_play(by_ev[ev_tier(c.get("ev") or 0)], c)
            for lo, hi, label in CAL_BUCKETS:
                if lo <= (c.get("prob") or 0) < hi:
                    add_play(cal[label], c)
                    break
        played = len({g["id"] for _, g in plays})
        weeks.append({
            "season": wk["season"], "seasonType": wk["seasonType"], "week": wk["week"],
            "backfilled": wk.get("backfilled", False),
            "gamesGraded": len(finished),
            "gamesWithPlay": played,
            "noPlay": len(finished) - played,
            **close_tally(wt),
        })

    clean_weeks = {(w["season"], w["seasonType"], w["week"]) for w in weeks
                   if not w["backfilled"]}
    graded_picks = [p for p in ledger["picks"] if p.get("result")
                    and (p["season"], p["seasonType"], p["week"]) in clean_weeks]
    top = new_tally()
    for p in graded_picks:
        add_play(top, {"outcome": p["result"]["outcome"],
                       "profit": p["result"]["profit"], "prob": p.get("prob")})

    return {
        "generatedAt": now.isoformat(),
        "demo": demo,
        "stake": STAKE,
        "thresholds": {"spread": MIN_EDGE_SPREAD, "total": MIN_EDGE_TOTAL,
                       "moneylineProb": MIN_EDGE_ML_PROB},
        "overall": close_tally(overall),
        "hindsight": close_tally(hindsight),
        "byType": [close_tally(by_type[t]) for t in ("spread", "total", "moneyline")],
        "byEv": [close_tally(by_ev[label]) for _, label in EV_TIERS],
        "calibration": [close_tally(cal[label]) for _, _, label in CAL_BUCKETS],
        "byWeek": weeks,
        "topFive": close_tally(top),
        "clv": clv_summary(graded_picks),
    }


# --- Sunday narrative ---------------------------------------------------------

def rec_str(t):
    """8-5 or 8-5-1."""
    base = f"{t['wins']}-{t['losses']}"
    return base + (f"-{t['pushes']}" if t["pushes"] else "")


def money(x):
    if x is None:
        return "even"
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


def pct_str(x, places=0):
    return "n/a" if x is None else f"{x * 100:.{places}f}%"


def money0(x):
    """Whole dollars: $10, not $10.00."""
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def article(n):
    """a 5-point edge, an 8-point edge."""
    head = f"{abs(n):.0f}".lstrip("0") or "0"
    return "an" if head[0] == "8" or head in ("11", "18") else "a"


def plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def describe_edge(c):
    e = abs(c.get("edgePts") or 0)
    if c["type"] == "moneyline":
        return f"{article(e)} {e:.0f}-point gap between the model and the price"
    return f"a {e:.1f}-point disagreement with the market"


def final_str(g):
    r = g.get("result") or {}
    h, a = r.get("homeScore"), r.get("awayScore")
    if h is None or a is None:
        return ""
    if a > h:
        return f"{g['awayTeam']} {a}, {g['homeTeam']} {h}"
    return f"{g['homeTeam']} {h}, {g['awayTeam']} {a}"


def matchup_str(g):
    return f"{g['awayTeam']} at {g['homeTeam']}"


def week_label_str(wk):
    if wk.get("seasonType") == "postseason":
        return "the postseason slate"
    return f"Week {wk['week']}"


def build_narrative(boards, ledger, record, now):
    """A short written read on the most recent completed week.

    Generated from the graded board, so it never claims anything the data
    does not support. Deterministic by design: the Sunday workflow run has
    no model available to write prose, and a wrong-but-fluent recap would
    be worse than a plain one.
    """
    weeks = graded_weeks(boards)
    if not weeks:
        return None
    wk = weeks[-1]
    plays = week_plays(wk)
    finished = [g for g in wk["games"].values() if g.get("result")]
    label = week_label_str(wk)

    wt = new_tally()
    types = {t: new_tally() for t in ("spread", "total", "moneyline")}
    for c, g in plays:
        add_play(wt, c)
        add_play(types[c["type"]], c)
    close_tally(wt)
    for t in types.values():
        close_tally(t)

    paras = []

    # --- 1: what it played and how it did ---
    played_games = len({g["id"] for _, g in plays})
    skipped = len(finished) - played_games
    if not plays:
        paras.append(
            f"{label} came and went without a single playable edge. The model "
            f"graded {len(finished)} games and found nothing clearing the "
            f"thresholds, so it sat the week out. That happens, and it beats "
            f"manufacturing a play to have something to say.")
    else:
        openers = [
            f"{label} put {len(finished)} graded games in front of the model.",
            f"The model looked at {len(finished)} games in {label}.",
            f"{len(finished)} games finished in {label}.",
        ]
        opener = openers[(wk.get("week") or 0) % len(openers)]
        result_clause = (
            f"It found a playable edge in {played_games} of them and left the "
            f"other {skipped} alone. Those plays went {rec_str(wt)}"
        ) if skipped else (
            f"It found a playable edge in every one of them, and those plays "
            f"went {rec_str(wt)}"
        )
        pl = (f", worth {money(wt['profit'])} at {money0(STAKE)} a play "
              f"({pct_str(wt['roi'], 1)} on the {money0(wt['staked'])} at risk)."
              if wt["staked"] else ".")
        paras.append(f"{opener} {result_clause}{pl}")

    # --- 2: where it came from, plus the best and worst call ---
    if plays:
        bits = [f"{name}s went {rec_str(t)}" if t["plays"] != 1
                else f"the lone {name} {'won' if t['wins'] else ('pushed' if t['pushes'] else 'lost')}"
                for name, t in (("spread", types["spread"]), ("total", types["total"]),
                                ("moneyline", types["moneyline"])) if t["plays"]]
        by_type_line = ""
        if len(bits) > 1:
            by_type_line = "By market, " + ", ".join(bits[:-1]) + " and " + bits[-1] + ". "
        elif bits:
            by_type_line = "Every play was the same market: " + bits[0] + ". "

        wins = [(c, g) for c, g in plays if c["outcome"] == "win"]
        losses = [(c, g) for c, g in plays if c["outcome"] == "loss"]
        detail = ""
        def where(c, g):
            # the moneyline pick already names the team; don't say it twice
            return "" if c["type"] == "moneyline" else f" in {matchup_str(g)}"

        if wins:
            c, g = max(wins, key=lambda x: x[0].get("ev") or 0)
            detail += (f"The best of them was {c['pick']}{where(c, g)}, "
                       f"{describe_edge(c)} that finished {final_str(g)}. ")
        if losses:
            c, g = max(losses, key=lambda x: x[0].get("ev") or 0)
            detail += (f"The one that stung was {c['pick']}{where(c, g)}, "
                       f"{describe_edge(c)} that finished {final_str(g)}.")
        if by_type_line or detail:
            paras.append((by_type_line + detail).strip())

    # --- 3: the published Top 5 against the full board ---
    wk_picks = [p for p in ledger["picks"]
                if p.get("result") and p["season"] == wk["season"]
                and p["seasonType"] == wk["seasonType"] and p["week"] == wk["week"]]
    if wk_picks and plays:
        tp = new_tally()
        for p in wk_picks:
            add_play(tp, {"outcome": p["result"]["outcome"],
                          "profit": p["result"]["profit"], "prob": p.get("prob")})
        close_tally(tp)
        cmp_word = ("ahead of" if (tp["winRate"] or 0) > (wt["winRate"] or 0)
                    else "behind" if (tp["winRate"] or 0) < (wt["winRate"] or 0)
                    else "in line with")
        paras.append(
            f"The five plays that actually got published went {rec_str(tp)} for "
            f"{money(tp['profit'])}, {cmp_word} the full board. Ranking by expected "
            f"value is supposed to concentrate the good ones at the top, and "
            f"whether it does is something only a season of these will answer.")

    # --- 4: season to date, closing line value, and the honest caveat ---
    ov = record["overall"]
    tail = []
    if ov["plays"]:
        tail.append(
            f"Season to date the model has made {ov['plays']} value "
            f"{plural(ov['plays'], 'play')} and gone {rec_str(ov)}, {money(ov['profit'])} "
            f"on flat {money0(STAKE)} bets ({pct_str(ov['roi'], 1)}).")
        if ov["winRate"] is not None and ov["expected"] is not None:
            tail.append(
                f"It expected to win {pct_str(ov['expected'])} of those and won "
                f"{pct_str(ov['winRate'])}.")
    clv = record.get("clv") or {}
    if clv.get("moved"):
        tail.append(
            f"On line movement, {clv['beat']} of the {clv['moved']} published picks "
            f"whose numbers moved closed worse than the site posted them, so the "
            f"market came our way {pct_str(clv['beatRate'])} of the time.")
    n = ov["plays"]
    if n and n < 60:
        tail.append(
            f"Treat all of it as early. At {n} {plural(n, 'play')} the record is "
            f"still mostly noise, and the thing worth watching is not the win rate "
            f"but whether the market keeps moving toward these numbers.")
    elif n:
        tail.append(
            "Sample is getting real enough to argue with, though a model that beats "
            "closing lines is rarer than one that beats a few hundred results.")
    if tail:
        paras.append(" ".join(tail))

    if wk.get("backfilled"):
        paras.append(
            "None of this counts toward the season record. The week was graded "
            "after the fact, with ratings that already knew how the games turned "
            "out, so it shows the format rather than testing the model. The real "
            "ledger starts with the first week called before kickoff.")

    return {
        "generatedAt": now.isoformat(),
        "source": "builtin",
        "season": wk["season"], "seasonType": wk["seasonType"], "week": wk["week"],
        "headline": (f"{rec_str(wt)} on {wt['plays']} value "
                     f"{plural(wt['plays'], 'play')}, {money(wt['profit'])}"
                     if wt["plays"] else "No playable edges this week"),
        "paragraphs": paras,
        "week_tally": wt,
    }


# --- facts for the written recap ---------------------------------------------

def facts_week_label(wk):
    return "Bowls/Playoff" if wk.get("seasonType") == "postseason" else f"Week {wk['week']}"


def pct_num(x, places=1):
    return None if x is None else round(x * 100, places)


def tally_facts(t):
    return {"plays": t["plays"], "record": rec_str(t) if t["plays"] else None,
            "wins": t["wins"], "losses": t["losses"], "pushes": t["pushes"],
            "profit": t["profit"], "staked": t["staked"],
            "winRatePct": pct_num(t["winRate"]), "roiPct": pct_num(t["roi"]),
            "modelExpectedPct": pct_num(t["expected"])}


def drift_pts(call, g):
    """Points the market moved toward this call's side between the week's
    opening number and the last one seen. Mirrors the site's drift column."""
    hist = g.get("lineHistory") or []
    if len(hist) < 2:
        return None
    o, c = hist[0], hist[-1]
    t, side = call["type"], call.get("side")
    if t == "spread":
        if o.get("spread") is None or c.get("spread") is None:
            return None
        frm = o["spread"] if side == "home" else -o["spread"]
        to = c["spread"] if side == "home" else -c["spread"]
        return round(frm - to, 1)
    if t == "total":
        if o.get("total") is None or c.get("total") is None:
            return None
        return round((c["total"] - o["total"]) if side == "over"
                     else (o["total"] - c["total"]), 1)
    if t == "moneyline":
        frm = o.get("mlHome") if side == "home" else o.get("mlAway")
        to = c.get("mlHome") if side == "home" else c.get("mlAway")
        if not frm or not to:
            return None
        return round((implied_prob(to) - implied_prob(frm)) * 100, 1)
    return None


def play_facts(c, g):
    r = g.get("result") or {}
    return {
        "matchup": f"{g['awayTeam']} at {g['homeTeam']}",
        "pick": c["pick"],
        "market": c["type"],
        "edge": c.get("edgePts"),
        "edgeUnit": "probability points" if c["type"] == "moneyline" else "points",
        "modelProbPct": pct_num(c.get("prob"), 0),
        "outcome": c["outcome"],
        "profit": c["profit"],
        "finalScore": final_str(g),
    }


def narrative_facts(boards, ledger, record, now):
    """Everything the writer is allowed to know, and nothing else.

    Any number absent from here is a number the recap cannot use, so this
    doubles as the whitelist the output is checked against.
    """
    weeks = graded_weeks(boards)
    if not weeks:
        return None
    wk = weeks[-1]
    plays = week_plays(wk)
    finished = [g for g in wk["games"].values() if g.get("result")]

    wt = new_tally()
    types = {t: new_tally() for t in ("spread", "total", "moneyline")}
    for c, g in plays:
        add_play(wt, c)
        add_play(types[c["type"]], c)
    close_tally(wt)
    for t in types.values():
        close_tally(t)

    wins = [(c, g) for c, g in plays if c["outcome"] == "win"]
    losses = [(c, g) for c, g in plays if c["outcome"] == "loss"]
    notable = {}
    if wins:
        c, g = max(wins, key=lambda x: x[0].get("ev") or 0)
        notable["bestCall"] = play_facts(c, g)
    if losses:
        c, g = max(losses, key=lambda x: x[0].get("ev") or 0)
        notable["worstCall"] = play_facts(c, g)

    moved = [d for d in (drift_pts(c, g) for c, g in plays) if d]
    drift = None
    if moved:
        our_way = sum(1 for d in moved if d > 0)
        drift = {"playsWhoseLineMoved": len(moved), "movedTowardTheModel": our_way,
                 "movedTowardTheModelPct": round(our_way / len(moved) * 100, 1)}

    wk_picks = [p for p in ledger["picks"]
                if p.get("result") and p["season"] == wk["season"]
                and p["seasonType"] == wk["seasonType"] and p["week"] == wk["week"]]
    published = None
    if wk_picks:
        tp = new_tally()
        for p in wk_picks:
            add_play(tp, {"outcome": p["result"]["outcome"],
                          "profit": p["result"]["profit"], "prob": p.get("prob")})
        published = tally_facts(close_tally(tp))

    ov = record["overall"]
    hs = record.get("hindsight") or {}
    season = {"weeksGradedForReal": sum(1 for w in record["byWeek"] if not w["backfilled"]),
              **tally_facts(ov)}
    if hs.get("plays"):
        season["excludedHindsightWeek"] = tally_facts(hs)

    return {
        "stakePerPlay": STAKE,
        "week": {
            "season": wk["season"],
            "label": facts_week_label(wk),
            "gradedInHindsight": bool(wk.get("backfilled")),
            "gamesGraded": len(finished),
            "gamesWithAPlay": len({g["id"] for _, g in plays}),
            "gamesPassedOn": len(finished) - len({g["id"] for _, g in plays}),
            **tally_facts(wt),
        },
        "weekByMarket": [{"market": name, **tally_facts(t)}
                         for name, t in types.items() if t["plays"]],
        "notable": notable,
        "publishedTopFiveThisWeek": published,
        "marketDrift": drift,
        "season": season,
        "everyPlay": [play_facts(c, g) for c, g in plays],
    }


# --- recap store: generate once per graded week ------------------------------

def facts_hash(facts):
    blob = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_recap_store():
    if RECAP_PATH.exists():
        try:
            data = json.loads(RECAP_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"could not read recap store ({e})", file=sys.stderr)
    return {}


def save_recap_store(store):
    RECAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECAP_PATH.write_text(json.dumps(store, indent=1), encoding="utf-8")


def compose_narrative(boards, ledger, record, now):
    """The deterministic recap, upgraded to a written one when Claude is
    available. Generated once per graded week: the Sunday read should not
    quietly reword itself on Tuesday, and there is no reason to pay for the
    same paragraphs four times."""
    builtin = build_narrative(boards, ledger, record, now)
    if not builtin:
        return None
    builtin["source"] = "builtin"

    facts = narrative_facts(boards, ledger, record, now)
    if not facts:
        return builtin

    wk = graded_weeks(boards)[-1]
    wkey = week_key(wk["season"], wk["seasonType"], wk["week"])
    fhash = facts_hash(facts)
    store = load_recap_store()

    if store.get("factsHash") == fhash and store.get("paragraphs"):
        return {**builtin, "headline": store["headline"],
                "paragraphs": store["paragraphs"],
                "source": store.get("source", "claude"),
                "writtenAt": store.get("generatedAt")}

    written, why = None, None
    try:
        import recap
        written = recap.write_recap(facts, store.get("recent"))
        why = None if written else recap.LAST_ERROR
    except Exception as e:                      # noqa: BLE001 - never fail the build
        why = f"{type(e).__name__}: {e}"
        print(f"recap: builtin ({why})", file=sys.stderr)

    chosen = {**builtin, **written} if written else builtin

    recent = [r for r in (store.get("recent") or []) if r.get("weekKey") != wkey]
    recent.append({"weekKey": wkey, "label": facts["week"]["label"],
                   "headline": chosen["headline"],
                   "opening": chosen["paragraphs"][0][:160] if chosen["paragraphs"] else ""})
    save_recap_store({
        "weekKey": wkey,
        # only pin the hash on a real write, so a transient failure retries
        "factsHash": fhash if written else None,
        "source": chosen["source"],
        # committed by the workflow, so a failure is readable without the log
        "lastError": why,
        "generatedAt": now.isoformat(),
        "headline": chosen["headline"],
        "paragraphs": chosen["paragraphs"],
        "recent": recent[-3:],
    })
    return chosen


def write_lastweek(boards, current_key, now, demo=False, narrative=None):
    """Publish the most recent completed week (excluding the current one)."""
    def order(wk):
        return (wk["season"], 0 if wk["seasonType"] == "regular" else 1, wk["week"])

    done = [wk for k, wk in boards["weeks"].items()
            if k != current_key and any(g.get("result") for g in wk["games"].values())]
    payload = {"generatedAt": now.isoformat(), "demo": demo, "stake": STAKE,
               "narrative": narrative, "week": None}
    if done:
        wk = max(done, key=order)
        games = sorted(wk["games"].values(), key=lambda g: (g.get("startDate") or "", g["homeTeam"]))
        payload["week"] = {"season": wk["season"], "seasonType": wk["seasonType"],
                           "week": wk["week"], "backfilled": wk.get("backfilled", False),
                           "games": games}
    LASTWEEK_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def update_week_picks(ledger, top, season, season_type, week, now):
    """Record this week's top picks. Picks lock at kickoff; unlocked picks
    are replaced by the latest run's board so the ledger reflects the last
    line before the game."""
    key = (season, season_type, week)

    def wk(p):
        return (p["season"], p["seasonType"], p["week"])

    others = [p for p in ledger["picks"] if wk(p) != key]
    current = [p for p in ledger["picks"] if wk(p) == key]
    locked = [p for p in current if p.get("result") or pick_started(p, now)]
    locked_games = {p["gameId"] for p in locked}
    # the number a reader would have gotten the first time we published this
    # play, kept across refreshes so closing line value means something
    prior = {(p["gameId"], p["type"], p.get("side")): p for p in current}

    fresh = []
    for c in top:
        if len(locked) + len(fresh) >= TOP_N:
            break
        if c["gameId"] in locked_games:
            continue
        was = prior.get((c["gameId"], c["type"], c.get("side")))
        fresh.append({
            "season": season, "seasonType": season_type, "week": week,
            "gameId": c["gameId"], "matchup": c["matchup"],
            "homeTeam": c.get("homeTeam"), "awayTeam": c.get("awayTeam"),
            "startDate": c["startDate"], "type": c["type"], "pick": c["pick"],
            "side": c.get("side"), "line": c.get("line"), "price": c["price"],
            "prob": c["prob"], "ev": c["ev"], "detail": c.get("detail"),
            "pickedAt": datetime.now(timezone.utc).isoformat(),
            "openLine": was.get("openLine", was.get("line")) if was else c.get("line"),
            "openPrice": was.get("openPrice", was.get("price")) if was else c["price"],
            "openAt": (was.get("openAt", was.get("pickedAt")) if was
                       else datetime.now(timezone.utc).isoformat()),
            "result": None,
        })
    for p in locked:
        p.setdefault("openLine", p.get("line"))
        p.setdefault("openPrice", p.get("price"))
        p.setdefault("openAt", p.get("pickedAt"))
    ledger["picks"] = others + locked + fresh


def write_results(ledger, now, demo=False):
    def order(p):
        return (p["season"], 0 if p["seasonType"] == "regular" else 1,
                p["week"], p.get("startDate") or "")

    payload = {
        "generatedAt": now.isoformat(),
        "demo": demo,
        "stake": STAKE,
        "picks": sorted(ledger["picks"], key=order),
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")


# --- demo fallback ------------------------------------------------------------

def demo_payload(now):
    demo_games = []
    fixtures = [
        ("Georgia", "Alabama", -3.5, 52.5, -165, +140, 4.8, 55.9),
        ("Ohio State", "Penn State", -6.5, 48.5, -240, +198, 9.9, 45.2),
        ("Texas", "Oklahoma", -7.0, 56.0, -260, +215, 4.1, 59.3),
        ("Oregon", "Washington", -9.5, 61.5, -340, +270, 6.3, 64.8),
        ("Kansas State", "Iowa State", -2.5, 44.5, -135, +115, 5.6, 41.0),
        ("Michigan", "USC", 1.5, 51.0, +105, -125, -1.9, 54.5),
        ("Notre Dame", "Clemson", -4.5, 47.5, -190, +160, 3.2, 44.1),
        ("LSU", "Ole Miss", -1.5, 63.5, -120, +100, 4.4, 66.2),
    ]
    for i, (home, away, spread, total, mlh, mla, m_margin, m_total) in enumerate(fixtures):
        lines = {
            "books": [{"provider": "DraftKings", "spread": spread, "overUnder": total,
                       "homeMoneyline": mlh, "awayMoneyline": mla,
                       "formattedSpread": f"{home} {spread:+g}"}],
            "spread": spread, "total": total, "mlHome": mlh, "mlAway": mla,
        }
        game = {
            "id": 1000 + i, "homeTeam": home, "awayTeam": away,
            "homeClassification": "fbs", "awayClassification": "fbs",
            "homeConference": "-", "awayConference": "-",
            "startDate": now.isoformat(), "neutralSite": False, "venue": "Demo Stadium",
        }
        ratings = {
            home: {"sp": m_margin + 8, "fpi": m_margin + 7.5, "elo": 1500 + (m_margin + 8) * 28,
                   "srs": m_margin + 8, "sp_off": m_total / 2 + m_margin / 2 + 2,
                   "sp_def": m_total / 2 - m_margin / 2 - 2},
            away: {"sp": 8 + 2.3, "fpi": 8 + 2.3, "elo": 1500 + (8 + 2.3) * 28,
                   "srs": 8 + 2.3, "sp_off": m_total / 2, "sp_def": m_total / 2},
        }
        demo_games.append(evaluate_game(
            game, ratings, 1500.0, {"off": 30.0, "def": 27.0}, {}, {1000 + i: lines}))
    return demo_games


def demo_ledger(payload, now, boards=None):
    """Fake two graded weeks plus the current demo picks, so results.html renders."""
    season = payload["season"]
    samples = [
        # week 1
        ("Utah @ BYU", "BYU", "Utah", "spread", "BYU -3.5", "home", -3.5, -110, 24, 20),
        ("Auburn @ Vanderbilt", "Vanderbilt", "Auburn", "moneyline", "Vanderbilt ML +180", "home", None, 180, 31, 27),
        ("Duke @ Cal", "Cal", "Duke", "total", "Under 51.5", "under", 51.5, -110, 21, 17),
        ("Rice @ Navy", "Navy", "Rice", "spread", "Navy -7.5", "home", -7.5, -110, 28, 24),
        ("Toledo @ Kentucky", "Kentucky", "Toledo", "spread", "Toledo +11.5", "away", 11.5, -110, 30, 13),
        # week 2
        ("Tulsa @ SMU", "SMU", "Tulsa", "spread", "SMU -13.5", "home", -13.5, -110, 38, 17),
        ("Akron @ Ohio", "Ohio", "Akron", "total", "Over 44.5", "over", 44.5, -110, 31, 24),
        ("Nevada @ UNLV", "UNLV", "Nevada", "moneyline", "UNLV ML -145", "home", None, -145, 27, 30),
        ("Troy @ Memphis", "Memphis", "Troy", "spread", "Troy +9.5", "away", 9.5, -110, 35, 28),
        ("Buffalo @ Kent State", "Kent State", "Buffalo", "total", "Under 47.5", "under", 47.5, -110, 24, 23),
    ]
    ledger = {"picks": []}
    for i, (matchup, home, away, btype, bpick, side, line, price, hp, ap) in enumerate(samples):
        week = 1 if i < 5 else 2
        p = {"season": season, "seasonType": "regular", "week": week,
             "gameId": 9000 + i, "matchup": matchup, "homeTeam": home, "awayTeam": away,
             "startDate": now.isoformat(), "type": btype, "pick": bpick,
             "side": side, "line": line, "price": price, "prob": 0.6, "ev": 0.1,
             "detail": "", "pickedAt": now.isoformat(), "result": None}
        p["result"] = grade_one(p, hp, ap)
        ledger["picks"].append(p)
    if boards:
        # graded value plays off the demo board, posted a point off the close
        wk = boards["weeks"][week_key(season, "regular", -1)]
        added = 0
        for g in wk["games"].values():
            res = g.get("result") or {}
            for c in res.get("calls", []):
                if added >= 5:
                    break
                line = c.get("line")
                ledger["picks"].append({
                    "season": season, "seasonType": "regular", "week": -1,
                    "gameId": g["id"], "matchup": f"{g['awayTeam']} @ {g['homeTeam']}",
                    "homeTeam": g["homeTeam"], "awayTeam": g["awayTeam"],
                    "startDate": g["startDate"], "type": c["type"], "pick": c["pick"],
                    "side": c.get("side"), "line": line, "price": c["price"],
                    "prob": c["prob"], "ev": c["ev"], "detail": "",
                    "pickedAt": now.isoformat(),
                    "openLine": (line + 1.0) if isinstance(line, (int, float)) else None,
                    "openPrice": c["price"], "openAt": now.isoformat(),
                    "result": {"homeScore": res["homeScore"], "awayScore": res["awayScore"],
                               "outcome": c["outcome"], "profit": c["profit"]},
                })
                added += 1
            if added >= 5:
                break
    update_week_picks(ledger, payload["topPicks"], season, "regular", 0, now)
    return ledger


def demo_boards(payload, now):
    """Fake 'last week' board: the demo games with made-up finals."""
    boards = {"weeks": {}}
    record_board(boards, payload["games"], payload["season"], "regular", -1, now)
    finals = [(31, 24), (21, 27), (38, 20), (45, 42), (17, 20), (28, 31), (24, 10), (35, 38)]
    games = boards["weeks"][week_key(payload["season"], "regular", -1)]["games"].values()
    for i, ((hp, ap), g) in enumerate(zip(finals, games)):
        # fake an opening capture so line movement has something to show
        close = (g.get("lineHistory") or [None])[-1]
        if close:
            drift = 1.5 if i % 3 == 0 else (-1.0 if i % 3 == 1 else 0.0)
            g["lineHistory"] = [{
                "at": now.isoformat(),
                "spread": (close["spread"] + drift) if close.get("spread") is not None else None,
                "total": (close["total"] - drift) if close.get("total") is not None else None,
                "mlHome": close.get("mlHome"), "mlAway": close.get("mlAway"),
            }, close]
        g["result"] = grade_game(g, hp, ap)
    return boards


# --- main ---------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    payload = {
        "generatedAt": now.isoformat(),
        "demo": False,
        "season": None, "week": None, "seasonType": None,
        "games": [], "topPicks": [],
        "modelInfo": {
            "weights": WEIGHTS, "homeField": HOME_FIELD_PTS,
            "sigmaAts": SIGMA_ATS, "sigmaMl": SIGMA_ML,
        },
    }

    if not API_KEY:
        print("CFBD_API_KEY not set - writing DEMO data", file=sys.stderr)
        payload["demo"] = True
        payload["season"] = current_season(now)
        payload["week"] = 0
        payload["seasonType"] = "regular"
        payload["games"] = demo_payload(now)
        payload["topPicks"] = top_picks(payload["games"])
        boards = demo_boards(payload, now)
        attach_line_history(boards, payload["games"], payload["season"], "regular", -1)
        ledger = demo_ledger(payload, now, boards)
        attach_clv(ledger, boards)
        record = build_model_record(boards, ledger, now, demo=True)
        RECORD_PATH.write_text(json.dumps(record, indent=1), encoding="utf-8")
        write_results(ledger, now, demo=True)
        write_lastweek(boards, "current", now, demo=True,
                       narrative=build_narrative(boards, ledger, record, now))
    else:
        year = current_season(now)
        weeks = calendar_weeks(year)
        season_type, week, games, prev_week = fetch_upcoming_week(year, now, weeks)
        print(f"Season {year}, {season_type} week {week}: {len(games)} upcoming games")

        ratings, elo_mean, sp_means = fetch_ratings(year)
        records = fetch_records(year)
        lines = fetch_lines(year, week, season_type)

        evaluated = [evaluate_game(g, ratings, elo_mean, sp_means, records, lines)
                     for g in games]
        evaluated = [e for e in evaluated if e["homeTeam"] and e["awayTeam"]]
        evaluated.sort(key=lambda e: (e["startDate"] or "", e["homeTeam"]))

        payload.update({"season": year, "week": week, "seasonType": season_type,
                        "games": evaluated, "topPicks": top_picks(evaluated)})

        ledger = load_ledger()
        boards = load_boards()
        record_board(boards, evaluated, year, season_type, week, now)
        attach_line_history(boards, evaluated, year, season_type, week)
        backfill_previous_week(boards, prev_week, year, now,
                               ratings, elo_mean, sp_means, records)

        scores = fetch_scores(pending_week_keys(ledger, boards, now))
        graded = grade_picks(ledger, scores, now)
        graded_games = grade_boards(boards, scores, now)
        regraded = regrade_boards(boards)
        update_week_picks(ledger, payload["topPicks"], year, season_type, week, now)
        attach_clv(ledger, boards)

        record = build_model_record(boards, ledger, now)
        try:
            narrative = compose_narrative(boards, ledger, record, now)
        except Exception as e:                  # noqa: BLE001 - prose is optional
            import traceback
            traceback.print_exc()
            print(f"recap: builtin (compose crashed: {type(e).__name__}: {e})", file=sys.stderr)
            narrative = build_narrative(boards, ledger, record, now)

        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
        BOARDS_PATH.write_text(json.dumps(boards, indent=1), encoding="utf-8")
        RECORD_PATH.write_text(json.dumps(record, indent=1), encoding="utf-8")
        write_results(ledger, now)
        write_lastweek(boards, week_key(year, season_type, week), now, narrative=narrative)
        print(f"Ledger: {len(ledger['picks'])} picks total, {graded} newly graded; "
              f"board games graded this run: {graded_games}, regraded: {regraded}")
        ov = record["overall"]
        print(f"Season value record: {ov['wins']}-{ov['losses']}-{ov['pushes']} "
              f"on {ov['plays']} plays, {ov['profit']:+.2f}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"Wrote {OUT_PATH} - {len(payload['games'])} games, "
          f"{len(payload['topPicks'])} top picks{' (DEMO)' if payload['demo'] else ''}")


if __name__ == "__main__":
    main()
