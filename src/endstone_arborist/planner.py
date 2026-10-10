"""Pure placement evaluation: turn a world snapshot into ranked planting recommendations.

This module contains no Endstone imports and no I/O. It consumes the :class:`~endstone_arborist.world.Snapshot`
produced by the scanner and returns :class:`Candidate` values, which makes every rule below unit-testable with a
hand-built grid.

Two ideas are kept deliberately separate:

*Hard validity* (a candidate is *possible*)
    The plot needs soil that the species accepts, the planting cells need to be empty or overwriteable, the
    trunk needs room to grow, no existing sapling may sit closer than the species' spacing, and no mature
    tree trunk may sit closer than ``min-trunk-distance``. Failing any of these removes the candidate; it
    is never merely penalised.

*Preference* (a candidate is *recommended*)
    Obstacles, crowding, mature trees and row alignment adjust a score. Recommendations therefore degrade
    gracefully: on a crowded plot the plugin still reports the least-bad open ground instead of nothing.
"""

from dataclasses import dataclass, field
from math import isfinite, sqrt

from endstone_arborist.blocks import (
    LOGS,
    SOIL,
    SurfaceState,
    classify_cell,
    is_sapling_block,
    is_tree_structure,
)
from endstone_arborist.config import RANGE_SPACING_SCALE, RANGE_TRUNK_DISTANCE, ArboristConfig
from endstone_arborist.profiles import TreeProfile
from endstone_arborist.world import Column, Snapshot

__all__ = [
    "REASON_TOO_CLOSE",
    "REASON_TOO_CLOSE_TO_TREE",
    "Candidate",
    "Plan",
    "PlanOptions",
    "ScaledGeometry",
    "Verdict",
    "plan",
    "plan_options",
    "plot_cells",
    "scaled_geometry",
    "score_plot",
]

#: Score a candidate starts from before any factor is applied.
BASE_SCORE = 100.0

#: Highest vertical clearance a scaled profile may ask for, matching the default
#: ``world.ColumnScanner`` window. Cells above it are unreadable, so a taller
#: requirement would silently stop rejecting low-ceilinged spots; see the same rule
#: documented on ``PROFILES``.
MAX_VERTICAL_CLEARANCE = 12

#: Reasons a candidate can be rejected outright. Kept as data so the debug command and the tests
#: can report *why* nothing was recommended instead of failing silently.
REASON_NO_GROUND = "no_ground"
REASON_ILLEGAL_GROUND = "illegal_ground"
REASON_OBSTRUCTED = "obstructed"
REASON_NO_CLEARANCE = "no_clearance"
#: Every cell of the plot already holds a sapling, so there is nothing left to plant.
REASON_ALREADY_PLANTED = "already_planted"
#: A neighbour column closer than the species spacing already holds a sapling. This is
#: hard, not a penalty: ``TreeProfile.spacing`` is the *minimum* distance between two
#: plots, so leaving it to the crowding factor kept a plot one block from a sapling
#: recommended and let repeated Auto-Plant commits pile saplings side by side. The
#: distance used is :attr:`ScaledGeometry.spacing`, so ``spacing-scale`` moves this gate
#: together with every other spacing the planner applies.
REASON_TOO_CLOSE = "too_close"
#: A neighbour column closer than ``min-trunk-distance`` already holds a mature tree trunk, i.e. a
#: log block. Same shape of ring as :data:`REASON_TOO_CLOSE`, but about trees rather than rival
#: saplings, and measured by its own operator knob: that gate only looks at saplings, so a plot two
#: blocks from an existing birch stayed valid, kept being recommended, and asked the player to plant
#: a sapling that can never grow there. The ring is deliberately *not* the species spacing --
#: ``spacing-scale`` sizes the berths between plots, while ``min-trunk-distance`` alone sizes how far
#: a sapling must start from an adult trunk, and ``0`` turns this gate off. Canopy deliberately is
#: *not* part of this rule: leaves hang low and overlap neighbouring plots by nature, so they stay
#: :func:`score_plot`'s ``mature_tree`` penalty.
REASON_TOO_CLOSE_TO_TREE = "too_close_to_tree"


