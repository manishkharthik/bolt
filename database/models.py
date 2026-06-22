"""SQLAlchemy 2.0 ORM models mapping the tables defined in CLAUDE.md section 4.

These models mirror the existing database exactly. They do NOT own the DDL — the canonical
schema lives in database/schema.sql and is applied out of band. Keep this file in sync with it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --- Match / wager status constants (mirror the schema CHECK constraints) ---

MATCH_SCHEDULED = "SCHEDULED"
MATCH_IN_PROGRESS = "IN_PROGRESS"
MATCH_FINISHED = "FINISHED"

WAGER_SCORE = "SCORE"
WAGER_ASSIST = "ASSIST"
WAGER_CARD = "CARD"  # player gets booked (yellow or red card)

WAGER_PENDING = "PENDING"
WAGER_HIT = "HIT"
WAGER_MISSED = "MISSED"
WAGER_VOID = "VOID"


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )


class Group(Base):
    __tablename__ = "groups"

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    group_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.telegram_chat_id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    joined_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SCHEDULED', 'IN_PROGRESS', 'FINISHED')",
            name="matches_status_check",
        ),
    )

    match_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    home_team: Mapped[str] = mapped_column(String(100), nullable=False)
    away_team: Mapped[str] = mapped_column(String(100), nullable=False)
    kickoff_time: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    home_score_90min: Mapped[int | None] = mapped_column(Integer, default=None)
    away_score_90min: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(20), default=MATCH_SCHEDULED)
    # Frozen pre-match "Match Winner" odds (migration 003). NULL = score this match flat.
    odds_home: Mapped[float | None] = mapped_column(Numeric(6, 2), default=None)
    odds_draw: Mapped[float | None] = mapped_column(Numeric(6, 2), default=None)
    odds_away: Mapped[float | None] = mapped_column(Numeric(6, 2), default=None)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("telegram_id", "match_id", name="predictions_telegram_id_match_id_key"),
    )

    prediction_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE")
    )
    match_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("matches.match_id", ondelete="CASCADE")
    )
    predicted_home_score: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_away_score: Mapped[int] = mapped_column(Integer, nullable=False)
    calculated_points: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )


class Wager(Base):
    __tablename__ = "wagers"
    __table_args__ = (
        CheckConstraint(
            "wager_type IN ('SCORE', 'ASSIST', 'CARD')", name="wagers_wager_type_check"
        ),
        CheckConstraint(
            "wager_status IN ('PENDING', 'HIT', 'MISSED', 'VOID')",
            name="wagers_wager_status_check",
        ),
    )

    wager_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE")
    )
    match_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("matches.match_id", ondelete="CASCADE")
    )
    player_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Canonical API-Football player id (matches /fixtures/players), copied from the picked
    # world_cup_players row. Nullable: legacy wagers placed before this column existed stay
    # NULL and fall back to name matching in the scoring engine.
    player_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wager_type: Mapped[str] = mapped_column(String(20))
    wager_status: Mapped[str] = mapped_column(String(20), default=WAGER_PENDING)
    calculated_points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )


class MatchdaySnapshot(Base):
    __tablename__ = "matchday_snapshots"

    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    matchday_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.telegram_chat_id", ondelete="CASCADE")
    )
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE")
    )
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    points_earned_today: Mapped[int] = mapped_column(Integer, nullable=False)
    cumulative_total_points: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )


class WorldCupPlayer(Base):
    __tablename__ = "world_cup_players"

    api_player_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    player_name: Mapped[str] = mapped_column(String(150), nullable=False)
    team_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Canonical API-Football player id (same namespace as /fixtures/players). Backfilled once by
    # scripts/backfill_player_ids.py; distinct from api_player_id, which is from another source.
    player_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Feedback(Base):
    # No FK to users: feedback may come from anyone, including users who never DM'd the bot.
    __tablename__ = "feedback"

    feedback_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(100))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_type: Mapped[str | None] = mapped_column(String(20))
    feedback_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.current_timestamp()
    )
