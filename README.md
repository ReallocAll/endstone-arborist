# Arborist

Automatic crop sowing and spacing-aware tree planting for [Endstone](https://github.com/EndstoneMC/endstone) Bedrock servers.

Players can independently enable crop sowing on vacant farmland (`/arb sow on`) or automatic tree planting (`/arb tree on`). Sowing uses a small bounded main-thread scan, while the background tree planner maintains a rolling candidate pool as the player walks. World writes, inventory changes and permission checks stay on the server thread.

## Features

- v0.4.1 fixes duplicate Endstone enum registration warnings by sharing the tree/sow action overload. Player commands and completion remain unchanged.

- Opt-in automatic sowing for wheat, carrots, potatoes and beetroot on vacant farmland; disabled by default.
- Supports eight tree species, including 2×2 dark oak planting.
- Per-player enable/disable state and compact/default/sparse density preferences, persisted by UUID.
- Rolling background planning, candidate reuse, and rate-limited block writes.
- Spark five-second TPS safety gate through Endstone PlaceholderAPI; unavailable or low TPS pauses planting.
- Read-only Territory claim integration with fail-closed protection when claim data cannot be trusted.
- Chinese player-facing messages and Endstone command suggestions.

## Installation

Download a wheel from the CI build artifacts, or build one locally. Place **one** `endstone_arborist-*.whl` in the BDS server's `plugins/` directory and restart Endstone. Arborist supports Python 3.11 or newer and requires Endstone 0.11 or newer. The Endstone installation must provide a native wheel for the selected Python version. As of October 2026, Python 3.15 support has been verified for Arborist's pure-Python code and wheel, but the public Endstone release matrix does not yet provide a `cp315` wheel.

Install the [Endstone PAPI](https://github.com/EndstoneMC/papi) and [Spark](https://github.com/EndstoneMC/spark) plugins for TPS reporting: automatic planting intentionally remains paused without a usable `{spark:tps_5s}` value. Arborist begins querying TPS after the first player joins. New players must opt in.

## Commands

One `/arb` command groups both independent features under `tree` and `sow`:

| Command | Purpose |
| --- | --- |
| `/arb tree on` | Enable automatic tree planting for yourself (healthy TPS and build permission required). |
| `/arb tree off` | Disable tree planting without changing crop sowing. |
| `/arb tree` or `/arb tree status` | Show tree planting state and spacing preference. |
| `/arb tree density [compact|default|sparse|server]` | Show or change tree spacing; `server` restores inheritance. |
| `/arb sow on` | Enable crop sowing independently of tree planting. |
| `/arb sow off` | Disable crop sowing. |
| `/arb sow` or `/arb sow status` | Show crop sowing state. |
| `/arb` or `/arb status` | Show both planting modes, tree density, Territory and Spark TPS status. |
| `/arb reload` | Reload server configuration (operator permission). |

The full name `/arborist` also works. The obsolete `/arb on`, `/arb off` and `/arb density ...` commands were removed; update ClockMenu to `/arb tree ...` before restarting the server.

The plugin can be exposed through a menu such as ClockMenu by dispatching these commands as the player.

Version 0.4.0 adds crop sowing and simplifies commands. Existing player preferences and config values remain intact; only the old command forms were dropped.

## Configuration

The plugin creates `plugins/arborist/config.toml` on first launch. Existing configurations are preserved when upgrading; new fields use built-in defaults until added manually. Explicit existing `sow-cooldown` values (including `10`) are not overwritten. Run `/arb reload` or restart after edits.

| Setting | Default | Meaning |
| --- | --- | --- |
| `scan-radius` | `8` | Horizontal scan radius in blocks. |
| `max-recommendations` | `3` | Maximum plots committed in one planting batch. |
| `plan-capacity` | `16` | Candidate reservoir size per planning pass. |
| `plan-prefetch-distance` | `3` | Horizontal movement before requesting a replacement plan. |
| `plan-reuse-distance` | `5` | Maximum movement allowed while using a cached plan. |
| `movement-prefetch-interval` | `5` | Minimum ticks between movement-triggered replans. |
| `refresh-interval` | `20` | Minimum ticks between ordinary scans. |
| `autoplant-cooldown` | `10` | Minimum ticks between tree planting batches. |
| `sow-radius` | `4` | Radius of the crop search (1–6 blocks). |
| `sow-max-per-batch` | `8` | Maximum crops sown per batch (1–16). |
| `sow-cooldown` | `5` | Minimum ticks between crop sowing batches; after a productive batch, the next may begin 5 ticks later. |
| `default-density` | `"default"` | Server-wide density preset. |
| `spacing-scale` | `1.0` | Global spacing multiplier. |
| `min-tps` | `19.0` | Minimum accepted Spark TPS. |
| `tps-check-interval` | `20` | TPS polling period in ticks. |
| `territory-protection` | `"auto"` | `auto`, `required`, or `off` (not recommended). |

See [`src/endstone_arborist/config.toml`](src/endstone_arborist/config.toml) for the complete reference template and legacy compatibility fields. Preferences (including the separate `sowing` flag) are stored in `plugins/arborist/players.json`. Old preferences remain valid, and sowing defaults to off. Do not commit files from a live server.

Automatic crop sowing only works in Survival mode and only fills air directly above farmland at the player's feet level. It never tills soil, harvests crops, replaces existing plants, or scans unloaded areas deliberately. It debits seeds before placing each block; a failed write attempts rollback. Sowing is skipped when TPS is low or unavailable, or when Territory denies building. Empty-hand and unsupported held items produce no block scans.

Direct block writes do not emit `BlockPlaceEvent`. Arborist independently checks Territory permissions before every write; other plugins relying solely on that event may not record automatic placements.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv build --wheel
```

The Python planner is independent of Endstone for unit testing. CI tests Python 3.11 through 3.15, builds the wheel, verifies its entry point and bundled configuration, and uploads the wheel as a workflow artifact. For Python 3.15, native Endstone installation is skipped until compatible wheels are available; this CI job validates only Arborist's Python code, package metadata, and wheel. CI does not publish releases or PyPI packages. Runtime behavior still requires validation on a BDS/Endstone server.

## License

GPL-3.0-only; see [LICENSE](LICENSE). Prior separately distributed 0.3.x wheels carried Apache-2.0 licensing information; this repository does not retroactively change those prior distributions. Arborist relies on public Endstone, PAPI, Spark and Territory interfaces rather than bundling third-party source or assets.
