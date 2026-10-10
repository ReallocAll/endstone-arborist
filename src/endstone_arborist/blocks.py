"""Bedrock block identifier classification used by the planting analysis.

Every identifier set in this module is namespaced (``minecraft:...``), matching
the strings returned by ``endstone.block.Block.type``. Classifications are
deliberately conservative: an unknown block is never treated as soil, and never
treated as pass-through vegetation, so unexpected blocks show up as obstacles
rather than as silently recommended planting spots.
"""

from __future__ import annotations

from enum import Enum

NAMESPACE = "minecraft:"


def bedrock(name: str) -> str:
    """Return ``name`` as a namespaced Bedrock block identifier."""
    return name if ":" in name else f"{NAMESPACE}{name}"


class SurfaceState(Enum):
    """What occupies the cell a sapling would be planted into.

    ``OBSTRUCTED`` is unreachable for cells directly above a column's soil
    block: the scanner defines soil as the highest non-pass-through block, so
    the cell above it is always air or pass-through. It exists so the enum can
    describe arbitrary cells (used when validating footprint overlaps).
    """

    EMPTY = "empty"
    VEGETATION = "vegetation"
    SAPLING = "sapling"
    CANOPY = "canopy"
    OBSTRUCTED = "obstructed"


AIR = bedrock("air")

#: Blocks that never count as a terrain surface and never block a sapling cell.
#: These are descended through while looking for soil, and count as clearance.
PASS_THROUGH: frozenset[str] = frozenset(
    {
        AIR,
        bedrock("short_grass"),
        bedrock("tallgrass"),
        bedrock("fern"),
        bedrock("double_plant"),
        bedrock("pitcher_plant"),
        bedrock("deadbush"),
        bedrock("yellow_flower"),
        bedrock("red_flower"),
        bedrock("blue_orchid"),
        bedrock("allium"),
        bedrock("azure_bluet"),
        bedrock("red_tulip"),
        bedrock("orange_tulip"),
        bedrock("white_tulip"),
        bedrock("pink_tulip"),
        bedrock("oxeye_daisy"),
        bedrock("cornflower"),
        bedrock("lily_of_the_valley"),
        bedrock("wildflowers"),
        bedrock("pink_petals"),
        bedrock("torchflower"),
        bedrock("torchflower_crop"),
        bedrock("pitcher_crop"),
        bedrock("sapling"),
        bedrock("bamboo_sapling"),
        bedrock("bamboo_leaves"),
        bedrock("seagrass"),
        bedrock("kelp"),
        bedrock("crimson_roots"),
        bedrock("warped_roots"),
        bedrock("crimson_fungus"),
        bedrock("warped_fungus"),
        bedrock("brown_mushroom"),
        bedrock("red_mushroom"),
        bedrock("waterlily"),
        bedrock("snow_layer"),
        bedrock("big_dripleaf"),
        bedrock("small_dripleaf_block"),
        bedrock("hanging_roots"),
        bedrock("mangrove_propagule"),
        bedrock("spore_blossom"),
        bedrock("sea_pickle"),
    }
)

#: Blocks a sapling may be planted through: vanilla replaces these with the
#: sapling, so they do not have to be cleared before recommending the cell.
REPLACABLE: frozenset[str] = frozenset(
    {
        bedrock("short_grass"),
        bedrock("tallgrass"),
        bedrock("fern"),
        bedrock("double_plant"),
        bedrock("yellow_flower"),
        bedrock("red_flower"),
        bedrock("blue_orchid"),
        bedrock("allium"),
        bedrock("azure_bluet"),
        bedrock("red_tulip"),
        bedrock("orange_tulip"),
        bedrock("white_tulip"),
        bedrock("pink_tulip"),
        bedrock("oxeye_daisy"),
        bedrock("cornflower"),
        bedrock("lily_of_the_valley"),
        bedrock("wildflowers"),
        bedrock("pink_petals"),
        bedrock("torchflower"),
        bedrock("deadbush"),
        bedrock("snow_layer"),
    }
)

#: Leaves of tree species, used to detect mature trees / canopies.
LEAVES: frozenset[str] = frozenset(
    {
        bedrock("leaves"),
        bedrock("leaves2"),
        bedrock("oak_leaves"),
        bedrock("birch_leaves"),
        bedrock("spruce_leaves"),
        bedrock("jungle_leaves"),
        bedrock("acacia_leaves"),
        bedrock("dark_oak_leaves"),
        bedrock("mangrove_leaves"),
        bedrock("cherry_leaves"),
        bedrock("pale_oak_leaves"),
        bedrock("azalea_leaves"),
        bedrock("azalea_leaves_flowered"),
        bedrock("crimson_leaves"),
        bedrock("warped_leaves"),
    }
)

#: Log/wood/stem blocks that indicate an existing tree structure.
LOGS: frozenset[str] = frozenset(
    {
        bedrock("log"),
        bedrock("log2"),
        bedrock("oak_log"),
        bedrock("birch_log"),
        bedrock("spruce_log"),
        bedrock("jungle_log"),
        bedrock("acacia_log"),
        bedrock("dark_oak_log"),
        bedrock("mangrove_log"),
        bedrock("cherry_log"),
        bedrock("pale_oak_log"),
        bedrock("crimson_stem"),
        bedrock("warped_stem"),
        bedrock("stripped_oak_log"),
        bedrock("stripped_birch_log"),
        bedrock("stripped_spruce_log"),
        bedrock("stripped_jungle_log"),
        bedrock("stripped_acacia_log"),
        bedrock("stripped_dark_oak_log"),
        bedrock("stripped_mangrove_log"),
        bedrock("stripped_cherry_log"),
        bedrock("stripped_pale_oak_log"),
        bedrock("oak_wood"),
        bedrock("birch_wood"),
        bedrock("spruce_wood"),
        bedrock("jungle_wood"),
        bedrock("acacia_wood"),
        bedrock("dark_oak_wood"),
        bedrock("mangrove_wood"),
        bedrock("cherry_wood"),
        bedrock("pale_oak_wood"),
        bedrock("crimson_hyphae"),
        bedrock("warped_hyphae"),
    }
)

