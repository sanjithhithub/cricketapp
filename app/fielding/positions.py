"""The canonical list of fielding positions and their anchor points.

One catalog, used three ways:

* the API returns it to the client, so the field diagram is drawn from the
  same coordinates the server considers valid - the browser never invents a
  position name the backend would reject;
* the crud layer validates every saved assignment against ``POSITION_KEYS``,
  so a hand-crafted request cannot persist a position that has no meaning;
* ``anchor_x`` / ``anchor_y`` on each assignment default to these anchors, so a
  stored position can be drawn without recomputing where it belongs.

Coordinates are normalized 0-100 on a top-down field diagram: x grows to the
right, y grows downward, the batting crease sits near the bottom-centre and
the bowling end above it. They are anchor points, not exact metres - a real
field is an oval and captains adjust by feel; what matters is that every
position has one stable place on the diagram.

Each entry also carries ``group`` - the category the picker groups it under
(close cordon, off side, leg side, boundary, bowling/special) - and ``depth``,
the older ring label kept because it is part of the published response and
older clients read it.

Two keys are deliberately *not* ordinary spots on the grass:

* ``unassigned`` - choosing it takes the player off the field. The crud layer
  drops such rows rather than storing them, so "Unassigned" is the absence of
  an assignment, which is what the panel means by "Not yet placed";
* ``custom_position`` - the fallback when a marker is dropped somewhere no
  anchor reaches. It carries whatever coordinates the drag ended on, so it has
  a neutral anchor that nothing else uses.

Neither is used for drop detection (see :data:`DETECTABLE_KEYS`), so a marker
can never be "snapped" onto a concept that has no real location.
"""

from __future__ import annotations

from typing import TypedDict


class PositionDef(TypedDict):
    key: str
    label: str
    # Anchor on the normalized top-down diagram (0-100 each).
    anchor_x: float
    anchor_y: float
    # Rough ring the position sits in, for grouping in the picker.
    depth: str
    # The category the picker groups under: close, off, leg, boundary, special.
    group: str


# The order the picker shows its groups in, and the headings it uses. The five
# categories are the scorer's own vocabulary - Close fielders, Off-side,
# Leg-side, Boundary positions, Other - and every position belongs to exactly
# one of them, so nothing is offered twice under two headings.
GROUP_LABELS: dict[str, str] = {
    "close": "Close fielders",
    "off": "Off-side",
    "leg": "Leg-side",
    "boundary": "Boundary positions",
    "special": "Other",
}


def _p(key: str, label: str, x: float, y: float, depth: str, group: str) -> PositionDef:
    return {
        "key": key,
        "label": label,
        "anchor_x": x,
        "anchor_y": y,
        "depth": depth,
        "group": group,
    }


