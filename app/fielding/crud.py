"""Reading and writing the field setup, with the rules that keep it legal.

Three invariants do the heavy lifting and both the schema constraints and
these functions enforce them, so neither can be lost by a race:

* the player named as keeper, or given a position, must belong to the
  *fielding* side - either its playing XI, or a reserve who has already come
  on through the substitution workflow. A batting-team player, a stranger and
  a reserve who has not been substituted on are refused with distinct
  messages, because "you picked the opposition", "he is not in the XI" and
  "send him on properly first" need different fixes;
* a side never fields more than :data:`MAX_ACTIVE_FIELDERS` players, and one
  player never stands in two places;
* an ordinary substitute may not bowl or keep wicket unless the competition's
  rules say otherwise (see :mod:`app.fielding.rules`).

Every write is a full replace applied in one transaction, so a save can never
leave a player on the field twice or a fielder without a position.

The fielding side itself is derived the same way scoring derives it: the toss
decides innings 1, and each later innings swaps. When the innings row already
exists its ``bowling_team_id`` is authoritative, which keeps the panel honest
after a Super Over.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import SquadRole
from app.fielding.models import FieldingAssignment, FieldingSetup, FieldSubstitution
from app.fielding.positions import (
    ANCHORS,
    MAX_ACTIVE_FIELDERS,
    NON_SPOT_KEYS,
    POSITIONS,
    label_for,
)
from app.fielding.rules import load_rules, replacement_type_for, validate_reason
from app.fielding.schemas import (
    FieldingEligiblePlayer,
    FieldingPositionCatalogEntry,
    FieldingPositionOut,
    FieldingSetupResponse,
    FieldingSetupUpsert,
    FieldingSubstitutionIn,
    FieldingSubstitutionOut,
    SubstitutionReasonOption,
    SubstitutionRulesOut,
)
from app.matches.models import Match
from app.players.models import Player
from app.scoring.models import Delivery, Innings
from app.teams.models import PlayerTeamAssignment, Team


async def derive_fielding_team_id(
    db: AsyncSession, match: Match, innings_number: int
) -> tuple[int | None, str | None]:
    """Which side fields in this innings, before or after the innings exists.

    Returns ``(team_id, error)``. The innings row wins when present; otherwise
    the toss decides innings 1 and innings 2 is its mirror. Innings 3+ (Super
    Over) must exist as a row to be knowable.
    """
    innings = (
        (
            await db.execute(
                select(Innings).where(
                    Innings.match_id == match.id, Innings.innings_number == innings_number
                )
            )
        )
        .scalars()
        .first()
    )
    if innings is not None:
        return innings.bowling_team_id, None

    if innings_number > 2:
        return None, f"Innings {innings_number} has not started for this match"

    # Toss logic, mirroring scoring.crud.start_scoring exactly.
    if match.toss_decision == "bat":
        first_batting = match.toss_winner_id
    else:
        first_batting = (
            match.team_b_id if match.toss_winner_id == match.team_a_id else match.team_a_id
        )
    first_fielding = match.team_b_id if first_batting == match.team_a_id else match.team_a_id
    if innings_number == 1:
        return first_fielding, None
    # Innings 2 fields the side that batted first, even before its row exists.
    return first_batting, None


async def playing_xi_ids(db: AsyncSession, team_id: int) -> set[int]:
    """The player ids in ``team_id``'s playing XI."""
    rows = await db.execute(
        select(PlayerTeamAssignment.player_id).where(
            PlayerTeamAssignment.team_id == team_id,
            PlayerTeamAssignment.role == SquadRole.PLAYING_11.value,
        )
    )
    return set(rows.scalars().all())


async def squad_ids(db: AsyncSession, team_id: int) -> set[int]:
    """Every player registered to ``team_id``, XI, reserves and bench alike."""
    rows = await db.execute(
        select(PlayerTeamAssignment.player_id).where(PlayerTeamAssignment.team_id == team_id)
    )
    return set(rows.scalars().all())


