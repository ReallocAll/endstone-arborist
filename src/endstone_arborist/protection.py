"""Compatibility guard for plugin-driven world writes.

Arborist writes saplings with ``Block.set_type``. That intentionally bypasses the normal
``BlockPlaceEvent`` chain, so a claim plugin that protects only that event would otherwise
be bypassed. The bundled Territory adapter mirrors Territory's current ``canBuild`` rules
from its SQLite database and config file before any Arborist write is allowed.

The adapter is read-only. If the Territory database exists but cannot be parsed, ``auto``
and ``required`` modes fail closed so a schema change cannot silently turn into a protection
bypass.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TerritoryBuildGuard", "TerritoryClaim"]


@dataclass(frozen=True)
class TerritoryClaim:
    name: str
    pos1: tuple[float, float, float]
    pos2: tuple[float, float, float]
    owner: str
    managers: frozenset[str]
    members: frozenset[str]
    dimension: str
    father: str
    if_build: bool

    def contains(self, x: int, y: int, z: int, dimension: str) -> bool:
        if self.dimension != dimension:
            return False
        lo_x, hi_x = sorted((self.pos1[0], self.pos2[0]))
        lo_y, hi_y = sorted((self.pos1[1], self.pos2[1]))
        lo_z, hi_z = sorted((self.pos1[2], self.pos2[2]))
        return lo_x <= x <= hi_x and lo_y <= y <= hi_y and lo_z <= z <= hi_z

    def member(self, player_name: str) -> bool:
        return player_name == self.owner or player_name in self.managers or player_name in self.members


def _split_names(value: object) -> frozenset[str]:
    # Territory stores comma-separated names. Its parser ignores commas inside braces; player
    # names normally need neither feature, but matching it here avoids a needless semantic drift.
    text = "" if value is None else str(value)
    out: list[str] = []
    current: list[str] = []
    in_braces = False
    for ch in text:
        if ch == "{":
            in_braces = True
            current.append(ch)
        elif ch == "}":
            in_braces = False
            current.append(ch)
        elif ch == "," and not in_braces:
            item = "".join(current).strip()
            if item:
                out.append(item)
            current.clear()
        else:
            current.append(ch)
    item = "".join(current).strip()
    if item:
        out.append(item)
    return frozenset(out)


class TerritoryBuildGuard:
    """Read-only mirror of Territory's build permission decision."""

    def __init__(
        self,
        db_path: Path | str,
        config_path: Path | str,
        *,
        mode: str = "auto",
        refresh_seconds: float = 2.0,
    ) -> None:
        mode = str(mode).strip().lower()
        if mode not in {"auto", "required", "off"}:
            raise ValueError("mode must be auto, required, or off")
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.mode = mode
        self.refresh_seconds = max(0.25, float(refresh_seconds))
        self.claims: tuple[TerritoryClaim, ...] = ()
        self.allow_op_as_member = False
        self.healthy = mode == "off"
        self.last_error: str | None = None
        self._last_check = 0.0
        self._seen_db = self.db_path.exists()

    @property
    def detected(self) -> bool:
        return self.db_path.exists()

    def refresh(self, *, force: bool = False) -> bool:
        if self.mode == "off":
            self.healthy = True
            self.last_error = None
            return True

        now = time.monotonic()
        if not force and now - self._last_check < self.refresh_seconds:
            return self.healthy
        self._last_check = now

        if not self.db_path.exists():
            self.claims = ()
            self.allow_op_as_member = False
            # In auto mode an installation that never had Territory is allowed. Once the DB
            # has been observed, its disappearance is treated as a protection failure instead
            # of silently turning checks off mid-session.
            self.healthy = self.mode == "auto" and not self._seen_db
            self.last_error = None if self.healthy else f"Territory database not found: {self.db_path}"
            return self.healthy
        self._seen_db = True

        try:
            # Reload on every throttled refresh instead of relying on the main DB mtime: SQLite
            # may keep Territory changes in a -wal file whose updates do not touch the main file.
            claims = self._load_claims()
            allow_op = self._load_allow_op()
        except Exception as error:
            self.healthy = False
            self.last_error = f"Territory protection unavailable: {error}"
            return False

        self.claims = claims
        self.allow_op_as_member = allow_op
        self.healthy = True
        self.last_error = None
        return True

    def _load_claims(self) -> tuple[TerritoryClaim, ...]:
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            rows = connection.execute(
                "SELECT name, pos1_x, pos1_y, pos1_z, pos2_x, pos2_y, pos2_z, "
                "owner, manager, member, dim, father_tty, if_build FROM territories"
            ).fetchall()
        finally:
            connection.close()
        claims: list[TerritoryClaim] = []
        for row in rows:
            claims.append(
                TerritoryClaim(
                    name=str(row[0]),
                    pos1=(float(row[1]), float(row[2]), float(row[3])),
                    pos2=(float(row[4]), float(row[5]), float(row[6])),
                    owner=str(row[7] or ""),
                    managers=_split_names(row[8]),
                    members=_split_names(row[9]),
                    dimension=str(row[10] or ""),
                    father=str(row[11] or ""),
                    if_build=bool(int(row[12] or 0)),
                )
            )
        return tuple(claims)

    def _load_allow_op(self) -> bool:
        if not self.config_path.exists():
            return False
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        return bool(raw.get("allow_op_as_member", False)) if isinstance(raw, dict) else False

    def can_build(self, player_name: str, is_op: bool, dimension: str, x: int, y: int, z: int) -> bool:
        if self.mode == "off":
            return True
        if not self.refresh():
            return False
        if not self.detected:
            return self.mode == "auto"
        matches = [claim for claim in self.claims if claim.contains(x, y, z, dimension)]
        if not matches:
            return True
        # Territory's list_in_tty gives a matching child claim precedence over its parent.
        # Multiple overlapping child claims are not expected; requiring all of them to allow is
        # deliberately stricter than picking an arbitrary row and cannot create a protection bypass.
        children = [claim for claim in matches if claim.father]
        effective = children if children else matches
        if is_op and self.allow_op_as_member:
            return True
        for claim in effective:
            if not claim.if_build and not claim.member(player_name):
                return False
        return True
