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
# Each was verified by hand against /players/squads or /players/profiles (name + nationality).
# 16 fringe players remain unmapped (not present in the API at all) and stay NULL -> name fallback.
MANUAL_OVERRIDES: dict[int, int] = {
    # --- matched against the team's /players/squads list ---
    4050427: 304229,   # Algeria: Melvin Masstil -> M. Mastil
    44580017: 441269,  # Australia: Paul Okon Jr -> Paul Okon-Engstler
    96903868: 280,     # Brazil: Alisson -> Alisson Becker
    55899417: 41608,   # Cape Verde: Diney -> Diney Borges
    87855951: 2660,    # Egypt: Nabil Emad -> Nabil Emad Dunga
    56650764: 307123,  # England: Nico O'Reilly -> N. Oreilly
    52755819: 21996,   # Ghana: Baba Abdul Rahman -> A. Baba
    50860283: 303467,  # Ghana: Abdul Fatawu Issahaku -> A. Fatawu
    52449326: 128766,  # Haiti: Don Deedson Louicius -> L. Deedson
    697460: 2685,      # Iran: Ehsan Hajsafi -> E. Hajisafi
    14040953: 2687,    # Iran: Hossein Kanaani -> H. Kanani
    68713332: 533035,  # Iran: Ali Nemati ... -> A. Nemati
    40225991: 53886,   # Iraq: Jalal Hassan -> Jalal Hassan Hachim
    50012126: 295394,  # Iraq: Manaf Younis -> Munaf Younus
    79472066: 626479,  # Iraq: Zaid Ismail -> Z. Ismaeel
    95011070: 229112,  # Iraq: Ahmed Qasim -> A. Qasem
    57515889: 542710,  # Jordan: Mohammed Abu Hashish -> M. Abu Hasheesh
    52513921: 651096,  # Jordan: Mohammed Al-Dawoud -> M. Al Daoud
    58398948: 542768,  # Jordan: Ibrahim Sadeh -> Ibrahim Sa'deh
    95700263: 2702,    # Morocco: Munir El Kajoui -> M. Mohamedi (Mohamedi El Kajoui)
    49046920: 2979,    # Panama: José Luis Rodríguez -> J. Rodríguez
    50771410: 41112,   # Portugal: Francisco Trincão -> Trincão
    62004026: 543059,  # Saudi Arabia: Jehad Thikri -> J. Thakri
    60450927: 409303,  # Senegal: El Hadji Malick Diouf -> E. Diouf
    89782268: 2990,    # Senegal: Idrissa Gana Gueye -> I. Gueye
    26434883: 237129,  # Senegal: Pape Matar Sarr -> P. Sarr
    22536808: 630895,  # Senegal: Bara Sapoko Ndiaye -> Bara Ndiaye
    44024272: 2890,    # South Korea: Jo Hyun-Woo -> Jo Hyeon-Woo
    6108996: 304951,   # South Korea: Lee Ki-Hyeok -> Lee Gi-Hyuk
    67635333: 34168,   # South Korea: Kim Jin-Kyu -> Kim Jin-Gyu
    78891590: 237050,  # South Korea: Um Ji-Sung -> Eom Ji-Sung
    68015084: 34710,   # South Korea: Oh Hyun-Kyu -> Oh Hyeon-Gyu
    20537854: 34211,   # South Korea: Cho Kyu-Sung -> Cho Gue-Sung
    70907041: 396623,  # Spain: Pau Cubarsí -> Pau Cubarsí Paredes
    47718202: 49423,   # Tunisia: Sabri Ben Hessen -> S. Ben Hsan
    30661636: 533394,  # Tunisia: Abdelmouhib Chamakh -> C. Abdelmouhib
    71585590: 135059,  # Tunisia: Mohamed Amine Ben Hamida -> A. Ben Hmida
    2959414: 1640,     # Türkiye: Hakan Calhanoglou -> H. Çalhanoglu
    10985028: 73514,   # Uzbekistan: Sherzod Nasrullaev -> S. Nasrullayev
    44589340: 73520,   # Uzbekistan: Azizjon Ganiev -> A. Ganiyev
    68797511: 72127,   # Uzbekistan: Oston Urunov -> O. Orunov
    # --- not in the squad list; resolved via /players/profiles (name + nationality) ---
    52887103: 6,       # Argentina: Leonardo Balerdi
    34475448: 715,     # Austria: Christoph Baumgartner
    23927299: 70480,   # Bosnia: Osman Hadzikic
    81288401: 284061,  # Canada: Marcelo Flores (Flores Dorrell)
    26027465: 425770,  # Czechia: Jan Koutny
    77202073: 18930,   # Czechia: Matej Vydra
    7618765: 8500,     # Japan: Wataru Endo
    82592213: 21694,   # Morocco: Nayef Aguerd
    15255538: 181421,  # Morocco: Abde Ezzalzouli
    52693289: 191189,  # Ivory Coast: Clément Akpa
    28491774: 432841,  # Jordan: Ibrahim Sabra
    21227778: 41960,   # Portugal: Ricardo Velho
    28362657: 130423,  # Scotland: Billy Gilmour
    74448164: 47985,   # Sweden: Emil Holm
    # --- same player, but first-initial guard (correctly) rejects the auto-match; verified by hand ---
    79399651: 53902,   # Jordan: Ihsan Haddad -> Ehsan Haddad
    94349827: 16831,   # Egypt: El Mahdy Soliman -> Al Mahdi Soliman
    98969031: 270774,  # Mexico: Raúl Rangel (squad lists a different Rangel; id from fixture stats)
    30138358: 38746,   # Netherlands: Jurriën Timber (brother Quinten is 38747)
    52651819: 44309,   # Saudi Arabia: Sultan Al-Ghannam
    38509327: 53894,   # Iraq: Mohanad Ali (listed in squad under nickname "Meme")
}

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


