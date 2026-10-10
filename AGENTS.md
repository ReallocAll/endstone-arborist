# Arborist development notes

- Keep the Endstone entry point `endstone_arborist:ArboristPlugin` and command names stable; preserve UUID preferences and old `config.toml` keys.
- All Endstone objects and PAPI parsing stay on the BDS main thread. The `ArboristPlanner` worker only receives immutable Python snapshot data; stale generations must be discarded.
- Enforce Territory protection again at commit time. Never bypass it for faster planting; an unavailable adapter fails closed.
- Avoid full replans on each movement event; preserve the rolling-plan and prefetch semantics.
- Preserve the GPL-3.0-only license and validate third-party license obligations before copying code or assets.
- Use `uv sync --extra dev`, `uv run ruff check .`, `uv run pytest`, and `uv build --wheel` before submitting changes. Verify wheel metadata, the `endstone` entry point, and the bundled `config.toml`.
- Do not commit server configurations, `players.json`, world data, profiling files, `.venv`, caches, or wheel files. No automatic PyPI or Release publishing.
