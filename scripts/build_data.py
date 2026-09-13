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


def fetch_upcoming_week(year, now):
    """Find the next week with games still to play.

    Walks the calendar forward from today and returns
    (seasonType, week, upcoming_games). A week whose games have all been
    played (e.g. it's Sunday and the calendar week hasn't rolled over yet)
    is skipped in favor of the next one. If the season is over, returns the
    final week's full slate as played.
    """
    weeks = calendar_weeks(year)
    if not weeks:
        return "regular", 1, api_get("/games", year=year, week=1,
                                     seasonType="regular", classification="fbs")
    candidates = [w for w in weeks if w[1] >= now] or weeks[-1:]
    last_fetch = None
    for _, _, season_type, week in candidates[:3]:
        games = api_get("/games", year=year, week=week,
                        seasonType=season_type, classification="fbs")
        last_fetch = (season_type, week, games)
        upcoming = [g for g in games if is_upcoming(g, now)]
        if upcoming:
            return season_type, week, upcoming
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
            return statistics.median(vals) if vals else None

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
            "edgePts": round(abs(edge), 1),
            "prob": round(p_cover, 3),
            "price": ASSUMED_SPREAD_PRICE,
            "ev": round(ev, 4),
            "eligible": abs(edge) >= MIN_EDGE_SPREAD,
            "detail": f"Model: {home} by {model_margin:+.1f} vs market {vegas_margin:+.1f}",
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
    else:
        year = current_season(now)
        season_type, week, games = fetch_upcoming_week(year, now)
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

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"Wrote {OUT_PATH} - {len(payload['games'])} games, "
          f"{len(payload['topPicks'])} top picks{' (DEMO)' if payload['demo'] else ''}")


if __name__ == "__main__":
    main()
