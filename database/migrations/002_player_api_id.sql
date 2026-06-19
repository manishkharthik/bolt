-- Migration: add the authoritative API-Football player id to players and wagers.
--
-- world_cup_players.api_player_id is the table's primary key but comes from a DIFFERENT id
-- namespace than API-Football's /fixtures/players stats (verified: 0/208 sampled fixture
-- player ids existed in api_player_id). That mismatch is why scoring fell back to fuzzy
-- name matching. We add player_id = the canonical API-Football player id (same namespace as
-- /fixtures/players and /players/squads) so scoring can join by id instead of by name.
--
-- world_cup_players.player_id is backfilled once by scripts/backfill_player_ids.py.
-- wagers.player_id is captured from the picker going forward; existing rows stay NULL and
-- continue to score via the name-matching fallback. Both columns are additive and nullable.
-- Safe to run once; apply out of band.

ALTER TABLE world_cup_players ADD COLUMN IF NOT EXISTS player_id INT;
ALTER TABLE wagers ADD COLUMN IF NOT EXISTS player_id INT;

CREATE INDEX IF NOT EXISTS idx_world_cup_players_player_id ON world_cup_players(player_id);
