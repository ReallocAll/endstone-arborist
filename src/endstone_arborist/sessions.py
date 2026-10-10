"""Per-player session state and throttling for automatic planting.

The 0.3.2 session model distinguishes *hard invalidation* from ordinary horizontal
movement.  Hard changes (dimension/Y, held sapling, density, external world changes)
advance ``generation`` and discard cached plans.  Walking on the same Y level keeps the
current plan alive inside a bounded reuse radius and requests a background prefetch
before that radius is exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from endstone_arborist.config import ArboristConfig
from endstone_arborist.planner import Plan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from endstone_arborist.profiles import TreeProfile

__all__ = ["PlayerSession", "SessionStore", "Throttle"]


def _chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Grid distance suited to the square scan window used by :class:`ColumnScanner`."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


@dataclass
class PlayerSession:
    """Mutable state for one opted-in player.

    ``plan_origin`` is the centre of the snapshot that produced ``plan``.  A player may
    move several blocks away and continue consuming the overlapping part of that plan.
    ``planning_origin`` plays the same role for the one in-flight background job.
    """

    unique_id: object
    name: str = ""
    dimension_key: str = ""
    x: int = 0
    y: int = 0
    z: int = 0
    sapling: str | None = None
    active: bool = False
    dirty: bool = True
    last_scan_tick: int | None = None
    last_render_tick: int | None = None
    last_tip_tick: int | None = None
    plan: Plan | None = None
    plan_origin: tuple[int, int] | None = None
    profile: TreeProfile | None = None
    spacing_scale: float = 1.0
    generation: int = 0
    planning: bool = False
    planning_origin: tuple[int, int] | None = None
    movement_prefetch: bool = False

    @property
    def block_pos(self) -> tuple[int, int]:
        return (self.x, self.z)

    def distance_from_plan(self) -> int | None:
        if self.plan_origin is None:
            return None
        return _chebyshev(self.block_pos, self.plan_origin)

    def move_to(
        self,
        dimension_key: str,
        x: int,
        y: int,
        z: int,
        *,
        prefetch_distance: int,
        reuse_distance: int,
    ) -> bool:
        """Update position while preserving a useful rolling plan.

        Horizontal walking is a soft state change.  The old reservoir remains usable
        until ``reuse_distance`` and a replacement is requested at
        ``prefetch_distance``.  Dimension or vertical movement is hard-invalidated
        because the old column heights/world identity are no longer trustworthy.
        """
        old_dimension = self.dimension_key
        old_x, old_y, old_z = self.x, self.y, self.z
        changed = old_dimension != dimension_key or old_x != x or old_y != y or old_z != z
        if not changed:
            return False

        self.dimension_key = dimension_key
        self.x = x
        self.y = y
        self.z = z

        if old_dimension != dimension_key or old_y != y:
            self.invalidate()
            return True

        # First observation after creation has no usable origin yet.
        if self.plan is not None and self.plan_origin is not None:
            distance = _chebyshev(self.block_pos, self.plan_origin)
            if distance > reuse_distance:
                self.plan = None
                self.plan_origin = None
                self.dirty = True
                self.movement_prefetch = True
            elif distance >= prefetch_distance:
                # Double buffering: retain the old plan while a fresh one is produced.
                self.dirty = True
                self.movement_prefetch = True
        elif self.planning and self.planning_origin is not None:
            # Do not bump generation for movement.  Completion performs a distance check
            # against the job's origin and may still accept it if the overlap is useful.
            if _chebyshev(self.block_pos, self.planning_origin) > reuse_distance:
                self.dirty = True
                self.movement_prefetch = True
        else:
            self.dirty = True
            self.movement_prefetch = True
        return True

    def hold(self, sapling: str | None) -> bool:
        previous = self.sapling
        self.sapling = sapling
        if previous != sapling:
            self.invalidate()
            return True
        return False

    def invalidate(self) -> None:
        """Hard-invalidates cached and in-flight results."""
        self.generation += 1
        self.plan = None
        self.plan_origin = None
        self.planning_origin = None
        self.dirty = True
        self.movement_prefetch = False

    def begin_plan(self, origin: tuple[int, int]) -> None:
        self.planning = True
        self.planning_origin = origin
        self.dirty = False
        self.movement_prefetch = False

    def reject_plan(self) -> None:
        self.planning = False
        self.planning_origin = None
        self.dirty = True

    def finish_plan(
        self,
        plan: Plan,
        generation: int,
        origin: tuple[int, int],
        *,
        prefetch_distance: int,
        reuse_distance: int,
    ) -> bool:
        """Install an async result when it is still useful at the player's live position."""
        self.planning = False
        self.planning_origin = None
        if generation != self.generation or not self.active:
            self.dirty = True
            return False
        distance = _chebyshev(self.block_pos, origin)
        if distance > reuse_distance:
            self.dirty = True
            self.movement_prefetch = True
            return False
        self.plan = plan
        self.plan_origin = origin
        # If the player moved near the edge while the worker was running, use this plan
        # immediately but queue the next double-buffered plan as soon as allowed.
        self.movement_prefetch = distance >= prefetch_distance
        self.dirty = self.movement_prefetch
        return True

    def near(self, dimension_key: str, x: int, z: int, radius: int) -> bool:
        if self.dimension_key != dimension_key:
            return False
        return abs(self.x - x) <= radius and abs(self.z - z) <= radius


