"""Bounded, cached world access for the planting analysis.

The plugin must never walk a whole chunk or dimension. Instead it samples short
vertical *columns* inside a small radius around each interested player, and reuses
cached columns until something nearby changes.

Everything in this module works against a plain callable::

    probe(x, y, z) -> block identifier

which keeps it independent of Endstone: :class:`ColumnScanner` is exercised by unit
tests with a synthetic probe, and :func:`endstone_arborist.plugin` supplies a probe
backed by ``Dimension.get_block_at``.

Design notes
------------
* A column result (:class:`Column`) is the only unit of world state the planner reads.
  Ground validity, occupancy, vertical clearance, obstacles, neighbouring saplings and
  mature-tree proximity are all answers to column-local questions, so no per-candidate
  probing is needed.
* ``ground_y`` is the highest block in the probe window that is neither air nor
  pass-through vegetation and is not itself a sapling. A sapling found above the ground
  is reported as the column's ``occupant`` instead, which is what lets the planner tell
  "good empty soil" apart from "soil that already has a sapling".
* Columns are cached per dimension with a generation counter. A single bump invalidates
  every cached column, which is the coarse but provably correct response to block
  changes and chunk unloads.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from endstone_arborist.blocks import SAPLING_BLOCKS, is_pass_through, is_sapling_block

__all__ = [
    "BlockProbe",
    "Column",
    "ColumnScanner",
    "Snapshot",
]

#: Reads the identifier of the block at an absolute coordinate.
BlockProbe = Callable[[int, int, int], str]


@dataclass(frozen=True)
class Column:
    """One sampled ``(x, z)`` column.

    Attributes:
        x: Block X coordinate of the column.
        z: Block Z coordinate of the column.
        ground_y: Y of the highest support block found, or ``None`` when the probe
            window contained nothing plantable (for example a pure-air column).
        ground: Identifier of the block at :attr:`ground_y`, or ``None``.
        cells: Identifiers from ``ground_y + 1`` upward, exactly ``clearance_depth``
            entries long. Missing reads are filled with the sentinel returned by the
            probe, so the tuple length is stable and indexable.
        probed: Number of block reads used to build this column. Used by tests and by
            the debug command to prove the work is bounded.
    """

    x: int
    z: int
    ground_y: int | None
    ground: str | None
    cells: tuple[str, ...] = ()
    probed: int = 0

    @property
    def surface_y(self) -> int | None:
        """Y of the cell a sapling would occupy, or ``None`` without ground."""
        return None if self.ground_y is None else self.ground_y + 1

    @property
    def occupant(self) -> str | None:
        """Identifier of whatever currently sits in the planting cell."""
        return self.cells[0] if self.cells else None

    @property
    def has_sapling(self) -> bool:
        """Whether the planting cell already holds a sapling."""
        occupant = self.occupant
        return occupant is not None and is_sapling_block(occupant)

    def block_at(self, y: int) -> str | None:
        """Cached identifier at absolute *y*, or ``None`` when outside the window."""
        if self.ground_y is None:
            return None
        index = y - (self.ground_y + 1)
        if 0 <= index < len(self.cells):
            return self.cells[index]
        return None

    def is_free_at(self, y: int) -> bool | None:
        """Whether the cell at *y* is passable; ``None`` when the cache cannot tell.

        The planting cell (``ground_y + 1``) is special: a replaceable plant such as tall
        grass counts as free because vanilla overwrites it when a sapling is placed.
        """
        block_type = self.block_at(y)
        if block_type is None:
            return None
        if self.ground_y is not None and y == self.ground_y + 1:
            # The planting cell accepts vegetation (vanilla overwrites it) and an
            # existing sapling (a candidate plot already partially planted).
            return is_pass_through(block_type) or is_sapling_block(block_type)
        return is_pass_through(block_type)


class _SupportsName(Protocol):
    name: str


def _dimension_key(dimension: object) -> str:
    """Return a cache key for a dimension-like object."""
    name = getattr(dimension, "name", None)
    return name if isinstance(name, str) else repr(dimension)


@dataclass
class Snapshot:
    """A set of columns covering one scan, plus the profile-independent facts about it.

    Attributes:
        columns: Cached :class:`Column` objects keyed by ``(x, z)``.
        origin_x: Block X the scan was centred on.
        origin_z: Block Z the scan was centred on.
        origin_y: Block Y the vertical probe windows were anchored to.
        radius: Horizontal radius actually covered.
    """

    columns: dict[tuple[int, int], Column] = field(default_factory=dict)
    origin_x: int = 0
    origin_z: int = 0
    origin_y: int = 0
    radius: int = 0

    def column(self, x: int, z: int) -> Column | None:
        """Return the sampled column at ``(x, z)`` when this snapshot covers it."""
        return self.columns.get((x, z))

    def covered(self, coords: Iterable[tuple[int, int]]) -> bool:
        """Whether every ``(x, z)`` in *coords* is present in this snapshot."""
        return all(coord in self.columns for coord in coords)

    def positions(self) -> Sequence[tuple[int, int]]:
        """All sampled horizontal positions, in insertion order."""
        return tuple(self.columns)


class ColumnScanner:
    """Builds :class:`Snapshot` objects from a probe, caching columns per dimension.

    Args:
        probe: Callable returning a block identifier for absolute coordinates.
        clearance_depth: How many cells above the ground to read. Must cover the tallest
            vertical clearance any profile asks for.
        look_up: Extra cells probed above the anchor Y when hunting for ground, so a
            player standing under an overhang still sees the overhang.
        look_down: Cells probed below the anchor Y. Bounds how far the search descends,
            which is what keeps the scan from walking the whole column.
    """

    def __init__(
        self,
        probe: BlockProbe,
        *,
        clearance_depth: int = 12,
        look_up: int = 4,
        look_down: int = 8,
        max_cached_columns: int = 8192,
    ) -> None:
        if clearance_depth < 1:
            raise ValueError("clearance_depth must be positive")
        if look_down < 1:
            raise ValueError("look_down must be positive")
        if max_cached_columns < 256:
            raise ValueError("max_cached_columns must be at least 256")
        self._probe = probe
        self._clearance_depth = clearance_depth
        self._look_up = look_up
        self._look_down = look_down
        self._max_cached_columns = int(max_cached_columns)
        self._cache: dict[str, OrderedDict[tuple[int, int], Column]] = {}
        self._generation = 0

    @property
    def clearance_depth(self) -> int:
        """Cells above the ground that columns retain."""
        return self._clearance_depth

    @property
    def generation(self) -> int:
        """Current cache generation, for diagnostics."""
        return self._generation

    def invalidate(self, dimension: object | None = None) -> None:
        """Drop cached columns.

        Args:
            dimension: When given, only that dimension's cache is cleared. ``None``
                clears every dimension.
        """
        if dimension is None:
            self._cache.clear()
        else:
            self._cache.pop(_dimension_key(dimension), None)
        self._generation += 1

    def drop(self, dimension: object, x: int, z: int) -> bool:
        """Forget one cached column.

        A block change can only alter the column that contains it: every cached field
        (ground height, ground type, the cells above it) is per-column, so the rest of
        the dimension stays valid. Returns True when a column was actually dropped.
        """
        cache = self._cache.get(_dimension_key(dimension))
        if not cache or (x, z) not in cache:
            return False
        del cache[(x, z)]
        self._generation += 1
        return True

    def cached_count(self, dimension: object) -> int:
        """Number of cached columns for *dimension* (diagnostics and tests)."""
        return len(self._cache.get(_dimension_key(dimension), {}))

    def scan(self, dimension: object, origin_x: int, origin_y: int, origin_z: int, radius: int) -> Snapshot:
        """Sample every column within *radius* of ``(origin_x, origin_z)``.

        Columns already cached for the current generation are reused verbatim, so a
        player standing still performs no new block reads.

        Args:
            dimension: The dimension being scanned; only used as a cache key.
            origin_x: Block X at the centre of the scan.
            origin_y: Block Y the probe windows are anchored to (usually the player's
                feet), which keeps the vertical probing bounded.
            origin_z: Block Z at the centre of the scan.
            radius: Horizontal radius in blocks.

        Returns:
            A :class:`Snapshot` covering the full ``(2r+1)^2` grid.
        """
        if radius < 0:
            raise ValueError("radius must not be negative")
        key = _dimension_key(dimension)
        cache = self._cache.setdefault(key, OrderedDict())
        snapshot = Snapshot(origin_x=origin_x, origin_z=origin_z, origin_y=origin_y, radius=radius)
        for x in range(origin_x - radius, origin_x + radius + 1):
            for z in range(origin_z - radius, origin_z + radius + 1):
                cached = cache.get((x, z))
                stale = cached is not None and (
                    cached.ground_y is None or abs(cached.ground_y - origin_y) > self._window()
                )
                if cached is None or stale:
                    cached = self._sample(x, origin_y, z)
                    cache[(x, z)] = cached
                else:
                    cache.move_to_end((x, z))
                snapshot.columns[(x, z)] = cached
        while len(cache) > self._max_cached_columns:
            cache.popitem(last=False)
        return snapshot

    def _window(self) -> int:
        return self._look_up + self._look_down + self._clearance_depth

    def _sample(self, x: int, anchor_y: int, z: int) -> Column:
        """Probe a single column and package the result as a :class:`Column`."""
        probed = 0
        ground_y: int | None = None
        ground: str | None = None
        # Descend from the top of the window, skipping air, vegetation and saplings,
        # until a real support block is found.
        for y in range(anchor_y + self._look_up, anchor_y - self._look_down - 1, -1):
            block_type = self._probe(x, y, z)
            probed += 1
            if is_pass_through(block_type) or block_type in SAPLING_BLOCKS:
                continue
            ground_y = y
            ground = block_type
            break
        cells: list[str] = []
        if ground_y is not None:
            for offset in range(1, self._clearance_depth + 1):
                cells.append(self._probe(x, ground_y + offset, z))
                probed += 1
        return Column(x=x, z=z, ground_y=ground_y, ground=ground, cells=tuple(cells), probed=probed)
