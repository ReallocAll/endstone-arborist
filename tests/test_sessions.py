from endstone_arborist.sessions import PlayerSession


def test_horizontal_movement_reuses_plan_and_prefetches():
    session = PlayerSession(unique_id="p", dimension_key="0", x=0, y=64, z=0, active=True)
    old_plan = object()
    session.plan = old_plan  # no planner API required to test cache lifetime
    session.plan_origin = (0, 0)
    generation = session.generation
    session.move_to("0", 2, 64, 0, prefetch_distance=3, reuse_distance=5)
    assert session.plan is old_plan
    assert session.generation == generation
    session.move_to("0", 3, 64, 0, prefetch_distance=3, reuse_distance=5)
    assert session.plan is old_plan and session.movement_prefetch
    session.move_to("0", 6, 64, 0, prefetch_distance=3, reuse_distance=5)
    assert session.plan is None


def test_dimension_and_height_hard_invalidate():
    session = PlayerSession(unique_id="p", dimension_key="0", x=0, y=64, z=0, active=True)
    session.plan = object()
    session.plan_origin = (0, 0)
    session.move_to("1", 0, 64, 0, prefetch_distance=3, reuse_distance=5)
    assert session.plan is None and session.generation == 1
    session.move_to("1", 0, 65, 0, prefetch_distance=3, reuse_distance=5)
    assert session.generation == 2
