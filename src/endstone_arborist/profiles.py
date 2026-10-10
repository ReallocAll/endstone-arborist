"""Tree profiles: the data model describing how each supported species wants to be planted.

A :class:`TreeProfile` captures what Arborist needs to reason about one species without
sprinkling ``if kind == "spruce"`` checks through the scoring code: which item places it,
whether it needs a 2x2 plot, how much vertical and horizontal room a mature tree wants,
which ground blocks may hold it, which blocks mark an already-mature specimen, and which
particle highlights a recommendation.

Profiles are immutable data and never import Endstone, which keeps them unit-testable and
lets :mod:`endstone_arborist.planner` stay a pure function of a world snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from endstone_arborist.blocks import (
    LEAVES,
    LOGS,
    bedrock,
    legacy_sapling_kind,
)

__all__ = [
    "DIRT_GROUND",
    "PROFILES",
    "SANDY_GROUND",
    "TreeProfile",
    "profile_for_block",
    "profile_for_item",
    "profile_for_legacy_sapling",
    "profiles",
    "profiles_by_kind",
    "supported_kinds",
]

#: The dirt/grass family: what a sapling is normally planted on.
DIRT_GROUND = frozenset(
    {
        bedrock("grass"),
        bedrock("grass_block"),
        bedrock("dirt"),
        bedrock("coarse_dirt"),
        bedrock("podzol"),
        bedrock("mycelium"),
        bedrock("dirt_with_roots"),
        bedrock("moss_block"),
        bedrock("pale_moss_block"),
    }
)

#: Sand-family ground that some species tolerate in the wild.
SANDY_GROUND = frozenset({bedrock("sand"), bedrock("red_sand")})


@dataclass(frozen=True)
class TreeProfile:
    """Planting rules for one tree species.

    Attributes:
        kind: Stable species identifier, e.g. ``"dark_oak"``.
        display_name: Name used in player-facing feedback.
        sapling: ``minecraft:`` identifier shared by the sapling block and item.
        footprint: Side length of the plot in blocks. ``2`` means the recommendation
            always describes a 2x2 area.
        optional_2x2: Whether the species also grows from a single sapling but reaches
            its full size from a 2x2 plot (spruce, jungle).
        vertical_clearance: Air blocks wanted above the planted cell.
        horizontal_clearance: Radius of the ring in which obstacles are penalised.
        spacing: Preferred minimum distance between two plots of this species.
        ground: Ground blocks that are *legal*; a candidate fails hard validity when
            its soil is absent from this set.
        preferred_ground: Subset of :attr:`ground` that carries no soil penalty.
        mature_logs: Log identifiers that mean "a tree is already here".
        mature_leaves: Leaf identifiers with the same meaning.
        particle: Particle effect name used to mark recommendations for this species.
        trunk_width: Trunk width of the mature tree, used for row alignment.
    """

    kind: str
    display_name: str
    sapling: str
    footprint: int = 1
    optional_2x2: bool = False
    vertical_clearance: int = 6
    horizontal_clearance: int = 2
    spacing: int = 4
    ground: frozenset[str] = field(default_factory=lambda: DIRT_GROUND)
    preferred_ground: frozenset[str] = field(default_factory=lambda: DIRT_GROUND)
    mature_logs: frozenset[str] = field(default_factory=lambda: LOGS)
    mature_leaves: frozenset[str] = field(default_factory=lambda: LEAVES)
    particle: str = "minecraft:biome_tinted_leaves_particle"
    trunk_width: int = 1

    def __post_init__(self) -> None:
        if self.footprint not in (1, 2):
            raise ValueError(f"{self.kind}: footprint must be 1 or 2, got {self.footprint}")
        if self.spacing < self.footprint:
            raise ValueError(f"{self.kind}: spacing ({self.spacing}) must be >= footprint")
        if not self.preferred_ground <= self.ground:
            raise ValueError(f"{self.kind}: preferred_ground must be a subset of ground")
        if self.vertical_clearance < 1:
            raise ValueError(f"{self.kind}: vertical_clearance must be positive")

    @property
    def max_footprint(self) -> int:
        """Largest plot this species can productively occupy."""
        return 2 if (self.footprint == 2 or self.optional_2x2) else 1

    def is_mature_block(self, block_type: str) -> bool:
        """Whether *block_type* indicates an existing tree of this species."""
        return block_type in self.mature_logs or block_type in self.mature_leaves


def _profile(kind: str, display: str, **overrides: object) -> TreeProfile:
    """Build a profile with the shared defaults applied."""
    raw_ground = overrides.pop("ground", DIRT_GROUND)
    raw_preferred = overrides.pop("preferred_ground", DIRT_GROUND)
    assert isinstance(raw_ground, frozenset) and isinstance(raw_preferred, frozenset)
    ground_set: frozenset[str] = raw_ground
    preferred_set: frozenset[str] = raw_preferred & ground_set
    return TreeProfile(
        kind=kind,
        display_name=display,
        sapling=bedrock(f"{kind}_sapling"),
        ground=ground_set,
        preferred_ground=preferred_set,
        **overrides,  # type: ignore[arg-type]
    )


#: Every species Arborist supports, in declaration order.
#:
#: ``vertical_clearance`` and ``spacing`` are derived from the vanilla tree features in
#: ``definitions/features/*_tree_feature.json`` of the supported BDS (``trunk_height``
#: range plus the canopy band above it), so a recommended spot is tall and open enough
#: for the tree that will actually grow there. ``vertical_clearance`` stays at or below
#: ``world.ColumnScanner`` clearance depth (12): cells above that window are unreadable,
#: and an unreachable requirement would only make the check silently permissive. Cherry
#: and pale oak have no feature file in this BDS version, so their values follow the
#: vanilla client dimensions rather than a measured feature.
PROFILES: tuple[TreeProfile, ...] = (
    _profile("oak", "橡树", vertical_clearance=7, spacing=5),
    _profile("birch", "白桦", vertical_clearance=8, spacing=4, horizontal_clearance=2),
    _profile("spruce", "云杉", vertical_clearance=11, spacing=6, optional_2x2=True, trunk_width=2),
    _profile(
        "jungle",
        "丛林树",
        vertical_clearance=11,
        spacing=6,
        horizontal_clearance=3,
        optional_2x2=True,
        trunk_width=4,
        ground=DIRT_GROUND | SANDY_GROUND,
    ),
    _profile(
        "acacia",
        "金合欢",
        vertical_clearance=7,
        spacing=5,
        horizontal_clearance=3,
        ground=DIRT_GROUND | SANDY_GROUND,
    ),
    _profile(
        "dark_oak",
        "深色橡树",
        footprint=2,
        vertical_clearance=10,
        spacing=7,
        horizontal_clearance=3,
        trunk_width=2,
    ),
    _profile(
        "cherry",
        "樱花树",
        vertical_clearance=7,
        spacing=6,
        particle="minecraft:cherry_leaves_particle",
    ),
    _profile(
        "pale_oak",
        "苍白橡树",
        vertical_clearance=7,
        spacing=5,
        particle="minecraft:glow_particle",
    ),
)

_BY_SAPLING: dict[str, TreeProfile] = {profile.sapling: profile for profile in PROFILES}
_BY_KIND: dict[str, TreeProfile] = {profile.kind: profile for profile in PROFILES}


def profiles() -> tuple[TreeProfile, ...]:
    """Return every registered profile in declaration order."""
    return PROFILES


def profiles_by_kind() -> dict[str, TreeProfile]:
    """Return a mapping of ``kind`` to profile."""
    return dict(_BY_KIND)


def supported_kinds() -> tuple[str, ...]:
    """Return the ``kind`` of every supported species."""
    return tuple(profile.kind for profile in PROFILES)


def profile_for_item(item_type: str) -> TreeProfile | None:
    """Return the profile for a held *item* identifier, or ``None`` if unsupported.

    Bedrock names sapling items the same as their block, so the lookup is shared with
    :func:`profile_for_block`.
    """
    return _BY_SAPLING.get(item_type)


def profile_for_block(block_type: str) -> TreeProfile | None:
    """Return the profile for a placed sapling *block*, or ``None`` if unsupported."""
    return _BY_SAPLING.get(block_type)


def profile_for_legacy_sapling(block_states: dict[str, bool | str | int]) -> TreeProfile | None:
    """Resolve a pre-1.20 ``minecraft:sapling`` block to a profile via ``sapling_type``."""
    kind = legacy_sapling_kind(block_states)
    return _BY_KIND.get(kind) if kind else None
