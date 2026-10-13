"""Endstone entrypoint for Arborist's automatic crop sowing and tree planting.

Players enable crop sowing with ``/arb sow on`` or tree planting with ``/arb on``.
The background tree planner is independent of lightweight main-thread sowing.
There is no preview/gesture state machine.

This module deliberately does not enable postponed annotations because Endstone inspects
event handler annotations at registration time and expects concrete event classes.
"""

import traceback
from dataclasses import replace
from pathlib import Path

from endstone import Player
from endstone.block import Block, BlockState
from endstone.command import Command, CommandSender
from endstone.event import (
    BlockBreakEvent,
    BlockGrowEvent,
    BlockPlaceEvent,
    EventPriority,
    LeavesDecayEvent,
    PlayerDimensionChangeEvent,
    PlayerGameModeChangeEvent,
    PlayerItemHeldEvent,
    PlayerJoinEvent,
    PlayerMoveEvent,
    PlayerQuitEvent,
    event_handler,
)
from endstone.inventory import PlayerInventory
from endstone.plugin import Plugin
from endstone.scheduler import Task

from endstone_arborist import autoplant
from endstone_arborist.blocks import SOIL, SurfaceState, classify_cell, is_pass_through, is_sapling_block
from endstone_arborist.adapter import Observation, dimension_key, held_sapling, make_probe, observe, supported_modes
from endstone_arborist.config import DENSITY_SCALES, RANGE_SPACING_SCALE, ArboristConfig
from endstone_arborist.controller import ArboristController
from endstone_arborist.performance import SparkTpsGate, TpsStatus
from endstone_arborist.planner import Candidate, scaled_geometry
from endstone_arborist.profiles import TreeProfile, supported_kinds
from endstone_arborist.protection import TerritoryBuildGuard
from endstone_arborist.sessions import PlayerSession
from endstone_arborist.settings import DENSITY_PRESETS, PlayerPreferences
from endstone_arborist.sowing import crop_for_item, sow_nearby, sowing_level
from endstone_arborist.world import ColumnScanner

__all__ = ["ArboristPlugin"]

_PREFERENCES_FILE = "players.json"
_TASK_PERIOD = 5


