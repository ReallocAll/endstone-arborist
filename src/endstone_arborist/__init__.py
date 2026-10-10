"""Arborist: an automatic tree planting assistant for Endstone servers.

The package exposes one plugin class; everything else is an implementation detail that other
plugins should not depend on.

Layering, from least to most coupled to the server:

    blocks     namespaced Bedrock identifiers and their classifications
    profiles   per-species planting rules (footprint, clearance, spacing, ground)
    world      cached column scanning behind a ``(x, y, z) -> id`` probe
    planner    hard validity then preference scoring, as pure functions
    sessions   per-player state and tick-based rate limits
    protection Territory-compatible build permission guard
    adapter    reads live Endstone objects and turns them into plain values
    controller tick orchestration that turns observations into effects
    plugin     Endstone entrypoint: lifecycle, events, command, effect execution

Only :mod:`endstone_arborist.plugin` needs Endstone at runtime, so the entrypoint class is
loaded lazily: importing a pure module such as ``endstone_arborist.planner`` does not have to
pull in the native ``endstone`` binary. The plugin loader resolves ``endstone_arborist:ArboristPlugin``
through :func:`__getattr__`, which is where the real import happens.
"""

from typing import TYPE_CHECKING

__all__ = ["ArboristPlugin"]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from endstone_arborist.plugin import ArboristPlugin


def __getattr__(name: str) -> type:
    """Import the plugin class on first use rather than at package import time."""
    if name == "ArboristPlugin":
        from endstone_arborist.plugin import ArboristPlugin

        return ArboristPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
