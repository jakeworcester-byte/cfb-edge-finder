# CFB Edge Finder

A self-updating GitHub Pages site that compares Vegas lines on every FBS
college football game against a four-model ensemble (SP+, FPI, Elo, SRS)
and surfaces the five highest-value bets each week. It then grades every
play it flagged, tracks how the market moved around those numbers, and posts
a written read on the week each Sunday.

- **Live site:** https://YOUR-USERNAME.github.io/cfb-edge-finder/ (after setup)
- **Setup steps:** [SETUP.md](SETUP.md) — five one-time manual steps
- **How the model works:** [MODEL.md](MODEL.md)

## How it stays updated

A GitHub Actions workflow ([.github/workflows/build-and-deploy.yml](.github/workflows/build-and-deploy.yml))
runs during the season (Aug–Jan) on this schedule:

- **Sunday 3am ET** — grades Saturday's games and posts results
- **Tuesday and Thursday 10am ET** — fresh lines for the coming week
- **Saturday 7am ET** — final look before kickoffs

Each run:

1. Finds the next week with games still to play
2. Pulls the FBS schedule, betting lines (DraftKings, ESPN Bet, Bovada,
   consensus), and the four rating systems
3. Runs the ensemble, scores every spread / total / moneyline for expected
   value, and picks the top 5
4. Snapshots the model's call on every game (`data/boards.json`) and the
   top 5 picks (`data/picks_history.json`); snapshots lock at kickoff, and
   each run appends to a per-game line history so movement is preserved
5. Grades every play that cleared a value threshold, on every game — not
   just the top 5. Games where the model found no edge are recorded as
   no-plays and never enter the record
6. Writes `site/data.json`, `site/lastweek.json`, `site/results.json`,
   `site/modelrecord.json` and the Sunday write-up, then deploys to Pages

You can also trigger a refresh anytime from the repo's Actions tab
(**Run workflow**).

## Local development

```
set CFBD_API_KEY=your-key-here
python scripts/build_data.py
```

Then open `site/index.html` in a browser (serve the folder with
`python -m http.server` so `fetch` works). Without a key, the script writes
demo data so the site still renders.

## Repo layout

```
scripts/build_data.py     data pipeline + model + grading + write-up
site/index.html           the site: board, last week, season record
site/results.html         the published Top 5 and its running P/L
site/data.json            generated weekly data (committed copy is demo/stale)
site/modelrecord.json     season record across every value play
site/lastweek.json        last completed week + the Sunday write-up
data/boards.json          locked per-game snapshots and line history
data/picks_history.json   the Top 5 pick ledger
.github/workflows/        the automation
MODEL.md                  methodology and honest limitations
SETUP.md                  one-time manual setup steps
```

## Three tabs, three different questions

- **This Week** — the board, the top 5, and how far each line has moved
  since the week opened.
- **Last Week** — every play the model flagged, graded, with a written
  read on how the week went.
- **Season Record** — the wide sample. Roughly 15-20 plays a week instead
  of 5, broken out by market, by modeled EV, and by whether the model's
  stated win probabilities actually hold up.

## Rolling back

Tag `v1-flat-board` is the last commit before value-play grading, line
history, the mobile layout, and the Sunday write-up. To restore just the
code without touching the accumulated ledger:

```
git checkout v1-flat-board -- scripts site
```

Then commit and push. The extra keys this version writes into
`data/boards.json` and `data/picks_history.json` are additive, so the older
code reads them fine and nothing in the history is lost.

Data courtesy of [CollegeFootballData.com](https://collegefootballdata.com).
For entertainment and decision support — bet responsibly.