class ArboristPlugin(Plugin):
    """Automatic crop sowing and tree planting for Endstone servers."""

    prefix = "Arborist"  # type: ignore[assignment]
    api_version = "0.11"  # type: ignore[assignment]
    soft_depend = ["papi", "spark"]  # type: ignore[assignment]
    commands = {  # type: ignore[assignment]  # noqa: RUF012
        "arborist": {
            "description": "管理 Arborist 自动播种与植树设置。",
            # Use real Endstone enum overloads so Bedrock can validate arguments and
            # provide client-side command suggestions instead of greedily consuming a message.
            "usages": [
                "/arborist",
                "/arborist <on|off|status|reload>",
                "/arborist <density> [compact|default|sparse|server]",
                "/arborist <sow> [on|off|status]",
            ],
            "aliases": ["arb"],
            "permissions": ["arborist.command.arborist"],
        }
    }
    permissions = {  # type: ignore[assignment]  # noqa: RUF012
        "arborist.command.arborist": {
            "description": "允许使用 Arborist 自动播种与植树设置。",
            "default": True,
        },
        "arborist.command.admin": {
            "description": "允许重载 Arborist 自动播种与植树配置。",
            "default": "op",
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._controller: ArboristController | None = None
        self._task: Task | None = None
        self._period = _TASK_PERIOD
        self._modes = supported_modes(respect_creative=True)
        self._prefs: PlayerPreferences | None = None
        self._protection: TerritoryBuildGuard | None = None
        self._next_plant_tick: dict[object, int] = {}
        self._next_sow_tick: dict[object, int] = {}
        self._last_sow_scan: dict[object, tuple[tuple[str, int, int, int, str], int]] = {}
        self._tps_gate: SparkTpsGate | None = None
        self._tps_allowed = False
        self._tps_state_key: tuple[bool, bool, str | None] | None = None
        # TPS monitoring is demand-driven. There is no automatic planting work to
        # protect before the first player joins, and Spark may legitimately have no
        # rolling TPS sample during early server bootstrap.
        self._tps_monitor_started = False
        self._clock_tick = 0

    # ------------------------------------------------------------------ lifecycle

    def on_enable(self) -> None:
        self.save_default_config()
        config = self._load_config()
        self._controller = ArboristController(config, self._scanner_for)
        self._modes = supported_modes(respect_creative=config.respect_creative)
        self._prefs = PlayerPreferences(self.data_folder / _PREFERENCES_FILE)
        self._prefs.load()
        for note in self._prefs.notes:
            self.logger.warning(f"preferences: {note}")
        self._protection = self._make_protection(config)
        self._tps_gate = SparkTpsGate(
            self.server.service_manager, minimum=config.min_tps, interval_ticks=config.tps_check_interval
        )
        self._tps_monitor_started = False
        self.register_events(self)
        self._task = self.server.scheduler.run_task(self, self._tick, delay=20, period=_TASK_PERIOD)
        self.logger.info(
            f"enabled crop sowing (opt-in) and tree planting for {len(supported_kinds())} species "
            f"(radius {config.scan_radius}, refresh {config.refresh_interval}t, "
            f"server density {config.default_density}, sow cooldown {config.sow_cooldown}t, "
            f"planner worker=1, min TPS {config.min_tps:g})"
        )
        self._log_protection_state()

    def on_disable(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._next_plant_tick.clear()
        self._next_sow_tick.clear()
        self._last_sow_scan.clear()
        if self._prefs is not None:
            self._prefs.save()
            self._prefs = None
        if self._controller is not None:
            self._controller.sessions.clear()
            self._controller.close()
            self._controller = None
        self._protection = None
        self._tps_gate = None
        self._tps_allowed = False
        self._tps_monitor_started = False

    def _load_config(self) -> ArboristConfig:
        config = ArboristConfig.from_mapping(dict(self.config))
        for note in config.warnings:
            self.logger.warning(f"config: {note}")
        return config

    def _make_protection(self, config: ArboristConfig) -> TerritoryBuildGuard:
        plugins_dir = Path(self.data_folder).parent
        guard = TerritoryBuildGuard(
            plugins_dir / "territory" / "territory_data.db",
            plugins_dir / "territory" / "config.json",
            mode=config.territory_protection,
        )
        guard.refresh(force=True)
        return guard

    def _log_protection_state(self) -> None:
        guard = self._protection
        if guard is None or guard.mode == "off":
            self.logger.warning("Territory protection adapter is disabled by configuration")
            return
        if guard.detected and guard.healthy:
            self.logger.info(
                f"Territory protection adapter active ({len(guard.claims)} claim(s), "
                f"allow_op_as_member={guard.allow_op_as_member})"
            )
        elif guard.detected:
            self.logger.error((guard.last_error or "Territory adapter unavailable") + "; automatic writes fail closed")
        elif guard.mode == "required":
            self.logger.error((guard.last_error or "Territory database not found") + "; automatic writes fail closed")
        else:
            self.logger.info("Territory database not detected; no Territory claim checks are needed")

    def _update_tps_gate(self, *, force: bool = False) -> TpsStatus:
        gate = self._tps_gate
        if gate is None:
            self._tps_allowed = False
            return TpsStatus(False, False, None, "TPS 保护未初始化")
        if not self._tps_monitor_started:
            self._tps_allowed = False
            return TpsStatus(False, False, None, "等待首位玩家进入服务器", warming_up=True)
        status = gate.refresh(self._clock_tick, force=force)
        key = (status.available, status.allowed, None if status.available else status.reason)
        previously_allowed = self._tps_allowed
        self._tps_allowed = status.allowed
        if previously_allowed and not status.allowed and self._controller is not None:
            # Make any in-flight result stale and drop cached plans immediately.
            self._controller.invalidate_all()
        if key != self._tps_state_key:
            if status.allowed:
                self.logger.info(f"Spark TPS guard ready: {status.tps:.2f} TPS (minimum {gate.minimum:.2f})")
            elif status.available:
                self.logger.warning(status.reason + "; automatic sowing and tree planting paused")
            elif status.warming_up:
                # Normal during server startup while Spark is still filling its rolling
                # TPS window.  The guard remains fail-closed, but this is not an error.
                pass
            else:
                self.logger.warning(status.reason + "; automatic sowing and tree planting fail-closed")
            self._tps_state_key = key
        return status

    def _tps_minimum(self) -> float:
        if self._tps_gate is not None:
            return self._tps_gate.minimum
        if self._controller is not None:
            return self._controller.config.min_tps
        return 0.0

    def _tps_rejection(self, status: TpsStatus) -> str:
        if status.tps is not None:
            return f"当前服务器 TPS 为 {status.tps:.2f}，低于 Arborist 启用阈值 {self._tps_minimum():g}，暂时无法开启自动种植。"
        return f"暂时无法验证服务器 TPS（{status.reason}），为避免卡服已禁止开启自动种植。"

    # ------------------------------------------------------------------ planning / automatic commit

    def _scanner_for(self, key: str) -> ColumnScanner:
        dimension = self.server.level.get_dimension(key)
        if dimension is None:
            self.logger.debug(f"dimension {key} not loaded; using an empty probe")
            return ColumnScanner(lambda _x, _y, _z: "")
        return ColumnScanner(make_probe(dimension))

    def _observe(self, player: Player) -> Observation | None:
        try:
            return observe(player, active_modes=self._modes, require_sneaking=False)
        except Exception:
            return None

    def _effective_density(self, player_id: object, config: ArboristConfig) -> tuple[str, float]:
        prefs = self._prefs
        preset = (prefs.density(player_id) if prefs is not None else None) or config.default_density
        multiplier = DENSITY_SCALES[preset]
        low, high = RANGE_SPACING_SCALE
        return preset, max(low, min(high, config.spacing_scale * multiplier))

    def _tick(self) -> None:
        controller = self._controller
        prefs = self._prefs
        if controller is None or prefs is None or not controller.enabled:
            return

        self._clock_tick += self._period
        players = list(self.server.online_players)
        if not players:
            # No player can consume Arborist work, so avoid touching PAPI/Spark entirely.
            return
        if not self._tps_monitor_started:
            self._tps_monitor_started = True
        tps_status = self._update_tps_gate()
        allow_automatic = tps_status.allowed

        observations: list[Observation] = []
        enabled_players: list[Player] = []
        sow_players: list[Player] = []
        for player in players:
            if prefs.sowing_enabled(player.unique_id):
                sow_players.append(player)
            if prefs.enabled(player.unique_id):
                enabled_players.append(player)
                _preset, scale = self._effective_density(player.unique_id, controller.config)
                controller.set_spacing_scale(player.unique_id, scale)
                observation = self._observe(player)
                if observation is not None:
                    observations.append(observation)

        try:
            controller.tick(observations, self._period, allow_planning=allow_automatic)
        except Exception:
            self.logger.error("tick failed; disabling Arborist\n" + traceback.format_exc())
            controller.set_enabled(False)
            return

        worker_error = controller.pop_worker_error()
        if worker_error:
            self.logger.error("background planner failed; result discarded\n" + worker_error)

        if not allow_automatic:
            return
        for player in sow_players:
            try:
                self._auto_sow(player, controller)
            except Exception:
                self.logger.error(f"automatic sowing failed for {player.name}\n" + traceback.format_exc())
        for player in enabled_players:
            try:
                self._auto_plant(player, controller)
            except Exception:
                self.logger.error(f"automatic planting failed for {player.name}\n" + traceback.format_exc())

    def _auto_plant(self, player: Player, controller: ArboristController) -> None:
        tick = controller.current_tick
        if tick < self._next_plant_tick.get(player.unique_id, 0):
            return
        session = controller.sessions.get(player.unique_id)
        if (
            session is None
            or not session.active
            or session.planning
            or session.plan is None
            or not session.plan.candidates
        ):
            return
        profile = held_sapling(player)
        if profile is None or session.profile is None or profile.sapling != session.profile.sapling:
            return

        # Rate-limit attempts, not only successes. Protected plots or a temporarily stale
        # inventory must not be retried four times a second while the player stands still.
        self._next_plant_tick[player.unique_id] = tick + max(1, controller.config.autoplant_cooldown)
        self._commit(controller, player, session, profile)

    def _auto_sow(self, player: Player, controller: ArboristController) -> None:
        # Only Survival consumes crop items. No planting in Creative/Adventure,
        # regardless of the tree-planting game-mode setting.
        if not player.is_valid or player.game_mode.name != "SURVIVAL":
            return
        held = player.inventory.item_in_main_hand
        crop = crop_for_item(str(held.type)) if held is not None and int(held.amount) > 0 else None
        if crop is None:
            return

        tick = controller.current_tick
        if tick < self._next_sow_tick.get(player.unique_id, 0):
            return
        pos = player.location
        dimension = player.dimension
        dimension_id = dimension_key(dimension)
        x, y, z = int(pos.block_x), int(pos.block_y), int(pos.block_z)
        signature = (dimension_id, x, y, z, crop.item)
        previous = self._last_sow_scan.get(player.unique_id)
        if previous is not None and previous[0] == signature and tick - previous[1] < controller.config.refresh_interval:
            return

        self._next_sow_tick[player.unique_id] = tick + controller.config.sow_cooldown
        self._last_sow_scan[player.unique_id] = (signature, tick)
        y = sowing_level(dimension, x, y, z)
        count = sow_nearby(
            dimension,
            player.inventory,
            crop,
            x,
            y,
            z,
            radius=controller.config.sow_radius,
            limit=controller.config.sow_max_per_batch,
            can_build=lambda bx, by, bz: self._can_build(
                player, autoplant.PlaceOp(bx, by, bz, crop.block)
            ),
            on_planted=lambda bx, by, bz: controller.on_block_change(dimension_id, bx, by, bz),
        )
        if count:
            player.send_tip(f"§aArborist §r| 已自动播种 §a{count} §r格{crop.display_name}")

    def _can_build(self, player: Player, op: autoplant.PlaceOp) -> bool:
        guard = self._protection
        if guard is None:
            return True
        return guard.can_build(
            str(player.name),
            bool(player.is_op),
            dimension_key(player.dimension),
            op.x,
            op.y,
            op.z,
        )

    def _live_candidate_ops(
        self,
        dimension: object,
        candidate: Candidate,
        profile: TreeProfile,
        vertical_clearance: int,
    ) -> list[autoplant.PlaceOp] | None:
        """Cheap main-thread recheck of only cells the worker intends to mutate.

        Neighbour spacing/tree rules are generation protected: every relevant block event
        invalidates the session while the worker runs. At commit time we only need to
        prove the exact soil, planting cell, and vertical column are still safe.
        """
        ops: list[autoplant.PlaceOp] = []
        for x, y, z in candidate.cells:
            block = dimension.get_block_at(x, y, z)  # type: ignore[attr-defined]
            current = str(block.type)
            if is_sapling_block(current):
                continue
            if classify_cell(current) not in (SurfaceState.EMPTY, SurfaceState.VEGETATION):
                return None
            ground = str(dimension.get_block_at(x, y - 1, z).type)  # type: ignore[attr-defined]
            if ground not in profile.ground or ground not in SOIL:
                return None
            for offset in range(1, vertical_clearance + 1):
                above = str(dimension.get_block_at(x, y + offset, z).type)  # type: ignore[attr-defined]
                if not is_pass_through(above):
                    return None
            ops.append(autoplant.PlaceOp(x, y, z, profile.sapling))
        return ops

    @staticmethod
    def _candidate_centre(candidate: Candidate) -> tuple[float, float]:
        cells = candidate.cells
        return (
            sum(cell[0] for cell in cells) / len(cells),
            sum(cell[2] for cell in cells) / len(cells),
        )

    def _candidate_live_distance2(self, candidate: Candidate, session: PlayerSession) -> float:
        cx, cz = self._candidate_centre(candidate)
        dx = cx - session.x
        dz = cz - session.z
        return dx * dx + dz * dz

    def _candidate_in_live_window(
        self, candidate: Candidate, session: PlayerSession, radius: int
    ) -> bool:
        cx, cz = self._candidate_centre(candidate)
        return abs(cx - session.x) <= radius and abs(cz - session.z) <= radius

    def _commit(
        self,
        controller: ArboristController,
        player: Player,
        session: PlayerSession,
        profile: TreeProfile,
    ) -> int:
        plan = session.plan
        if plan is None or not plan.candidates:
            return 0
        inventory = player.inventory
        owned = _count_saplings(inventory, profile.sapling)
        if owned <= 0:
            return 0

        dimension = player.dimension
        if dimension_key(dimension) != session.dimension_key:
            session.invalidate()
            return 0

        geometry = scaled_geometry(profile, controller.options_for(session))
        remaining: list[Candidate] = []
        placed_ops: list[autoplant.PlaceOp] = []
        written_blocks: list[tuple[Block, str, autoplant.PlaceOp]] = []
        protected_removed = 0
        committed_plots = 0
        budget = owned
        stale = False
        dropped_outside = 0
        moved_from_plan = session.plan_origin is not None and session.plan_origin != session.block_pos
        candidates = list(plan.candidates)
        if moved_from_plan:
            # A rolling plan was scored around an older origin.  Keep its mutually-spaced
            # geometry, but consume the candidates nearest the live player first so walking
            # forward does not keep planting the reservoir's old centre behind them.
            candidates.sort(key=lambda c: (self._candidate_live_distance2(c, session), -c.score, c.anchor))

        for index, candidate in enumerate(candidates):
            if not self._candidate_in_live_window(candidate, session, controller.config.scan_radius):
                # This position belonged to the non-overlapping tail of an older square
                # snapshot.  Consume it rather than carrying dead weight forward.
                dropped_outside += 1
                continue
            if committed_plots >= controller.config.max_recommendations:
                remaining.extend(candidates[index:])
                break
            try:
                ops = self._live_candidate_ops(dimension, candidate, profile, geometry.vertical_clearance)
            except Exception:
                ops = None
            if ops is None:
                # An event was missed or the result raced a world mutation. Throw the
                # reservoir away and rebuild rather than trying to salvage stale scoring.
                stale = True
                break
            if not ops:
                # Plot was completed by something else; simply consume it from reservoir.
                continue
            if len(ops) > budget:
                remaining.extend(candidates[index:])
                break
            if not all(self._can_build(player, op) for op in ops):
                protected_removed += 1
                continue

            group_written: list[tuple[Block, str, autoplant.PlaceOp]] = []
            ok = True
            for op in ops:
                try:
                    block = dimension.get_block_at(op.x, op.y, op.z)
                    before = str(block.type)
                    block.set_type(op.sapling)
                    group_written.append((block, before, op))
                except Exception:
                    ok = False
                    break
            if not ok:
                for block, before, _op in group_written:
                    try:
                        block.set_type(before)
                    except Exception:
                        self.logger.debug("could not undo a partial plot")
                stale = True
                break

            budget -= len(ops)
            committed_plots += 1
            written_blocks.extend(group_written)
            placed_ops.extend(op for _block, _before, op in group_written)

        placed = len(placed_ops)
        if placed:
            paid = _consume_saplings(inventory, profile.sapling, placed)
            if paid < placed:
                self.logger.warning(
                    f"inventory desync for {player.name}: consumed {paid} of {placed} planted saplings"
                )
            # Keep the source player's reservoir alive: _spread_out already guaranteed
            # all recommendations are mutually spaced. Other nearby players must replan.
            for op in placed_ops:
                controller.on_block_change(
                    session.dimension_key,
                    op.x,
                    op.y,
                    op.z,
                    exclude_unique_id=player.unique_id,
                )

        if stale:
            session.invalidate()
        elif remaining:
            session.plan = replace(plan, candidates=tuple(remaining))
            # Preserve a movement-triggered prefetch request while the overlapping
            # reservoir remains usable.  Standing players keep the old zero-work state.
            distance = session.distance_from_plan()
            moving_prefetch = distance is not None and distance >= controller.config.plan_prefetch_distance
            session.movement_prefetch = moving_prefetch
            session.dirty = moving_prefetch
        elif placed or dropped_outside:
            # The reservoir is exhausted (or only its non-overlapping tail remained).
            # During continuous movement refill at the short prefetch cadence; while
            # standing, retain the normal refresh cadence.  This is a soft replan: the
            # player's own writes were already reflected in the scanner cache drops.
            session.plan = None
            session.plan_origin = None
            session.dirty = True
            session.movement_prefetch = moved_from_plan or dropped_outside > 0
        elif protected_removed:
            # Standing in protected land should not continuously regenerate the same plan.
            session.plan = None
            session.plan_origin = None
            session.dirty = False
            session.movement_prefetch = False
        else:
            session.plan = None
            session.plan_origin = None
            session.dirty = False
            session.movement_prefetch = False

        if placed:
            player.send_tip(f"§aArborist §r| 已自动植树 §a{placed} §r株{profile.display_name}")
        return placed

    # ------------------------------------------------------------------ cache invalidation events

    def _session_block(self, block: Block | BlockState) -> tuple[str, int, int, int] | None:
        try:
            return (dimension_key(block.dimension), int(block.x), int(block.y), int(block.z))
        except Exception:
            return None

    def _dirty_at(self, block: Block | BlockState) -> None:
        controller = self._controller
        if controller is None:
            return
        located = self._session_block(block)
        if located is not None:
            controller.on_block_change(*located)

    def _refresh_player(self, player: Player) -> None:
        controller = self._controller
        if controller is not None:
            controller.on_position_change(player.unique_id)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_join(self, event: PlayerJoinEvent) -> None:
        controller = self._controller
        if controller is not None:
            controller.on_join(event.player.unique_id, str(event.player.name))
        if not self._tps_monitor_started:
            self._tps_monitor_started = True
            self._update_tps_gate(force=True)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_quit(self, event: PlayerQuitEvent) -> None:
        controller = self._controller
        if controller is not None:
            controller.on_quit(event.player.unique_id)
        self._next_plant_tick.pop(event.player.unique_id, None)
        self._next_sow_tick.pop(event.player.unique_id, None)
        self._last_sow_scan.pop(event.player.unique_id, None)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_move(self, event: PlayerMoveEvent) -> None:
        # Horizontal movement is sampled by the scheduled controller tick and handled as
        # rolling-plan reuse/prefetch there.  Hard-invalidating on every MoveEvent was
        # exactly what made straight-line planting throw away most of a 16-candidate plan.
        # Vertical/dimension changes are still hard-invalidated by observation/dedicated
        # events before any commit can occur.
        return

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_item_held(self, event: PlayerItemHeldEvent) -> None:
        self._refresh_player(event.player)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_game_mode_change(self, event: PlayerGameModeChangeEvent) -> None:
        self._refresh_player(event.player)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_dimension_change(self, event: PlayerDimensionChangeEvent) -> None:
        self._refresh_player(event.player)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_block_place(self, event: BlockPlaceEvent) -> None:
        self._dirty_at(event.block)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_block_break(self, event: BlockBreakEvent) -> None:
        self._dirty_at(event.block)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_block_grow(self, event: BlockGrowEvent) -> None:
        self._dirty_at(event.new_state)

    @event_handler(priority=EventPriority.MONITOR)  # type: ignore[untyped-decorator]
    def on_leaves_decay(self, event: LeavesDecayEvent) -> None:
        self._dirty_at(event.block)

    # ------------------------------------------------------------------ commands

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "arborist":
            return True
        if not isinstance(sender, Player):
            if args and args[0].lower() == "reload":
                return self._admin_reload(sender)
            sender.send_error_message("玩家个人设置只能在游戏内修改；控制台仅可使用 /arb reload。")
            return True

        prefs = self._prefs
        controller = self._controller
        if prefs is None or controller is None:
            sender.send_error_message("Arborist 尚未启用。")
            return True

        sub = args[0].lower() if args else "status"
        if sub == "on":
            status = self._update_tps_gate(force=True)
            if not status.allowed:
                sender.send_error_message(self._tps_rejection(status))
                return True
            if not self._can_enable_at_player(sender):
                return True
            prefs.set_enabled(sender.unique_id, True)
            self._next_plant_tick.pop(sender.unique_id, None)
            self._refresh_player(sender)
            sender.send_message("§aArborist 自动植树已开启。§r手持受支持的树苗即可自动植树。")
            return True
        if sub == "off":
            prefs.set_enabled(sender.unique_id, False)
            self._next_plant_tick.pop(sender.unique_id, None)
            controller.on_quit(sender.unique_id)
            sender.send_message("§eArborist 自动植树已关闭。")
            return True
        if sub == "status":
            self._send_status(sender, controller)
            return True
        if sub == "density":
            return self._density_command(sender, args[1:] if len(args) > 1 else [])
        if sub == "sow":
            return self._sow_command(sender, args[1:])
        if sub == "reload":
            return self._admin_reload(sender)

        sender.send_error_message("用法：/arb <on|off|status|reload>、/arb density [compact|default|sparse|server] 或 /arb sow [on|off|status]")
        return True

    def _can_enable_at_player(self, player: Player) -> bool:
        """Reject ``/arb on`` when Territory denies building at the player's position.

        This is only an early user-facing guard. Every concrete placement is still checked
        again by :meth:`_can_build`, because a planting plan can cross a claim boundary.
        """
        guard = self._protection
        if guard is None or guard.mode == "off":
            return True
        if not guard.refresh(force=True):
            player.send_error_message("§cTerritory 保护状态异常，当前无法开启自动种植。")
            return False
        if not guard.detected:
            return guard.mode == "auto"

        location = player.location
        if guard.can_build(
            str(player.name),
            bool(player.is_op),
            dimension_key(player.dimension),
            int(location.block_x),
            int(location.block_y),
            int(location.block_z),
        ):
            return True

        player.send_error_message("§c当前领地受保护，你没有建造权限，无法开启自动播种或植树。")
        return False

    def _sow_command(self, player: Player, args: list[str]) -> bool:
        prefs = self._prefs
        if prefs is None:
            return True
        mode = args[0].lower() if args else "status"
        if mode == "status":
            player.send_message(f"Arborist 自动播种：{'§a开启' if prefs.sowing_enabled(player.unique_id) else '§e关闭'}§r。")
            return True
        if mode == "on":
            tps = self._update_tps_gate(force=True)
            if not tps.allowed:
                player.send_error_message(self._tps_rejection(tps))
                return True
            if not self._can_enable_at_player(player):
                return True
            prefs.set_sowing_enabled(player.unique_id, True)
            self._next_sow_tick.pop(player.unique_id, None)
            self._last_sow_scan.pop(player.unique_id, None)
            player.send_message("§aArborist 自动播种已开启。§r手持小麦或甜菜种子、胡萝卜、马铃薯经过空闲耕地即可播种。")
            return True
        if mode == "off":
            prefs.set_sowing_enabled(player.unique_id, False)
            self._next_sow_tick.pop(player.unique_id, None)
            self._last_sow_scan.pop(player.unique_id, None)
            player.send_message("§eArborist 自动播种已关闭。")
            return True
        player.send_error_message("用法：/arb sow [on|off|status]")
        return True

    def _density_command(self, player: Player, args: list[str]) -> bool:
        prefs = self._prefs
        controller = self._controller
        if prefs is None or controller is None:
            return True
        if not args or args[0].lower() == "status":
            explicit = prefs.density(player.unique_id)
            effective = explicit or controller.config.default_density
            source = "个人设置" if explicit else "服务器默认"
            player.send_message(f"Arborist 密度：{_density_label(effective)}（{source}）。")
            return True
        wanted = args[0].lower()
        if wanted == "server":
            prefs.set_density(player.unique_id, None)
            effective = controller.config.default_density
            _preset, scale = self._effective_density(player.unique_id, controller.config)
            controller.set_spacing_scale(player.unique_id, scale)
            player.send_message(f"Arborist 密度：已恢复跟随服务器默认（{_density_label(effective)}）。")
            return True
        if wanted not in DENSITY_PRESETS:
            player.send_error_message("用法：/arb density <compact|default|sparse|server>")
            return True
        prefs.set_density(player.unique_id, wanted)
        _preset, scale = self._effective_density(player.unique_id, controller.config)
        controller.set_spacing_scale(player.unique_id, scale)
        player.send_message(f"Arborist 密度：已设置为{_density_label(wanted)}。")
        return True

    def _admin_reload(self, sender: CommandSender) -> bool:
        if not sender.has_permission("arborist.command.admin"):
            sender.send_error_message("你没有重载 Arborist 的权限。")
            return True
        self.reload_config()
        config = self._load_config()
        old_controller = self._controller
        self._controller = ArboristController(config, self._scanner_for)
        if old_controller is not None:
            old_controller.close()
        self._modes = supported_modes(respect_creative=config.respect_creative)
        self._protection = self._make_protection(config)
        self._tps_gate = SparkTpsGate(
            self.server.service_manager, minimum=config.min_tps, interval_ticks=config.tps_check_interval
        )
        self._tps_state_key = None
        self._tps_allowed = False
        if self._tps_monitor_started:
            self._update_tps_gate(force=True)
        self._next_plant_tick.clear()
        self._next_sow_tick.clear()
        self._last_sow_scan.clear()
        sender.send_message("Arborist 配置已重载。")
        self._log_protection_state()
        return True

    def _send_status(self, player: Player, controller: ArboristController) -> None:
        prefs = self._prefs
        enabled = bool(prefs and prefs.enabled(player.unique_id))
        sowing = bool(prefs and prefs.sowing_enabled(player.unique_id))
        explicit = prefs.density(player.unique_id) if prefs else None
        effective = explicit or controller.config.default_density
        guard = self._protection
        if guard is None or guard.mode == "off":
            protection = "off"
        elif guard.detected:
            protection = "territory-ok" if guard.healthy else "territory-blocked"
        else:
            protection = "territory-not-detected" if guard.mode == "auto" else "territory-required-missing"
        tps = self._update_tps_gate(force=True)
        minimum = self._tps_minimum()
        if tps.tps is not None:
            tps_text = f"{tps.tps:.2f}（{'允许' if tps.allowed else '暂停'}，最低 {minimum:g}）"
        else:
            tps_text = f"不可用（{tps.reason}，最低 {minimum:g}）"
        player.send_message(
            f"Arborist：自动播种={'§a开启' if sowing else '§e关闭'}§r，"
            f"自动植树={'§a开启' if enabled else '§e关闭'}§r，"
            f"密度={_density_label(effective)}（{'个人设置' if explicit else '服务器默认'}），"
            f"领地保护={_protection_label(protection)}，Spark TPS={tps_text}。"
        )


_DENSITY_LABELS = {
    "compact": "紧凑",
    "default": "默认",
    "sparse": "稀疏",
}

_PROTECTION_LABELS = {
    "off": "关闭",
    "territory-ok": "Territory 已启用",
    "territory-blocked": "Territory 异常，已阻止自动种植",
    "territory-not-detected": "未检测到 Territory",
    "territory-required-missing": "缺少必需的 Territory 数据",
}

def _density_label(value: str) -> str:
    return _DENSITY_LABELS.get(value, value)


def _protection_label(value: str) -> str:
    return _PROTECTION_LABELS.get(value, value)


def _count_saplings(inventory: PlayerInventory, sapling: str) -> int:
    return sum(int(stack.amount) for stack in inventory.all(sapling).values() if stack is not None)


def _consume_saplings(inventory: PlayerInventory, sapling: str, want: int) -> int:
    remaining = max(0, int(want))
    paid = 0
    slots = sorted(
        ((index, stack) for index, stack in inventory.all(sapling).items() if stack is not None),
        key=lambda pair: -int(pair[1].amount),
    )
    for index, stack in slots:
        if remaining <= 0:
            break
        take = min(remaining, int(stack.amount))
        left = int(stack.amount) - take
        if left > 0:
            stack.amount = left
            inventory.set_item(index, stack)
        else:
            inventory.set_item(index, None)
        remaining -= take
        paid += take
    return paid


def _groups_in_order(ops: list[autoplant.PlaceOp], footprint: int) -> list[list[autoplant.PlaceOp]]:
    span = max(int(footprint) - 1, 0)
    groups: list[list[autoplant.PlaceOp]] = []
    current: list[autoplant.PlaceOp] = []
    anchor = None
    for op in ops:
        if anchor is None or abs(op.x - anchor.x) > span or abs(op.z - anchor.z) > span:
            if current:
                groups.append(current)
            current, anchor = [], op
        current.append(op)
    if current:
        groups.append(current)
    return groups