def _first_initial(name: str) -> str:
    tokens = _norm(name).split()
    return tokens[0][0] if tokens else ""


def _is_mononym(name: str) -> bool:
    return len(_norm(name).split()) == 1


class TeamIndex:
    def __init__(self, players: list[tuple[int, str]]) -> None:
        self.players = players
        self.name_by_id = dict(players)
        self.by_exact: dict[str, list[int]] = defaultdict(list)
        self.by_initial: dict[str, list[int]] = defaultdict(list)
        self.by_surname: dict[str, list[int]] = defaultdict(list)
        for api_id, name in players:
            self.by_exact[_norm(name)].append(api_id)
            self.by_initial[_initial_key(name)].append(api_id)
            self.by_surname[_surname_key(name)].append(api_id)

    def _initial_ok(self, name: str, api_id: int) -> bool:
        """Guard for the loose tiers: the squad entry must be a mononym (distinctive nickname
        like 'Trézéguet') or share the first initial. Without this, a unique surname match would
        wrongly bind a player to a same-surname teammate (e.g. Jurriën -> Quinten Timber, or
        Jo Yu-Min -> Son Heung-Min once the hyphen makes 'Min' look like the surname)."""
        cand = self.name_by_id[api_id]
        return _is_mononym(cand) or _first_initial(cand) == _first_initial(name)

    def resolve(self, name: str) -> tuple[int | None, str]:
        # exact + initial keys are strict (full surname tokens must match), so a unique hit is safe.
        for label, key_fn, idx in (
            ("exact", _norm, self.by_exact),
            ("initial", _initial_key, self.by_initial),
        ):
            hits = idx.get(key_fn(name), [])
            if len(hits) == 1:
                return hits[0], label
            if len(hits) > 1:
                return None, f"ambiguous-{label}"

        # surname: unique surname, but only if the first initial agrees (or squad entry is a mononym).
        hits = self.by_surname.get(_surname_key(name), [])
        if len(hits) == 1:
            if self._initial_ok(name, hits[0]):
                return hits[0], "surname"
            return None, "surname-initial-mismatch"
        if len(hits) > 1:
            return None, "ambiguous-surname"

        # Fuzzy fallback: best token-sorted similarity, must clear threshold + margin AND agree on
        # the first initial (so a high score on a same-surname relative can't slip through).
        target = _sorted_tokens(name)
        scored = sorted(
            ((SequenceMatcher(None, target, _sorted_tokens(n)).ratio(), pid, n)
             for pid, n in self.players),
            reverse=True,
        )
        if (
            scored
            and scored[0][0] >= FUZZY_ACCEPT
            and (len(scored) == 1 or scored[0][0] - scored[1][0] >= FUZZY_MARGIN)
            and self._initial_ok(name, scored[0][1])
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
            # final_id is what we write (None clears any stale value from a prior run).
            if row.api_player_id in MANUAL_OVERRIDES:
                final_id, tier = MANUAL_OVERRIDES[row.api_player_id], "manual"
            else:
                team_key = TEAM_ALIASES.get(_norm(row.team_name), _norm(row.team_name))
                index = indexes.get(team_key)
                if index is None:
                    final_id, reason, tier = None, "no-team", "no-team"
                else:
                    final_id, reason = index.resolve(row.player_name)
                    tier = reason.split(":", 1)[0]

            counts[tier] += 1
            if not dry_run:
                row.player_id = final_id  # always assign so stale wrong ids get reset to NULL
            if final_id is None:
                unmatched.append(f"  [{reason:<18}] {row.team_name:<20} {row.player_name}")
            elif tier == "fuzzy":
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
