# SETUP — Your Manual Steps to Go Live

Everything is built and committed locally. Five steps remain that only you
can do (they need your logins). Total time: about 15 minutes, one time only.
After this, the site updates itself every Tuesday, Thursday, and Saturday
during the season.

---

## Step 1: Get a free CFBD API key (~2 min)

1. Go to **https://collegefootballdata.com/key**
2. Enter your email and click to register. The key arrives by email.
3. Keep that email handy — you'll paste the key in Step 4.

The free tier allows 1,000 calls/month. This project uses about 6 calls per
run, 3 runs per week — roughly 75/month. Plenty of headroom.

## Step 2: Create the GitHub repository (~2 min)

1. Go to **https://github.com/new**
2. Repository name: `cfb-edge-finder` (or anything you like)
3. Visibility: **Public** (required for free GitHub Pages)
4. Do **not** check "Add a README" — the repo needs to start empty.
5. Click **Create repository**.

## Step 3: Push the code (~2 min)

Open a terminal in the `Sports Betting` folder and run these, replacing
`YOUR-USERNAME` with your GitHub username:

```bash
git remote add origin https://github.com/YOUR-USERNAME/cfb-edge-finder.git
```

```bash
git push -u origin main
```

If git asks you to log in, a browser window will pop up — sign in to GitHub
and approve.

## Step 4: Add the API key as a secret (~2 min)

1. In your new repo on GitHub: **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `CFBD_API_KEY` (exactly that, all caps)
4. Value: paste the key from Step 1
5. Click **Add secret**

## Step 5: Turn on GitHub Pages (~1 min)

1. In the repo: **Settings → Pages**
2. Under **Build and deployment → Source**, choose **GitHub Actions**
3. That's it — no other settings needed.

## Step 6: Run it once and check (~2 min)

1. Go to the repo's **Actions** tab
2. Click **Build data and deploy to Pages** in the left sidebar
3. Click **Run workflow → Run workflow** (green button)
4. Wait ~1 minute for both jobs to go green
5. Your site is live at:
   **https://YOUR-USERNAME.github.io/cfb-edge-finder/**

Bookmark that URL. It refreshes automatically Tue/Thu 10am ET and Sat 7am ET
during the season (August through January).

---

## Troubleshooting

- **Site shows a yellow "Demo data" banner** → the secret from Step 4 is
  missing or misnamed. It must be exactly `CFBD_API_KEY`. Fix it, then re-run
  the workflow (Step 6).
- **Workflow fails on the deploy job** → Pages source isn't set to
  "GitHub Actions" (Step 5).
- **Push rejected in Step 3** → the repo wasn't created empty. Delete it and
  redo Step 2 without the README checkbox.
- **Want a fresh update right now** (say, Saturday morning after Friday line
  moves) → Actions tab → Run workflow. Takes a minute.

## Optional: the written Sunday recap

Without this, the site still posts a recap each week, just a plainly worded
one generated from the data. With it, Claude writes the recap instead.

1. Go to **https://console.anthropic.com** and sign in. This is the developer
   console and is separate from a Claude.ai subscription, which does not
   include API credits.
2. **Billing** -> add a payment method and buy prepaid credits. The $5 minimum
   will outlast the season; this feature costs roughly a dime a week.
3. **API keys** -> **Create Key**, name it `cfb-edge-finder`, and copy it. The
   console will not show it again.
4. In the repo: **Settings -> Secrets and variables -> Actions -> New
   repository secret**. Name it `ANTHROPIC_API_KEY` and paste the value.
5. Actions tab -> **Run workflow**. The build log prints `recap: claude` on
   success, or `recap: builtin (...)` with the reason if it fell back.

The recap is generated once per graded week and cached in `data/recap.json`,
so the Tuesday and Thursday runs reuse Sunday's text instead of paying to
reword it. If the key is missing, revoked, or rate limited, the build prints
why and publishes the deterministic recap. It never fails the build.

## Optional later upgrades

- Add The Odds API (the-odds-api.com, free tier) for live prices from more
  books including juice on spreads/totals.
- Track pick results week over week (needs a small results-grading step —
  ask Claude to build it once a few weeks of picks exist).
