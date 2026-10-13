"""Sowing stays pure Python so safety checks run without a BDS instance."""

import pytest

from endstone_arborist.sowing import crop_for_item, sow_nearby


class Stack:
    def __init__(self, name="minecraft:wheat_seeds", amount=5):
        self.type = name
        self.amount = amount


class Inventory:
    def __init__(self, stack=None):
        self.slot = stack

    def all(self, item):
        if self.slot is None or self.slot.type != item:
            return {}
        return {0: self.slot}

    def get_item(self, index):
        assert index == 0
        return self.slot

    def set_item(self, index, item):
        assert index == 0
        self.slot = item


class Block:
    def __init__(self, block_type, *, on_set=None, fail_after=False):
        self.type = block_type
        self.on_set = on_set
        self.fail_after = fail_after

    def set_type(self, block_type):
        if self.on_set:
            self.on_set(block_type)
        self.type = block_type
        if self.fail_after and block_type != "minecraft:air":
            raise OSError("simulated partially applied block write")


class Dimension:
    def __init__(self):
        self.blocks = {}
        self.reads = 0

    def get_block_at(self, x, y, z):
        self.reads += 1
        return self.blocks.setdefault((x, y, z), Block("minecraft:air"))

    def farmland(self, x, z, *, y=64, above="minecraft:air"):
        self.blocks[(x, y, z)] = Block("minecraft:farmland")
        self.blocks[(x, y + 1, z)] = Block(above)


def run_sow(dimension, inventory, *, can_build=lambda x, y, z: True, limit=8, radius=2, callback=None):
    return sow_nearby(
        dimension,
        inventory,
        crop_for_item("minecraft:wheat_seeds"),
        0,
        65,
        0,
        radius=radius,
        limit=limit,
        can_build=can_build,
        on_planted=callback or (lambda x, y, z: None),
    )


def test_vanilla_crop_identifiers_and_unsupported_items():
    assert {
        "minecraft:wheat_seeds": "minecraft:wheat",
        "minecraft:carrot": "minecraft:carrots",
        "minecraft:potato": "minecraft:potatoes",
        "minecraft:beetroot_seeds": "minecraft:beetroot",
    } == {
        item: crop_for_item(item).block
        for item in ("minecraft:wheat_seeds", "minecraft:carrot", "minecraft:potato", "minecraft:beetroot_seeds")
    }
    assert crop_for_item("minecraft:melon_seeds") is None
    assert crop_for_item("minecraft:oak_sapling") is None


def test_places_only_on_empty_farmland_with_permission_and_limits():
    world = Dimension()
    for x, z in [(0, 0), (1, 0), (0, 1), (2, 0), (0, 2)]:
        world.farmland(x, z)
    world.farmland(-1, 0, above="minecraft:wheat")  # Already planted
    world.blocks[(0, 65, -1)] = Block("minecraft:air")  # No farmland below
    seen = []
    items = Inventory(Stack(amount=5))
    count = run_sow(
        world,
        items,
        limit=2,
        can_build=lambda x, y, z: (x, z) != (0, 0),
        callback=lambda x, y, z: seen.append((x, y, z)),
    )
    assert count == 2
    assert len(seen) == 2 and len(set(seen)) == 2
    assert (0, 65, 0) not in seen and (-1, 65, 0) not in seen
    assert items.slot.amount == 3
    assert all(world.blocks[coord].type == "minecraft:wheat" for coord in seen)
    assert world.blocks[(0, 65, 0)].type == "minecraft:air"
    assert world.blocks[(-1, 65, 0)].type == "minecraft:wheat"


def test_no_seeds_means_zero_world_reads():
    world = Dimension()
    assert run_sow(world, Inventory(), limit=3) == 0
    assert world.reads == 0


def test_exhausted_inventory_stops_and_exactly_debits():
    world = Dimension()
    for x in range(-2, 3):
        world.farmland(x, 0)
    inventory = Inventory(Stack(amount=2))
    assert run_sow(world, inventory, limit=8) == 2
    assert inventory.slot is None
    assert sum(block.type == "minecraft:wheat" for (x, y, z), block in world.blocks.items() if y == 65) == 2


def test_failed_write_rolls_back_both_crop_and_item():
    world = Dimension()
    world.farmland(0, 0)
    world.blocks[(0, 65, 0)].fail_after = True
    inventory = Inventory(Stack(amount=1))
    with pytest.raises(OSError, match="partially"):
        run_sow(world, inventory, radius=1)
    assert world.blocks[(0, 65, 0)].type == "minecraft:air"
    assert inventory.slot.amount == 1


def test_seed_debit_precedes_each_block_write():
    world = Dimension()
    world.farmland(0, 0)
    inventory = Inventory(Stack(amount=1))

    def verify_paid(_block_type):
        assert inventory.slot is None

    world.blocks[(0, 65, 0)].on_set = verify_paid
    assert run_sow(world, inventory, radius=1) == 1
    assert inventory.slot is None


def test_occupied_air_column_or_non_farmland_is_never_modified():
    world = Dimension()
    world.farmland(0, 0, above="minecraft:carrots")
    world.blocks[(1, 64, 0)] = Block("minecraft:dirt")
    world.blocks[(1, 65, 0)] = Block("minecraft:air")
    inventory = Inventory(Stack(amount=5))
    assert run_sow(world, inventory, radius=1) == 0
    assert inventory.slot.amount == 5
    assert world.blocks[(0, 65, 0)].type == "minecraft:carrots"
