"""Presentation layer: pure functions that render bot data into Telegram HTML strings.

Shared by command handlers (bot/handlers) and the scheduled DMs (services/cron_scheduler) so
every surface uses one consistent "Bolt" look. These functions never touch the DB or
the network — callers fetch the data, these turn it into text. Keyboards live in bot/keyboards.

The bot runs with parse_mode=HTML, so dynamic values are escaped via ``esc``.
"""

from __future__ import annotations

import datetime as dt
import html

from database.models import (
    MATCH_FINISHED,
    MATCH_IN_PROGRESS,
    WAGER_HIT,
    WAGER_MISSED,
    WAGER_VOID,
    Match,
    Prediction,
    Wager,
)
from services import matches_service
from services.predictions_service import MAX_WAGERS_PER_MATCH, DayEntry
from services.scoring_engine import (
    POINTS_CORRECT_RESULT,
    POINTS_EXACT_BONUS,
    POINTS_WAGER_HIT,
    POINTS_WAGER_MISS,
)

SEP = "----------------------------------------------"
_NUM = ["", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


# --- small helpers -----------------------------------------------------------

def esc(value: object) -> str:
    return html.escape(str(value))


def ordinal(i: int) -> str:
    return _NUM[i] if i < len(_NUM) else f"{i}."


def _sgt(when: dt.datetime) -> dt.datetime:
    return when.astimezone(matches_service.settings.tzinfo)


def fmt_time(when: dt.datetime) -> str:
    """e.g. '02:00 AM SGT'."""
    return _sgt(when).strftime("%I:%M %p") + " SGT"


def fmt_date_long(day: dt.date) -> str:
    """e.g. 'June 11, 2026'."""
    return day.strftime("%B %d, %Y")


def _day_qualifier(when: dt.datetime, now: dt.datetime) -> str:
    d = _sgt(when).date()
    today = _sgt(now).date()
    if d == today:
        return " (Today)"
    if d == today + dt.timedelta(days=1):
        return " (Tomorrow)"
    return f" ({d:%a %d %b})"


def _pred_text(prediction: Prediction | None) -> str:
    if prediction is None:
        return "❌ <i>Not Submitted</i>"
    return f"✅ {prediction.predicted_home_score} - {prediction.predicted_away_score}"


def _wagers_text(wagers: list[Wager]) -> str:
    if not wagers:
        return "❌ <i>Not Submitted</i>"
    parts = [f"✅ {esc(w.player_name)} ({w.wager_type})" for w in wagers]
    text = ", ".join(parts)
    left = MAX_WAGERS_PER_MATCH - len(wagers)
    if left > 0:
        text += f" [{left} slot{'s' if left != 1 else ''} left]"
    return text


def _slate_blocks(entries: list[DayEntry]) -> str:
    blocks = []
    for i, e in enumerate(entries, start=1):
        m = e.match
        blocks.append(
            f"{ordinal(i)} <b>{esc(m.home_team)} vs {esc(m.away_team)}</b> "
            f"(⏰ {fmt_time(m.kickoff_time)})\n"
            f"• Prediction: {_pred_text(e.prediction)}\n"
            f"• Wagers: {_wagers_text(e.wagers)}"
        )
    return "\n\n".join(blocks)


# --- Daily Blast / status / slacker ------------------------------------------

def daily_blast(day: dt.date, entries: list[DayEntry]) -> str:
    return (
        f"⚽ <b>Bolt Daily Slate: {fmt_date_long(day)}</b> ⚽\n"
        f"{SEP}\n"
        "It's a new day! Here are today's World Cup matchups.\n"
        "Tap the buttons below to lock in your scorelines and player wagers.\n\n"
        "⚠️ <i>Lockdown happens exactly 1 hour before each kickoff!</i>\n\n"
        "📅 TODAY'S SLATE (SGT):\n\n"
        f"{_slate_blocks(entries)}\n"
        f"{SEP}"
    )


# status uses the identical body — it's the "after you've filled things in" view.
status = daily_blast


def slacker_warning(day: dt.date, entries: list[DayEntry]) -> str:
    return (
        "🚨 <b>BOLT LOCKDOWN WARNING!</b> 🚨\n"
        f"{SEP}\n"
        "The first game of the day kicks off in 4 hours! Your daily predictions and wagers "
        "are currently incomplete.\n\n"
        "Please lock in your choices below before the respective 1-hour pre-game deadlines hit.\n\n"
        "📅 YOUR INCOMPLETE SLATE (SGT):\n\n"
        f"{_slate_blocks(entries)}\n"
        f"{SEP}"
    )


# --- Post-Match Analysis -----------------------------------------------------

def _result_label(prediction: Prediction) -> str:
    pts = prediction.calculated_points
    if pts >= 200:
        return "Perfect Scoreline!"
    if pts >= 50:
        return "Correct Result, Incorrect Scoreline"
    return "Incorrect"


def _wager_result_line(idx: int, wager: Wager) -> str:
    base = f"• Wager {idx}: {esc(wager.player_name)} ({wager.wager_type}) -> "
    if wager.wager_status == WAGER_HIT:
        return base + f"⚽ HIT! (+{wager.calculated_points} pts)"
    if wager.wager_status == WAGER_MISSED:
        return base + f"❌ MISSED ({wager.calculated_points} pts)"
    if wager.wager_status == WAGER_VOID:
        return base + "🩹 VOID (0 mins played)"
    return base + "⏳ Pending"


def post_match(match: Match, prediction: Prediction | None, wagers: list[Wager]) -> str:
    lines = [
        "🏁 <b>Bolt Match Resolution</b> 🏁",
        SEP,
        "The whistle has blown! Here is how your predictions and wagers performed for this match.",
        "",
        f"⚽ <b>{esc(match.home_team)} [ {match.home_score_90min} ] vs "
        f"[ {match.away_score_90min} ] {esc(match.away_team)}</b> (FINISHED)",
        "",
        "🔮 YOUR PREDICTION:",
    ]
    if prediction is None:
        lines.append("• <i>No prediction submitted</i>")
        pred_pts = 0
    else:
        pred_pts = prediction.calculated_points
        lines.append(
            f"• Your Guess: {prediction.predicted_home_score} - {prediction.predicted_away_score}"
        )
        lines.append(f"• Outcome: {_result_label(prediction)}")
        lines.append(f"👉 Prediction Score: {pred_pts:+d} pts")

    lines.append("")
    lines.append("🎯 YOUR PLAYER WAGERS:")
    wager_pts = 0
    if not wagers:
        lines.append("• <i>No wagers placed</i>")
    else:
        for idx, w in enumerate(wagers, start=1):
            wager_pts += w.calculated_points
            lines.append(_wager_result_line(idx, w))
        lines.append(f"👉 Wagers Score: {wager_pts:+d} pts")

    lines.append(SEP)
    lines.append(f"📈 Total Points from Match: {pred_pts + wager_pts:+d} pts")
    return "\n".join(lines)


# --- /timeline ---------------------------------------------------------------

def timeline(day: dt.date, matches: list[Match], now: dt.datetime) -> str:
    lines = [
        "⏱️ <b>Bolt Countdown Timeline</b> ⏱️",
        SEP,
        "Matches are listed in chronological order.",
        "⚠️ <i>Predictions and wagers lock exactly 60 minutes before kickoff!</i>",
        "",
        "⏳ COUNTDOWN TO LOCKDOWN (SGT):",
        "",
    ]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{ordinal(i)} <b>{esc(m.home_team)} vs {esc(m.away_team)}</b>")
        lines.append(f"• Kickoff: {fmt_time(m.kickoff_time)}{_day_qualifier(m.kickoff_time, now)}")
        if matches_service.is_locked(m, at=now):
            lines.append("• Status: 🛑 LOCKED (Lineups are out!)")
        else:
            lock_at = m.kickoff_time - dt.timedelta(minutes=matches_service.LOCK_LEAD_MINUTES)
            mins = max(0, int((lock_at - now).total_seconds() // 60))
            lines.append(f"👉 Lockdown in: <code>{mins // 60} hours {mins % 60} minutes</code>")
        lines.append("")
    lines.append(SEP)
    lines.append("🤖 Tap /status in Private DM to fill or edit open entries.")
    return "\n".join(lines)


# --- /matchday ---------------------------------------------------------------

def matchday(day: dt.date, matches: list[Match]) -> str:
    lines = [
        f"🗓️ <b>Bolt Matchday Slate ({day:%d %B %Y})</b>",
        SEP,
        "Here is the official World Cup schedule for this matchday.",
        "All times are displayed in Singapore Time (SGT).",
        "Predictions and wagers for all matches open 8 hours before the earliest match kickoff.",
        "",
        "⚽ TODAY'S FIXTURES:",
        "",
    ]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{ordinal(i)} <b>{esc(m.home_team)} vs {esc(m.away_team)}</b>")
        lines.append(f"⏰ Kickoff: <code>{fmt_time(m.kickoff_time)}</code>")
        lines.append("")
    lines.append(SEP)
    lines.append(
        "📥 Want to win points? Go to my private DM and type /status to lock in your "
        "scorelines and player wagers!"
    )
    return "\n".join(lines)


# --- /breakdown --------------------------------------------------------------

def _match_status_label(match: Match, now: dt.datetime) -> str:
    if match.status == MATCH_FINISHED:
        return "FINISHED"
    if match.status == MATCH_IN_PROGRESS:
        return "Live"
    mins = int((match.kickoff_time - now).total_seconds() // 60)
    if mins <= 0:
        return "Starting now"
    if mins < 120:
        return f"UPCOMING - Starts in {mins} min"
    return f"UPCOMING - Starts in {mins // 60}h"


def breakdown(day: dt.date, entries: list[DayEntry], now: dt.datetime) -> str:
    any_active = any(e.match.status != MATCH_FINISHED for e in entries)
    lines = [
        "📊 <b>Bolt Matchday Breakdown</b>",
        SEP,
        f"🏆 Current Status: {'ACTIVE GAMES' if any_active else 'ALL GAMES FINISHED'}",
        "",
    ]
    total = 0
    for i, e in enumerate(entries, start=1):
        m = e.match
        status = _match_status_label(m, now)
        if m.status == MATCH_FINISHED:
            head = (
                f"⚽ Match {i}: {esc(m.home_team)} [ {m.home_score_90min} ] vs "
                f"[ {m.away_score_90min} ] {esc(m.away_team)} ({status})"
            )
        else:
            head = f"⚽ Match {i}: {esc(m.home_team)} vs {esc(m.away_team)} ({status})"
        lines.append(head)

        if e.prediction is None:
            lines.append("• Your Prediction: ❌ Not Submitted")
        else:
            tag = ""
            if m.status == MATCH_FINISHED:
                tag = f" ({_result_label(e.prediction)} {e.prediction.calculated_points:+d} pts)"
            elif e.locked:
                tag = " (Locked)"
            lines.append(
                f"• Your Prediction: {e.prediction.predicted_home_score} - "
                f"{e.prediction.predicted_away_score}{tag}"
            )

        match_pts = (e.prediction.calculated_points if e.prediction else 0)
        for j, w in enumerate(e.wagers, start=1):
            match_pts += w.calculated_points
            if m.status == MATCH_FINISHED:
                lines.append(_wager_result_line(j, w))
            else:
                lines.append(f"• Wager {j}: {esc(w.player_name)} ({w.wager_type})")

        if m.status == MATCH_FINISHED:
            lines.append(f"👉 Total Match Points: {match_pts:+d} pts")
        else:
            lines.append("👉 Current Match Points: 0 pts")
        lines.append("")
        total += match_pts if m.status == MATCH_FINISHED else 0

    lines.append(SEP)
    lines.append(f"📈 Total Day Score: {total:+d} pts")
    lines.append("🤖 Tap /status in Private DM to fill or edit open entries.")
    return "\n".join(lines)


# --- /recap ------------------------------------------------------------------

def recap(day: dt.date, entries: list[DayEntry], now: dt.datetime) -> str:
    """Read-only recap of the user's predictions, wagers and points for a past matchday.

    Unlike /breakdown (which focuses on the upcoming/active matchday), this looks backwards at
    a day whose games are already done, so everything is final.
    """
    lines = [
        "🔁 <b>Bolt Matchday Recap</b>",
        SEP,
        f"📅 {day:%a %d %b %Y} (SGT) — final results",
        "",
    ]
    total = 0
    for i, e in enumerate(entries, start=1):
        m = e.match
        if m.status == MATCH_FINISHED:
            head = (
                f"⚽ Match {i}: {esc(m.home_team)} [ {m.home_score_90min} ] vs "
                f"[ {m.away_score_90min} ] {esc(m.away_team)}"
            )
        else:
            head = f"⚽ Match {i}: {esc(m.home_team)} vs {esc(m.away_team)} ({_match_status_label(m, now)})"
        lines.append(head)

        if e.prediction is None:
            lines.append("• Your Prediction: ❌ Not Submitted")
        else:
            tag = ""
            if m.status == MATCH_FINISHED:
                tag = f" ({_result_label(e.prediction)} {e.prediction.calculated_points:+d} pts)"
            lines.append(
                f"• Your Prediction: {e.prediction.predicted_home_score} - "
                f"{e.prediction.predicted_away_score}{tag}"
            )

        match_pts = (e.prediction.calculated_points if e.prediction else 0)
        for j, w in enumerate(e.wagers, start=1):
            match_pts += w.calculated_points
            if m.status == MATCH_FINISHED:
                lines.append(_wager_result_line(j, w))
            else:
                lines.append(f"• Wager {j}: {esc(w.player_name)} ({w.wager_type})")

        if m.status == MATCH_FINISHED:
            lines.append(f"👉 Total Match Points: {match_pts:+d} pts")
            total += match_pts
        lines.append("")

    lines.append(SEP)
    lines.append(f"📈 Total Day Score: {total:+d} pts")
    return "\n".join(lines)


# --- /groups -----------------------------------------------------------------

def groups_overview(group_ranks: list) -> str:
    lines = [
        "👥 <b>Bolt My Leagues Overview</b> 👥",
        SEP,
        "Here are the active leagues you have registered in and your current standing on "
        "their respective leaderboards.",
        "",
        "📊 YOUR LEAGUE RANKS:",
        "",
    ]
    for i, g in enumerate(group_ranks, start=1):
        lines.append(f"⚽ <b>{i}. {esc(g.group_name)}</b>")
        lines.append(f"• Your Rank: 🔹 #{g.rank} / {g.members} players")
        lines.append(f"• Your Total Score: <code>{g.points} pts</code>")
        if g.rank == 1 and g.second_username is not None:
            lines.append(f"• Runner-up: {esc(g.second_username)} ({g.second_points} pts)")
        elif g.top_username is not None:
            lines.append(f"• Leader: {esc(g.top_username)} ({g.top_points} pts)")
        lines.append("")
    lines.append(SEP)
    lines.append(
        "💡 Want to see the full leaderboard for a group? Go to that specific group chat "
        "and type /leaderboard!"
    )
    return "\n".join(lines)


# --- /leaderboard + Daily Reveal ---------------------------------------------

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _rank_marker(rank: int) -> str:
    return _MEDALS.get(rank, "🔹")


def leaderboard(group_name: str, rows: list) -> str:
    """Overall championship standings (used by /leaderboard). rows: LeaderboardRow sorted desc."""
    lines = [
        f"🏆 <b>Bolt Leaderboard — {esc(group_name)}</b> 🏆",
        SEP,
        "🏆 OVERALL CHAMPIONSHIP LEADERBOARD:",
        "",
    ]
    if not rows:
        lines.append("<i>No one has registered yet. Use /register to join.</i>")
    for i, r in enumerate(rows, start=1):
        lines.append(f"{_rank_marker(i)} {i}. {esc(r.username)} ────── {r.points} pts")
    lines.append("")
    lines.append(SEP)
    lines.append(
        "📊 Use /daily to re-view standings for just this matchday or /individual for match-by-match "
        "breakdowns!"
    )
    return "\n".join(lines)


def daily_reveal(
    group_name: str,
    day: dt.date,
    snapshot_rows: list,
    next_open_at: dt.datetime | None = None,
    note: str | None = None,
) -> str:
    """Group-chat reveal after all of a matchday's games finish. snapshot_rows: MatchdaySnapshot.

    next_open_at: when predictions for the next matchday open (T-8h before its first kickoff), or
    None if there is no upcoming matchday. Shown as a closing note.

    note: optional one-off announcement appended at the very end (e.g. a rules change).
    """
    rows = sorted(snapshot_rows, key=lambda s: s.cumulative_total_points, reverse=True)
    lines = [
        f"🔔 <b>BOLT DAILY REVEAL: {day:%d %B}</b> 🔔",
        SEP,
        "The points have been tallied for today's matches! Here is how the group performed.",
        "",
        "🏆 OVERALL CHAMPIONSHIP LEADERBOARD:",
        "",
        ""
    ]
    for i, s in enumerate(rows, start=1):
        lines.append(
            f"{_rank_marker(i)} {i}. {esc(s.username)} ────── "
            f"{s.cumulative_total_points} pts ({s.points_earned_today:+d})"
        )
    lines.append("")
    lines.append(SEP)
    lines.append(
        "📊 Use /daily to re-view standings for just this matchday or /individual to see the match-by-match "
        "point breakdowns for everyone! To see your personal breakdown for this matchday, go to "
        "your private DM and type /recap "
    )
    if next_open_at is not None:
        lines.append("")
        lines.append(
            f"⏰ Predictions for the next matchday open on {fmt_date_long(_sgt(next_open_at).date())} "
            f"at {fmt_time(next_open_at)} — the bot will DM every user the slate then. Be ready! ⚽"
        )
    if note:
        lines.append("")
        lines.append(SEP)
        lines.append(note)
    return "\n".join(lines)


# --- /daily ------------------------------------------------------------------

def daily_standings(day: dt.date, snapshot_rows: list) -> str:
    rows = sorted(snapshot_rows, key=lambda s: s.points_earned_today, reverse=True)
    lines = [
        f"📅 <b>Bolt Standings: {day:%B %d} ONLY</b> 📅",
        SEP,
        "Here is the standalone leaderboard for the last completed matchday.",
        "",
        f"🌟 MATCHDAY {day:%d %b} RANKINGS:",
        "",
    ]
    for i, s in enumerate(rows, start=1):
        marker = _MEDALS.get(i, f"{i}.")
        champ = " (🔥 Daily Champion!)" if i == 1 else ""
        lines.append(
            f"{marker} {i}. {esc(s.username)} ────── {s.points_earned_today:+d} pts{champ}"
        )
    lines.append("")
    lines.append(SEP)
    lines.append("🏆 Want to see the overall tournament standings? Type /leaderboard instead!")
    return "\n".join(lines)


# --- /individual -------------------------------------------------------------

def individual(day: dt.date, match_breakdowns: list) -> str:
    """match_breakdowns: list of dicts {match, rows:[{username, pred_pts, wager_pts, total,
    participated}]} in match order; rows pre-sorted by group standing."""
    lines = [
        f"📋 <b>Bolt Daily Breakdown: Matchday {day:%d %b %Y}</b> 📋",
        SEP,
        "Here is how everyone scored on the last completed matchday.",
        "Format: [ Prediction Pts / Wager Pts = Total Game Pts ]",
        "",
    ]
    for i, block in enumerate(match_breakdowns, start=1):
        m = block["match"]
        score = ""
        if m.status == MATCH_FINISHED:
            score = f" ({m.home_score_90min} - {m.away_score_90min})"
        lines.append(f"⚽ <b>MATCH {i}: {esc(m.home_team)} vs {esc(m.away_team)}{score}</b>")
        for row in block["rows"]:
            if not row["participated"]:
                lines.append(f"• {esc(row['username'])}: ❌ Did Not Participate = 0 pts")
            else:
                lines.append(
                    f"• {esc(row['username'])}: {row['pred_pts']:+d} / {row['wager_pts']:+d} "
                    f"= {row['total']:+d} pts"
                )
        lines.append("")
    lines.append(SEP)
    lines.append(
        "💡 Missed a game? Remember, your breakdown will output 0 for any match you do not "
        "participate in!"
    )
    return "\n".join(lines)


# --- /faq --------------------------------------------------------------------

def faq() -> str:
    """Answers the common doubts users have about how Bolt works. Numbers are pulled from
    the engine constants so this can never drift from the actual scoring."""
    return "\n".join([
        "❓ <b>Bolt FAQ — Common Questions</b> ❓",
        SEP,
        "<b>Do I make separate predictions for each group?</b>",
        "No. Your predictions are <i>yours</i>, not a group's. You submit a prediction "
        "once and it counts toward every group you're registered in. Make it once, "
        "compete everywhere.",
        "",
        "<b>What if I join a group halfway through?</b>",
        "Any predictions you already made still count for that group going forward — you "
        "don't lose your history. If you've never played before, you can only start "
        "predicting from the next matchday whose games haven't locked yet.",
        "",
        "<b>Can I be in more than one group?</b>",
        "Yes. Use /groups to see every league you're in and your ranking in each. One set "
        "of predictions feeds all of them.",
        "",
        "<b>Can other people see my predictions?</b>",
        "No. Predictions and wagers are private — made in our DM. Nobody in the group "
        "sees them until games finish and points are revealed.",
        "",
        "<b>When can I make or change predictions?</b>",
        f"Predictions for all games in a matchday will open <b>8 hours</b> before the first game. You can make your predictions any time after that up until <b>{matches_service.LOCK_LEAD_MINUTES} minutes</b> before each "
        "kickoff, when lineups drop and that match locks. Before then, edit freely with "
        "/status; after, you can still view but not change.",
        "",
        "<b>What if I forget to predict a game?</b>",
        "You only earn (or lose) points on games you actually entered. Miss one and it "
        "simply scores 0 — it won't drag you down, but you leave points on the table. "
        "I'll DM you a reminder <b>4 hours</b> before the earliest game's kickoff if you're missing any for that matchday.",
        "",
        "<b>How are points actually calculated?</b>",
        "Send /scoring for the full points system and how wagers work.",
        SEP,
        "🤖 DM me /status to make predictions, or /help for the full command list.",
    ])


# --- /scoring ----------------------------------------------------------------

def scoring() -> str:
    """The points system in detail, including the high-risk / high-reward nature of wagers.
    Numbers are pulled from the engine constants so this can never drift."""
    exact_total = POINTS_CORRECT_RESULT + POINTS_EXACT_BONUS
    return "\n".join([
        "💯 <b>Bolt Scoring</b> 💯",
        SEP,
        "Predict scorelines and back players to earn points, then climb your group's "
        "leaderboard. Here's exactly how points work.",
        "",
        "🔮 <b>SCORE PREDICTIONS</b>",
        f"• Correct result (win / draw / loss): <b>+{POINTS_CORRECT_RESULT}</b> pts",
        f"• Exact scoreline: <b>+{POINTS_EXACT_BONUS}</b> more "
        f"(<b>{exact_total}</b> pts total)",
        "• Wrong result: <b>0</b> pts",
        "",
        "🎯 <b>PLAYER WAGERS — go big or go home</b>",
        "Wagers are your wildcard. Unlike predictions, they swing <i>both ways</i>: nail "
        "one and you surge up the table, miss and you take the hit. High risk, high reward.",
        f"• Up to <b>{MAX_WAGERS_PER_MATCH}</b> wagers per match.",
        "• <b>SCORE</b> wager hits if your player scores 1+ goal.",
        "• <b>ASSIST</b> wager hits if your player records 1+ assist.",
        "• <b>CARD</b> wager hits if your player gets booked (yellow or red card).",
        f"• Hit: <b>+{POINTS_WAGER_HIT}</b> pts  |  "
        f"Miss: <b>{POINTS_WAGER_MISS}</b> pts",
        "• 🩹 Void (player plays 0 minutes): <b>0</b> pts — no deduction, no risk.",
        f"• Back all {MAX_WAGERS_PER_MATCH} and call them right? "
        f"<b>+{POINTS_WAGER_HIT * MAX_WAGERS_PER_MATCH}</b> pts on one match. "
        "Get greedy and wrong? Just as far the other way.",
        "",
        "🔒 <b>LOCKING</b>",
        f"• Predictions and wagers lock <b>{matches_service.LOCK_LEAD_MINUTES} minutes</b> "
        "before each kickoff.",
        "• Before then you can edit freely; after, you can only view.",
        SEP,
        "🤖 DM me /status to make predictions, or /faq for common questions.",
    ])