@dataclass(frozen=True)
class Verdict:
    """Outcome of evaluating one plot.

    Attributes:
        valid: Whether the plot may be planted at all.
        reasons: Rejection reason identifiers, empty when valid.
    """

    valid: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanOptions:
    """Knobs that control how aggressively candidates are filtered and ranked.

    Attributes:
        max_candidates: Upper bound on returned recommendations.
        min_score: Candidates scoring below this are dropped, which keeps Arborist from pointing at
            hopeless ground just to have something to show.
        max_plot_y_delta: Largest allowed height difference between the cells of one plot. A 2x2 dark
            oak plot on a slope is legal in vanilla but tends to look wrong, so plots must be level.
        obstacle_weight: Score removed per obstructing block inside the clearance ring.
        crowding_weight: Score removed per sapling found in the crowding band, the ring that
            starts where :data:`REASON_TOO_CLOSE` stops. :func:`evaluate_plot` rejects any plot
            with a neighbour strictly inside the *scaled* spacing, so this factor measures the
            band ``spacing <= distance < 2 * spacing`` and weighs it
            ``(2 * spacing - distance) / spacing``: a barely-legal plot ranks below a generous
            one and ``penalties.sapling-spacing`` stays meaningful.
        mature_weight: Score removed per mature-tree block inside the clearance ring.
        alignment_weight: Score added when the plot lines up with an existing row of saplings.
        flat_weight: Score added for a plot whose soil is at exactly one height.
        soil_penalty: Score removed when the ground is legal but not preferred (sand, clay, farmland).
        reach_weight: Score removed per block of horizontal distance from the player.
        spacing_scale: Multiplier on every species spacing and clearance, from
            ``config.toml``'s ``spacing-scale``. ``1.0`` keeps the profile values,
            larger spreads plots farther apart, smaller packs them tighter.
        min_trunk_distance: Ring, in whole blocks, inside which a neighbouring mature tree trunk
            makes a plot invalid, from ``config.toml``'s ``min-trunk-distance``. Sized by its own
            knob rather than by the species spacing, so ``spacing-scale`` does not move it, and
            ``0`` turns the trunk gate off entirely.
    """

    max_candidates: int = 3
    min_score: float = 45.0
    max_plot_y_delta: int = 0
    obstacle_weight: float = 9.0
    crowding_weight: float = 14.0
    density_weight: float = 5.0
    mature_weight: float = 7.0
    alignment_weight: float = 8.0
    flat_weight: float = 4.0
    soil_penalty: float = 10.0
    reach_weight: float = 2.5
    spacing_scale: float = 1.0
    min_trunk_distance: int = 3

    def __post_init__(self) -> None:
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        for name, value in self._weights().items():
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        low, high = RANGE_SPACING_SCALE
        if not isfinite(self.spacing_scale) or not low <= self.spacing_scale <= high:
            raise ValueError(f"spacing_scale must be finite and within {low}..{high}")
        low, high = RANGE_TRUNK_DISTANCE
        if not low <= self.min_trunk_distance <= high:
            raise ValueError(f"min_trunk_distance must be within {low}..{high}")

    def _weights(self) -> dict[str, float]:
        names = (
            "min_score",
            "obstacle_weight",
            "crowding_weight",
            "mature_weight",
            "alignment_weight",
            "flat_weight",
            "soil_penalty",
            "reach_weight",
        )
        return {name: float(getattr(self, name)) for name in names}


@dataclass(frozen=True)
class ScaledGeometry:
    """A species' planting distances once :attr:`PlanOptions.spacing_scale` is applied.

    :class:`~endstone_arborist.profiles.TreeProfile` stays frozen and untouched: the
    scaled numbers exist only here, so a session's profile identity survives and
    Auto-Plant can keep revalidating against the same species object.

    Attributes:
        footprint: Plot side length, never scaled -- a 2x2 species still needs 2x2.
        spacing: Preferred distance between two plots, whole blocks, at least :attr:`footprint`.
        horizontal_clearance: Obstacle ring radius in blocks, at least one block.
        vertical_clearance: Free cells wanted above the planting cell, at least one and
            never more than :data:`MAX_VERTICAL_CLEARANCE`.
        requested_vertical_clearance: What :attr:`PlanOptions.spacing_scale` asked for before
            the window cap, so a caller can report that the scale was truncated.
    """

    footprint: int
    spacing: int
    horizontal_clearance: int
    vertical_clearance: int
    requested_vertical_clearance: int


def _scaled_blocks(value: int, scale: float, minimum: int) -> int:
    """Return *value* multiplied by *scale* as whole blocks, rounded half up, never below *minimum*."""
    return max(minimum, int(value * scale + 0.5))


def scaled_geometry(profile: TreeProfile, options: PlanOptions) -> ScaledGeometry:
    """Scale *profile*'s distances for one planning pass.

    Whole blocks are used because every consumer counts cells. At the default scale of
    ``1.0`` the rounding is an identity on integers, so the shipped behaviour is exactly
    the profile's own values. The clamps then re-enforce the invariants
    :meth:`~endstone_arborist.profiles.TreeProfile.__post_init__` guarantees, which means
    a low scale cannot ask for plots that share a cell or for trunks with no room, and a
    high scale cannot ask for clearance the scanned column window cannot answer.

    Args:
        profile: Species being planted. Read only, never modified.
        options: Supplies :attr:`PlanOptions.spacing_scale`.

    Returns:
        The distances :func:`evaluate_plot` and :func:`score_plot` should use.
    """
    scale = options.spacing_scale
    footprint = profile.footprint
    wanted = _scaled_blocks(profile.vertical_clearance, scale, 1)
    return ScaledGeometry(
        footprint=footprint,
        spacing=_scaled_blocks(profile.spacing, scale, footprint),
        horizontal_clearance=_scaled_blocks(profile.horizontal_clearance, scale, 1),
        # ``min`` keeps the ask inside the scanned window; ``max`` keeps it positive.
        vertical_clearance=min(MAX_VERTICAL_CLEARANCE, wanted),
        requested_vertical_clearance=wanted,
    )


@dataclass(frozen=True)
class Candidate:
    """One recommended planting plot.

    Attributes:
        cells: Absolute ``(x, y, z)`` of each sapling cell, in a stable order (west-to-east then
            north-to-south). Single-sapling plots have one entry, 2x2 plots have four.
        anchor: The northwest cell of the plot; handy for messages and particles.
        score: Final preference score, higher is better.
        factors: Per-factor contribution to :attr:`score`, for explainability and tests.
        footprint: Side length of the plot in cells.
    """

    cells: tuple[tuple[int, int, int], ...]
    anchor: tuple[int, int, int]
    score: float
    factors: dict[str, float] = field(default_factory=dict)
    footprint: int = 1

    @property
    def height(self) -> int:
        """Y of the planting cell (all cells of a plot share it)."""
        return self.anchor[1]


