"""Client-side feedback: particle markers and short tips for recommended plots.

Everything Endstone-facing lives behind two narrow protocols (:class:`ParticleSink`,
:class:`TipSink`) so the rendering logic can be exercised with a plain list-recording
fake in tests, without importing Endstone or starting a server.

Rendering is deliberately cheap: one particle per plot corner at the planting height,
plus at most one tip message per :attr:`~endstone_arborist.config.ArboristConfig.tip_interval`.
Endstone's :meth:`Player.spawn_particle` is player-scoped (it sends a spawn-particle packet
to that client only), so recommendations never leak to bystanders and nothing is modified
in the world — the plugin is advisory by design.
"""

from typing import Protocol

from endstone_arborist.planner import Plan
from endstone_arborist.profiles import TreeProfile

__all__ = ["Feedback", "ParticleSink", "TipSink", "plot_outline", "tip_text"]

#: Vertical offset applied to the tip so it floats at the player's head height.
TIP_OFFSET = 1.6


class ParticleSink(Protocol):
    """Something that can draw one particle for the local player."""

    def spawn_particle(self, name: str, x: float, y: float, z: float) -> None:
        """Draw *name* at an absolute position."""
        ...


class TipSink(Protocol):
    """Something that can show a short text message to the local player."""

    def send_tip(self, message: str) -> None:
        """Show *message* as a tip."""
        ...


def plot_outline(cells: tuple[tuple[int, int, int], ...]) -> tuple[tuple[float, float, float], ...]:
    """Return the particle positions that trace the corners of a plot.

    Each cell is reported at its northwest corner, offset half a block up so the
    particle sits inside the planting cell rather than on its floor. A 1x1 plot draws
    four corners; a 2x2 plot draws the four outer corners of the whole area, which
    reads as a square on the ground instead of a grid of duplicates.

    Args:
        cells: Absolute ``(x, y, z)`` planting cells of a candidate.

    Returns:
        Distinct ``(x, y, z)`` float positions, deterministic order.
    """
    if not cells:
        return ()
    xs = [cell[0] for cell in cells]
    zs = [cell[2] for cell in cells]
    y = cells[0][1]
    low_x, high_x, low_z, high_z = min(xs), max(xs) + 1, min(zs), max(zs) + 1
    corners = [(low_x, y, low_z), (high_x, y, low_z), (low_x, y, high_z), (high_x, y, high_z)]
    return tuple((float(x), float(cy) + 0.5, float(z)) for x, cy, z in corners)


def tip_text(profile: TreeProfile, plan: Plan) -> str:
    """Build the short hint shown next to the markers.

    Args:
        profile: Species the player is holding.
        plan: The current plan for that species.

    Returns:
        A single-line message. Empty string when nothing can be recommended.
    """
    if not plan.candidates:
        return f"Arborist：附近没有适合{profile.display_name}的位置"
    best = plan.candidates[0]
    size = "2×2" if best.footprint > 1 else "单株"
    return f"Arborist：发现 {len(plan.candidates)} 个{profile.display_name}候选位置（{size}，质量 {best.score:.0f}）"


class Feedback:
    """Renders plans for one player through injected sinks.

    Args:
        particles: Sink used to draw particles (usually the player).
        tips: Sink used to show tips (usually the same player).
    """

    def __init__(self, particles: ParticleSink, tips: TipSink) -> None:
        self._particles = particles
        self._tips = tips

    def show(self, profile: TreeProfile, plan: Plan, *, tip: str | None = None) -> int:
        """Draw every candidate of *plan*.

        Args:
            profile: Species being planted, used for the particle effect.
            plan: Recommendations to render.
            tip: Optional tip text; ``None`` suppresses the message.

        Returns:
            The number of particles sent, which lets callers assert bounded work.
        """
        sent = 0
        for candidate in plan.candidates:
            for x, y, z in plot_outline(candidate.cells):
                self._particles.spawn_particle(profile.particle, x, y, z)
                sent += 1
        if tip:
            self._tips.send_tip(tip)
        return sent

    def send_tip(self, message: str) -> None:
        """Send *message* on its own (used by the command and the activation hint)."""
        self._tips.send_tip(message)
