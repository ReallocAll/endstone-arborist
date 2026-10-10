import sys
import types

from endstone_arborist.performance import SparkTpsGate, parse_spark_tps


def test_spark_tps_parser():
    assert parse_spark_tps("§a19.17") == 19.17
    assert parse_spark_tps("§a20.00") == 20.0
    assert parse_spark_tps("§a*20.00") == 20.0
    assert parse_spark_tps("{spark:tps_5s}") is None
    assert parse_spark_tps("error 19.00") is None
    assert parse_spark_tps("not available") is None


def test_guard_starts_fail_closed_and_handles_threshold(monkeypatch):
    class Papi:
        active = True
        value = "§a19.17"

        def is_registered(self, name):
            return name == "spark"

        def set_placeholders(self, player, token):
            assert player is None and token == "{spark:tps_5s}"
            return self.value

    service = Papi()
    module = types.ModuleType("endstone_papi")

    class PlaceholderAPI:
        @staticmethod
        def load(manager):
            return service

    module.PlaceholderAPI = PlaceholderAPI
    monkeypatch.setitem(sys.modules, "endstone_papi", module)
    gate = SparkTpsGate(object(), minimum=19.0, interval_ticks=20)
    assert not gate.status.allowed
    assert gate.refresh(20).allowed
    service.value = "§c18.75"
    assert gate.refresh(21).allowed  # cached until the next poll
    assert not gate.refresh(40).allowed
    service.value = "§a0.00"
    state = gate.refresh(60)
    assert not state.available and not state.allowed
