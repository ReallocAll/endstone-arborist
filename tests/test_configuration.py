import tomllib
from pathlib import Path

from endstone_arborist.config import ArboristConfig


def test_default_configuration_preserves_existing_values():
    file = Path(__file__).resolve().parents[1] / "src" / "endstone_arborist" / "config.toml"
    data = tomllib.loads(file.read_text(encoding="utf-8"))
    config = ArboristConfig.from_mapping(data)
    assert config.scan_radius == 8
    assert config.default_density == "default"
    assert config.autoplant_cooldown == 10
    assert config.plan_capacity == 16
    assert config.plan_prefetch_distance == 3
    assert config.plan_reuse_distance == 5
    assert config.min_tps == 19.0
    assert config.sow_radius == 4
    assert config.sow_max_per_batch == 8
    assert config.sow_cooldown == 5
    assert not config.warnings


def test_old_config_still_loads_with_new_defaults():
    config = ArboristConfig.from_mapping({"scan-radius": 8, "default-density": "compact"})
    assert config.scan_radius == 8
    assert config.default_density == "compact"
    assert config.plan_capacity == 16
    assert config.min_tps == 19.0


def test_prefetch_window_repaired_without_disabling_planting():
    config = ArboristConfig.from_mapping({"scan-radius": 3, "plan-prefetch-distance": 3, "plan-reuse-distance": 1})
    assert config.plan_reuse_distance >= config.plan_prefetch_distance
    assert config.warnings



def test_sowing_limits_are_bounded_without_breaking_old_config():
    config = ArboristConfig.from_mapping({
        "sow-radius": 200,
        "sow-max-per-batch": 300,
        "sow-cooldown": 0,
    })
    assert config.sow_radius == 6
    assert config.sow_max_per_batch == 16
    assert config.sow_cooldown == 1
    assert config.warnings
