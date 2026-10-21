"""Partnership tracking for an innings.

A partnership is the stand the two batsmen at the crease put together between one
wicket and the next. Everything here is derived from the delivery log - the same
log the score engine replays - so a partnership can never disagree with the
scorecard: the runs are the delivery runs, the balls are the legal deliveries,
and a stand ends on the very wicket that ends it in the engine.

The cricket rules mirrored from :mod:`app.scoring.engine`:

- The stand's runs include every team run scored while the pair were together,
  extras included, so the two batsmen's runs may add up to less than the stand.
- Only the striker of a delivery is credited with its runs off the bat, and with
  facing it - except a wide, which no batsman faces. A no-ball IS faced but the
  batsman scores only what came off the bat.
- Wides and no-balls are not balls of the over, so they are not balls of the
  partnership either.
- A new stand begins on the delivery after a wicket: the crease recorded on that
  delivery names the player who replaced the dismissed batsman.
- The delivery that first reaches a chase target never registers its wicket in
  the engine, so its stand is reported unbroken and no new one is opened.

Deriving rather than persisting is deliberate. The delivery log already carries
every fact a partnership needs, so a partnership table would be a second copy of
the same truth and could drift from it; it would also need de-duplication on
retry. Replaying the log is idempotent, so a retried request can only ever
recompute the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.scoring.enums import ExtraType

# Wides and no-balls are penalty deliveries: they never count as a ball of the
# over, so a partnership's ball count excludes them.
ILLEGAL_BALL_EXTRAS = (ExtraType.WIDE.value, ExtraType.NO_BALL.value)


def _extra_value(delivery) -> str:
    """The delivery's extra type as a plain string, whichever form it is in.

    The ORM model stores it as a ``str``; an in-flight ``DeliveryRecord`` may
    still hold the enum. Normalising here keeps every caller free of that.
    """
    value = delivery.extra_type
    if isinstance(value, ExtraType):
        return value.value
    return value or ExtraType.NONE.value


def _winning_delivery_id(target, deliveries) -> int | None:
    """The id of the delivery that first reaches ``target``, if there is one.

    Mirrors the engine: that ball ends the chase before its wicket is read, so
    the stand it would have broken is treated as unbroken.
    """
    if target is None:
        return None
    running = 0
    for d in deliveries:
        running += d.runs_batsman + d.runs_extras
        if running >= target:
            return d.id
    return None


@dataclass
class BatterContribution:
    """One batsman's share of a stand."""

    player_id: int
    runs: int = 0
    balls: int = 0


@dataclass
class Partnership:
    """A stand between two wickets (or the unbroken one still at the crease)."""

    batter1: BatterContribution
    batter2: BatterContribution
    # Team runs added while the pair were together, extras included. This is the
    # figure a scorecard shows as "the partnership", so it is not the sum of the
    # two batsmen's own runs whenever there were extras.
    total_runs: int = 0
    # Legal balls faced together: wides and no-balls excluded.
    total_balls: int = 0
    unbroken: bool = True
    player_out_id: int | None = None

    @property
    def batter_runs(self) -> int:
        """Runs the two batsmen actually scored, excluding extras."""
        return self.batter1.runs + self.batter2.runs

    @property
    def run_rate(self) -> float:
        """Partnership runs per over, 0.0 when no legal ball has been faced.

        A zero-ball stand is a real state (the openers are together before the
        first ball, or a stand ended on a wide), so this must not divide by zero.
        """
        if not self.total_balls:
            return 0.0
        return round(self.total_runs / (self.total_balls / 6), 2)

    def batter_by_id(self, player_id: int) -> BatterContribution | None:
        if player_id == self.batter1.player_id:
            return self.batter1
        if player_id == self.batter2.player_id:
            return self.batter2
        return None

    def contribution_of(self, player_id: int) -> float:
        """A batsman's runs as a percentage of the pair's combined runs.

        The denominator is the two batsmen's own runs, not ``total_runs``: a
        contribution percentage asks "how much of the stand did each man make",
        and extras were made by nobody. When neither has scored the split is
        undefined, and reported as 0.0 rather than a division by zero.
        """
        totals = self.batter_runs
        if not totals:
            return 0.0
        batter = self.batter_by_id(player_id)
        runs = batter.runs if batter is not None else 0
        return round(runs * 100 / totals, 2)


def _attribute(stand: Partnership, delivery) -> None:
    """Add one delivery's runs and balls to a stand."""
    stand.total_runs += delivery.runs_batsman + delivery.runs_extras
    extra = _extra_value(delivery)
    if extra not in ILLEGAL_BALL_EXTRAS:
        stand.total_balls += 1

    # Crediting the delivery's recorded striker (rather than re-deriving the
    # strike) is what keeps byes, leg byes and a run out at the non-striker's end
    # correct: the log already says who faced the ball.
    batter = stand.batter_by_id(delivery.striker_id)
    if batter is not None:
        batter.runs += delivery.runs_batsman
        # A wide is not faced by the batsman; a no-ball is.
        if extra != ExtraType.WIDE.value:
            batter.balls += 1


def compute_partnerships(deliveries, target: int | None = None) -> list[Partnership]:
    """Cut a delivery log into stands, one per pair, in order.

    ``deliveries`` must already be in chronological order. ``target`` is the
    innings target, used only to mirror the engine's winning-ball rule. The last
    entry is ``unbroken`` when the innings is still going or ended on a wicketless
    note; a completed stand carries the ``player_out_id`` that ended it.
    """
    winning_id = _winning_delivery_id(target, deliveries)
    stands: list[Partnership] = []
    current: Partnership | None = None
    for d in deliveries:
        if current is None:
            current = Partnership(
                batter1=BatterContribution(d.striker_id),
                batter2=BatterContribution(d.non_striker_id),
            )
        _attribute(current, d)
        # The winning ball of a chase never registers its wicket, so it cannot
        # end the stand either.
        if getattr(d, "wicket_type", None) and getattr(d, "id", None) != winning_id:
            current.player_out_id = d.dismissed_player_id
            current.unbroken = False
            stands.append(current)
            current = None
    if current is not None:
        stands.append(current)
    return stands


def current_partnership(deliveries, target: int | None = None) -> Partnership | None:
    """The stand still at the crease, or ``None`` when a wicket just fell.

    Returns the trailing stand only while it is unbroken. A wicket leaves the
    last stand closed and (until the next ball) nobody batting together, so the
    answer is genuinely "no current partnership" rather than a stale pair.
    """
    stands = compute_partnerships(deliveries, target)
    if stands and stands[-1].unbroken:
        return stands[-1]
    return None


__all__ = [
    "BatterContribution",
    "Partnership",
    "compute_partnerships",
    "current_partnership",
    "ILLEGAL_BALL_EXTRAS",
]
