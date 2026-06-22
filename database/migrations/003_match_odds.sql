-- Migration: add frozen pre-match "Match Winner" odds to matches.
--
-- Odds-based scoring multiplies the result points by sqrt(odd) of the predicted outcome
-- (see services/scoring_engine.py ODDS_SCORING_START_DATE). The odds are fetched once from
-- API-Football's /odds endpoint at Daily Blast time and frozen onto the match row so scoring
-- stays deterministic and idempotent (never re-fetched/overwritten).
--
-- All three columns are additive and nullable: a NULL odd (fetch failed / market missing /
-- legacy match) means "score this match flat" (multiplier 1.0). NUMERIC(6,2) comfortably
-- holds decimal odds (e.g. 1.45, 17.00). Safe to run once; apply out of band.

ALTER TABLE matches ADD COLUMN IF NOT EXISTS odds_home NUMERIC(6,2);
ALTER TABLE matches ADD COLUMN IF NOT EXISTS odds_draw NUMERIC(6,2);
ALTER TABLE matches ADD COLUMN IF NOT EXISTS odds_away NUMERIC(6,2);