# Key -> definition. The key is what is stored in the database; the label is
# only ever presentation.
#
# Grouped in the order the picker shows them. Every standard position appears
# exactly once: when a name belongs to two categories (Fine Leg is a leg-side
# position *and* a boundary position) it sits under the one that describes
# where the fielder actually stands, so the dropdown never offers the same
# position twice.
POSITIONS: list[PositionDef] = [
    # --- close fielders: the cordon around the batter ----------------------
    # Behind the wicket on the off side (slips, gully), level with or just in
    # front of the batter (silly point, silly mid-on/off), and the leg-side
    # close men (short leg, leg slip, leg gully).
    _p("slip", "Slip", 60.0, 86.0, "close", "close"),
    _p("first_slip", "First Slip", 57.0, 91.0, "close", "close"),
    _p("second_slip", "Second Slip", 66.0, 84.0, "close", "close"),
    _p("third_slip", "Third Slip", 73.0, 82.0, "close", "close"),
    _p("fourth_slip", "Fourth Slip", 79.0, 79.0, "close", "close"),
    _p("leg_slip", "Leg Slip", 42.0, 92.0, "close", "close"),
    _p("gully", "Gully", 68.0, 78.0, "close", "close"),
    _p("leg_gully", "Leg Gully", 46.0, 80.0, "close", "close"),
    _p("short_leg", "Short Leg", 38.0, 84.0, "close", "close"),
    _p("forward_short_leg", "Forward Short Leg", 36.0, 78.0, "close", "close"),
    _p("backward_short_leg", "Backward Short Leg", 37.0, 89.0, "close", "close"),
    _p("silly_point", "Silly Point", 74.0, 73.0, "close", "close"),
    _p("silly_mid_off", "Silly Mid-off", 58.0, 76.0, "close", "close"),
    _p("silly_mid_on", "Silly Mid-on", 43.0, 76.0, "close", "close"),
    # --- off side: from square to straight, near rope to far rope ----------
    _p("point", "Point", 76.0, 68.0, "close", "off"),
    _p("backward_point", "Backward Point", 70.0, 76.0, "close", "off"),
    _p("deep_point", "Deep Point", 91.0, 70.0, "boundary", "off"),
    _p("square_point", "Square Point", 84.0, 74.0, "close", "off"),
    _p("cover", "Cover", 72.0, 56.0, "close", "off"),
    _p("extra_cover", "Extra Cover", 63.0, 48.0, "close", "off"),
    _p("deep_cover", "Deep Cover", 84.0, 44.0, "boundary", "off"),
    _p("deep_extra_cover", "Deep Extra Cover", 76.0, 34.0, "boundary", "off"),
    _p("sweeper_cover", "Sweeper Cover", 90.0, 60.0, "boundary", "off"),
    _p("mid_off", "Mid-off", 57.0, 40.0, "close", "off"),
    _p("wide_mid_off", "Wide Mid-off", 64.0, 32.0, "close", "off"),
    _p("long_off", "Long-off", 55.0, 22.0, "boundary", "off"),
    _p("wide_long_off", "Wide Long-off", 70.0, 20.0, "boundary", "off"),
    # --- leg side: mirror of the off side ---------------------------------
    _p("mid_on", "Mid-on", 43.0, 40.0, "close", "leg"),
    _p("wide_mid_on", "Wide Mid-on", 36.0, 32.0, "close", "leg"),
    _p("long_on", "Long-on", 45.0, 22.0, "boundary", "leg"),
    _p("midwicket", "Midwicket", 30.0, 50.0, "close", "leg"),
    _p("deep_midwicket", "Deep Midwicket", 16.0, 44.0, "boundary", "leg"),
    _p("square_leg", "Square Leg", 24.0, 66.0, "close", "leg"),
    _p("backward_square_leg", "Backward Square Leg", 30.0, 76.0, "close", "leg"),
    _p("forward_square_leg", "Forward Square Leg", 20.0, 54.0, "close", "leg"),
    _p("deep_square_leg", "Deep Square Leg", 16.0, 62.0, "boundary", "leg"),
    _p("fine_leg", "Fine Leg", 32.0, 84.0, "boundary", "leg"),
    _p("deep_fine_leg", "Deep Fine Leg", 34.0, 90.0, "boundary", "leg"),
    _p("short_fine_leg", "Short Fine Leg", 26.0, 84.0, "close", "leg"),
    # --- boundary: the men on or near the rope who are not off/leg specials -
    _p("third_man", "Third Man", 72.0, 88.0, "boundary", "boundary"),
    _p("deep_third_man", "Deep Third Man", 78.0, 84.0, "boundary", "boundary"),
    _p("long_leg", "Long Leg", 22.0, 80.0, "boundary", "boundary"),
    _p("deep_backward_square_leg", "Deep Backward Square Leg", 24.0, 74.0, "boundary", "boundary"),
    # --- other: the designations and the escape hatches --------------------
    # The wicketkeeper lives here because it is a *designation* managed by
    # the Wicketkeeper selector, not a spot a fielder chooses; the bowler is
    # an XI member at the opposite end of the pitch; Unassigned takes a
    # player off the field; Custom Position is where a drop that reached no
    # anchor lands.
    _p("bowler", "Bowler", 50.0, 66.0, "special", "special"),
    _p("wicket_keeper", "Wicket Keeper", 50.0, 88.0, "close", "special"),
    _p("non_striker_end", "At the Non-striker's End", 50.0, 60.0, "special", "special"),
    _p("custom_position", "Custom Position", 50.0, 50.0, "special", "special"),
    _p("unassigned", "Unassigned", 50.0, 44.0, "special", "special"),
]

POSITION_KEYS: frozenset[str] = frozenset(p["key"] for p in POSITIONS)
LABELS: dict[str, str] = {p["key"]: p["label"] for p in POSITIONS}
ANCHORS: dict[str, tuple[float, float]] = {
    p["key"]: (p["anchor_x"], p["anchor_y"]) for p in POSITIONS
}
GROUPS: dict[str, str] = {p["key"]: p["group"] for p in POSITIONS}

# Keys that are concepts rather than spots on the grass. They never take part
# in drop detection, and the diagram never draws an anchor dot for them.
NON_SPOT_KEYS: frozenset[str] = frozenset({"custom_position", "unassigned"})

# The anchors a released marker may snap to. ``wicket_keeper`` is added per
# marker by the client: only the designated keeper may hold it, so offering it
# to everybody would produce a save the server refuses.
DETECTABLE_KEYS: tuple[str, ...] = tuple(
    p["key"] for p in POSITIONS if p["key"] not in NON_SPOT_KEYS and p["key"] != "wicket_keeper"
)

# How far (in diagram units) a released marker may be from an anchor and still
# count as standing there. Beyond it the player is simply at a custom spot.
DETECT_RADIUS: float = 7.0

# A side never fields more than this.
MAX_ACTIVE_FIELDERS: int = 11


def is_valid_position(key: str) -> bool:
    return key in POSITION_KEYS


def label_for(key: str) -> str:
    return LABELS.get(key, key)


def group_for(key: str) -> str:
    return GROUPS.get(key, "special")


def nearest_anchor(
    x: float,
    y: float,
    *,
    include_keeper: bool = False,
    radius: float = DETECT_RADIUS,
) -> str | None:
    """The anchor a marker released at ``(x, y)`` stands in, or ``None``.

    Nearest wins rather than first-in-list wins, so two anchors a drag could
    land between resolve the same way every time instead of depending on the
    catalog's ordering. ``None`` means "outside every zone" - the caller marks
    it ``custom_position`` rather than guessing a name.
    """
    keys = DETECTABLE_KEYS + (("wicket_keeper",) if include_keeper else ())
    best_key: str | None = None
    best_d2 = radius * radius
    for key in keys:
        ax, ay = ANCHORS[key]
        d2 = (x - ax) ** 2 + (y - ay) ** 2
        if d2 <= best_d2:
            best_d2 = d2
            best_key = key
    return best_key
