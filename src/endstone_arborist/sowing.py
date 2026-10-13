"""Small, main-thread crop planter for vacant Bedrock farmland.

Unlike tree planting, crops require no scoring or background planner. Only four basic
farmland crops are supported, and every write is protected and paid for beforehand.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

__all__ = ["Crop", "crop_for_item", "sow_nearby"]


@dataclass(frozen=True)
class Crop:
    item: str
    block: str
    display_name: str


_CROPS = {
    crop.item: crop
    for crop in (
        Crop("minecraft:wheat_seeds", "minecraft:wheat", "小麦"),
        Crop("minecraft:carrot", "minecraft:carrots", "胡萝卜"),
        Crop("minecraft:potato", "minecraft:potatoes", "马铃薯"),
        Crop("minecraft:beetroot_seeds", "minecraft:beetroot", "甜菜"),
    )
}


def crop_for_item(item: str) -> Crop | None:
    return _CROPS.get(item)


@lru_cache(maxsize=6)
def _offsets(radius: int) -> tuple[tuple[int, int], ...]:
    return tuple(
        sorted(
            ((dx, dz) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)),
            key=lambda pair: (pair[0] * pair[0] + pair[1] * pair[1], pair[0], pair[1]),
        )
    )


def sow_nearby(
    dimension: object,
    inventory: object,
    crop: Crop,
    x: int,
    y: int,
    z: int,
    *,
    radius: int,
    limit: int,
    can_build: Callable[[int, int, int], bool],
    on_planted: Callable[[int, int, int], None],
) -> int:
    """Plant up to *limit* empty farmland cells near the player's feet.

    The caller must run this on the BDS main thread, after opt-in, TPS and
    game-mode checks. Unknown/unloaded blocks are never treated as farmland.
    Item debit precedes the block write; failed writes restore the item only
    after confirming that the target is empty again.
    """
    stacks = [
        [slot, stack, int(stack.amount)]
        for slot, stack in inventory.all(crop.item).items()
        if stack is not None and int(stack.amount) > 0
    ]
    if not stacks:
        return 0

    planted = 0
    stack_index = 0
    for dx, dz in _offsets(radius):
        if planted >= limit:
            break
        while stack_index < len(stacks) and stacks[stack_index][2] <= 0:
            stack_index += 1
        if stack_index >= len(stacks):
            break

        px, pz = x + dx, z + dz
        # Restrict to the player's feet level: no digging, vertical searching,
        # trampling existing crops, or overwriting other blocks.
        if str(dimension.get_block_at(px, y - 1, pz).type) != "minecraft:farmland":
            continue
        target = dimension.get_block_at(px, y, pz)
        if str(target.type) != "minecraft:air":
            continue
        if not can_build(px, y, pz):
            continue

        slot, stack, count = stacks[stack_index]
        slot = int(slot)
        # Debit the exact source stack before editing the world. In particular,
        # a failed inventory write cannot create a free crop.
        if count > 1:
            stack.amount = count - 1
        try:
            inventory.set_item(slot, stack if count > 1 else None)
        except Exception:
            stack.amount = count
            raise

        actual = inventory.get_item(slot)
        if (count == 1 and actual is not None) or (
            count > 1
            and (actual is None or str(actual.type) != crop.item or int(actual.amount) != count - 1)
        ):
            stack.amount = count
            inventory.set_item(slot, stack)
            raise RuntimeError("crop seed debit did not persist")

        try:
            target.set_type(crop.block)
            if str(target.type) != crop.block:
                raise RuntimeError("crop block write did not persist")
        except Exception:
            # Never refund a seed while its crop may still exist in the world.
            if str(target.type) != "minecraft:air":
                target.set_type("minecraft:air")
                if str(target.type) != "minecraft:air":
                    raise RuntimeError("crop rollback failed; keeping charged seed")
            stack.amount = count
            inventory.set_item(slot, stack)
            raise

        stacks[stack_index][2] = count - 1
        planted += 1
        on_planted(px, y, pz)

    return planted
