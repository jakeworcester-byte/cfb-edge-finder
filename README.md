# CFB Edge Finder

A self-updating GitHub Pages site that compares Vegas lines on every FBS
college football game against a four-model ensemble (SP+, FPI, Elo, SRS)
and surfaces the five highest-value bets each week.

- **Live site:** https://YOUR-USERNAME.github.io/cfb-edge-finder/ (after setup)
- **Setup steps:** [SETUP.md](SETUP.md) — five one-time manual steps
- **How the model works:** [MODEL.md](MODEL.md)

## How it stays updated

A GitHub Actions workflow ([.github/workflows/build-and-deploy.yml](.github/workflows/build-and-deploy.yml))
runs Tuesday and Thursday at 10am ET and Saturday at 7am ET during the season
(Aug–Jan). Each run:

1. Detects the current week from the CFBD calendar
2. Pulls the FBS schedule, betting lines (DraftKings, ESPN Bet, Bovada,
   consensus), and the four rating systems
3. Runs the ensemble, scores every spread / total / moneyline for expected
   value, and picks the top 5
4. Writes `site/data.json` and deploys the static site to GitHub Pages

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
scripts/build_data.py     data pipeline + model
site/index.html           the site (self-contained, no build step)
site/data.json            generated weekly data (committed copy is demo/stale)
.github/workflows/        the automation
MODEL.md                  methodology and honest limitations
SETUP.md                  one-time manual setup steps
```

Data courtesy of [CollegeFootballData.com](https://collegefootballdata.com).
For entertainment and decision support — bet responsibly.
