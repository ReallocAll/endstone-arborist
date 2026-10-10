"""Auto-Plant: turn the player's current internal plan into placements, as data.

Placement re-uses :func:`endstone_arborist.planner.evaluate_plot` rather than re-implementing
validity, so the planning and commit validation cannot drift apart: a cell stops being plantable for
exactly the reasons the planner would have refused to recommend it.

This module performs no I/O. It emits :class:`PlaceOp` records and the plugin executes them
against Endstone. Rules encoded here:

* The world is re-read at commit time; a plot that became invalid is skipped, never forced.
* Cells are only claimed when the planner still accepts the whole plot, so a 2x2 species never
  ends up with a broken half array.
* Placements never exceed the sapling budget the caller says the player owns.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from endstone_arborist.planner import Plan, PlanOptions, evaluate_plot, scaled_geometry
from endstone_arborist.profiles import TreeProfile
from endstone_arborist.world import ColumnScanner

__all__ = ["CommitPlan", "PlaceOp", "feedback", "prepare"]

#: ``(x, y, z) -> block id`` for the dimension being planted.
Probe = Callable[[int, int, int], str]

#: Budget ran out before this plot could be planted whole.
SKIP_FUNDED = "not_enough_saplings"

#: Ceiling for the commit scan radius. The spacing gate is what needs the wide sample, so a
#: heavily scaled species must not turn one commit into a whole-field scan; a spacing above this
#: is still enforced at preview time, where the full scan already exists.
MAX_COMMIT_SCAN_RADIUS = 12


@dataclass(frozen=True)
class PlaceOp:
    """One sapling to place at an absolute cell.

    Attributes:
        x: Block X of the planting cell.
        y: Block Y of the planting cell (the cell the sapling occupies).
        z: Block Z of the planting cell.
        sapling: Identifier to place.
    """

    x: int
    y: int
    z: int
    sapling: str


@dataclass(frozen=True)
class CommitPlan:
    """Outcome of re-validating a :class:`Plan`.

    Attributes:
        ops: Placements to perform, in commit order.
        skipped: Counts keyed by planner reason strings plus :data:`SKIP_FUNDED`.
        plots_total: Candidate plots considered.
        plots_committed: Plots that survived validation and were affordable.
    """

    ops: tuple[PlaceOp, ...] = ()
    skipped: Mapping[str, int] = field(default_factory=dict)
    plots_total: int = 0
    plots_committed: int = 0

    @property
    def count(self) -> int:
        """Saplings this commit places."""
        return len(self.ops)


def prepare(
    plan: Plan,
    profile: TreeProfile,
    scanner: ColumnScanner,
    dimension_key: str,
    *,
    saplings_available: int,
    options: PlanOptions | None = None,
) -> CommitPlan:
    """Re-validate *plan* against the live world and emit placement ops.

    Args:
        plan: The plan currently displayed to this player.
        profile: Species being planted.
        scanner: Column scanner bound to the player's dimension. Only the spacing ring around
            each plot is sampled, so committing costs one bounded ring, not a field scan.
        dimension_key: Cache key passed through to the scanner.
        saplings_available: How many saplings the player actually holds. The commit never
            exceeds this, so the inventory cannot go negative or be duplicated.
        options: Planner options; ``max_plot_y_delta``, ``spacing_scale`` and
            ``min_trunk_distance`` are consulted, the last two because they size the neighbour ring
            the commit has to sample.

    Returns:
        A :class:`CommitPlan` whose ops are the empty cells of every accepted plot.
    """
    opts = options or PlanOptions()
    budget = max(0, int(saplings_available))
    # evaluate_plot rejects a plot with a rival sapling strictly inside the *scaled* spacing, and a
    # trunk strictly inside ``min-trunk-distance``, so the commit snapshot has to reach whichever
    # ring is wider or those shared rules are blind at planting time (a 1x1 species' own footprint
    # only needed radius 0). Preview and commit therefore scan the same ring.
    radius = min(
        max(scaled_geometry(profile, opts).spacing, max(0, opts.min_trunk_distance)),
        MAX_COMMIT_SCAN_RADIUS,
    )
    ops: list[PlaceOp] = []
    skipped: dict[str, int] = {}
    committed = 0

    for candidate in plan.candidates:
        anchor = candidate.anchor
        snapshot = scanner.scan(dimension_key, anchor[0], anchor[1], anchor[2], radius)
        verdict, cells = evaluate_plot(snapshot, profile, anchor[0], anchor[2], opts)
        if not verdict.valid:
            for reason in verdict.reasons:
                skipped[reason] = skipped.get(reason, 0) + 1
            continue
        # evaluate_plot accepts a partially planted plot on purpose (finish-your-2x2),
        # so only the cells that are still empty are worth placing.
        empty = [cell for cell in cells if not _already_planted(snapshot, cell)]
        if not empty:
            continue
        if len(empty) > budget:
            # Planting part of a 2x2 would leave the player with a broken array, and
            # part of a 1x1 is impossible, so the whole plot is declined.
            skipped[SKIP_FUNDED] = skipped.get(SKIP_FUNDED, 0) + len(empty) - budget
            continue
        budget -= len(empty)
        committed += 1
        ops.extend(PlaceOp(x, y, z, profile.sapling) for x, y, z in empty)

    return CommitPlan(
        ops=tuple(ops),
        skipped=skipped,
        plots_total=len(plan.candidates),
        plots_committed=committed,
    )


def _already_planted(snapshot: object, cell: tuple[int, int, int]) -> bool:
    """Whether the planting cell of *cell* already holds a sapling."""
    column = snapshot.column(cell[0], cell[2])  # type: ignore[attr-defined]
    return bool(column is not None and column.has_sapling)


def feedback(commit: CommitPlan, profile: TreeProfile) -> str:
    """Build the one short message sent after a commit attempt.

    Deliberately terse: repeated swings must not fill the screen.
    """
    if commit.count:
        return f"Arborist：已自动种植 {commit.count} 株{profile.display_name}"
    if commit.skipped.get(SKIP_FUNDED):
        return f"Arborist：{profile.display_name}树苗不足，无法完成整组种植"
    if commit.plots_total:
        return "Arborist：原候选位置已经不再适合种植"
    return "Arborist：附近暂时没有可种植位置"
