# Project Plan
# Setting up group
User creates group chat and adds bot (or adds bot to existing group chat)
Users use the /register command to register themselves for that group chat
In DB, group is identified with the immutable Telegram chat_id

# Intermediate joiners (user joins halfway to group chat)
If user has played before
The predictions they made before and on the day will be included in the group chat
If user has not played before
They can only make predictions 8 hours prior to the gameday which they joined, and after that their points will count towards that group and any other group they join after

# User Flow
Bot asks users to make predictions + wagers for all games on the day 8 hours prior to the first game of the day (via private dm) 
Each user will individually submit to the bot their predictions + wagers for each game
Users can also check or change their predictions + wagers (via /status) but this will be locked 60 mins before each game start when lineups are out
Each user cannot see another users predictions + wagers
If a user is in multiple groups, their predictions + wagers would count towards all groups upon submission
They will also get a reminder four hours before the start of the first game if they have not submitted predictions for all games on that day
Once the game is over, the user will get a breakdown of the points earned for that game
Once all games in the day are over, the bot will publish the updated leaderboard to the group chat.
If users want, they can use /individual command to see the points each user got from each game or /daily command to see leaderboard for that particular day 

# Points System
Scoring
+50 points for correct result
+150 points for exact scoreline (200 points total)
+/- 100 points for each wager (maximum of 3 wagers for each game)
If a wagered player registers 0 minutes of playing time, that wager is voided (no points deducted)
Potential issues
If for a gameday, a user only inputs predictions + wagers for one game and not the others, they will only get points for that game
Breakdown will output 0 for any game they do not participate in

# CRON jobs
Daily Blast (8 hours before first game)
Loops through all registered users in DB
Sends a private DM presenting the daily slate of games with prompts to collect score predictions and player wagers for each game
Slacker Warning (2 hours before first game)
Runs a database query to find users who have a missing entry for any of the games on that day (if they predicted game A but not yet game B etc)
Sends a private DM reminder to lock-in predictions
Lineup Lockdown (1 hour before each game)
Updates the match status in your database to LOCKED
Any incoming user message attempting to modify a prediction or wager for that specific match_id after this timestamp is rejected with an error message
Post-Match Analysis (After each game)
Hits sports API to pull final scores and player match statistics (minutes played, goals, assists)
Updates the predictions and wagers tables, calculates points, and sends a private DM presenting the breakdown for that game
Updates leaderboards that the users are in as well
Daily Reveal (After all games on that day)
Publishes updated leaderboard to group chat
Saves individual breakdowns (/individual) and daily (/daily) leaderboard (to be displayed if user requests for it)
Player A: Game 1 (+50 points), Game 2 (-100 points), Total (-50 points)
1. Player B: +100 points 2. Player A: -50 points

# Commands
Group set-up
/add (to add bot)
/register (for users to register themselves into a group)
Matchday (a - Individual, b,c - Individual & Group)
/status (displays predictions + wagers made for each game on that day, option to edit if more than one hour before start of game)
/timeline (displays duration before start of each game on that day)
/matchday (displays the games for that day, with timings in SGT)
Individual
/groups (displays the groups the user is in, and their ranking on the leaderboard for each)
/breakdown (displays the breakdown and points earned for elapsed games, as well as games that are currently in progress/yet to start)
Group
/leaderboard (displays current standings for the group)
/individual (displays points each user got from each game on the matchday_id which is the last day on which the bot published leaderboard to the group)
/daily (displays leaderboard for just the games on the matchday_id, which refers to the last day on which the bot published leaderboard to the group)

# Suggested folder structure
│
├── .env                  # Secrets: Telegram Bot Token, Database URLs, API Keys
├── Dockerfile            # Container deployment configuration
├── requirements.txt      # Python dependencies (aiogram, asyncpg, apscheduler)
│
├── config/
│   ├── __init__.py
│   ├── db.py             # PostgreSQL asyncpg connection pool initialization
│   └── settings.py       # Global environment variable loaders (Timezone settings)
│
├── database/
│   ├── schema.sql        # Database tables initialization scripts
│   └── migrations/       # For any future table structural adjustments
│
├── bot/
│   ├── __init__.py
│   ├── main.py           # Application Entry Point (Starts the polling/webhook engine)
│   │
│   ├── handlers/         # Commands logic split by context
│   │   ├── __init__.py
│   │   ├── group.py      # /register, /leaderboard, /daily, /individual
│   │   ├── private.py    # /matchday, /status, /timeline, /groups, /breakdown
│   │   └── system.py     # Captures bot joins, welcome messages, error handling
│   │
│   └── states/
│       ├── __init__.py
│       └── prediction.py # Finite State Machine (FSM) paths for form submissions
│
└── services/
    ├── __init__.py
    ├── cron_scheduler.py # APScheduler configuration (Daily Blast, Slacker Warning)
    ├── scoring_engine.py # The mathematical scoring logic block (90-minute filters)
    └── sports_api.py     # Outbound HTTP Client fetching schedules & player data


# 1. Project Mission: bolt

bolt is a Telegram World Cup prediction game.

Users predict match results and player wagers.

The system awards points based on prediction accuracy and wager outcomes.

Users compete on leaderboards inside Telegram group chats.

The primary goal is:

1. Ship before World Cup starts.
2. Prioritize reliability over cleverness.
3. Minimize infrastructure complexity.
4. Avoid premature optimization.
5. Keep implementation understandable by a single developer.