async def _substitution_events(
    db: AsyncSession, match_id: int, innings_number: int
) -> list[tuple[int | None, int | None, str]]:
    """The substitution history for one innings, oldest first.

    Each row is ``(incoming_id, outgoing_id, replacement_type)``; a caller can
    rebuild either "who came on" or "who is on the field now" by replaying the
    same ordered events, which is what keeps those two views consistent.
    """
    rows = await db.execute(
        select(
            FieldSubstitution.incoming_player_id,
            FieldSubstitution.outgoing_player_id,
            FieldSubstitution.replacement_type,
        )
        .join(FieldingSetup, FieldingSetup.id == FieldSubstitution.setup_id)
        .where(
            FieldingSetup.match_id == match_id,
            FieldingSetup.innings_number == innings_number,
        )
        .order_by(FieldSubstitution.id)
    )
    return [tuple(row) for row in rows.all()]


async def substitution_state(
    db: AsyncSession, match_id: int, innings_number: int
) -> dict[int, str]:
    """Who came onto the field as a substitute, and under which type.

    Replayed in order rather than set-subtracted: a player who came on, went
    off and came on again must end up counted once, and a player who went off
    must not be counted at all. The value is the replacement type of the
    *latest* event involving that player - "special" replacements (approved
    concussion replacements and the like) keep their privileges, "normal" ones
    do not.
    """
    state: dict[int, str] = {}
    for incoming, outgoing, rtype in await _substitution_events(db, match_id, innings_number):
        if outgoing:
            state.pop(outgoing, None)
        if incoming:
            state[incoming] = rtype
    return state


async def active_membership(
    db: AsyncSession, match_id: int, innings_number: int, xi: set[int]
) -> set[int]:
    """Who is on the field right now: the XI minus those who left, plus those
    who came on.

    Distinct from *placed* (holding a named position): an XI player who has
    not been given a spot yet is still one of the eleven, and a substitute who
    has come on but is not yet positioned is too. The panel shows both counts.
    """
    members = set(xi)
    for incoming, outgoing, _ in await _substitution_events(db, match_id, innings_number):
        if outgoing:
            members.discard(outgoing)
        if incoming:
            members.add(incoming)
    return members


async def _eligible_players(
    db: AsyncSession,
    team_id: int,
    *,
    on_field: dict[int, str],
    substitute_ids: dict[int, str],
    active: set[int],
) -> list[FieldingEligiblePlayer]:
    """The fielding side's XI, annotated with where each player stands now."""
    rows = await db.execute(
        select(PlayerTeamAssignment)
        .options(selectinload(PlayerTeamAssignment.player))
        .where(
            PlayerTeamAssignment.team_id == team_id,
            PlayerTeamAssignment.role == SquadRole.PLAYING_11.value,
        )
        .order_by(PlayerTeamAssignment.player_id)
    )
    out: list[FieldingEligiblePlayer] = []
    for a in rows.scalars().all():
        p: Player | None = a.player
        if p is None:
            continue
        out.append(
            FieldingEligiblePlayer(
                player_id=p.id,
                first_name=p.first_name,
                last_name=p.last_name,
                full_name=f"{p.first_name} {p.last_name}",
                player_code=p.player_code,
                profile_image=p.profile_image,
                is_captain=bool(a.is_captain),
                is_vice_captain=bool(a.is_vice_captain),
                xi_role=a.xi_role,
                squad_role=a.role,
                is_active=p.id in active,
                is_substitute=p.id in substitute_ids,
                on_field=p.id in on_field,
                position_key=on_field.get(p.id),
            )
        )
    return out


