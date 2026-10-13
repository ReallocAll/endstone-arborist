import json
from endstone_arborist.settings import PlayerPreferences


def test_legacy_enabled_migration_and_persistence(tmp_path):
    path = tmp_path / "players.json"
    path.write_text(json.dumps({"autoplant": {"user-id": True}, "density": {"user-id": "compact"}}))
    settings = PlayerPreferences(path)
    settings.load()
    assert settings.enabled("user-id")
    assert settings.density("user-id") == "compact"
    settings.set_density("user-id", None)
    settings.set_enabled("other-id", True)
    persisted = json.loads(path.read_text())
    assert persisted["enabled"]["user-id"]
    assert persisted["enabled"]["other-id"]
    assert "user-id" not in persisted["density"]
    assert not PlayerPreferences(tmp_path / "new.json").enabled("new-player")
    assert not settings.sowing_enabled("user-id")
    settings.set_sowing_enabled("user-id", True)
    fresh = PlayerPreferences(path)
    fresh.load()
    assert fresh.sowing_enabled("user-id")
    assert not fresh.sowing_enabled("other-id")
    fresh.set_sowing_enabled("user-id", False)
    assert not PlayerPreferences(path).sowing_enabled("user-id")
