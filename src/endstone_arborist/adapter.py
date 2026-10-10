"""Endstone-facing adapters: the only module that touches live server types.

Endstone types stay behind ``TYPE_CHECKING`` and their annotations are quoted, which keeps
the pure layers importable without the native module while still letting callers introspect
these signatures. ``from __future__ import annotations`` is deliberately *not* used here:
:mod:`endstone_arborist.plugin` explains why that matters for event handlers.

Everything here converts Endstone objects into plain Python values that the pure layers
(:mod:`~endstone_arborist.planner`, :mod:`~endstone_arborist.sessions`,
:mod:`~endstone_arborist.controller`) understand, or calls one Endstone method to produce visible
output. Keeping that boundary narrow means the plugin's logic is unit-testable with fakes and an API
change in Endstone breaks one file instead of the whole plugin.

Every member used below was checked against the installed Endstone stubs and the pybind bindings:
``Player.location``, ``Player.game_mode``, ``Player.is_sneaking``, ``Player.unique_id``,
``Player.inventory``, ``PlayerInventory.item_in_main_hand``, ``ItemStack.type``, ``ItemStack.data``,
``Dimension.name`` and ``Block.type`` are properties; ``Dimension.get_block_at``,
``Player.spawn_particle`` and ``Player.send_tip`` are methods. ``Player`` is exported from the
``endstone`` package root (there is no ``endstone.player`` module).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from endstone_arborist.blocks import LEGACY_SAPLING_INDEX
from endstone_arborist.profiles import TreeProfile, profile_for_item, profiles_by_kind

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from endstone import Player
    from endstone.block import Block
    from endstone.inventory import ItemStack
    from endstone.level import Dimension

__all__ = [
    "UNREADABLE",
    "BlockProbe",
    "Observation",
    "dimension_key",
    "held_sapling",
    "make_probe",
    "observe",
    "supported_modes",
]

#: Sentinel returned instead of raising when a block cannot be read (unloaded chunk, out of build
#: height). It is deliberately not a real identifier: unknown blocks are treated as solid, so an
#: unreadable column fails *validity* instead of being recommended. That is the conservative
#: direction for an advisory plugin, and it also means the cache never stores a misleading "air".
UNREADABLE = ""

BlockProbe = Callable[[int, int, int], str]

#: The legacy combined sapling item/block id; its ``data`` value selects the species.
LEGACY_SAPLING = "minecraft:sapling"


def dimension_key(dimension: "Dimension") -> str:
    """Return a stable cache key for *dimension*.

    ``Dimension.name`` is unique within a level (``overworld``, ``nether``, ``the_end``), so it is a
    safer key than ``id()``, which can be recycled by a different wrapper object for the same
    dimension.
    """
    return str(dimension.name)


def make_probe(dimension: "Dimension") -> BlockProbe:
    """Build a ``(x, y, z) -> block identifier`` probe backed by *dimension*.

    The probe never raises. Endstone throws for unloaded chunks, and a player walking at the edge of
    the loaded area must not be able to break the tick task; unreadable cells come back as
    :data:`UNREADABLE`.
    """

    def probe(x: int, y: int, z: int) -> str:
        try:
            block: Block = dimension.get_block_at(x, y, z)
            return str(block.type)
        except Exception:
            return UNREADABLE

    return probe


def held_sapling(player: "Player") -> TreeProfile | None:
    """Return the profile for the sapling in *player*'s main hand, if it is a supported species.

    Bedrock exposes per-species sapling items (``minecraft:oak_sapling``). The legacy combined
    ``minecraft:sapling`` item stores the species in its ``data`` value, resolved here through the
    same table used for legacy sapling blocks.
    """
    stack: ItemStack | None = player.inventory.item_in_main_hand
    if stack is None:
        return None
    item_id = str(stack.type)
    profile = profile_for_item(item_id)
    if profile is not None:
        return profile
    if item_id == LEGACY_SAPLING:
        try:
            species = LEGACY_SAPLING_INDEX.get(int(stack.data))
        except (TypeError, ValueError):
            return None
        return profiles_by_kind().get(species) if species else None
    return None


@dataclass(frozen=True)
class Observation:
    """One server-independent reading of a single player.

    Attributes:
        unique_id: The player's stable ``uuid.UUID`` identity.
        name: Display name, used only for messages and logs.
        key: Dimension cache key (see :func:`dimension_key`).
        x: Block X of the player's feet.
        y: Block Y of the player's feet.
        z: Block Z of the player's feet.
        profile: Species of the sapling being held, or ``None``.
        eligible: Whether analysis is permitted for this player right now (game mode and sneak
            requirement). ``False`` deactivates the session without deleting it.
    """

    unique_id: object
    name: str
    key: str
    x: int
    y: int
    z: int
    profile: TreeProfile | None = None
    eligible: bool = True


def supported_modes(*, respect_creative: bool) -> frozenset[str]:
    """Return the game-mode names the assistant may activate in.

    Args:
        respect_creative: When true, Creative and Spectator are excluded: those players are not
            gathering saplings, so markers would only be noise.
    """
    if respect_creative:
        return frozenset({"SURVIVAL", "ADVENTURE"})
    return frozenset({"SURVIVAL", "ADVENTURE", "CREATIVE", "SPECTATOR"})


def observe(
    player: "Player",
    *,
    active_modes: frozenset[str],
    require_sneaking: bool = False,
) -> "Observation":
    """Read *player* into an :class:`Observation`.

    Args:
        player: The Endstone player to inspect.
        active_modes: Game-mode names the assistant is allowed to run in.
        require_sneaking: When true, the assistant only activates while the player is sneaking.

    Returns:
        An observation whose :attr:`~Observation.eligible` flag is ``False`` when the player is in a
        disallowed mode or is not sneaking as required. A player who is no longer valid yields an
        observation with ``profile=None`` and ``eligible=False``, which deactivates their session
        rather than leaking it.
    """
    if not player.is_valid:
        return Observation(unique_id=player.unique_id, name=str(player.name), key="", x=0, y=0, z=0, eligible=False)
    location = player.location
    mode = str(player.game_mode.name)
    profile = held_sapling(player) if mode in active_modes else None
    if require_sneaking and not bool(player.is_sneaking):
        profile = None
    return Observation(
        unique_id=player.unique_id,
        name=str(player.name),
        key=dimension_key(player.dimension),
        x=int(location.block_x),
        y=int(location.block_y),
        z=int(location.block_z),
        profile=profile,
        eligible=profile is not None,
    )
