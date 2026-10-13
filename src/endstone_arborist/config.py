"""Plugin configuration: validation, clamping, and repair reporting.

Endstone hands plugins a loosely typed ``dict`` from ``config.toml``. This module turns that
mapping into a frozen, typed :class:`ArboristConfig` and never trusts the raw values: every
number is bounded, and a bounded failure falls back to the documented default instead of
raising, because a typo in a server config must not stop a plugin from loading.

Everything the plugin rejects is collected in :attr:`ArboristConfig.warnings` so the operator
can see it in the log rather than wonder why a setting "did nothing".

Keys use hyphens in the file (Bedrock/Endstone convention) and underscores in Python.
"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Final

__all__ = [
    "DEFAULTS",
    "DEFAULT_PENALTIES",
    "RANGE_MAX_RECOMMENDATIONS",
    "RANGE_RADIUS",
    "RANGE_SCORE",
    "RANGE_SPACING_SCALE",
    "RANGE_TICKS",
    "RANGE_TRUNK_DISTANCE",
    "DENSITY_SCALES",
    "ArboristConfig",
]

#: Penalty weights, each a fraction of the planner's base score (0..1).
DENSITY_SCALES: Final[dict[str, float]] = {"compact": 0.8, "default": 1.0, "sparse": 1.3}

DEFAULT_PENALTIES: Final[dict[str, float]] = {
    "obstacle": 0.12,
    "sapling-spacing": 0.35,
    "mature-tree": 0.45,
    "density": 0.08,
    "conditional-soil": 0.20,
    "alignment-bonus": 0.10,
}

#: Every supported key with its default. This is also the shape of ``config.toml``.
DEFAULTS: Final[dict[str, object]] = {
    "enabled": True,
    "scan-radius": 8,
    "max-recommendations": 3,
    "plan-capacity": 16,
    "plan-prefetch-distance": 3,
    "plan-reuse-distance": 5,
    "movement-prefetch-interval": 5,
    "refresh-interval": 20,
    "particle-interval": 10,
    "show-tip": True,
    "tip-interval": 60,
    "min-score": 0.35,
    "spacing-scale": 1.0,
    "default-density": "default",
    "territory-protection": "auto",
    "respect-creative": True,
    "require-sneaking": False,
    "autoplant-cooldown": 5,
    "sow-radius": 4,
    "sow-max-per-batch": 8,
    "sow-cooldown": 10,
    "min-tps": 19.0,
    "tps-check-interval": 20,
    "min-trunk-distance": 3,
    "penalties": DEFAULT_PENALTIES,
}

#: Bounds chosen so a typo cannot make the plugin scan the world or spam a client.
RANGE_RADIUS: Final[tuple[int, int]] = (2, 12)
RANGE_TICKS: Final[tuple[int, int]] = (1, 200)
RANGE_SCORE: Final[tuple[float, float]] = (0.0, 1.0)
RANGE_MAX_RECOMMENDATIONS: Final[tuple[int, int]] = (1, 8)
RANGE_PLAN_CAPACITY: Final[tuple[int, int]] = (3, 32)
RANGE_PLAN_DISTANCE: Final[tuple[int, int]] = (1, 12)
RANGE_TPS: Final[tuple[float, float]] = (1.0, 20.0)
#: Multiplier applied by the planner to species spacing and clearances. Kept within
#: bounds so a typo cannot ask for trees sharing a plot or a forest with no gaps.
RANGE_SPACING_SCALE: Final[tuple[float, float]] = (0.5, 2.0)
#: Ring, in whole blocks, inside which a mature tree trunk makes a plot invalid. Lower
#: bound 0 is the documented way to turn the gate off; the upper bound keeps a typo from
#: excluding every position a scan-radius-8 preview can reach.
RANGE_TRUNK_DISTANCE: Final[tuple[int, int]] = (0, 8)


def _as_bool(value: object, default: bool, warnings: list[str], key: str) -> bool:
    """Read a boolean, accepting the ``"true"``/``"false"`` strings TOML editors produce."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on"}:
            return True
        if lowered in {"false", "no", "off"}:
            return False
    warnings.append(f"{key}={value!r} is not a boolean, using {default}")
    return default


def _as_choice(value: object, default: str, choices: frozenset[str], warnings: list[str], key: str) -> str:
    if isinstance(value, str):
        selected = value.strip().lower()
        if selected in choices:
            return selected
    warnings.append(f"{key}={value!r} is invalid, using {default}")
    return default