@dataclass(frozen=True)
class Plan:
    """Result of one evaluation pass.

    Attributes:
        candidates: Ranked recommendations, best first.
        evaluated: Number of plots that passed hard validity before scoring.
        rejected: Number of plots rejected for a reason (excludes plots skipped as unreachable).
        scanned_columns: Columns present in the snapshot, useful for performance assertions.
    """

    candidates: tuple[Candidate, ...] = ()
    evaluated: int = 0
    rejected: int = 0
    scanned_columns: int = 0


def plot_cells(x: int, y: int, z: int, footprint: int) -> tuple[tuple[int, int, int], ...]:
    """Absolute cells occupied by a plot of *footprint* whose northwest corner is ``(x, y, z)``."""
    if footprint < 1:
        raise ValueError("footprint must be positive")
    return tuple((x + dx, y, z + dz) for dz in range(footprint) for dx in range(footprint))


# Kept as an alias because the 2x2 terminology shows up in player-facing text.


def _column(snapshot: Snapshot, x: int, z: int) -> Column | None:
    return snapshot.column(x, z)


def _planting_cell_free(column: Column, y: int) -> bool:
    """Whether a sapling can end up in this cell: empty, overwriteable plants, or already a sapling."""
    if column.ground_y is None:
        return False
    if y != column.ground_y + 1:
        # The snapshot window did not reach this height; refuse to guess.
        return False
    occupant = column.occupant
    if occupant is None:
        return False
    state = classify_cell(occupant)
    return state in (SurfaceState.EMPTY, SurfaceState.VEGETATION, SurfaceState.SAPLING)


def evaluate_plot(
    snapshot: Snapshot,
    profile: TreeProfile,
    x: int,
    z: int,
    options: PlanOptions,
) -> tuple[Verdict, tuple[tuple[int, int, int], ...]]:
    """Check hard validity for a plot whose northwest corner sits at ``(x, z)``.

    Args:
        snapshot: Sampled world state.
        profile: Species being planted.
        x: West edge of the plot.
        z: North edge of the plot.
        options: Thresholds, here :attr:`PlanOptions.max_plot_y_delta`, the
            :attr:`PlanOptions.spacing_scale` that sizes the plot clearances, and the
            :attr:`PlanOptions.min_trunk_distance` that sizes the trunk gate.

    Returns:
        A :class:`Verdict` plus the plot cells when valid.
    """
    footprint = profile.footprint
    geometry = scaled_geometry(profile, options)
    columns: list[Column] = []
    for dx in range(footprint):
        for dz in range(footprint):
            column = _column(snapshot, x + dx, z + dz)
            if column is None or column.ground_y is None or column.ground is None:
                return Verdict(False, (REASON_NO_GROUND,)), ()
            columns.append(column)

    # Every column above proved ``ground_y`` is not None, so the heights are ints here.
    ground_ys = {int(column.ground_y) for column in columns if column.ground_y is not None}
    if len(ground_ys) > 1 and max(ground_ys) - min(ground_ys) > options.max_plot_y_delta:
        return Verdict(False, (REASON_NO_GROUND,)), ()
    # ``plant_y`` is the cell the sapling itself occupies: one above the soil block.
    # Every coordinate handed to candidates, particles and clearance checks uses this convention.
    plant_y = max(ground_ys) + 1

    reasons: list[str] = []
    planted = 0
    ordered = sorted(columns, key=lambda column: (column.z, column.x))
    for column, cell in zip(ordered, plot_cells(x, plant_y, z, footprint), strict=True):
        if column.ground not in profile.ground or column.ground not in SOIL:
            reasons.append(REASON_ILLEGAL_GROUND)
        if not _planting_cell_free(column, cell[1]):
            reasons.append(REASON_OBSTRUCTED)
        if column.has_sapling:
            planted += 1
        # Vertical clearance: the trunk needs ``vertical_clearance`` free cells above the
        # sapling. This check runs for every cell of every candidate: skipping it is what
        # produces the obviously wrong recommendation of a tree that cannot grow.
        for offset in range(1, geometry.vertical_clearance + 1):
            if column.is_free_at(plant_y + offset) is False:
                reasons.append(REASON_NO_CLEARANCE)
                break
    # Spacing is a validity rule, not a preference. Preview and commit share this function
    # (:func:`endstone_arborist.autoplant.prepare` re-validates through it), so one rule set
    # keeps a recommended plot and a planted plot honest. The distance is the *scaled* spacing
    # from :func:`scaled_geometry`, which is what makes ``spacing-scale`` move the gate itself.
    cells = plot_cells(x, plant_y, z, footprint)
    if _sapling_within_spacing(snapshot, cells, geometry.spacing):
        reasons.append(REASON_TOO_CLOSE)
    # A trunk inside the ring is the same impossibility as a rival sapling: the tree that would
    # grow here needs the ground the existing one already owns. The distance is the operator's
    # ``min-trunk-distance``, not the species spacing, so ``spacing-scale`` cannot move this gate,
    # and ``0`` skips the check altogether. The plot's own columns are excluded, so a 2x2 array is
    # never rejected by its own trunk cells.
    trunk_distance = max(0, options.min_trunk_distance)
    if trunk_distance > 0 and _trunk_within_spacing(snapshot, cells, trunk_distance):
        reasons.append(REASON_TOO_CLOSE_TO_TREE)
    # Recommending a plot that is already fully planted asks the player to do nothing.
    # A partially planted plot stays valid: that is the "finish your 2x2" case.
    if planted == len(columns):
        reasons.append(REASON_ALREADY_PLANTED)
    if reasons:
        return Verdict(False, tuple(dict.fromkeys(reasons))), ()
    return Verdict(True), cells