#: Blocks that count as tree structure when measuring canopy proximity.
TREE_STRUCTURE: frozenset[str] = LEAVES | LOGS

#: Ground blocks a sapling may legally be planted on, keyed by vanilla rules.
#: Kept as one set here; per-profile allow lists narrow it further.
SOIL: frozenset[str] = frozenset(
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
        bedrock("farmland"),
        bedrock("sand"),
        bedrock("red_sand"),
        bedrock("clay"),
        bedrock("mud"),
        bedrock("muddy_mangrove_roots"),
        bedrock("soul_sand"),
        bedrock("soul_soil"),
        bedrock("crimson_nylium"),
        bedrock("warped_nylium"),
    }
)

#: Soil that is a working surface rather than a natural planting bed. Planting
#: is legal but usually unintended, so it is scored down rather than rejected.
CONDITIONAL_SOIL: frozenset[str] = frozenset(
    {
        bedrock("farmland"),
        bedrock("clay"),
        bedrock("mud"),
        bedrock("sand"),
        bedrock("red_sand"),
    }
)

#: Liquids and blocks whose presence under a sapling makes a recommendation
#: untrustworthy (trees do not grow in them; the cell is also hard to reach).
LIQUIDS: frozenset[str] = frozenset(
    {
        bedrock("water"),
        bedrock("flowing_water"),
        bedrock("lava"),
        bedrock("flowing_lava"),
    }
)

#: Legacy (pre-1.20) combined sapling block plus the modern per-species blocks.
#: ``minecraft:sapling`` carries a ``sapling_type`` state, so legacy stacks are
#: resolved with :func:`legacy_sapling_kind`.
SAPLING_BLOCKS: frozenset[str] = frozenset(
    {
        bedrock("sapling"),
        bedrock("oak_sapling"),
        bedrock("birch_sapling"),
        bedrock("spruce_sapling"),
        bedrock("jungle_sapling"),
        bedrock("acacia_sapling"),
        bedrock("dark_oak_sapling"),
        bedrock("cherry_sapling"),
        bedrock("pale_oak_sapling"),
    }
)

#: Not a tree: bamboo shoots must never be treated as saplings.
NON_TREE_SAPLINGS: frozenset[str] = frozenset({bedrock("bamboo_sapling")})

#: Sapling item identifiers that are not tree saplings.
NON_TREE_SAPLING_ITEMS: frozenset[str] = NON_TREE_SAPLINGS


def is_air(block_type: str) -> bool:
    """Whether ``block_type`` leaves a cell completely free."""
    return block_type == AIR


def is_pass_through(block_type: str) -> bool:
    """Whether ``block_type`` can be descended through when finding soil."""
    return block_type in PASS_THROUGH


def is_soil(block_type: str) -> bool:
    """Whether ``block_type`` is a ground block that can support a sapling."""
    return block_type in SOIL


def is_sapling_block(block_type: str) -> bool:
    """Whether ``block_type`` is a tree sapling block (excluding bamboo)."""
    return block_type in SAPLING_BLOCKS


def is_tree_structure(block_type: str) -> bool:
    """Whether ``block_type`` indicates an existing tree or canopy."""
    return block_type in TREE_STRUCTURE


def classify_cell(block_type: str) -> SurfaceState:
    """Classify the occupancy of a cell that a sapling might be placed into."""
    if block_type == AIR:
        return SurfaceState.EMPTY
    if block_type in SAPLING_BLOCKS:
        return SurfaceState.SAPLING
    if block_type in LEAVES:
        return SurfaceState.CANOPY
    if block_type in REPLACABLE:
        return SurfaceState.VEGETATION
    if block_type in PASS_THROUGH:
        return SurfaceState.VEGETATION
    return SurfaceState.OBSTRUCTED


def legacy_sapling_kind(block_states: dict[str, bool | str | int]) -> str | None:
    """Map the legacy ``minecraft:sapling`` ``sapling_type`` state to a species.

    Args:
        block_states: The ``BlockData.block_states`` mapping of a legacy sapling.

    Returns:
        The modern species name (for example ``"oak"``), or ``None`` when the
        state is missing or uses an index this plugin does not recognise.
    """
    raw = block_states.get("sapling_type")
    if isinstance(raw, str):
        return raw if raw in LEGACY_SAPLING_NAMES else None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return LEGACY_SAPLING_INDEX.get(raw)
    return None


#: Vanilla order of ``sapling_type`` on the legacy combined sapling block.
LEGACY_SAPLING_INDEX: dict[int, str] = {
    0: "oak",
    1: "spruce",
    2: "birch",
    3: "jungle",
    4: "acacia",
    5: "dark_oak",
}

LEGACY_SAPLING_NAMES: frozenset[str] = frozenset(LEGACY_SAPLING_INDEX.values())