def _coerce_int(value: object) -> int | None:
    """Convert *value* to an int, or ``None`` when it is not an integer-like number.

    ``bool`` is rejected deliberately: True is not a tick count. Floats must be finite.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _as_int(value: object, default: int, bounds: tuple[int, int], warnings: list[str], key: str) -> int:
    number = _coerce_int(value)
    if number is None:
        warnings.append(f"{key}={value!r} is not an integer, using {default}")
        return default
    low, high = bounds
    clamped = max(low, min(high, number))
    if clamped != number:
        warnings.append(f"{key}={number} clamped to {clamped} (allowed {low}..{high})")
    return clamped


def _as_float(value: object, default: float, bounds: tuple[float, float], warnings: list[str], key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        warnings.append(f"{key}={value!r} is not a number, using {default}")
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        warnings.append(f"{key}={value!r} is not a number, using {default}")
        return default
    if not isfinite(number):
        # NaN/inf are not "out of range": clamping them would silently pick an extreme.
        warnings.append(f"{key}={number!r} is not finite, using {default}")
        return default
    low, high = bounds
    clamped = max(low, min(high, number))
    if clamped != number:
        warnings.append(f"{key}={number} clamped to {clamped} (allowed {low}..{high})")
    return clamped


@dataclass(frozen=True)
class ArboristConfig:
    """Validated plugin settings.

    Attributes:
        enabled: Master switch; also the initial value of the runtime toggle.
        scan_radius: Horizontal radius sampled around each player, in blocks.
        max_recommendations: How many positions to highlight at once.
        refresh_interval: Minimum ticks between scans for one player.
        particle_interval: Minimum ticks between particle redraws for one player.
        show_tip: Whether to accompany particles with a tip message.
        tip_interval: Minimum ticks between tip messages for one player.
        min_score: Normalised (0..1) quality floor for a recommendation.
        plan_prefetch_distance: Blocks from the current plan centre at which a rolling
            background replacement is requested while keeping the old reservoir.
        plan_reuse_distance: Maximum horizontal grid distance at which a cached plan
            may still be consumed while its replacement is prepared.
        movement_prefetch_interval: Minimum ticks between movement-triggered snapshot
            captures; normally shorter than the ordinary refresh interval.
        spacing_scale: Multiplier (0.5..2.0) applied to each species' spacing and
            clearances when planning, so density can be tuned without editing profiles.
            ``1.0`` reproduces the vanilla-derived profile values exactly.
        default_density: Density preset inherited by players without an override.
        territory_protection: Territory compatibility mode: auto, required, or off.
        respect_creative: Skip Creative and Spectator players.
        require_sneaking: Only activate while the player is sneaking.
        autoplant_cooldown: Minimum ticks between two Auto-Plant commits by one
            player, so holding the attack button cannot chain plantings.
        min_trunk_distance: Ring of blocks inside which a neighbouring mature tree trunk
            rejects a plot (0..8). ``0`` disables the gate; the species spacing gate
            (:data:`endstone_arborist.planner.REASON_TOO_CLOSE`) is separate and always on.
        obstacle: Weight of the trunk-height obstacle penalty.
        sapling_spacing: Weight of the near-sapling crowding penalty.
        mature_tree: Weight of the mature-tree proximity penalty.
        density: Weight of the overall planting-density penalty.
        conditional_soil: Weight of the "legal but not preferred soil" penalty.
        alignment_bonus: Weight of the row-alignment bonus.
        warnings: Repairs applied while parsing, empty for a well-formed file.
    """

    enabled: bool = True
    scan_radius: int = 8
    max_recommendations: int = 3
    plan_capacity: int = 16
    plan_prefetch_distance: int = 3
    plan_reuse_distance: int = 5
    movement_prefetch_interval: int = 5
    refresh_interval: int = 20
    particle_interval: int = 10
    show_tip: bool = True
    tip_interval: int = 60
    min_score: float = 0.35
    spacing_scale: float = 1.0
    default_density: str = "default"
    territory_protection: str = "auto"
    respect_creative: bool = True
    require_sneaking: bool = False
    autoplant_cooldown: int = 5
    sow_radius: int = 4
    sow_max_per_batch: int = 8
    sow_cooldown: int = 10
    min_tps: float = 19.0
    tps_check_interval: int = 20
    min_trunk_distance: int = 3
    obstacle: float = DEFAULT_PENALTIES["obstacle"]
    sapling_spacing: float = DEFAULT_PENALTIES["sapling-spacing"]
    mature_tree: float = DEFAULT_PENALTIES["mature-tree"]
    density: float = DEFAULT_PENALTIES["density"]
    conditional_soil: float = DEFAULT_PENALTIES["conditional-soil"]
    alignment_bonus: float = DEFAULT_PENALTIES["alignment-bonus"]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, data: dict[str, object] | None) -> "ArboristConfig":
        """Build a config from a loosely typed mapping, repairing what it can.

        Args:
            data: The mapping from ``Plugin.config``; ``None`` yields defaults.

        Returns:
            A validated :class:`ArboristConfig` with any repairs recorded.
        """
        source = dict(data or {})
        notes: list[str] = []
        penalties = dict(DEFAULT_PENALTIES)
        raw_penalties = source.pop("penalties", {})
        if isinstance(raw_penalties, dict):
            for key, value in raw_penalties.items():
                if key not in penalties:
                    notes.append(f"penalties.{key} is unknown and was ignored")
                    continue
                penalties[key] = _as_float(value, penalties[key], RANGE_SCORE, notes, f"penalties.{key}")
        else:
            notes.append("penalties is not a table, using defaults")

        for key in sorted(set(source) - set(DEFAULTS)):
            notes.append(f"{key} is unknown and was ignored")

        def get_int(name: str, default: int, bounds: tuple[int, int]) -> int:
            return _as_int(source.get(name, default), default, bounds, notes, name)

        scan_radius = get_int("scan-radius", 8, RANGE_RADIUS)
        distance_bounds = (1, scan_radius)
        prefetch_distance = get_int("plan-prefetch-distance", min(3, scan_radius), distance_bounds)
        reuse_distance = get_int("plan-reuse-distance", min(5, scan_radius), distance_bounds)
        if reuse_distance < prefetch_distance:
            notes.append(
                f"plan-reuse-distance={reuse_distance} is smaller than plan-prefetch-distance={prefetch_distance}; "
                f"using {prefetch_distance}"
            )
            reuse_distance = prefetch_distance

        return cls(
            enabled=_as_bool(source.get("enabled", True), True, notes, "enabled"),
            scan_radius=scan_radius,
            max_recommendations=get_int("max-recommendations", 3, RANGE_MAX_RECOMMENDATIONS),
            plan_capacity=get_int("plan-capacity", 16, RANGE_PLAN_CAPACITY),
            plan_prefetch_distance=prefetch_distance,
            plan_reuse_distance=reuse_distance,
            movement_prefetch_interval=get_int("movement-prefetch-interval", 5, RANGE_TICKS),
            refresh_interval=get_int("refresh-interval", 20, RANGE_TICKS),
            particle_interval=get_int("particle-interval", 10, RANGE_TICKS),
            show_tip=_as_bool(source.get("show-tip", True), True, notes, "show-tip"),
            tip_interval=get_int("tip-interval", 60, RANGE_TICKS),
            min_score=_as_float(
                source.get("min-score", DEFAULTS["min-score"]),
                float(DEFAULT_PENALTIES.get("min-score", 0.35) if False else 0.35),
                RANGE_SCORE,
                notes,
                "min-score",
            ),
            spacing_scale=_as_float(
                source.get("spacing-scale", DEFAULTS["spacing-scale"]),
                1.0,
                RANGE_SPACING_SCALE,
                notes,
                "spacing-scale",
            ),
            default_density=_as_choice(
                source.get("default-density", DEFAULTS["default-density"]),
                "default",
                frozenset(DENSITY_SCALES),
                notes,
                "default-density",
            ),
            territory_protection=_as_choice(
                source.get("territory-protection", DEFAULTS["territory-protection"]),
                "auto",
                frozenset({"auto", "required", "off"}),
                notes,
                "territory-protection",
            ),
            respect_creative=_as_bool(source.get("respect-creative", True), True, notes, "respect-creative"),
            require_sneaking=_as_bool(source.get("require-sneaking", False), False, notes, "require-sneaking"),
            autoplant_cooldown=_as_int(
                source.get("autoplant-cooldown", DEFAULTS["autoplant-cooldown"]),
                5,
                RANGE_TICKS,
                notes,
                "autoplant-cooldown",
            ),
            sow_radius=get_int("sow-radius", 4, (1, 6)),
            sow_max_per_batch=get_int("sow-max-per-batch", 8, (1, 16)),
            sow_cooldown=get_int("sow-cooldown", 10, RANGE_TICKS),
            min_tps=_as_float(
                source.get("min-tps", DEFAULTS["min-tps"]),
                19.0,
                RANGE_TPS,
                notes,
                "min-tps",
            ),
            tps_check_interval=get_int("tps-check-interval", 20, RANGE_TICKS),
            min_trunk_distance=_as_int(
                source.get("min-trunk-distance", DEFAULTS["min-trunk-distance"]),
                3,
                RANGE_TRUNK_DISTANCE,
                notes,
                "min-trunk-distance",
            ),
            obstacle=penalties["obstacle"],
            sapling_spacing=penalties["sapling-spacing"],
            mature_tree=penalties["mature-tree"],
            density=penalties["density"],
            conditional_soil=penalties["conditional-soil"],
            alignment_bonus=penalties["alignment-bonus"],
            warnings=tuple(notes),
        )
