"""Thumb-button swipe detection: when does held-button mouse motion become a
touchpad swipe, and which way does it go?

Pure logic (no GLib, no devices) so it can be tested on recorded motion.
The daemon feeds it press / motion / release with timestamps and forwards
the answers to the virtual touchpad.

Why it is careful about the start: GNOME fixes a swipe's orientation —
workspaces (horizontal) or overview (vertical) — from the first ~0.4 mm of
finger motion, so the first motion handed to the fingers decides the whole
gesture. The thumb press can nudge the mouse, and hands often start a swipe
on a slight curve, so the direction is taken from where the hand is heading
once the motion is clearly one way, not from everything since the press.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SwipeConfig:
    start: int = 100  # counts of travel after the settle window before a swipe can begin
    gain: float = 1.5  # touchpad units per sensor count
    axis_lock: bool = True  # once begun, drop motion across the swipe's axis
    settle_ms: float = 30  # motion this soon after the press is the press nudge
    recent_ms: float = 60  # window of motion that decides the direction
    recent_min: int = 50  # counts that window needs before it can decide
    dominance: float = 1.4  # dominant axis must beat the other by this (~35 degrees)
    flick_min: int = 150  # a release before the swipe began, after this much travel, is a flick


class ThumbSwipe:
    """State of one thumb-button hold."""

    def __init__(self, cfg: SwipeConfig | None = None):
        self.cfg = cfg or SwipeConfig()
        self.reset()

    def press(self, t: float) -> None:
        """A fresh press: forget the previous hold."""
        self.reset()
        self.t0 = t

    def reset(self) -> None:
        self.t0 = 0.0
        self.active = False
        self.axis = ""  # "x", "y", or "" (free) once active
        self.total = [0, 0]  # everything since the press, nudge included
        self.path = 0  # travel after the settle window
        self.recent: list[tuple[float, int, int]] = []

    def motion(self, t: float, dx: int, dy: int) -> tuple[str, float, float] | None:
        """Feed raw sensor motion while the button is held.

        Returns ("begin", x, y) when the swipe starts (put the fingers down
        and move them by x, y touchpad units), ("move", x, y) while it runs,
        or None while it is still undecided.
        """
        self.total[0] += dx
        self.total[1] += dy
        g = self.cfg.gain
        if self.active:
            if self.axis == "x":
                dy = 0
            elif self.axis == "y":
                dx = 0
            return ("move", dx * g, dy * g) if dx or dy else None
        if (t - self.t0) * 1000 < self.cfg.settle_ms:
            return None
        self.path += abs(dx) + abs(dy)
        self.recent.append((t, dx, dy))
        horizon = t - self.cfg.recent_ms / 1000
        self.recent = [r for r in self.recent if r[0] >= horizon]
        if self.path < self.cfg.start:
            return None
        rx = sum(r[1] for r in self.recent)
        ry = sum(r[2] for r in self.recent)
        if abs(rx) + abs(ry) < self.cfg.recent_min:
            return None
        return self._begin(rx, ry)

    def release(self) -> tuple[str, float, float]:
        """The button came up. Returns ("end", 0, 0) for a running swipe,
        ("flick", x, y) for a quick swipe that ended before it began (play it
        out now), or ("tap", 0, 0) for a press that never became a swipe."""
        if self.active:
            return ("end", 0.0, 0.0)
        tx, ty = self.total
        if abs(tx) + abs(ty) >= self.cfg.flick_min:
            # that much travel is never a tap; no more motion is coming to
            # settle the direction, so a diagonal goes with its stronger axis
            _, x, y = self._begin(tx, ty, decided=True)
            return ("flick", x, y)
        return ("tap", 0.0, 0.0)

    def _begin(self, vx: float, vy: float, decided: bool = False) -> tuple[str, float, float] | None:
        horizontal = abs(vx) >= abs(vy)
        major, minor = (abs(vx), abs(vy)) if horizontal else (abs(vy), abs(vx))
        if not decided and major < self.cfg.dominance * minor:
            return None  # no clear direction yet
        self.active = True
        if self.cfg.axis_lock:
            self.axis = "x" if horizontal else "y"
            if horizontal:
                vy = 0
            else:
                vx = 0
        g = self.cfg.gain
        return ("begin", vx * g, vy * g)
