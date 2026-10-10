"""PAPI -> Spark TPS gate used by the automatic planter.

The PAPI service itself must be queried on Endstone's primary server thread. This
adapter therefore performs no background work; it only rate-limits the placeholder
lookup and exposes a small immutable status for the rest of Arborist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["SparkTpsGate", "TpsStatus", "parse_spark_tps"]

_SPARK_TOKEN = "{spark:tps_5s}"
_COLOR = re.compile(r"§.")
_NUMBER = re.compile(r"\*?(?:\d+(?:\.\d*)?|\.\d+)")


def parse_spark_tps(value: str) -> float | None:
    """Parse Spark's Java-compatible coloured TPS placeholder output."""
    cleaned = _COLOR.sub("", str(value)).strip()
    match = _NUMBER.fullmatch(cleaned)
    if match is None:
        return None
    try:
        number = float(match.group(0).lstrip("*"))
    except ValueError:
        return None
    if not 0.0 <= number <= 20.0:
        return None
    return number


@dataclass(frozen=True)
class TpsStatus:
    available: bool
    allowed: bool
    tps: float | None
    reason: str
    raw: str = ""
    warming_up: bool = False


class SparkTpsGate:
    """Read ``{spark:tps_5s}`` through Endstone PAPI and enforce a floor."""

    def __init__(self, service_manager: Any, *, minimum: float = 19.0, interval_ticks: int = 20) -> None:
        self._service_manager = service_manager
        self.minimum = float(minimum)
        self.interval_ticks = max(1, int(interval_ticks))
        self._api: Any | None = None
        self._last_tick: int | None = None
        self._status = TpsStatus(False, False, None, "Spark TPS 暂无可用采样")

    @property
    def status(self) -> TpsStatus:
        return self._status

    def refresh(self, tick: int, *, force: bool = False) -> TpsStatus:
        if not force and self._last_tick is not None and tick - self._last_tick < self.interval_ticks:
            return self._status
        self._last_tick = int(tick)

        try:
            from endstone_papi import PlaceholderAPI  # type: ignore[import-not-found]
        except Exception:
            self._api = None
            self._status = TpsStatus(False, False, None, "未安装或无法导入 Endstone PAPI")
            return self._status

        try:
            api = self._api
            if api is None or not bool(api.active):
                api = PlaceholderAPI.load(self._service_manager)
                self._api = api
            if api is None or not bool(api.active):
                self._status = TpsStatus(False, False, None, "PlaceholderAPI 服务未就绪")
                return self._status
            if not bool(api.is_registered("spark")):
                self._status = TpsStatus(False, False, None, "Spark 尚未注册 PAPI 扩展")
                return self._status
            raw = str(api.set_placeholders(None, _SPARK_TOKEN))
        except Exception as exc:
            self._status = TpsStatus(False, False, None, f"读取 Spark TPS 失败：{exc}")
            return self._status

        tps = parse_spark_tps(raw)
        # A zero/unresolved value is not a real TPS measurement. Arborist's plugin
        # lifecycle handles server bootstrap separately (it does not start this guard
        # until the first player joins), so the gate itself only reports availability.
        if tps is None or raw == _SPARK_TOKEN or tps <= 0.0:
            self._status = TpsStatus(False, False, None, "Spark TPS 暂无可用采样", raw)
            return self._status
        allowed = tps >= self.minimum
        reason = "TPS 正常" if allowed else f"TPS {tps:.2f} 低于阈值 {self.minimum:.2f}"
        self._status = TpsStatus(True, allowed, tps, reason, raw)
        return self._status