async def _reserve_players(
    db: AsyncSession,
    team_id: int,
    *,
    on_field: dict[int, str],
    substitute_ids: dict[int, str],
    active: set[int],
) -> list[FieldingEligiblePlayer]:
    """Every same-team squad member who is *not* in the playing XI.

    The reserves are read from the squad itself rather than from a fixed
    size, so a squad of 12, 15 or 20 is reported honestly and an empty
    reserve list is an empty list - never an error and never an assumption
    that a squad is 11 or 15 long.
    """
    rows = await db.execute(
        select(PlayerTeamAssignment)
        .options(selectinload(PlayerTeamAssignment.player))
        .where(
            PlayerTeamAssignment.team_id == team_id,
            PlayerTeamAssignment.role != SquadRole.PLAYING_11.value,
        )
        .order_by(PlayerTeamAssignment.player_id)
    )
    out: list[FieldingEligiblePlayer] = []
    for a in rows.scalars().all():
        p: Player | None = a.player
        if p is None:
            continue
        out.append(
            FieldingEligiblePlayer(
                player_id=p.id,
                first_name=p.first_name,
                last_name=p.last_name,
                full_name=f"{p.first_name} {p.last_name}",
                player_code=p.player_code,
                profile_image=p.profile_image,
                is_captain=bool(a.is_captain),
                is_vice_captain=bool(a.is_vice_captain),
                xi_role=None,
                squad_role=a.role,
                is_active=p.id in active,
                is_substitute=p.id in substitute_ids,
                on_field=p.id in on_field,
                position_key=on_field.get(p.id),
            )
        )
    return out


async def _player_name(db: AsyncSession, player_id: int) -> str:
    p = (await db.execute(select(Player).where(Player.id == player_id))).scalar_one_or_none()
    return f"{p.first_name} {p.last_name}" if p else f"Player #{player_id}"


async def _get_setup_row(
    db: AsyncSession, match_id: int, innings_number: int
) -> FieldingSetup | None:
    return (
        await db.execute(
            select(FieldingSetup)
            .options(
                selectinload(FieldingSetup.assignments),
                selectinload(FieldingSetup.substitutions),
            )
            .where(
                FieldingSetup.match_id == match_id,
                FieldingSetup.innings_number == innings_number,
            )
        )
    ).scalar_one_or_none()


