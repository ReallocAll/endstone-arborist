"""Persistent per-player Arborist preferences.

The store is keyed by player UUID and is loaded once into memory. Player commands write
through atomically, while the automatic planting hot path performs no disk I/O.

Version 0.2 migrates the old ``autoplant`` table to ``enabled`` so existing explicit
choices are preserved. New players default to disabled and opt in with ``/arb on``.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DENSITY_PRESETS",
    "DEFAULT_ENABLED",
    "PlayerPreferences",
    "Preferences",
]

DEFAULT_ENABLED = False
DENSITY_PRESETS = frozenset({"compact", "default", "sparse"})


@dataclass(frozen=True)
class Preferences:
    enabled: dict[str, bool] = field(default_factory=dict)
    density: dict[str, str] = field(default_factory=dict)
    corrupt: bool = False
    notes: tuple[str, ...] = ()


def _as_uuid_key(player_id: object) -> str:
    if isinstance(player_id, uuid.UUID):
        return str(player_id)
    return str(player_id)


class PlayerPreferences:
    """In-memory preference table backed by one JSON file."""

    def __init__(self, path: Path | str, *, default_enabled: bool = DEFAULT_ENABLED) -> None:
        self._path = Path(path)
        self._default_enabled = bool(default_enabled)
        self._enabled: dict[str, bool] = {}
        self._density: dict[str, str] = {}
        self._loaded = False
        self._notes: list[str] = []
        self._dirty = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def default_enabled(self) -> bool:
        return self._default_enabled

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(self._notes)

    def load(self) -> Preferences:
        self._notes = []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            parsed = Preferences()
        except OSError as error:
            parsed = Preferences(corrupt=True, notes=(f"preferences file unreadable ({error}); using defaults",))
        else:
            try:
                decoded = json.loads(raw)
            except ValueError as error:
                parsed = Preferences(corrupt=True, notes=(f"preferences file unreadable ({error}); using defaults",))
            else:
                parsed = self._parse(decoded)
        self._notes.extend(parsed.notes)
        self._enabled = dict(parsed.enabled)
        self._density = dict(parsed.density)
        self._loaded = True
        if parsed.corrupt:
            self._dirty = True
        return parsed

    @staticmethod
    def _parse(raw: object) -> Preferences:
        if not isinstance(raw, Mapping):
            return Preferences(corrupt=True, notes=("preferences root is not an object",))

        notes: list[str] = []
        corrupt = False

        # v0.2 uses "enabled". Migrate v0.1's explicit Auto-Plant choices when present.
        enabled_raw = raw.get("enabled")
        if enabled_raw is None:
            enabled_raw = raw.get("autoplant", {})
            if "autoplant" in raw:
                notes.append("migrated legacy preferences.autoplant to preferences.enabled")
        if not isinstance(enabled_raw, Mapping):
            enabled_raw = {}
            corrupt = True
            notes.append("preferences.enabled is not an object")

        enabled: dict[str, bool] = {}
        for key, value in enabled_raw.items():
            if not isinstance(key, str) or not isinstance(value, bool):
                notes.append(f"skipped invalid enabled entry for {key!r}")
                continue
            enabled[key] = bool(value)

        density_raw = raw.get("density", {})
        if not isinstance(density_raw, Mapping):
            density_raw = {}
            corrupt = True
            notes.append("preferences.density is not an object")

        density: dict[str, str] = {}
        for key, value in density_raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                notes.append(f"skipped invalid density entry for {key!r}")
                continue
            preset = value.strip().lower()
            if preset not in DENSITY_PRESETS:
                notes.append(f"skipped unknown density {value!r} for {key}")
                continue
            density[key] = preset

        return Preferences(enabled=enabled, density=density, corrupt=corrupt, notes=tuple(notes))

    def enabled(self, player_id: object) -> bool:
        if not self._loaded:
            self.load()
        return self._enabled.get(_as_uuid_key(player_id), self._default_enabled)

    def set_enabled(self, player_id: object, value: bool) -> bool:
        if not self._loaded:
            self.load()
        flag = bool(value)
        key = _as_uuid_key(player_id)
        if self._enabled.get(key) != flag:
            self._enabled[key] = flag
            self._dirty = True
            self.save()
        return flag

    def toggle(self, player_id: object) -> bool:
        return self.set_enabled(player_id, not self.enabled(player_id))

    def density(self, player_id: object) -> str | None:
        """Return the player's explicit density preset, or ``None`` to inherit the server default."""
        if not self._loaded:
            self.load()
        return self._density.get(_as_uuid_key(player_id))

    def set_density(self, player_id: object, value: str | None) -> str | None:
        """Persist a density override. ``None`` clears it and restores server inheritance."""
        if not self._loaded:
            self.load()
        key = _as_uuid_key(player_id)
        if value is None:
            if key in self._density:
                del self._density[key]
                self._dirty = True
                self.save()
            return None
        preset = str(value).strip().lower()
        if preset not in DENSITY_PRESETS:
            raise ValueError(f"unknown density preset: {value}")
        if self._density.get(key) != preset:
            self._density[key] = preset
            self._dirty = True
            self.save()
        return preset

    def save(self) -> bool:
        if not self._dirty:
            return True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"enabled": self._enabled, "density": self._density},
                indent=2,
                sort_keys=True,
            )
            temp: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=str(self._path.parent),
                    prefix=self._path.name + ".",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp = Path(handle.name)
                    handle.write(payload + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self._path)
            except BaseException:
                if temp is not None:
                    temp.unlink(missing_ok=True)
                raise
        except OSError as error:
            self._notes.append(f"could not save preferences ({error})")
            return False
        self._dirty = False
        return True

    def snapshot(self) -> dict[str, object]:
        if not self._loaded:
            self.load()
        return {"enabled": dict(self._enabled), "density": dict(self._density)}
