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
| Moneyline | Model win prob beats the implied prob by ≥ 5 points, and EV > 0 |

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

Bet responsibly. This is a decision-support tool, not a money printer.