# 2. Technology Decisions

These decisions are final.

Backend:
- Python 3.12
- FastAPI
- Aiogram

Database:
- PostgreSQL

Scheduling:
- APScheduler

Hosting:
- Docker

HTTP:
- httpx

ORM:
- SQLAlchemy 2.0 async

Timezone:
- Asia/Singapore

Do not introduce:
- Redis
- Kafka
- Celery
- RabbitMQ
- Microservices
- Event Sourcing

# 3. Architecture Rules

This project is a monolith.

All business logic lives in services/.

Handlers only:
- validate input
- call service layer
- return response

Handlers must never:
- perform SQL queries
- calculate points
- call external APIs

All business logic belongs inside services/.

# 4. Current DB (world_cup_players has been populated)
-- 1. Users Table (Private DM Layer)
CREATE TABLE users (
    telegram_id BIGINT PRIMARY KEY,
    username VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Groups Table (Group Chat Layer)
CREATE TABLE groups (
    telegram_chat_id BIGINT PRIMARY KEY,
    group_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Group Membership Table
CREATE TABLE group_members (
    group_chat_id BIGINT REFERENCES groups(telegram_chat_id) ON DELETE CASCADE,
    telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_chat_id, telegram_id)
);

-- 4. World Cup Matches Reference Table
CREATE TABLE matches (
    match_id INT PRIMARY KEY,
    home_team VARCHAR(100) NOT NULL,
    away_team VARCHAR(100) NOT NULL,
    kickoff_time TIMESTAMP WITH TIME ZONE NOT NULL,
    home_score_90min INT DEFAULT NULL,
    away_score_90min INT DEFAULT NULL,
    status VARCHAR(20) DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED', 'IN_PROGRESS', 'FINISHED'))
);

-- 5. User Match Predictions Table
CREATE TABLE predictions (
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
CREATE TABLE wagers (
    wager_id SERIAL PRIMARY KEY,
    telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    match_id INT REFERENCES matches(match_id) ON DELETE CASCADE,
    player_name VARCHAR(150) NOT NULL,
    wager_type VARCHAR(20) CHECK (wager_type IN ('SCORE', 'ASSIST')),
    wager_status VARCHAR(20) DEFAULT 'PENDING' CHECK (wager_status IN ('PENDING', 'HIT', 'MISSED', 'VOID')),
    calculated_points INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 7. Static Matchday Leaderboard Snapshots Table
CREATE TABLE matchday_snapshots (
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
CREATE TABLE world_cup_players (
    api_player_id INT PRIMARY KEY,
    player_name VARCHAR(150) NOT NULL,
    team_name VARCHAR(100) NOT NULL
);

-- ⚡ Core Performance Indexes
CREATE INDEX idx_matches_kickoff ON matches(kickoff_time);
CREATE INDEX idx_predictions_lookup ON predictions(telegram_id, match_id);
CREATE INDEX idx_wagers_lookup ON wagers(telegram_id, match_id);
CREATE INDEX idx_snapshots_lookup ON matchday_snapshots(group_chat_id, matchday_id DESC);

# 5. Scoring Rules

Match Result:
Correct outcome:
+50

Exact score:
+150 additional

Wagers:
Correct wager:
+100

Incorrect wager:
-100

Player wager void:
0 points

Void condition:
Player registered 0 minutes played.

Maximum wagers per match:
3

# 6. Prediction Locking

Predictions open:
Until 60 minutes before kickoff.

Predictions locked:
60 minutes before kickoff.

Users may edit predictions until lock.

After lock:
Reject all modifications.

Users can still view predictions.

# 7. Group Membership Rules

Users own predictions.

Groups never own predictions.

A prediction is submitted once.

That prediction contributes to all groups where the user is registered.

When a user joins a new group:
Historical predictions remain valid.

Future leaderboard calculations include all eligible predictions.

Group membership affects visibility only.

# 8. Development Workflow

Before writing code:

1. Explain affected files.
2. Explain database impact.
3. Explain migration impact.
4. Explain API impact.
5. Explain edge cases.

Then generate code.

Never generate code first.

# 9. Error handling
Expected business errors:
Return friendly Telegram message.

Unexpected errors:
Log error.
Return generic error.

Never expose stack traces to users.

# 10. Sports Data

Sports API is authoritative.

Match status:
Scheduled
Live
Finished

Only Finished matches may be scored.

Player statistics are fetched after match completion.

Scoring jobs must be idempotent.

# 11. Scheduled Jobs

Jobs must be idempotent.

Running a job twice must produce identical results.

Jobs:

Daily Blast
Slacker Warning
Lineup Lockdown
Post Match Analysis
Daily Reveal

# 12. Deployment

Single Docker container.

Single PostgreSQL database.

Environment variables only.

No secrets in code.

All cron jobs run inside application process.

# 13. Do Not Do

Do not introduce Redis.

Do not introduce Celery.

Do not introduce message queues.

Do not introduce microservices.

Do not cache leaderboards.

Do not optimize prematurely.

Do not create abstractions unless used at least twice.

Do not create generic repositories.

# 14. Shipping Priority

Priority Order:

P0
User registration
Predictions
Wagers
Scoring
Leaderboards

P1
Breakdowns
Daily views
History

P2
Fancy formatting
Analytics
Charts

If a decision must be made between speed and elegance,
choose speed.

If a decision must be made between reliability and features,
choose reliability.