class Throttle:
    def __init__(self, config: ArboristConfig) -> None:
        self._config = config

    @property
    def config(self) -> ArboristConfig:
        return self._config

    def can_scan(self, session: PlayerSession, tick: int) -> bool:
        if not session.active or not session.dirty:
            return False
        last = session.last_scan_tick
        if last is None:
            return True
        interval = (
            min(self._config.refresh_interval, self._config.movement_prefetch_interval)
            if session.movement_prefetch
            else self._config.refresh_interval
        )
        return tick - last >= interval

    def can_render(self, session: PlayerSession, tick: int) -> bool:
        last = session.last_render_tick
        return last is None or tick - last >= self._config.particle_interval

    def can_tip(self, session: PlayerSession, tick: int) -> bool:
        if not self._config.show_tip:
            return False
        last = session.last_tip_tick
        return last is None or tick - last >= self._config.tip_interval

    def scan_radius(self) -> int:
        return self._config.scan_radius


class SessionStore:
    def __init__(self, config: ArboristConfig) -> None:
        self._sessions: dict[object, PlayerSession] = {}
        self._throttle = Throttle(config)

    @property
    def throttle(self) -> Throttle:
        return self._throttle

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def sessions(self) -> tuple[PlayerSession, ...]:
        return tuple(self._sessions.values())

    @property
    def active(self) -> tuple[PlayerSession, ...]:
        return tuple(session for session in self._sessions.values() if session.active)

    def get(self, unique_id: object) -> PlayerSession | None:
        return self._sessions.get(unique_id)

    def get_or_create(self, unique_id: object, name: str = "") -> PlayerSession:
        session = self._sessions.get(unique_id)
        if session is None:
            session = PlayerSession(unique_id=unique_id, name=name)
            self._sessions[unique_id] = session
        elif name:
            session.name = name
        return session

    def remove(self, unique_id: object) -> bool:
        return self._sessions.pop(unique_id, None) is not None

    def clear(self) -> None:
        self._sessions.clear()

    def invalidate_near(
        self, dimension_key: str, pos: tuple[int, int], radius: int, *, exclude: object | None = None
    ) -> int:
        count = 0
        for session in self._sessions.values():
            if exclude is not None and session.unique_id == exclude:
                continue
            if not session.active:
                continue
            if session.near(dimension_key, pos[0], pos[1], radius):
                session.invalidate()
                count += 1
        return count
