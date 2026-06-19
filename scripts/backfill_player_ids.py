"""One-time backfill: resolve each world_cup_players row to its canonical API-Football id.

Why: world_cup_players.api_player_id is from a different id namespace than API-Football's
/fixtures/players stats, so scoring could only match wagers by fuzzy name. This populates the
new world_cup_players.player_id column with the canonical id (same namespace as /fixtures/players
and /players/squads) so scoring can join by id and never name-match again.

Matching is done ONCE here, offline, with a printed report — not at scoring time. For each
world_cup_players row we find its API team (with an alias map), then match the player within that
team's squad through tiers, strongest first:

    1. exact   — normalized full name is identical          (e.g. "Lisandro Martínez")
    2. initial — unique first-initial + surname key match    ("Thibaut Courtois" <-> "T. Courtois")
    3. surname — unique normalized-surname match
    4. fuzzy   — best token-sorted similarity within the team, only if it clears a high
                 threshold AND clearly beats the runner-up (handles transliteration drift like
                 "Rebin Sulaka" <-> "Rebin Solaka"). Flagged in the report for human review.
    5. manual  — MANUAL_OVERRIDES below, for anything the above can't resolve safely.

Only world_cup_players.player_id is written (additive). No scoring data is touched. Rows that
stay unresolved keep player_id = NULL and continue to score via the name-matching fallback, so
this is never a regression.

Usage:
    python -m scripts.backfill_player_ids            # apply
    python -m scripts.backfill_player_ids --dry-run  # report only, write nothing
"""

from __future__ import annotations

import asyncio
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

from sqlalchemy import select

from config.db import session_scope
from config.settings import settings
from database.models import WorldCupPlayer
from services.sports_api import sports_api

# world_cup_players.team_name -> API-Football team name, where they differ.
TEAM_ALIASES = {
    "bosnia-herzegovina": "bosnia & herzegovina",
    "cape verde": "cape verde islands",
    "united states": "usa",
}

# Last-resort manual mapping for players no tier can resolve safely:
#   world_cup_players.api_player_id -> canonical API-Football player_id
# Fill in from the report's "no-match" / rejected-fuzzy lines after eyeballing the squad.
MANUAL_OVERRIDES: dict[int, int] = {}

FUZZY_ACCEPT = 0.84   # minimum similarity to accept a fuzzy match
FUZZY_MARGIN = 0.08   # best must beat the runner-up by at least this much


def _norm(name: str) -> str:
    """Casefold, strip diacritics, and drop punctuation (hyphens, apostrophes, dots)."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in stripped)
    return " ".join(cleaned.casefold().split())


def _initial_key(name: str) -> str:
    tokens = _norm(name).split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return tokens[0][0] + " " + " ".join(tokens[1:])


def _surname_key(name: str) -> str:
    tokens = _norm(name).split()
    return tokens[-1] if tokens else ""


def _sorted_tokens(name: str) -> str:
    return " ".join(sorted(_norm(name).split()))


async def _fetch_api_squads() -> dict[str, list[tuple[int, str]]]:
    """Return {normalized_api_team_name: [(api_player_id, player_name), ...]} for all WC teams."""
    teams = await sports_api._get(
        "/teams", {"league": settings.LEAGUE_ID, "season": settings.SEASON}
    )
    squads: dict[str, list[tuple[int, str]]] = {}
    for entry in teams:
        team = entry["team"]
        resp = await sports_api._get("/players/squads", {"team": team["id"]})
        players = [
            (p["id"], p["name"]) for block in resp for p in block.get("players", [])
        ]
        squads[_norm(team["name"])] = players
    print(f"  fetched {len(squads)} squads")
    return squads


class TeamIndex:
    def __init__(self, players: list[tuple[int, str]]) -> None:
        self.players = players
        self.by_exact: dict[str, list[int]] = defaultdict(list)
        self.by_initial: dict[str, list[int]] = defaultdict(list)
        self.by_surname: dict[str, list[int]] = defaultdict(list)
        for api_id, name in players:
            self.by_exact[_norm(name)].append(api_id)
            self.by_initial[_initial_key(name)].append(api_id)
            self.by_surname[_surname_key(name)].append(api_id)

    def resolve(self, name: str) -> tuple[int | None, str]:
        for label, idx in (
            ("exact", self.by_exact),
            ("initial", self.by_initial),
            ("surname", self.by_surname),
        ):
            hits = idx.get(
                {"exact": _norm, "initial": _initial_key, "surname": _surname_key}[label](
                    name
                ),
                [],
            )
            if len(hits) == 1:
                return hits[0], label
            if len(hits) > 1:
                return None, f"ambiguous-{label}"

        # Fuzzy fallback: best token-sorted similarity, must clear threshold + margin.
        target = _sorted_tokens(name)
        scored = sorted(
            ((SequenceMatcher(None, target, _sorted_tokens(n)).ratio(), pid, n)
             for pid, n in self.players),
            reverse=True,
        )
        if scored and scored[0][0] >= FUZZY_ACCEPT and (
            len(scored) == 1 or scored[0][0] - scored[1][0] >= FUZZY_MARGIN
        ):
            return scored[0][1], f"fuzzy:{scored[0][0]:.2f}->{scored[0][2]}"
        return None, "no-match"


async def main(dry_run: bool) -> None:
    print("Fetching API-Football squads for all World Cup teams...")
    squads = await _fetch_api_squads()
    indexes = {team: TeamIndex(pl) for team, pl in squads.items()}

    counts: dict[str, int] = defaultdict(int)
    fuzzy_log: list[str] = []
    unmatched: list[str] = []

    async with session_scope() as session:
        rows = (await session.execute(select(WorldCupPlayer))).scalars().all()
        for row in rows:
            if row.api_player_id in MANUAL_OVERRIDES:
                counts["manual"] += 1
                if not dry_run:
                    row.player_id = MANUAL_OVERRIDES[row.api_player_id]
                continue

            team_key = _norm(row.team_name)
            team_key = TEAM_ALIASES.get(team_key, team_key)
            index = indexes.get(team_key)
            if index is None:
                counts["no-team"] += 1
                unmatched.append(f"  [no-team           ] {row.team_name:<20} {row.player_name}")
                continue

            api_id, reason = index.resolve(row.player_name)
            tier = reason.split(":", 1)[0]
            counts[tier] += 1
            if api_id is None:
                unmatched.append(f"  [{reason:<18}] {row.team_name:<20} {row.player_name}")
            else:
                if not dry_run:
                    row.player_id = api_id
                if tier == "fuzzy":
                    fuzzy_log.append(f"  [{reason}]  {row.team_name} / {row.player_name}")
        if dry_run:
            await session.rollback()

    total = len(rows)
    resolved = sum(counts[t] for t in ("exact", "initial", "surname", "fuzzy", "manual"))
    print(f"\n{'DRY RUN — nothing written' if dry_run else 'Backfill applied'}")
    print(f"Resolved: {resolved}/{total}")
    for tier in ("exact", "initial", "surname", "fuzzy", "manual"):
        if counts[tier]:
            print(f"  {tier:<8}: {counts[tier]}")
    if fuzzy_log:
        print(f"\nFuzzy matches to review ({len(fuzzy_log)}):")
        print("\n".join(fuzzy_log))
    if unmatched:
        print(f"\nUnresolved ({len(unmatched)}) — left NULL, will use name fallback:")
        print("\n".join(unmatched))


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
