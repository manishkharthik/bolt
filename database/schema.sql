-- Canonical schema for bolt (wcbot). Matches CLAUDE.md section 4 verbatim.
-- Load into a fresh database with:
--   psql "postgresql://user:password@host:5432/wcbot" -f database/schema.sql
-- world_cup_players is expected to be populated separately (already done in the live DB).

-- 1. Users Table (Private DM Layer)
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Groups Table (Group Chat Layer)
CREATE TABLE IF NOT EXISTS groups (
    telegram_chat_id BIGINT PRIMARY KEY,
    group_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Group Membership Table
CREATE TABLE IF NOT EXISTS group_members (
    group_chat_id BIGINT REFERENCES groups(telegram_chat_id) ON DELETE CASCADE,
    telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_chat_id, telegram_id)
);

-- 4. World Cup Matches Reference Table
CREATE TABLE IF NOT EXISTS matches (
    match_id INT PRIMARY KEY,
    home_team VARCHAR(100) NOT NULL,
    away_team VARCHAR(100) NOT NULL,
    kickoff_time TIMESTAMP WITH TIME ZONE NOT NULL,
    home_score_90min INT DEFAULT NULL,
    away_score_90min INT DEFAULT NULL,
    status VARCHAR(20) DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED', 'IN_PROGRESS', 'FINISHED')),
    -- Frozen pre-match "Match Winner" odds (see migration 003). NULL = score flat.
    odds_home NUMERIC(6,2) DEFAULT NULL,
    odds_draw NUMERIC(6,2) DEFAULT NULL,
    odds_away NUMERIC(6,2) DEFAULT NULL
);

-- 5. User Match Predictions Table
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id SERIAL PRIMARY KEY,
    telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    match_id INT REFERENCES matches(match_id) ON DELETE CASCADE,
    predicted_home_score INT NOT NULL,
    predicted_away_score INT NOT NULL,
    calculated_points INT DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(telegram_id, match_id)
);

-- 6. User Player Wagers Table
CREATE TABLE IF NOT EXISTS wagers (
    wager_id SERIAL PRIMARY KEY,
    telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    match_id INT REFERENCES matches(match_id) ON DELETE CASCADE,
    player_name VARCHAR(150) NOT NULL,
    player_id INT,  -- canonical API-Football id (same namespace as /fixtures/players); see world_cup_players.player_id
    wager_type VARCHAR(20) CHECK (wager_type IN ('SCORE', 'ASSIST', 'CARD')),
    wager_status VARCHAR(20) DEFAULT 'PENDING' CHECK (wager_status IN ('PENDING', 'HIT', 'MISSED', 'VOID')),
    calculated_points INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 7. Static Matchday Leaderboard Snapshots Table
CREATE TABLE IF NOT EXISTS matchday_snapshots (
    snapshot_id SERIAL PRIMARY KEY,
    matchday_id INT NOT NULL,
    group_chat_id BIGINT REFERENCES groups(telegram_chat_id) ON DELETE CASCADE,
    telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    username VARCHAR(100) NOT NULL,
    points_earned_today INT NOT NULL,
    cumulative_total_points INT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 8. Local Player Pool Table for Search Engine
-- api_player_id is the seed source's id (NOT API-Football's stats namespace). player_id is the
-- canonical API-Football id used for scoring joins, backfilled by scripts/backfill_player_ids.py.
CREATE TABLE IF NOT EXISTS world_cup_players (
    api_player_id INT PRIMARY KEY,
    player_name VARCHAR(150) NOT NULL,
    team_name VARCHAR(100) NOT NULL,
    player_id INT
);
CREATE INDEX IF NOT EXISTS idx_world_cup_players_player_id ON world_cup_players(player_id);

-- 9. User Feedback Table (no FK to users: anyone, registered or not, may leave feedback)
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    username VARCHAR(100),
    chat_id BIGINT,
    chat_type VARCHAR(20),
    feedback_text VARCHAR(1000) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Core Performance Indexes
CREATE INDEX IF NOT EXISTS idx_matches_kickoff ON matches(kickoff_time);
CREATE INDEX IF NOT EXISTS idx_predictions_lookup ON predictions(telegram_id, match_id);
CREATE INDEX IF NOT EXISTS idx_wagers_lookup ON wagers(telegram_id, match_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_lookup ON matchday_snapshots(group_chat_id, matchday_id DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC);