def _sapling_within_spacing(
    snapshot: Snapshot,
    cells: tuple[tuple[int, int, int], ...],
    spacing: int,
) -> bool:
    """Whether a neighbour column closer than *spacing* blocks already holds a sapling.

    The ring is the same idea as :func:`score_plot`'s plot ring: every cell of the plot looks
    out ``spacing`` blocks, the plot's own cells are skipped because they are the cells this
    planting fills rather than rivals (which is what keeps "finish your 2x2" legal), and a
    distance of ``0`` is not a neighbour. The test is strict, so a rival standing at exactly
    *spacing* is legal. A ``spacing == 1`` species therefore has no ring to walk and an empty
    field finds nothing to reject, so both keep their previous behaviour.

    Args:
        snapshot: Sampled world state. Columns the scan never covered are unknown, not crowded.
        cells: The plot's planting cells, as built by :func:`plot_cells`.
        spacing: The minimum distance between two plots in whole blocks, i.e.
            :attr:`ScaledGeometry.spacing` for this planning pass.

    Returns:
        True when some neighbour column strictly inside that distance reports a sapling.
    """
    own = {(cell[0], cell[2]) for cell in cells}
    for cell_x, cell_z in sorted(own):
        for dx in range(-spacing, spacing + 1):
            for dz in range(-spacing, spacing + 1):
                nx, nz = cell_x + dx, cell_z + dz
                if (nx, nz) in own:
                    continue
                distance = (dx * dx + dz * dz) ** 0.5
                if distance == 0.0 or distance >= spacing:
                    continue
                neighbour = _column(snapshot, nx, nz)
                if neighbour is not None and neighbour.has_sapling:
                    return True
    return False


def _is_trunk_block(block_type: str) -> bool:
    """Whether *block_type* is a mature trunk: a log, and deliberately not canopy.

    :func:`endstone_arborist.blocks.is_tree_structure` is the predicate :func:`score_plot` uses for
    its ``mature_tree`` factor, and it also matches leaves. This gate must not: canopy hangs low and
    overlaps the open ground beside a tree, so treating it as hard would refuse plots that are simply
    fine. Logs alone are the trunk a sapling cannot grow into.
    """
    return block_type in LOGS


def _column_has_trunk(column: Column) -> bool:
    """Whether a sampled column carries trunk logs above the surface the scanner stopped on.

    A standing tree reads its highest log inside the probe window as ``ground`` and keeps the logs
    above it in :attr:`~endstone_arborist.world.Column.cells`, so requiring a log *above* the
    column's surface is what separates a trunk from a single log block lying on the soil plane;
    the latter is judged by the ground rules and has no canopy to grow into. A column the scan never
    covered reports no cells at all, so an unreadable or unaffordable column is unknown, not crowded.
    """
    return any(_is_trunk_block(block) for block in column.cells)


def _trunk_within_spacing(
    snapshot: Snapshot,
    cells: tuple[tuple[int, int, int], ...],
    ring: int,
) -> bool:
    """Whether a neighbour column closer than *ring* blocks holds a mature tree trunk.

    This is :func:`_sapling_within_spacing` with the trunk predicate swapped in, and it keeps every
    one of that function's rules so the two gates cannot disagree: every cell of the plot looks out
    ``ring`` blocks, the plot's own columns are skipped (which is what lets "finish your 2x2" stand
    on its own trunk cells), a distance of ``0`` is not a neighbour, and the test is strict, so a
    trunk standing at exactly *ring* is legal. What differs is only where the distance comes from:
    the caller passes ``min-trunk-distance`` rather than the species spacing, and skips this check
    entirely when that knob is ``0``.

    Args:
        snapshot: Sampled world state. Columns the scan never covered are unknown, not crowded.
        cells: The plot's planting cells, as built by :func:`plot_cells`.
        ring: The minimum distance, in whole blocks, a plot must keep from an existing trunk, i.e.
            :attr:`PlanOptions.min_trunk_distance` for this planning pass. The caller guarantees it
            is positive.

    Returns:
        True when some neighbour column strictly inside that distance reports trunk logs.
    """
    own = {(cell[0], cell[2]) for cell in cells}
    for cell_x, cell_z in sorted(own):
        for dx in range(-ring, ring + 1):
            for dz in range(-ring, ring + 1):
                nx, nz = cell_x + dx, cell_z + dz
                if (nx, nz) in own:
                    continue
                distance = (dx * dx + dz * dz) ** 0.5
                if distance == 0.0 or distance >= ring:
                    continue
                neighbour = _column(snapshot, nx, nz)
                if neighbour is not None and _column_has_trunk(neighbour):
                    return True
    return False


