"""Main-thread orchestration plus a single pure-Python background planner.

All Endstone-backed world reads happen while :meth:`tick` captures a ``Snapshot`` on
server thread. The expensive planner receives only immutable/plain Python data and runs
on a dedicated ``ArboristPlanner`` thread. Results are generation tagged; movement,
item changes, density changes, or nearby block changes make late results stale.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from endstone_arborist.adapter import Observation
from endstone_arborist.config import ArboristConfig
from endstone_arborist.planner import Plan, PlanOptions, plan, plan_options, scaled_geometry
from endstone_arborist.profiles import TreeProfile, profile_for_item, profiles
from endstone_arborist.sessions import PlayerSession, SessionStore, Throttle
from endstone_arborist.world import ColumnScanner, Snapshot

__all__ = ["ArboristController", "Effects", "ScanBudget"]

DEFAULT_CLEARANCE_DEPTH = 12


@dataclass(frozen=True)
class ScanBudget:
    scans: int = 0
    renders: int = 0
    tips: int = 0


@dataclass
class Effects:
    render: list[tuple[object, Plan, TreeProfile]] = field(default_factory=list)
    tips: list[tuple[object, str]] = field(default_factory=list)
    clears: list[object] = field(default_factory=list)
    planning_started: set[object] = field(default_factory=set)
    budget: ScanBudget = ScanBudget()

    @property
    def scan_count(self) -> int:
        return self.budget.scans


@dataclass(frozen=True)
class _PlanningMeta:
    unique_id: object
    generation: int
    sapling: str
    dimension_key: str
    origin: tuple[int, int]


def _run_plan(
    snapshot: Snapshot,
    profile: TreeProfile,
    options: PlanOptions,
    origin: tuple[int, int],
) -> Plan:
    """Worker entrypoint. It must stay entirely Endstone-free."""
    return plan(snapshot, profile, options, origin)


class ArboristController:
    def __init__(
        self,
        config: ArboristConfig,
        scanner_factory: Callable[[str], ColumnScanner],
        *,
        profile_lookup: Callable[[str], TreeProfile | None] = profile_for_item,
    ) -> None:
        self._config = config
        self._store = SessionStore(config)
        self._throttle: Throttle = self._store.throttle
        self._scanner_factory = scanner_factory
        self._scanners: dict[str, ColumnScanner] = {}
        self._profiles = plan_options(config)
        self._profile_lookup = profile_lookup
        self._tick = 0
        self._enabled = config.enabled
        # One worker is intentional on normal CPython: more CPU-bound Python threads only
        # fight over the GIL. One worker removes the expensive planner from the BDS tick
        # while keeping work coalesced and bounded.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ArboristPlanner")
        self._future: Future[Plan] | None = None
        self._future_meta: _PlanningMeta | None = None
        self._worker_error: str | None = None
        self._round_robin = 0

    @property
    def config(self) -> ArboristConfig:
        return self._config

    @property
    def options(self) -> PlanOptions:
        return self._profiles

    @property
    def sessions(self) -> SessionStore:
        return self._store

    @property
    def current_tick(self) -> int:
        return self._tick

    @property
    def worker_busy(self) -> bool:
        return self._future is not None

    @property
    def clearance_warnings(self) -> tuple[str, ...]:
        tallest = max(scaled_geometry(profile, self._profiles).requested_vertical_clearance for profile in profiles())
        if tallest <= DEFAULT_CLEARANCE_DEPTH:
            return ()
        return (
            f"spacing-scale={self._config.spacing_scale} asks {tallest} free cells above ground but a "
            f"column reports only {DEFAULT_CLEARANCE_DEPTH}; the check is capped, the extra height unreadable",
        )

    def pop_worker_error(self) -> str | None:
        error = self._worker_error
        self._worker_error = None
        return error

    def close(self) -> None:
        """Stop accepting work. Running pure-Python work may finish in the background."""
        future = self._future
        if future is not None:
            future.cancel()
        self._future = None
        self._future_meta = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def scanner(self, dimension_key: str) -> ColumnScanner:
        scanner = self._scanners.get(dimension_key)
        if scanner is None:
            scanner = self._scanner_factory(dimension_key)
            self._scanners[dimension_key] = scanner
        return scanner

    def on_join(self, unique_id: object, name: str) -> None:
        self._store.get_or_create(unique_id, name)

    def on_quit(self, unique_id: object) -> None:
        session = self._store.get(unique_id)
        if session is not None:
            session.invalidate()
        self._store.remove(unique_id)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self.invalidate_all()
            self._store.clear()
            for scanner in self._scanners.values():
                scanner.invalidate()

    def invalidate_all(self) -> None:
        """Invalidate every active plan/result, used when the TPS guard closes."""
        for session in self._store.sessions:
            if session.active:
                session.invalidate()

    def on_block_change(
        self,
        dimension_key: str,
        x: int,
        y: int,
        z: int,
        *,
        exclude_unique_id: object | None = None,
    ) -> int:
        scanner = self._scanners.get(dimension_key)
        if scanner is not None:
            scanner.drop(dimension_key, x, z)
        return self._store.invalidate_near(
            dimension_key,
            (x, z),
            self._config.scan_radius,
            exclude=exclude_unique_id,
        )

    def on_position_change(self, unique_id: object) -> None:
        session = self._store.get(unique_id)
        if session is not None:
            session.invalidate()

    def set_spacing_scale(self, unique_id: object, scale: float) -> None:
        session = self._store.get_or_create(unique_id)
        value = float(scale)
        if abs(session.spacing_scale - value) > 1e-9:
            session.spacing_scale = value
            session.invalidate()

    def options_for(self, session: PlayerSession) -> PlanOptions:
        return replace(self._profiles, spacing_scale=session.spacing_scale)

    def _apply(self, session: PlayerSession, observation: Observation) -> bool:
        session.name = observation.name
        session.move_to(
            observation.key,
            observation.x,
            observation.y,
            observation.z,
            prefetch_distance=self._config.plan_prefetch_distance,
            reuse_distance=self._config.plan_reuse_distance,
        )
        eligible = observation.eligible and observation.profile is not None
        profile = observation.profile if eligible else None
        session.hold(profile.sapling if profile else None)
        if not eligible:
            if session.active:
                session.invalidate()
            session.active = False
            session.profile = None
            return False
        if not session.active:
            session.active = True
            session.invalidate()
        session.profile = profile
        return True

    def _collect_completed(self, *, accept: bool) -> bool:
        future = self._future
        meta = self._future_meta
        if future is None or meta is None or not future.done():
            return False
        self._future = None
        self._future_meta = None
        session = self._store.get(meta.unique_id)
        try:
            result = future.result()
        except Exception:
            if session is not None:
                session.reject_plan()
            self._worker_error = traceback.format_exc()
            return True
        if session is None:
            return True
        if not accept:
            session.reject_plan()
            return True
        # Generation is the authoritative stale-result check. The extra metadata checks
        # are cheap belt-and-suspenders guards against accidental future refactors.
        if (
            session.generation != meta.generation
            or session.dimension_key != meta.dimension_key
            or session.sapling != meta.sapling
        ):
            session.reject_plan()
            return True
        session.finish_plan(
            result,
            meta.generation,
            meta.origin,
            prefetch_distance=self._config.plan_prefetch_distance,
            reuse_distance=self._config.plan_reuse_distance,
        )
        return True

    def _submit_one(self) -> object | None:
        if self._future is not None:
            return None
        candidates = [s for s in self._store.sessions if s.active and s.profile is not None]
        if not candidates:
            return None
        count = len(candidates)
        for step in range(count):
            index = (self._round_robin + step) % count
            session = candidates[index]
            if session.planning or not self._throttle.can_scan(session, self._tick):
                continue
            profile = session.profile
            if profile is None:
                continue
            # World/API access is deliberately completed here on the server thread.
            scanner = self.scanner(session.dimension_key)
            snapshot = scanner.scan(
                session.dimension_key,
                session.x,
                session.y,
                session.z,
                self._config.scan_radius,
            )
            generation = session.generation
            options = self.options_for(session)
            origin = session.block_pos
            session.begin_plan(origin)
            session.last_scan_tick = self._tick
            self._future_meta = _PlanningMeta(
                unique_id=session.unique_id,
                generation=generation,
                sapling=profile.sapling,
                dimension_key=session.dimension_key,
                origin=origin,
            )
            self._future = self._executor.submit(_run_plan, snapshot, profile, options, origin)
            self._round_robin = (index + 1) % count
            return session.unique_id
        return None

    def tick(
        self,
        observations: Sequence[Observation],
        elapsed: int = 1,
        *,
        allow_planning: bool = True,
    ) -> Effects:
        if elapsed < 1:
            raise ValueError("elapsed must be at least one tick")
        self._tick += elapsed
        effects = Effects()
        completed = self._collect_completed(accept=allow_planning)
        seen: set[object] = set()
        if not self._enabled:
            return effects

        for observation in observations:
            seen.add(observation.unique_id)
            session = self._store.get_or_create(observation.unique_id, observation.name)
            self._apply(session, observation)

        self._forget(seen, effects)
        # If a result completed this tick, let the plugin commit it before capturing
        # the next player snapshot; that avoids planning against world state that this
        # same tick is about to change.
        submitted = self._submit_one() if allow_planning and not completed else None
        if submitted is not None:
            effects.planning_started.add(submitted)
        effects.budget = ScanBudget(scans=1 if submitted is not None else 0)
        return effects

    def _forget(self, seen: set[object], effects: Effects) -> None:
        for session in list(self._store.sessions):
            if session.unique_id not in seen:
                session.invalidate()
                self._store.remove(session.unique_id)
                effects.clears.append(session.unique_id)

    def describe(self, unique_id: object) -> str:
        session = self._store.get(unique_id)
        if session is None:
            return "Arborist：当前没有你的跟踪状态"
        if not session.active:
            return "Arborist：空闲（手持受支持的树苗即可开始）"
        count = 0 if session.plan is None else len(session.plan.candidates)
        kind = session.profile.display_name if session.profile is not None else "未知树种"
        scale = session.spacing_scale
        density = "" if scale == 1.0 else f"，间距倍率={scale:g}"
        worker = "，规划中" if session.planning else ""
        return (
            f"Arborist：正在跟踪{kind}，当前位置 {session.x} {session.y} {session.z}，"
            f"候选区域 {count} 个{density}{worker}"
        )