async def _current_bowler_id(db: AsyncSession, match_id: int, innings_number: int) -> int | None:
    """Who bowled the most recent ball of this innings, if any.

    Marked as "(Bowler)" in the XI list. The bowler is an XI member standing
    at the bowling end - never a twelfth player, and never invented by the
    field panel: it is read from the scoring records the engine already
    keeps.
    """
    innings_id = (
        (
            await db.execute(
                select(Innings.id).where(
                    Innings.match_id == match_id, Innings.innings_number == innings_number
                )
            )
        )
        .scalars()
        .first()
    )
    if innings_id is None:
        return None
    bowler = (
        (
            await db.execute(
                select(Delivery.bowler_id)
                .where(Delivery.innings_id == innings_id)
                .order_by(Delivery.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return bowler


async def _substitution_history(
    db: AsyncSession, setup: FieldingSetup | None
) -> list[FieldingSubstitutionOut]:
    if setup is None:
        return []
    out: list[FieldingSubstitutionOut] = []
    for s in setup.substitutions:
        out.append(
            FieldingSubstitutionOut(
                id=s.id,
                match_id=s.match_id,
                innings_number=s.innings_number,
                team_id=s.team_id,
                outgoing_player_id=s.outgoing_player_id,
                outgoing_player_name=await _player_name(db, s.outgoing_player_id),
                incoming_player_id=s.incoming_player_id,
                incoming_player_name=await _player_name(db, s.incoming_player_id),
                reason=s.reason,
                replacement_type=s.replacement_type,
                authorized_by_user_id=s.authorized_by_user_id,
                created_at=s.created_at,
            )
        )
    return out


async def build_response(
    db: AsyncSession,
    match: Match,
    innings_number: int,
    fielding_team_id: int,
    setup: FieldingSetup | None,
    *,
    innings_started: bool,
) -> FieldingSetupResponse:
    """Assemble the full payload the Field Setup screen renders."""
    team = (await db.execute(select(Team).where(Team.id == fielding_team_id))).scalar_one_or_none()
    batting_team_id = match.team_b_id if fielding_team_id == match.team_a_id else match.team_a_id

    # Where each player currently stands, keyed by player id. Drives both
    # "placed / not yet placed" and the substitutes' on-field badge.
    on_field: dict[int, str] = {}
    assignments: list[FieldingPositionOut] = []
    if setup is not None:
        for a in setup.assignments:
            on_field[a.player_id] = a.position_key
            assignments.append(
                FieldingPositionOut(
                    player_id=a.player_id,
                    player_name=await _player_name(db, a.player_id),
                    position_key=a.position_key,
                    # A custom position shows the scorer's own label; every
                    # other position shows the catalog's name.
                    position_label=a.custom_label or label_for(a.position_key),
                    anchor_x=a.anchor_x,
                    anchor_y=a.anchor_y,
                    is_substitute=bool(a.is_substitute),
                    custom_label=a.custom_label,
                )
            )

    substitute_ids = await substitution_state(db, match.id, innings_number)
    xi = await playing_xi_ids(db, fielding_team_id)
    # Who is on the field right now: the XI minus anyone who was substituted
    # off, plus whoever came on. Independent of whether they hold a position.
    active = await active_membership(db, match.id, innings_number, xi)
    rules = load_rules()

    return FieldingSetupResponse(
        match_id=match.id,
        innings_number=innings_number,
        fielding_team_id=fielding_team_id,
        fielding_team_name=team.name if team else f"Team #{fielding_team_id}",
        batting_team_id=batting_team_id,
        wicketkeeper_player_id=setup.wicketkeeper_player_id if setup else None,
        assignments=assignments,
        eligible_players=await _eligible_players(
            db, fielding_team_id, on_field=on_field, substitute_ids=substitute_ids, active=active
        ),
        eligible_substitutes=await _reserve_players(
            db,
            fielding_team_id,
            on_field=on_field,
            substitute_ids=substitute_ids,
            active=active,
        ),
        position_catalog=[FieldingPositionCatalogEntry(**p) for p in POSITIONS],
        substitutions=await _substitution_history(db, setup),
        substitution_rules=SubstitutionRulesOut(
            enabled=rules.enabled,
            reasons=[SubstitutionReasonOption(**o) for o in rules.reason_options],
            max_per_match=rules.max_per_match,
            ordinary_can_bowl=rules.ordinary_can_bowl,
            ordinary_can_keep=rules.ordinary_can_keep,
            ordinary_can_captain=rules.ordinary_can_captain,
        ),
        max_active_fielders=MAX_ACTIVE_FIELDERS,
        current_bowler_player_id=await _current_bowler_id(db, match.id, innings_number),
        innings_started=innings_started,
    )


async def get_fielding_setup(
    db: AsyncSession, match: Match, innings_number: int
) -> FieldingSetupResponse | str:
    """The current setup for one innings, or a reason it cannot be read."""
    fielding_team_id, error = await derive_fielding_team_id(db, match, innings_number)
    if error:
        return error
    assert fielding_team_id is not None
    setup = await _get_setup_row(db, match.id, innings_number)
    innings_started = (
        await db.execute(
            select(Innings.id).where(
                Innings.match_id == match.id, Innings.innings_number == innings_number
            )
        )
    ).scalars().first() is not None
    return await build_response(
        db, match, innings_number, fielding_team_id, setup, innings_started=innings_started
    )


async def _membership_error(
    db: AsyncSession,
    match: Match,
    fielding_team_id: int,
    player_id: int,
    what: str,
    *,
    members: set[int],
) -> str | None:
    """Why ``player_id`` may not be named, or ``None`` when they may.

    ``members`` is who is on the field right now - the XI minus anyone
    substituted off, plus whoever came on. Four distinct refusals, because
    four different fixes are needed: the opposition, a stranger, a same-team
    reserve who has to be substituted on properly, and an XI player who was
    substituted *off* and therefore cannot simply be placed again.
    """
    if player_id in members:
        return None

    own_squad = await squad_ids(db, fielding_team_id)
    other_team = match.team_b_id if fielding_team_id == match.team_a_id else match.team_a_id
    other_squad = await squad_ids(db, other_team)
    if player_id in other_squad:
        return (
            f"{what} {player_id} belongs to the batting side; only the fielding "
            "side's playing XI may be named"
        )

    if player_id in own_squad:
        xi = await playing_xi_ids(db, fielding_team_id)
        if player_id in xi:
            return (
                f"{what} {player_id} was substituted off and is no longer on the "
                "field; bring a substitute on if you need a twelfth man"
            )
        return (
            f"{what} {player_id} is a reserve outside the playing XI; use "
            "'Bring Substitute Onto Field' to send them on first"
        )
    return f"{what} {player_id} is not in the fielding side's playing XI"


def _privilege_error(
    active_subs: dict[int, str], player_id: int, position_key: str, what: str
) -> str | None:
    """Refuse an ordinary substitute a job the Laws reserve for XI players.

    A normal substitute may not bowl or keep wicket; only a competition-
    approved special replacement inherits the player's privileges, and only
    when the deployment's rules allow it at all.
    """
    replacement_type = active_subs.get(player_id)
    if replacement_type is None:
        return None  # an XI player, with full privileges
    rules = load_rules()
    if replacement_type == "special":
        return None
    if position_key == "bowler" and not rules.ordinary_can_bowl:
        return (
            f"{what} {player_id} came on as an ordinary substitute and may not "
            "bowl under this competition's rules"
        )
    if position_key == "wicket_keeper" and not rules.ordinary_can_keep:
        return (
            f"{what} {player_id} came on as an ordinary substitute and may not "
            "keep wicket under this competition's rules"
        )
    return None


def _coordinates_for(position_key: str, x: float | None, y: float | None) -> tuple[float, float]:
    """The stored coordinates: the dragged drop point, else the anchor.

    ``custom_position`` and ``unassigned`` have no meaningful default, so a
    marker dropped there keeps the drop point itself (or the neutral centre
    when it was chosen from the list without being dragged).
    """
    if x is not None and y is not None:
        return float(x), float(y)
    if position_key in NON_SPOT_KEYS:
        return 50.0, 50.0
    return ANCHORS[position_key]


async def save_fielding_setup(
    db: AsyncSession, match: Match, data: FieldingSetupUpsert
) -> FieldingSetupResponse | str:
    """Replace the setup for one innings, or return the reason it is illegal."""
    fielding_team_id, error = await derive_fielding_team_id(db, match, data.innings_number)
    if error:
        return error
    assert fielding_team_id is not None

    # The innings must exist before anything can be saved against it: the
    # setup is a record of a live field, not a pre-registration.
    innings_row = (
        (
            await db.execute(
                select(Innings.id).where(
                    Innings.match_id == match.id,
                    Innings.innings_number == data.innings_number,
                )
            )
        )
        .scalars()
        .first()
    )
    if innings_row is None:
        return f"Innings {data.innings_number} has not started for this match"

    if len(data.assignments) > MAX_ACTIVE_FIELDERS:
        return (
            f"a side fields at most {MAX_ACTIVE_FIELDERS} players; "
            f"{len(data.assignments)} were sent"
        )

    active_subs = await substitution_state(db, match.id, data.innings_number)
    xi = await playing_xi_ids(db, fielding_team_id)
    # Who is on the field right now: exactly who may be named, which is the
    # XI minus anyone substituted off plus whoever came on.
    members = await active_membership(db, match.id, data.innings_number, xi)

    async def check_member(player_id: int, what: str) -> str | None:
        return await _membership_error(
            db,
            match,
            fielding_team_id,
            player_id,
            what,
            members=members,
        )

    if data.wicketkeeper_player_id is not None:
        msg = await check_member(data.wicketkeeper_player_id, "Wicketkeeper")
        if msg:
            return msg
        msg = _privilege_error(
            active_subs, data.wicketkeeper_player_id, "wicket_keeper", "Wicketkeeper"
        )
        if msg:
            return msg

    for a in data.assignments:
        msg = await check_member(a.player_id, f"Fielder in position '{a.position_key}'")
        if msg:
            return msg
        msg = _privilege_error(active_subs, a.player_id, a.position_key, "Fielder")
        if msg:
            return msg

    # The wicket_keeper anchor belongs to the keeper and nobody else: the
    # designation lives in its own column, and this keeps the diagram from
    # drawing a second pair of gloves.
    keeper = data.wicketkeeper_player_id
    for a in data.assignments:
        if a.position_key == "wicket_keeper" and a.player_id != keeper:
            return (
                f"Player {a.player_id} is assigned the wicket_keeper position but is "
                "not the designated wicketkeeper"
            )

    # --- one transaction: replace the whole setup or nothing ---------------
    setup = await _get_setup_row(db, match.id, data.innings_number)
    if setup is None:
        setup = FieldingSetup(
            match_id=match.id,
            innings_number=data.innings_number,
            fielding_team_id=fielding_team_id,
        )
        db.add(setup)
        await db.flush()
    else:
        setup.fielding_team_id = fielding_team_id
        setup.wicketkeeper_player_id = None
        await db.execute(delete(FieldingAssignment).where(FieldingAssignment.setup_id == setup.id))

    setup.wicketkeeper_player_id = keeper
    for a in data.assignments:
        x, y = _coordinates_for(a.position_key, a.anchor_x, a.anchor_y)
        db.add(
            FieldingAssignment(
                setup_id=setup.id,
                player_id=a.player_id,
                position_key=a.position_key,
                anchor_x=x,
                anchor_y=y,
                is_substitute=a.player_id in active_subs,
                custom_label=a.custom_label if a.position_key == "custom_position" else None,
            )
        )
    await db.commit()

    # The session is expire_on_commit=False, so the setup we just mutated -
    # and its loaded `assignments` collection - still reflect the pre-save
    # rows in memory. Expire them so the re-query below reloads from the
    # database instead of handing back the stale collection (that stale read
    # is exactly how a "moved fielder" silently failed to show up).
    db.expire(setup)

    saved = await _get_setup_row(db, match.id, data.innings_number)
    return await build_response(
        db, match, data.innings_number, fielding_team_id, saved, innings_started=True
    )


async def bring_substitute_onto_field(
    db: AsyncSession,
    match: Match,
    data: FieldingSubstitutionIn,
    *,
    authorized_by_user_id: int | None,
) -> FieldingSetupResponse | str:
    """Swap a reserve onto the field for an active player, atomically.

    The whole thing runs in one transaction: the outgoing player's row is
    removed, the incoming player's row is added (with the outgoing player's
    position when one was given, or unplaced when not), and the substitution
    is appended to the match history. A failure anywhere leaves the field
    exactly as it was - there is no window with twelve men on the ground or
    with nobody at slip.
    """
    fielding_team_id, error = await derive_fielding_team_id(db, match, data.innings_number)
    if error:
        return error
    assert fielding_team_id is not None

    innings_row = (
        (
            await db.execute(
                select(Innings.id).where(
                    Innings.match_id == match.id, Innings.innings_number == data.innings_number
                )
            )
        )
        .scalars()
        .first()
    )
    if innings_row is None:
        return f"Innings {data.innings_number} has not started for this match"

    rules = load_rules()
    reason_error = validate_reason(data.reason, rules)
    if reason_error:
        return reason_error

    setup = await _get_setup_row(db, match.id, data.innings_number)
    if setup is None:
        setup = FieldingSetup(
            match_id=match.id,
            innings_number=data.innings_number,
            fielding_team_id=fielding_team_id,
        )
        db.add(setup)
        await db.flush()

    # Who is on the field right now: the XI minus anyone who left, plus
    # whoever came on. The positions themselves live in the assignment rows.
    xi = await playing_xi_ids(db, fielding_team_id)
    members = await active_membership(db, match.id, data.innings_number, xi)

    # 1. the outgoing player must be one of the eleven currently on the
    #    field - a member of the XI (placed or not), or a substitute who has
    #    already come on.
    if data.outgoing_player_id not in members:
        return (
            f"Player {data.outgoing_player_id} is not on the field, so there is "
            "nobody for them to be replaced by"
        )

    # 2. the incoming player must be a same-team reserve who is not already
    #    active. An XI player was never "off the field" in the first place -
    #    they are placed, not substituted - and an opposition player is
    #    simply the wrong team.
    if data.incoming_player_id == data.outgoing_player_id:
        return "A player cannot replace themselves"
    if data.incoming_player_id in xi:
        return (
            f"Player {data.incoming_player_id} is in the playing XI; place them "
            "on the field instead of substituting them on"
        )
    if data.incoming_player_id in members:
        return f"Player {data.incoming_player_id} is already on the field"
    own_squad = await squad_ids(db, fielding_team_id)
    if data.incoming_player_id not in own_squad:
        other_team = match.team_b_id if fielding_team_id == match.team_a_id else match.team_a_id
        other_squad = await squad_ids(db, other_team)
        if data.incoming_player_id in other_squad:
            return (
                f"Player {data.incoming_player_id} belongs to the other side; only "
                "the fielding side's own reserves may come on"
            )
        return f"Player {data.incoming_player_id} is not registered to the fielding side"

    # 3. an ordinary substitute may not take a job the Laws reserve for XI
    #    players, unless this competition's rules say otherwise.
    replacement_type = replacement_type_for(data.reason, rules)
    if data.position_key is not None:
        msg = _privilege_error(
            {data.incoming_player_id: replacement_type},
            data.incoming_player_id,
            data.position_key,
            "Substitute",
        )
        if msg:
            return msg

    # 4. the competition's own cap, when it sets one.
    if rules.max_per_match > 0:
        already = (
            (
                await db.execute(
                    select(FieldSubstitution.id)
                    .join(FieldingSetup, FieldingSetup.id == FieldSubstitution.setup_id)
                    .where(
                        FieldSubstitution.match_id == match.id,
                        FieldSubstitution.team_id == fielding_team_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(already) >= rules.max_per_match:
            return (
                f"This side has already made {len(already)} substitutions; the "
                f"competition allows {rules.max_per_match} per match"
            )

    # --- one transaction ---------------------------------------------------
    # The outgoing player keeps their profile, their squad membership and
    # their place in the XI: only their *position on the field* is released.
    await db.execute(
        delete(FieldingAssignment).where(
            FieldingAssignment.setup_id == setup.id,
            FieldingAssignment.player_id == data.outgoing_player_id,
        )
    )

    if data.position_key is not None:
        x, y = _coordinates_for(data.position_key, data.anchor_x, data.anchor_y)
        db.add(
            FieldingAssignment(
                setup_id=setup.id,
                player_id=data.incoming_player_id,
                position_key=data.position_key,
                anchor_x=x,
                anchor_y=y,
                is_substitute=True,
                custom_label=data.custom_label if data.position_key == "custom_position" else None,
            )
        )

    # If the keeper was the one who left, the gloves go with them: there is
    # no keeper until someone is named again, rather than a stale claim.
    if setup.wicketkeeper_player_id == data.outgoing_player_id:
        setup.wicketkeeper_player_id = None

    db.add(
        FieldSubstitution(
            match_id=match.id,
            setup_id=setup.id,
            team_id=fielding_team_id,
            innings_number=data.innings_number,
            outgoing_player_id=data.outgoing_player_id,
            incoming_player_id=data.incoming_player_id,
            reason=data.reason,
            replacement_type=replacement_type,
            authorized_by_user_id=authorized_by_user_id,
        )
    )
    await db.commit()
    db.expire(setup)

    saved = await _get_setup_row(db, match.id, data.innings_number)
    return await build_response(
        db, match, data.innings_number, fielding_team_id, saved, innings_started=True
    )