def score_plot(
    snapshot: Snapshot,
    profile: TreeProfile,
    cells: tuple[tuple[int, int, int], ...],
    options: PlanOptions,
    origin: tuple[int, int] | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute the preference score for a valid plot.

    Args:
        snapshot: Sampled world state used for neighbourhood queries.
        profile: Species being planted.
        cells: Plot cells as returned by :func:`evaluate_plot`.
        options: Factor weights.
        origin: Player ``(x, z)`` used for the reach penalty. ``None`` skips it.

    Returns:
        The score and the per-factor breakdown that produced it.
    """
    factors: dict[str, float] = {"base": BASE_SCORE}
    centre_x = sum(cell[0] for cell in cells) / len(cells)
    centre_z = sum(cell[2] for cell in cells) / len(cells)

    soil_penalties = 0.0
    for cell in cells:
        column = _column(snapshot, cell[0], cell[2])
        if column is not None and column.ground is not None and column.ground not in profile.preferred_ground:
            soil_penalties += options.soil_penalty
    factors["soil"] = -soil_penalties / len(cells)

    # Three radii: obstacles only matter within the trunk's own clearance ring, alignment and
    # density must reach the species' spacing (a row planted at the correct distance otherwise
    # falls outside the window and the alignment bonus can never fire), and the crowding band
    # runs from there out to twice the spacing.
    geometry = scaled_geometry(profile, options)
    ring = max(geometry.horizontal_clearance, 1)
    # ``crowd_ring`` is the window this scan always had: obstacles, mature trees and density are
    # measured there and keep their previous reach. The re-banded crowding factor needs the ring
    # that starts at the hard gate and runs to twice the spacing, so ``plot_ring`` now reaches
    # that far and the extra columns only ever feed ``saplings``.
    crowd_ring = max(geometry.spacing, ring)
    plot_ring = max(2 * geometry.spacing, crowd_ring)
    own = {(cell[0], cell[2]) for cell in cells}
    obstacles = 0.0
    mature = 0.0
    saplings = 0.0
    crowding = 0.0
    aligned = 0.0

    for sx, sy, sz in ((c[0], c[1], c[2]) for c in cells):
        for dx in range(-plot_ring, plot_ring + 1):
            for dz in range(-plot_ring, plot_ring + 1):
                nx, nz = sx + dx, sz + dz
                if (nx, nz) in own:
                    # Sibling cells of this same plot are not a rival planting.
                    continue
                distance = (dx * dx + dz * dz) ** 0.5
                if distance > plot_ring or distance == 0.0:
                    continue
                neighbour = _column(snapshot, nx, nz)
                if neighbour is None or neighbour.ground_y is None:
                    continue
                ground = neighbour.ground
                occupant = neighbour.occupant
                is_tree = (ground is not None and is_tree_structure(ground)) or (
                    occupant is not None and is_tree_structure(occupant)
                )
                near_enough = distance <= ring
                if neighbour.has_sapling:
                    # Anything strictly inside the spacing was rejected by evaluate_plot, so the
                    # band that starts there is what this factor can actually see: closer to its
                    # inner edge (barely legal) costs more than a roomy plot.
                    if geometry.spacing <= distance < 2 * geometry.spacing:
                        saplings += (2 * geometry.spacing - distance) / geometry.spacing
                    # Alignment: existing saplings exactly one spacing apart on a straight row.
                    if (dx == 0 or dz == 0) and abs(distance - geometry.spacing) < 0.5:
                        aligned += 1.0
                if is_tree:
                    # A log or leaf column: an existing tree, not a bare step.
                    if near_enough:
                        mature += 1.0
                elif near_enough:
                    if neighbour.ground_y >= sy:
                        # Surface level with or above the planting cell: a step or pillar.
                        obstacles += 1.0
                    else:
                        # Or a solid block standing next to the trunk at trunk height.
                        for height in range(0, min(geometry.vertical_clearance, 3) + 1):
                            if neighbour.is_free_at(sy + height) is False:
                                obstacles += 1.0
                                break
                # Density counts *planted* neighbours only: an open field must not be
                # penalised for being empty, which would make the factor a constant.
                if distance <= crowd_ring and (is_tree or neighbour.has_sapling):
                    crowding += 1.0 / (distance * distance)

    factors["alignment"] = options.alignment_weight * min(aligned, 2.0)
    factors["flat"] = options.flat_weight if _is_flat_plot(snapshot, cells, ring) else 0.0

    factors["obstacle"] = -options.obstacle_weight * obstacles / len(cells)
    factors["mature_tree"] = -options.mature_weight * mature / len(cells)
    factors["saplings"] = -options.crowding_weight * saplings / len(cells)
    factors["density"] = -options.density_weight * crowding / len(cells)

    if origin is not None:
        reach = ((centre_x - origin[0]) ** 2 + (centre_z - origin[1]) ** 2) ** 0.5
        factors["reach"] = -options.reach_weight * max(0.0, reach - 1.0)
    else:
        factors["reach"] = 0.0

    score = sum(factors.values())
    return score, factors


def _is_flat_plot(snapshot: Snapshot, cells: tuple[tuple[int, int, int], ...], ring: int) -> bool:
    """Whether the plot *and* its surroundings sit on one level.

    A sapling on a slope can be displaced by the neighbouring block, and a plot boxed in
    by higher ground grows oddly, so level ground is preferred. Checking only the plot's
    own cells would make this constant for 1x1 plantings, which is why the ring is included.
    """
    if not cells:
        return False
    heights: set[int] = set()
    for x, _y, z in cells:
        column = _column(snapshot, x, z)
        if column is None or column.ground_y is None:
            return False
        heights.add(column.ground_y)
    if len(heights) != 1:
        return False
    level = heights.pop()
    centre_x = sum(cell[0] for cell in cells) / len(cells)
    centre_z = sum(cell[2] for cell in cells) / len(cells)
    for x, _y, z in cells:
        for dx in range(-ring, ring + 1):
            for dz in range(-ring, ring + 1):
                if dx == 0 and dz == 0:
                    continue
                neighbour = _column(snapshot, x + dx, z + dz)
                if neighbour is None or neighbour.ground_y is None:
                    continue
                if abs(x + dx - centre_x) > ring or abs(z + dz - centre_z) > ring:
                    continue
                if neighbour.ground_y != level:
                    return False
    return True


def _neighbourhood_saplings(snapshot: Snapshot, cells: tuple[tuple[int, int, int], ...]) -> int:
    """Count saplings already inside the plot, used to prefer finishing a 2x2 array."""
    total = 0
    for x, _, z in cells:
        column = _column(snapshot, x, z)
        occupant = column.occupant if column is not None else None
        if occupant is not None and is_sapling_block(occupant):
            total += 1
    return total



@dataclass(frozen=True)
class _SnapshotIndex:
    """Facts derived once per snapshot instead of rediscovered for every candidate."""

    saplings: frozenset[tuple[int, int]]
    trunks: frozenset[tuple[int, int]]
    trees: frozenset[tuple[int, int]]
    ground_y: dict[tuple[int, int], int]
    blocked_masks: dict[tuple[int, int], int]


def _build_index(snapshot: Snapshot) -> _SnapshotIndex:
    saplings: set[tuple[int, int]] = set()
    trunks: set[tuple[int, int]] = set()
    trees: set[tuple[int, int]] = set()
    ground_y: dict[tuple[int, int], int] = {}
    blocked_masks: dict[tuple[int, int], int] = {}
    for pos, column in snapshot.columns.items():
        if column.ground_y is not None:
            ground_y[pos] = int(column.ground_y)
        if column.has_sapling:
            saplings.add(pos)
        # Preserve the public evaluator's trunk semantics exactly, but pay the
        # O(clearance-depth) scan only once per column instead of once per candidate.
        if _column_has_trunk(column):
            trunks.add(pos)
        ground = column.ground
        occupant = column.occupant
        if (ground is not None and is_tree_structure(ground)) or (
            occupant is not None and is_tree_structure(occupant)
        ):
            trees.add(pos)
        # Bit i means Column.is_free_at(ground_y + 1 + i) is definitively False.
        # This turns the hot obstacle vertical probe into one integer mask test.
        mask = 0
        if column.ground_y is not None:
            base = int(column.ground_y) + 1
            for i in range(len(column.cells)):
                if column.is_free_at(base + i) is False:
                    mask |= 1 << i
        blocked_masks[pos] = mask
    return _SnapshotIndex(
        frozenset(saplings), frozenset(trunks), frozenset(trees), ground_y, blocked_masks
    )


def _near_sparse(
    positions: frozenset[tuple[int, int]],
    own: frozenset[tuple[int, int]],
    radius: int,
) -> bool:
    """Whether a sparse indexed point lies strictly inside *radius* of the plot."""
    if radius <= 1 or not positions:
        return False
    limit2 = radius * radius
    for px, pz in positions:
        if (px, pz) in own:
            continue
        for ox, oz in own:
            dx, dz = px - ox, pz - oz
            d2 = dx * dx + dz * dz
            if 0 < d2 < limit2:
                return True
    return False


def _fast_evaluate_plot(
    snapshot: Snapshot,
    index: _SnapshotIndex,
    profile: TreeProfile,
    x: int,
    z: int,
    options: PlanOptions,
    geometry: ScaledGeometry,
) -> tuple[Verdict, tuple[tuple[int, int, int], ...]]:
    footprint = profile.footprint
    columns: list[Column] = []
    for dx in range(footprint):
        for dz in range(footprint):
            column = snapshot.column(x + dx, z + dz)
            if column is None or column.ground_y is None or column.ground is None:
                return Verdict(False, (REASON_NO_GROUND,)), ()
            columns.append(column)

    ground_ys = {int(column.ground_y) for column in columns if column.ground_y is not None}
    if len(ground_ys) > 1 and max(ground_ys) - min(ground_ys) > options.max_plot_y_delta:
        return Verdict(False, (REASON_NO_GROUND,)), ()
    plant_y = max(ground_ys) + 1
    cells = plot_cells(x, plant_y, z, footprint)
    reasons: list[str] = []
    planted = 0
    ordered = sorted(columns, key=lambda column: (column.z, column.x))
    for column, cell in zip(ordered, cells, strict=True):
        if column.ground not in profile.ground or column.ground not in SOIL:
            reasons.append(REASON_ILLEGAL_GROUND)
        if not _planting_cell_free(column, cell[1]):
            reasons.append(REASON_OBSTRUCTED)
        if column.has_sapling:
            planted += 1
        for offset in range(1, geometry.vertical_clearance + 1):
            if column.is_free_at(plant_y + offset) is False:
                reasons.append(REASON_NO_CLEARANCE)
                break

    own = frozenset((cell[0], cell[2]) for cell in cells)
    if _near_sparse(index.saplings, own, geometry.spacing):
        reasons.append(REASON_TOO_CLOSE)
    trunk_distance = max(0, options.min_trunk_distance)
    if trunk_distance > 0 and _near_sparse(index.trunks, own, trunk_distance):
        reasons.append(REASON_TOO_CLOSE_TO_TREE)
    if planted == len(columns):
        reasons.append(REASON_ALREADY_PLANTED)
    if reasons:
        return Verdict(False, tuple(dict.fromkeys(reasons))), ()
    return Verdict(True), cells


def _fast_is_flat_plot(
    index: _SnapshotIndex,
    cells: tuple[tuple[int, int, int], ...],
    ring: int,
) -> bool:
    if not cells:
        return False
    levels = {index.ground_y.get((x, z)) for x, _y, z in cells}
    if None in levels or len(levels) != 1:
        return False
    level = next(iter(levels))
    centre_x = sum(cell[0] for cell in cells) / len(cells)
    centre_z = sum(cell[2] for cell in cells) / len(cells)
    min_x = min(cell[0] for cell in cells) - ring
    max_x = max(cell[0] for cell in cells) + ring
    min_z = min(cell[2] for cell in cells) - ring
    max_z = max(cell[2] for cell in cells) + ring
    # The old routine visited this same centre-clipped set once per footprint cell.
    # Visiting each coordinate once is equivalent and much cheaper for 2x2 species.
    for x in range(min_x, max_x + 1):
        if abs(x - centre_x) > ring:
            continue
        for z in range(min_z, max_z + 1):
            if abs(z - centre_z) > ring:
                continue
            neighbour_y = index.ground_y.get((x, z))
            if neighbour_y is not None and neighbour_y != level:
                return False
    return True


def _blocked_between(index: _SnapshotIndex, pos: tuple[int, int], start_y: int, span: int) -> bool:
    ground_y = index.ground_y.get(pos)
    if ground_y is None:
        return False
    start = start_y - (ground_y + 1)
    end = start + span
    # Out-of-window cells were ``None`` in Column.is_free_at and therefore were not
    # counted as obstacles by the original scorer. Clip to the known bit range.
    lo = max(0, start)
    hi = min(12, end + 1)
    if lo >= hi:
        return False
    bits = ((1 << (hi - lo)) - 1) << lo
    return bool(index.blocked_masks.get(pos, 0) & bits)


def _fast_score_plot(
    snapshot: Snapshot,
    index: _SnapshotIndex,
    profile: TreeProfile,
    cells: tuple[tuple[int, int, int], ...],
    options: PlanOptions,
    geometry: ScaledGeometry,
    origin: tuple[int, int] | None,
) -> tuple[float, dict[str, float]]:
    """Equivalent scorer with sparse tree/sapling queries and no hot-loop sqrt."""
    factors: dict[str, float] = {"base": BASE_SCORE}
    n_cells = len(cells)
    centre_x = sum(cell[0] for cell in cells) / n_cells
    centre_z = sum(cell[2] for cell in cells) / n_cells

    soil_penalties = 0.0
    for cell in cells:
        column = snapshot.column(cell[0], cell[2])
        if column is not None and column.ground is not None and column.ground not in profile.preferred_ground:
            soil_penalties += options.soil_penalty
    factors["soil"] = -soil_penalties / n_cells

    ring = max(geometry.horizontal_clearance, 1)
    crowd_ring = max(geometry.spacing, ring)
    plot_ring = max(2 * geometry.spacing, crowd_ring)
    ring2 = ring * ring
    crowd2 = crowd_ring * crowd_ring
    plot2 = plot_ring * plot_ring
    own = frozenset((cell[0], cell[2]) for cell in cells)

    obstacles = 0.0
    mature = 0.0
    saplings = 0.0
    crowding = 0.0
    aligned = 0.0

    # Mature-tree and sapling work is sparse. The old implementation walked every
    # cell in a 29x29 circle for dark oak, then repeatedly rescanned Column.cells.
    for sx, _sy, sz in cells:
        for px, pz in index.saplings:
            if (px, pz) in own:
                continue
            dx, dz = px - sx, pz - sz
            d2 = dx * dx + dz * dz
            if d2 <= 0 or d2 > plot2:
                continue
            if d2 <= crowd2:
                crowding += 1.0 / d2
            # Only actual saplings pay for sqrt, and there are normally very few.
            distance = sqrt(d2)
            if geometry.spacing <= distance < 2 * geometry.spacing:
                saplings += (2 * geometry.spacing - distance) / geometry.spacing
            if (dx == 0 or dz == 0) and abs(distance - geometry.spacing) < 0.5:
                aligned += 1.0

        for px, pz in index.trees:
            if (px, pz) in own:
                continue
            dx, dz = px - sx, pz - sz
            d2 = dx * dx + dz * dz
            if d2 <= 0:
                continue
            if d2 <= ring2:
                mature += 1.0
            if d2 <= crowd2:
                crowding += 1.0 / d2

        # Terrain/obstacle checks are dense but only need the small trunk-clearance
        # radius (<=3 for shipped profiles), never the much wider 2*spacing ring.
        sy = cells[0][1] if n_cells == 1 else next(c[1] for c in cells if c[0] == sx and c[2] == sz)
        for dx in range(-ring, ring + 1):
            for dz in range(-ring, ring + 1):
                d2 = dx * dx + dz * dz
                if d2 == 0 or d2 > ring2:
                    continue
                nx, nz = sx + dx, sz + dz
                if (nx, nz) in own or (nx, nz) in index.trees:
                    continue
                neighbour_y = index.ground_y.get((nx, nz))
                if neighbour_y is None:
                    continue
                if neighbour_y >= sy:
                    obstacles += 1.0
                elif _blocked_between(index, (nx, nz), sy, min(geometry.vertical_clearance, 3)):
                    obstacles += 1.0

    factors["alignment"] = options.alignment_weight * min(aligned, 2.0)
    factors["flat"] = options.flat_weight if _fast_is_flat_plot(index, cells, ring) else 0.0
    factors["obstacle"] = -options.obstacle_weight * obstacles / n_cells
    factors["mature_tree"] = -options.mature_weight * mature / n_cells
    factors["saplings"] = -options.crowding_weight * saplings / n_cells
    factors["density"] = -options.density_weight * crowding / n_cells

    if origin is not None:
        dx, dz = centre_x - origin[0], centre_z - origin[1]
        reach = sqrt(dx * dx + dz * dz)
        factors["reach"] = -options.reach_weight * max(0.0, reach - 1.0)
    else:
        factors["reach"] = 0.0
    return sum(factors.values()), factors

def plan_options(config: ArboristConfig) -> PlanOptions:
    """Translate user-facing settings into planner weights.

    The config stores penalties as fractions of the base score (0..1) because that is what an
    administrator can reason about in a TOML file; the planner works in absolute points. The
    preference floor is likewise a fraction, mapped onto :data:`BASE_SCORE`.

    Args:
        config: Validated plugin configuration.

    Returns:
        Weights to hand to :func:`plan`.
    """
    return PlanOptions(
        max_candidates=config.plan_capacity,
        min_score=config.min_score * BASE_SCORE,
        obstacle_weight=config.obstacle * BASE_SCORE,
        crowding_weight=config.sapling_spacing * BASE_SCORE,
        mature_weight=config.mature_tree * BASE_SCORE,
        alignment_weight=config.alignment_bonus * BASE_SCORE,
        soil_penalty=config.conditional_soil * BASE_SCORE,
        density_weight=config.density * BASE_SCORE,
        spacing_scale=config.spacing_scale,
        min_trunk_distance=config.min_trunk_distance,
    )


def plan(
    snapshot: Snapshot,
    profile: TreeProfile,
    options: PlanOptions | None = None,
    origin: tuple[int, int] | None = None,
) -> Plan:
    """Evaluate a snapshot using one derived index and return a reusable plan reservoir."""
    opts = options or PlanOptions()
    geometry = scaled_geometry(profile, opts)
    index = _build_index(snapshot)
    positions = sorted(snapshot.positions())
    evaluated = 0
    rejected = 0
    scored: list[Candidate] = []

    for x, z in positions:
        verdict, cells = _fast_evaluate_plot(snapshot, index, profile, x, z, opts, geometry)
        if not verdict.valid:
            rejected += 1
            continue
        evaluated += 1
        score, factors = _fast_score_plot(snapshot, index, profile, cells, opts, geometry, origin)
        if profile.footprint > 1:
            planted = _neighbourhood_saplings(snapshot, cells)
            if planted:
                factors["array"] = opts.alignment_weight * planted
                score += factors["array"]
        if score >= opts.min_score:
            scored.append(
                Candidate(
                    cells=cells,
                    anchor=cells[0],
                    score=round(score, 3),
                    factors=factors,
                    footprint=profile.footprint,
                )
            )

    scored.sort(key=lambda candidate: (-candidate.score, candidate.anchor))
    best = _spread_out(scored, geometry.spacing)
    return Plan(
        candidates=tuple(best[: opts.max_candidates]),
        evaluated=evaluated,
        rejected=rejected,
        scanned_columns=len(positions),
    )


def _plot_centre(candidate: Candidate) -> tuple[float, float]:
    """Horizontal centre of a plot, in block coordinates."""
    xs = [cell[0] for cell in candidate.cells]
    zs = [cell[2] for cell in candidate.cells]
    return (sum(xs) / len(xs), sum(zs) / len(zs))


def _spread_out(candidates: list[Candidate], spacing: int) -> list[Candidate]:
    """Keep recommendations far enough apart to each be worth planting.

    Markers one block apart would ask the player to build exactly the crowding the
    scorer penalises, because scoring only sees saplings that already exist and not
    the other plots recommended in the same tick. So an accepted plot must sit at
    least the species' spacing away from every plot already accepted -- the spacing
    *after* ``spacing-scale``, so widening the knob really does spread the markers --
    which also subsumes the old rule that merely removed identical cells.

    Args:
        candidates: Scored candidates, best first.
        spacing: Minimum centre-to-centre distance in blocks.
    """
    minimum = float(spacing)
    kept: list[Candidate] = []
    centres: list[tuple[float, float]] = []
    for candidate in candidates:
        centre = _plot_centre(candidate)
        crowded = any(
            (centre[0] - other[0]) ** 2 + (centre[1] - other[1]) ** 2 < minimum * minimum for other in centres
        )
        if crowded:
            continue
        kept.append(candidate)
        centres.append(centre)
    return kept
