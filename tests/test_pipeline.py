"""End-to-end check of the live build, without the CFBD API.

Runs scripts/build_data.py's real (non-demo) path against a fake CFBD API and
a frozen clock, walking through one full week the way the scheduled runs see it:

  1. Saturday morning - week 12 is upcoming; picks are posted
  2. Sunday           - week 12 is final, the site rolls forward to week 13
                        (the first run of a new week: no stored board yet)
  3. Tuesday          - week 13 again, now with its board on file

Every run must finish, and the Sunday run must grade Saturday's picks.
Each run starts from the files the previous one wrote, like production.

Run:  python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_data as bd  # noqa: E402

YEAR = 2026
SAT_KICK = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
NEXT_KICK = SAT_KICK + timedelta(days=7)

RUN_SAT = SAT_KICK - timedelta(hours=8)                # Sat 12:00 UTC
RUN_SUN = SAT_KICK + timedelta(hours=12)               # Sun 08:00 UTC
RUN_TUE = SAT_KICK + timedelta(days=2, hours=19)       # Tue 15:00 UTC

# (home, away, home spread, total, ml home, ml away, model margin, model total)
FIXTURES = [
    ("Georgia", "Alabama", -3.5, 52.5, -165, +140, 4.8, 55.9),
    ("Ohio State", "Penn State", -6.5, 48.5, -240, +198, 9.9, 45.2),
    ("Texas", "Oklahoma", -7.0, 56.0, -260, +215, 4.1, 59.3),
    ("Oregon", "Washington", -9.5, 61.5, -340, +270, 6.3, 64.8),
    ("Kansas State", "Iowa State", -2.5, 44.5, -135, +115, 5.6, 41.0),
    ("Michigan", "USC", 1.5, 51.0, +105, -125, -1.9, 54.5),
    ("Notre Dame", "Clemson", -4.5, 47.5, -190, +160, 3.2, 44.1),
    ("LSU", "Ole Miss", -1.5, 63.5, -120, +100, 4.4, 66.2),
]
FINALS = [(31, 24), (27, 20), (24, 28), (41, 30), (17, 14), (21, 24), (20, 23), (38, 35)]


def frozen_clock(at):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz else at.replace(tzinfo=None)
    return Frozen


class FakeCFBD:
    """Answers the CFBD endpoints build_data.py calls, for a given moment."""

    def __init__(self):
        self.now = None

    def ratings(self):
        out = {}
        for home, away, *_, m_margin, m_total in FIXTURES:
            out[home] = {"sp": m_margin + 8, "fpi": m_margin + 7.5,
                         "elo": 1500 + (m_margin + 8) * 28, "srs": m_margin + 8,
                         "sp_off": m_total / 2 + m_margin / 2 + 2,
                         "sp_def": m_total / 2 - m_margin / 2 - 2}
            out[away] = {"sp": 10.3, "fpi": 10.3, "elo": 1500 + 10.3 * 28, "srs": 10.3,
                         "sp_off": m_total / 2, "sp_def": m_total / 2}
        return out

    def games(self, week):
        base_id, kick = (1000, SAT_KICK) if week == 12 else (2000, NEXT_KICK)
        out = []
        for i, (home, away, *_rest) in enumerate(FIXTURES):
            done = self.now > kick + timedelta(hours=4)
            g = {"id": base_id + i, "season": YEAR, "week": week, "seasonType": "regular",
                 "startDate": kick.isoformat().replace("+00:00", "Z"),
                 "completed": done, "neutralSite": False, "venue": "Test Stadium",
                 "homeTeam": home, "awayTeam": away,
                 "homeClassification": "fbs", "awayClassification": "fbs",
                 "homeConference": "-", "awayConference": "-",
                 "homePoints": FINALS[i][0] if done else None,
                 "awayPoints": FINALS[i][1] if done else None}
            out.append(g)
        return out

    def lines(self, week):
        base_id = 1000 if week == 12 else 2000
        rows = []
        for i, (home, _away, spread, total, mlh, mla, *_rest) in enumerate(FIXTURES):
            rows.append({"id": base_id + i, "lines": [{
                "provider": "DraftKings", "spread": spread, "overUnder": total,
                "homeMoneyline": mlh, "awayMoneyline": mla,
                "formattedSpread": f"{home} {spread:+g}"}]})
        return rows

    def __call__(self, path, **params):
        week = params.get("week")
        if path == "/calendar":
            return [{"season": YEAR, "week": w, "seasonType": "regular",
                     "startDate": (k - timedelta(days=5)).isoformat(),
                     # calendar week 12 runs through Monday, so on Sunday the
                     # site has to roll forward on its own, as it does for real
                     "endDate": (k + timedelta(days=2)).isoformat()}
                    for w, k in ((12, SAT_KICK), (13, NEXT_KICK))]
        if path == "/games":
            return self.games(week) if week in (12, 13) else []
        if path == "/lines":
            return self.lines(week) if week in (12, 13) else []
        if path.startswith("/ratings/"):
            kind = path.rsplit("/", 1)[1]
            field = {"sp": "rating", "fpi": "fpi", "elo": "elo", "srs": "rating"}[kind]
            rows = []
            for team, r in self.ratings().items():
                row = {"team": team, field: r[kind]}
                if kind == "sp":
                    row["offense"] = {"rating": r["sp_off"]}
                    row["defense"] = {"rating": r["sp_def"]}
                rows.append(row)
            return rows
        if path == "/records":
            return [{"team": t, "total": {"wins": 8, "losses": 3}} for t in self.ratings()]
        raise AssertionError(f"unexpected CFBD call: {path} {params}")


class WeekCycleTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "data").mkdir()
        (self.tmp / "site").mkdir()
        self.saved = {}
        patches = {
            "API_KEY": "test-key",
            "OUT_PATH": self.tmp / "site" / "data.json",
            "LEDGER_PATH": self.tmp / "data" / "picks_history.json",
            "OPENERS_PATH": self.tmp / "data" / "openers_history.json",
            "BOARDS_PATH": self.tmp / "data" / "boards.json",
            "RESULTS_PATH": self.tmp / "site" / "results.json",
            "LASTWEEK_PATH": self.tmp / "site" / "lastweek.json",
            "RECORD_PATH": self.tmp / "site" / "modelrecord.json",
            "RECAP_PATH": self.tmp / "data" / "recap.json",
            "NEWS_CACHE_PATH": self.tmp / "data" / "news_checks.json",
        }
        self.api = FakeCFBD()
        patches["api_get"] = self.api
        for name, value in patches.items():
            self.saved[name] = getattr(bd, name)
            setattr(bd, name, value)
        self.saved["datetime"] = bd.datetime
        # keep the Claude-backed recap and news check out of the test
        self.saved_env = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(bd, name, value)
        if self.saved_env is not None:
            os.environ["ANTHROPIC_API_KEY"] = self.saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_at(self, when):
        self.api.now = when
        bd.datetime = frozen_clock(when)
        bd.main()
        return json.loads(bd.OUT_PATH.read_text())

    def ledger(self):
        return json.loads(bd.LEDGER_PATH.read_text())["picks"]

    def test_full_week_cycle(self):
        sat = self.run_at(RUN_SAT)
        self.assertEqual(sat["week"], 12)
        self.assertFalse(sat["demo"])
        self.assertTrue(sat["topPicks"], "Saturday run posted no picks; fixture lost its edge")
        self.assertTrue(all(p["result"] is None for p in self.ledger()))

        # Sunday: first run of week 13. This is the run that crashed on 2026-11-22.
        sun = self.run_at(RUN_SUN)
        self.assertEqual(sun["week"], 13)
        wk12 = [p for p in self.ledger() if p["week"] == 12]
        self.assertTrue(wk12)
        self.assertTrue(all(p["result"] for p in wk12), "Sunday run did not grade week 12")
        lastweek = json.loads(bd.LASTWEEK_PATH.read_text())
        self.assertTrue(lastweek, "Sunday run wrote no last-week recap")

        tue = self.run_at(RUN_TUE)
        self.assertEqual(tue["week"], 13)

    def test_first_ever_run_with_no_history(self):
        """A brand-new repo (or wiped data/) lands mid-week with nothing on file."""
        out = self.run_at(RUN_TUE)
        self.assertEqual(out["week"], 13)


if __name__ == "__main__":
    unittest.main()
