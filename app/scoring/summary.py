"""The completed-match summary: everything a finished match needs to show.

Gathers both innings scorecards into one response: the teams and the result,
who won and by how much, each innings' total and overs, the toss, the venue and
date, the (human-chosen) player of the match, the top run scorer and the best
bowling figures from either side, and a list of one-line highlights.

The top run scorer, best bowler and player of the match deliberately come from
the whole match, not from the winning team: a match summary is only faithful if
a losing team's best effort is still shown.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.models import Match
from app.players.models import Player
from app.scoring.crud import ScoringRepository
from app.scoring.engine import ScoreEngine, ScoreEngineError
from app.scoring.models import Innings
from app.teams.models import PlayerTeamAssignment, Team


async def _load_teams(db: AsyncSession, match: Match) -> dict[int, dict]:
    ids = {match.team_a_id, match.team_b_id, match.toss_winner_id}
    result = await db.execute(select(Team.id, Team.name, Team.short_name).where(Team.id.in_(ids)))
    return {row[0]: {"id": row[0], "name": row[1], "short_name": row[2]} for row in result.all()}


async def _list_innings(db: AsyncSession, match_id: int) -> list[Innings]:
    result = await db.execute(
        select(Innings)
        .where(Innings.match_id == match_id)
        .order_by(Innings.innings_number, Innings.id)
    )
    return list(result.scalars().all())


async def _scorecards(db: AsyncSession, innings_list: list[Innings]) -> dict[int, object]:
    """Each innings' scorecard, keyed by innings id.

    A scorecard error or an innings with no batting order yields ``None`` so one
    broken innings does not take the summary down with it.
    """
    engine = ScoreEngine(ScoringRepository(db))
    cards: dict[int, object] = {}
    for innings in innings_list:
        try:
            cards[innings.id] = await engine.get_scorecard(innings.id)
        except ScoreEngineError:
            cards[innings.id] = None
    return cards


def _derive_result(
    innings_list: list[Innings],
    cards: dict[int, object],
) -> tuple[int | None, str | None]:
    """The winner (team id) and a short margin string, structurally derived.

    Mirrors the official result rules: a chase that reaches the target wins by
    wickets, a chase that falls short loses by runs, an exact tie goes to the
    Super Over when one was played. Never derives a winner from a not-yet
    complete chase.
    """
    normal = [i for i in innings_list if not i.is_super_over]
    if len(normal) >= 2:
        first, second = normal[0], normal[1]
        first_card = cards.get(first.id)
        second_card = cards.get(second.id)
        if first_card is not None and second_card is not None and second_card.completed:
            if second_card.total > first_card.total:
                wickets_left = 10 - second_card.wickets
                margin = (
                    "last ball"
                    if wickets_left < 1
                    else f"{wickets_left} wicket{'s' if wickets_left != 1 else ''}"
                )
                return second.batting_team_id, margin
            if second_card.total < first_card.total:
                margin = first_card.total - second_card.total
                return first.batting_team_id, f"{margin} run{'s' if margin != 1 else ''}"

    super_overs = [i for i in innings_list if i.is_super_over]
    if super_overs:
        chase = super_overs[-1]
        setter = next(
            (i for i in super_overs if i.innings_number == chase.innings_number - 1), None
        )
        if setter is not None:
            setter_card = cards.get(setter.id)
            chase_card = cards.get(chase.id)
            if setter_card is not None and chase_card is not None:
                if chase_card.total > setter_card.total:
                    diff = chase_card.total - setter_card.total
                    return chase.batting_team_id, f"{diff} run{'s' if diff != 1 else ''}"
                if setter_card.total > chase_card.total:
                    diff = setter_card.total - chase_card.total
                    return setter.batting_team_id, f"{diff} run{'s' if diff != 1 else ''}"

    return None, None


def _player_name(first: str | None, last: str | None, player_id: int) -> str:
    if first or last:
        return f"{first or ''} {last or ''}".strip()
    return f"Player {player_id}"


def _run_display(runs: int, out: bool) -> str:
    return f"{runs}{'' if out else '*'}"


def _aggregate_batsmen(innings_list: list[Innings], cards: dict[int, object]) -> dict[int, dict]:
    totals: dict[int, dict] = {}
    for innings in innings_list:
        card = cards.get(innings.id)
        if card is None:
            continue
        for b in card.batsmen:
            if b.did_not_bat:
                continue
            acc = totals.setdefault(
                b.player_id,
                {
                    "player_id": b.player_id,
                    "first_name": b.first_name,
                    "last_name": b.last_name,
                    "player_code": b.player_code,
                    "team_id": innings.batting_team_id,
                    "runs": 0,
                    "balls_faced": 0,
                    "fours": 0,
                    "sixes": 0,
                },
            )
            acc["runs"] += b.runs
            acc["balls_faced"] += b.balls_faced
            acc["fours"] += b.fours
            acc["sixes"] += b.sixes
    return totals


def _aggregate_bowlers(innings_list: list[Innings], cards: dict[int, object]) -> dict[int, dict]:
    totals: dict[int, dict] = {}
    for innings in innings_list:
        card = cards.get(innings.id)
        if card is None:
            continue
        for b in card.bowlers:
            acc = totals.setdefault(
                b.player_id,
                {
                    "player_id": b.player_id,
                    "first_name": b.first_name,
                    "last_name": b.last_name,
                    "player_code": b.player_code,
                    "team_id": innings.bowling_team_id,
                    "balls_bowled": 0,
                    "runs_conceded": 0,
                    "wickets": 0,
                    "maidens": 0,
                },
            )
            acc["balls_bowled"] += b.balls_bowled
            acc["runs_conceded"] += b.runs_conceded
            acc["wickets"] += b.wickets
            acc["maidens"] += b.maidens
    return totals


def _build_highlights(
    innings_list: list[Innings],
    cards: dict[int, object],
    top_scorer: dict | None,
    best_bowler: dict | None,
) -> list[dict]:
    highlights: list[dict] = []
    for innings in innings_list:
        card = cards.get(innings.id)
        if card is None:
            continue
        for b in card.batsmen:
            if b.did_not_bat or b.runs == 0:
                continue
            if b.runs >= 100:
                kind, label = "century", "century"
            elif b.runs >= 50:
                kind, label = "fifty", "fifty"
            else:
                continue
            name = _player_name(b.first_name, b.last_name, b.player_id)
            highlights.append(
                {
                    "type": kind,
                    "text": (
                        f"{name} scored {_run_display(b.runs, b.out)} off {b.balls_faced} "
                        f"- a {label}"
                    ),
                    "player_id": b.player_id,
                    "team_id": innings.batting_team_id,
                }
            )
        for b in card.bowlers:
            if b.wickets >= 5:
                kind, label = "five_wicket_haul", "five-wicket haul"
            elif b.wickets >= 4:
                kind, label = "four_wicket_haul", "four-wicket haul"
            else:
                continue
            name = _player_name(b.first_name, b.last_name, b.player_id)
            highlights.append(
                {
                    "type": kind,
                    "text": f"{name} claimed a {label} with {b.wickets}/{b.runs_conceded}",
                    "player_id": b.player_id,
                    "team_id": innings.bowling_team_id,
                }
            )

    if top_scorer is not None:
        name = _player_name(
            top_scorer["first_name"], top_scorer["last_name"], top_scorer["player_id"]
        )
        highlights.append(
            {
                "type": "top_score",
                "text": (
                    f"{name} top-scored with {top_scorer['runs']} off {top_scorer['balls_faced']}"
                ),
                "player_id": top_scorer["player_id"],
                "team_id": top_scorer["team_id"],
            }
        )

    if best_bowler is not None:
        name = _player_name(
            best_bowler["first_name"], best_bowler["last_name"], best_bowler["player_id"]
        )
        full, rem = divmod(best_bowler["balls_bowled"], 6)
        highlights.append(
            {
                "type": "best_bowling",
                "text": (
                    f"{name} produced the best spell - {best_bowler['wickets']}/"
                    f"{best_bowler['runs_conceded']} in {full}.{rem}"
                ),
                "player_id": best_bowler["player_id"],
                "team_id": best_bowler["team_id"],
            }
        )
    return highlights


async def get_match_summary(db: AsyncSession, match: Match) -> dict:
    """Build the full match summary payload for ``match``."""
    teams = await _load_teams(db, match)
    innings_list = await _list_innings(db, match.id)
    cards = await _scorecards(db, innings_list)

    winner_id, margin = _derive_result(innings_list, cards)

    innings_output = []
    for innings in innings_list:
        card = cards.get(innings.id)
        if card is None:
            continue
        team = teams.get(innings.batting_team_id)
        if team is None:
            continue
        innings_output.append(
            {
                "innings_number": innings.innings_number,
                "team": team,
                "total": card.total,
                "wickets": card.wickets,
                "overs_bowled": card.overs_bowled,
                "overs_bowled_str": card.overs_bowled_str,
                "run_rate": card.current_run_rate,
                "extras": card.extras,
                "target": innings.target,
                "is_super_over": innings.is_super_over,
                "completed": card.completed,
            }
        )

    bat_totals = _aggregate_batsmen(innings_list, cards)
    bowl_totals = _aggregate_bowlers(innings_list, cards)

    top_scorer = None
    if bat_totals:
        top_scorer = max(
            bat_totals.values(),
            key=lambda a: (a["runs"], -a["balls_faced"]),
        )
        top_scorer["strike_rate"] = (
            round(top_scorer["runs"] * 100 / top_scorer["balls_faced"], 2)
            if top_scorer["balls_faced"]
            else 0.0
        )

    best_bowler = None
    if bowl_totals:
        best_bowler = max(
            bowl_totals.values(),
            key=lambda a: (a["wickets"], -a["runs_conceded"], -a["balls_bowled"]),
        )
        full, rem = divmod(best_bowler["balls_bowled"], 6)
        best_bowler["overs"] = round(full + rem / 6, 2)
        best_bowler["overs_str"] = f"{full}.{rem}"
        best_bowler["economy"] = (
            round(best_bowler["runs_conceded"] / (best_bowler["balls_bowled"] / 6), 2)
            if best_bowler["balls_bowled"]
            else 0.0
        )

    highlights = _build_highlights(innings_list, cards, top_scorer, best_bowler)

    player_of_match = None
    if match.player_of_match_id is not None:
        player_of_match = await _resolve_player_of_match(db, match, teams)

    return {
        "match_id": match.id,
        "status": match.status,
        "result_text": match.result,
        "winner": teams.get(winner_id) if winner_id is not None else None,
        "margin": margin,
        "toss": {
            "winner": teams.get(match.toss_winner_id),
            "decision": match.toss_decision,
        }
        if match.toss_winner_id in teams
        else None,
        "venue": match.venue,
        "match_date": match.match_date,
        "match_time": match.match_time,
        "match_type": match.match_type,
        "teams": [teams.get(tid) for tid in (match.team_a_id, match.team_b_id) if tid in teams],
        "innings": innings_output,
        "player_of_match": player_of_match,
        "top_run_scorer": top_scorer,
        "best_bowler": best_bowler,
        "highlights": highlights,
    }


async def _resolve_player_of_match(
    db: AsyncSession, match: Match, teams: dict[int, dict]
) -> dict | None:
    player_id = match.player_of_match_id
    result = await db.execute(
        select(Player.first_name, Player.last_name, Player.player_code).where(
            Player.id == player_id
        )
    )
    row = result.first()
    if row is None:
        return None
    first_name, last_name, player_code = row

    team_id = None
    result = await db.execute(
        select(PlayerTeamAssignment.team_id).where(
            PlayerTeamAssignment.player_id == player_id,
            PlayerTeamAssignment.team_id.in_([match.team_a_id, match.team_b_id]),
        )
    )
    assigned = result.scalars().first()
    if assigned is not None:
        team_id = assigned

    return {
        "player_id": player_id,
        "first_name": first_name,
        "last_name": last_name,
        "player_code": player_code,
        "team_id": team_id,
        "team_name": teams.get(team_id, {}).get("name") if team_id is not None else None,
    }
