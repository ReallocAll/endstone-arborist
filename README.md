# Arborist

Automatic, spacing-aware tree planting for [Endstone](https://github.com/EndstoneMC/endstone) Bedrock servers.

When a player enables Arborist and holds a supported sapling, the plugin finds suitable positions and plants nearby. The background planner maintains a rolling candidate pool as the player walks; world reads, permission checks, inventory changes, and block placement remain on the server thread.

## Features

- Supports eight tree species, including 2×2 dark oak planting.
- Per-player enable/disable state and compact/default/sparse density preferences, persisted by UUID.
- Rolling background planning, candidate reuse, and rate-limited block writes.
- Spark five-second TPS safety gate through Endstone PlaceholderAPI; unavailable or low TPS pauses planting.
- Read-only Territory claim integration with fail-closed protection when claim data cannot be trusted.
- Chinese player-facing messages and Endstone command suggestions.

## Installation

Download a wheel from the CI build artifacts, or build one locally. Place **one** `endstone_arborist-*.whl` in the BDS server's `plugins/` directory and restart Endstone. The installed version of Endstone must be 0.11 or newer.

Install the [Endstone PAPI](https://github.com/EndstoneMC/papi) and [Spark](https://github.com/EndstoneMC/spark) plugins for TPS reporting: automatic planting intentionally remains paused without a usable `{spark:tps_5s}` value. Arborist begins querying TPS after the first player joins. New players must opt in.

## Commands

| Command | Purpose |
| --- | --- |
| `/arb on` | Enable automatic planting for yourself; requires healthy TPS and build permission at your position. |
| `/arb off` | Disable planting. |
| `/arb status` | Show current status and density. |
| `/arb density` | Show current density. |
| `/arb density compact` | Plant more densely. |
| `/arb density default` | Use the original default spacing as your personal choice. |
| `/arb density sparse` | Increase spacing. |
| `/arb density server` | Remove the personal override and follow the server default. |
| `/arb reload` | Reload server configuration (operator permission). |

The full command name `/arborist` also works. The plugin can be exposed through a menu such as ClockMenu by dispatching the commands above as the player.

## Configuration

The plugin creates `plugins/arborist/config.toml` on first launch. Existing configurations are preserved when upgrading; new fields use built-in defaults until added manually. Run `/arb reload` or restart after edits.

| Setting | Default | Meaning |
| --- | --- | --- |
| `scan-radius` | `8` | Horizontal scan radius in blocks. |
| `max-recommendations` | `3` | Maximum plots committed in one planting batch. |
| `plan-capacity` | `16` | Candidate reservoir size per planning pass. |
| `plan-prefetch-distance` | `3` | Horizontal movement before requesting a replacement plan. |
| `plan-reuse-distance` | `5` | Maximum movement allowed while using a cached plan. |
| `movement-prefetch-interval` | `5` | Minimum ticks between movement-triggered replans. |
| `refresh-interval` | `20` | Minimum ticks between ordinary scans. |
| `autoplant-cooldown` | `10` | Minimum ticks between planting batches. |
| `default-density` | `"default"` | Server-wide density preset. |
| `spacing-scale` | `1.0` | Global spacing multiplier. |
| `min-tps` | `19.0` | Minimum accepted Spark TPS. |
| `tps-check-interval` | `20` | TPS polling period in ticks. |
| `territory-protection` | `"auto"` | `auto`, `required`, or `off` (not recommended). |

See [`src/endstone_arborist/config.toml`](src/endstone_arborist/config.toml) for the complete reference template and legacy compatibility fields. Preferences are stored in `plugins/arborist/players.json`. Do not commit files from a live server.

Direct block writes do not emit `BlockPlaceEvent`. Arborist independently checks Territory permissions before every write; other plugins relying solely on that event may not record automatic placements.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv build --wheel
```

The Python planner is independent of Endstone for unit testing. CI tests Python 3.10, 3.12 and 3.14, builds the wheel, verifies its entry point and bundled configuration, and uploads the wheel as a workflow artifact. CI does not publish releases or PyPI packages. Runtime behavior still requires validation on a BDS/Endstone server.

## License

GPL-3.0-only; see [LICENSE](LICENSE). Prior separately distributed 0.3.x wheels carried Apache-2.0 licensing information; this repository does not retroactively change those prior distributions. Arborist relies on public Endstone, PAPI, Spark and Territory interfaces rather than bundling third-party source or assets.
