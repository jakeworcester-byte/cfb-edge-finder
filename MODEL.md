# CFB Edge Finder — Model Methodology

## What the model does

For every FBS game in the current week, the model produces:

1. A predicted margin (equivalent to a "fair" point spread)
2. A predicted total (fair over/under)
3. A win probability for each team (fair moneyline)

It then compares those against the actual market lines and flags the gaps.

## The ensemble

The prediction is a weighted blend of four established, publicly available
computer rating systems, all pulled from the CollegeFootballData.com API:

| System | Weight | Why it's included |
|---|---|---|
| **SP+** (Bill Connelly, ESPN) | 35% | The gold standard opponent-adjusted efficiency rating. Includes offense, defense, and special teams components on a points-per-game scale. The offense/defense splits also drive the totals model. |
| **FPI** (ESPN Football Power Index) | 30% | ESPN's forward-looking power rating, tuned for prediction rather than resume ranking. Historically one of the strongest public predictors. |
| **Elo** (CFBD) | 20% | A pure results-based rating that responds week to week. Converted to points at ~28 Elo points per scoreboard point. |
| **SRS** (Simple Rating System) | 15% | Margin-of-victory plus strength-of-schedule. A sanity anchor that keeps the ensemble honest against pure efficiency views. |

If a source is missing for a team, its weight redistributes across the
remaining sources. Teams with no ratings at all (FCS opponents) get a
placeholder rating and the game is flagged **low confidence** and excluded
from picks.

**A note on FEI / "FE+":** Brian Fremeau's FEI (and the combined F+ metric)
has no API and its host site is not reliably machine-readable, so it is not
in the ensemble. FPI and SRS cover the same conceptual ground (drive-based
efficiency and schedule-adjusted results). If FEI ever gets a stable feed,
it slots in at ~10-15% by trimming SP+/FPI.

## Margin, total, and probabilities

- **Margin** = (home power rating − away power rating) + **2.3 pts home field**
  (0 at neutral sites).
- **Total** = SP+ offense vs. opposing SP+ defense for each side, shrunk 50%
  toward the league average to avoid overreacting to early-season ratings.
  Projected scores are re-split so they stay consistent with the ensemble margin.
- **Win probability** = normal CDF of margin with σ = 15.5 (college scoring
  variance is high; this is deliberately conservative).
- **Cover probability** = normal CDF of the edge with σ = 13.5.

## What counts as value

| Bet type | Threshold to be pick-eligible |
|---|---|
| Spread | Model disagrees with market by ≥ 2.0 points |
| Total | Model disagrees by ≥ 3.5 points |
| Moneyline | Model win prob beats the implied prob by ≥ 5 points, EV > 0, model win prob ≥ 35%, and price between −300 and +300. The prob and price floors exist because the normal-curve tail systematically overrates longshot underdogs — a "+2000 dog with 165% EV" is a model artifact, not a bet. |

Expected value assumes −110 pricing on spreads and totals (CFBD does not
carry juice) and the **best available moneyline** across listed books.

The **Top 5** ranks all eligible bets by EV per unit, limited to one bet
per game.

## Honest limitations

- Closing lines at major books are extremely efficient. A 3-4 point model
  edge is meaningful but is not a guarantee; long-run win rates for even
  excellent models rarely exceed 54-55% against the spread.
- Early-season ratings (weeks 1-3) lean heavily on preseason priors and are
  the noisiest. Trust the model more from October on.
- The model does not know about injuries, suspensions, weather, or coaching
  changes announced after ratings were published. **Always check news before
  betting a flagged game** — a big model-vs-market gap is sometimes the
  market knowing something the ratings don't.
- Assumed −110 pricing understates juice at some books.

## How the model grades itself

Two records are kept, and they answer different questions.

**The published Top 5** (`site/results.html`) is the shortlist, five plays a
week, graded at a flat $10. It is the honest record of what the site actually
told you to do, and it is also a small sample: 5 plays a week means a full
season lands near 70 bets, which is not enough to separate a real edge from
luck.

**Every value play** (the Season Record tab) grades every bet on every game
that cleared a threshold, roughly 15-20 a week. Games where nothing cleared
are recorded as no-plays and never enter the record. Same model, same
thresholds, three times the sample. This is where a real answer comes from.

Alongside the record, two things worth more than the win rate this early:

- **Calibration.** When the model says a play hits 62%, does it hit 62%? A
  model that is well calibrated but unprofitable has a pricing problem. One
  that is poorly calibrated is broken regardless of results.
- **EV tiers.** Plays grouped by the expected value the model assigned them.
  The Top 5 is drawn from the highest tier, so if the tiers do not separate,
  the ranking that picks the Top 5 is not doing anything.

## Line movement

Every run appends the current spread, total, and moneylines to a per-game
line history. Two measures come out of it:

- **Drift** (Last Week tab) compares the week's opening number to the last
  one seen before kickoff, in the direction of the model's side. Positive
  drift means the market ended up moving toward the model. This is computed
  for every value play on the board.
- **Closing line value** (Top 5 page) compares the number the site actually
  published a pick at to the last number seen. A board snapshot refreshes
  until kickoff, so this only means something for published picks, which
  remember the line they were first posted at.

Why this matters more than the record right now: a model that consistently
sits on the right side of line movement has found something real, and that
shows up in 20 or 30 picks. A win rate takes hundreds. Beating closing
lines is also a much harder test than beating results, so a good drift
number with a mediocre record is more encouraging than the reverse.

## Backfilled weeks

A week played before the site started snapshotting gets graded after the
fact, using ratings that already reflect those results. Those weeks are
flagged, excluded from the season record, and reported separately. They
show the format; they are not evidence.

Bet responsibly. This is a decision-support tool, not a money printer.
