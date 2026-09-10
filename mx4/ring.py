"""The actions ring — the radial popup the thumb button shows.

A frameless, transparent overlay drawn with cairo: round buttons laid out on
a circle around the cursor, the entry under the pointer highlighted, its
label in a pill underneath. Selection is angular like a pie menu, so a flick
towards an entry and a click is enough — the pointer never has to land
inside the small circle. A click beyond the ring dismisses it.

Three kinds of entry:
  * an action — click it and its command runs;
  * a dial (brightness, volume) — scroll over it to move the level, which the
    arc around the button shows; clicking opens the settings page;
  * a toggle (the microphone) — its icon shows what a click would do.

Everything moves: the buttons spiral out of the centre when the ring opens
and fall back into it when it closes, the highlight eases in and out, and a
level change slides the arc rather than snapping it. All the drawing is in
unscaled pixels; `scale` stretches the whole thing at paint time.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from . import levels  # noqa: E402

TAU = 2 * math.pi

# geometry (px)
ITEM_R = 26.0          # resting radius of an entry circle
HOVER_SCALE = 1.10     # the highlighted one, and only it, grows a tenth
ITEM_LIFT = 2.0        # ...and steps this far further out
ITEM_GAP = 20.0        # minimum space between two entry circles
PAD = 30.0             # room around the ring for shadows, growth and arcs
CLOSE_R = 15.0         # the "×" in the middle
DEADZONE = 46.0        # no angular selection this close to the centre
SECTOR_REACH = 38.0    # ...nor beyond the buttons: out there a click dismisses
LABEL_GAP = 16.0
LABEL_H = 32.0
ARC_GAP = 7.0          # distance from a dial button to its level arc
ARC_W = 3.0
ICON_PX = 23.0

# colours
BG_ITEM = (0.96, 0.96, 0.96, 0.94)
BG_ITEM_HOVER = (0.38, 0.46, 0.56, 0.96)   # slate: grey with a little blue in it
EDGE_ITEM = (0.0, 0.0, 0.0, 0.07)
EDGE_ITEM_HOVER = (1.0, 1.0, 1.0, 0.10)
FG_ITEM = (0.16, 0.16, 0.16, 1.0)
FG_ITEM_HOVER = (1.0, 1.0, 1.0, 1.0)
# the arc sits *outside* the button, over the desktop, so it carries its own
# light track instead of borrowing a colour from whatever is behind the ring
ARC_TRACK = (0.94, 0.94, 0.94, 0.95)
ARC_TRACK_HOVER = (0.97, 0.97, 0.97, 0.95)
ARC_FILL = (0.36, 0.45, 0.56, 0.95)
ARC_FILL_HOVER = (0.44, 0.56, 0.71, 1.0)
BG_CLOSE = (0.96, 0.96, 0.96, 0.80)
BG_CLOSE_HOVER = (0.90, 0.36, 0.34, 0.95)
BG_LABEL = (1.0, 1.0, 1.0, 0.97)
FG_LABEL = (0.10, 0.10, 0.10, 1.0)

# timings (ms)
OPEN_MS = 288.0
STAGGER_MS = 19.0      # each button leaves the centre a moment after the last
CLOSE_MS = 204.0
HOVER_MS = 130.0
LEVEL_MS = 120.0
OPEN_SPIN = TAU * 0.5   # the ring turns half a revolution as it spirals open
START_DELAY = 300.0     # ms of stillness first, so the whole spiral is seen
FRAME_MS = 16

# subpixel antialiasing fringes badly on a translucent surface
FONT_OPTS = cairo.FontOptions()
FONT_OPTS.set_antialias(cairo.Antialias.GRAY)
FONT_OPTS.set_hint_style(cairo.HintStyle.SLIGHT)


# -- easing ---------------------------------------------------------------------

def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_out_back(t: float) -> float:
    """Overshoots a little, so the buttons land rather than merely stop."""
    return 1 + 2.0 * (t - 1) ** 3 + 1.1 * (t - 1) ** 2


def ease_in(t: float) -> float:
    return t ** 3


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def mix(a, b, t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


# -- glyphs ---------------------------------------------------------------------
#
# One hand-drawn set instead of whatever the icon theme happens to ship: the
# same 24px grid, the same round 1.9px stroke, so a ring of eight buttons
# reads as one family. Each glyph draws around (0, 0) into a context already
# centred and scaled on its button, and is handed the two colours in play —
# `bg` is for knocking a gap out of a shape that crosses another.

def _pen(cr) -> None:
    cr.set_line_width(1.9)
    cr.set_line_cap(cairo.LineCap.ROUND)
    cr.set_line_join(cairo.LineJoin.ROUND)


def _g_rocket(cr, fg, bg) -> None:
    cr.move_to(0, -10.4)                                  # nose
    cr.curve_to(4.3, -5.0, 4.7, 0.8, 3.4, 5.4)
    cr.line_to(-3.4, 5.4)
    cr.curve_to(-4.7, 0.8, -4.3, -5.0, 0, -10.4)
    cr.close_path()
    cr.stroke()
    cr.arc(0, -3.2, 1.7, 0, TAU)                          # porthole
    cr.stroke()
    for side in (-1, 1):                                  # fins
        cr.move_to(side * 3.3, 1.0)
        cr.line_to(side * 7.6, 6.2)
        cr.line_to(side * 7.6, 8.2)
        cr.line_to(side * 3.4, 5.6)
        cr.stroke()
    cr.move_to(-2.0, 6.4)                                 # exhaust
    cr.line_to(0, 10.2)
    cr.line_to(2.0, 6.4)
    cr.stroke()


def _g_sun(cr, fg, bg) -> None:
    cr.arc(0, 0, 4.1, 0, TAU)
    cr.stroke()
    for i in range(8):
        a = TAU * i / 8
        cr.move_to(6.6 * math.cos(a), 6.6 * math.sin(a))
        cr.line_to(9.4 * math.cos(a), 9.4 * math.sin(a))
        cr.stroke()


def _g_speaker(cr, fg, bg) -> None:
    cr.move_to(-8.2, -3.0)
    cr.line_to(-4.6, -3.0)
    cr.line_to(-0.2, -7.6)
    cr.line_to(-0.2, 7.6)
    cr.line_to(-4.6, 3.0)
    cr.line_to(-8.2, 3.0)
    cr.close_path()
    cr.stroke()
    for radius in (3.4, 6.4):                             # sound
        cr.new_sub_path()
        cr.arc(1.0, 0, radius, -0.95, 0.95)
        cr.stroke()


def _g_terminal(cr, fg, bg) -> None:
    _rounded(cr, -9.5, -7.5, 19, 15, 2.6)
    cr.stroke()
    cr.move_to(-5.0, -2.4)
    cr.line_to(-1.8, 0.4)
    cr.line_to(-5.0, 3.2)
    cr.stroke()
    cr.move_to(0.6, 3.4)
    cr.line_to(5.2, 3.4)
    cr.stroke()


def _g_lock(cr, fg, bg) -> None:
    _rounded(cr, -6.6, -1.4, 13.2, 10.4, 2.2)
    cr.stroke()
    cr.new_sub_path()
    cr.arc(0, -1.6, 3.9, math.pi, TAU)                    # shackle
    cr.stroke()
    cr.arc(0, 3.6, 1.25, 0, TAU)                          # keyhole
    cr.stroke()


def _g_camera(cr, fg, bg) -> None:
    cr.move_to(-4.6, -5.6)
    cr.line_to(-3.2, -8.4)
    cr.line_to(1.6, -8.4)
    cr.line_to(3.0, -5.6)
    cr.stroke()
    _rounded(cr, -9.6, -5.6, 19.2, 13.4, 2.6)
    cr.stroke()
    cr.arc(0, 1.1, 3.5, 0, TAU)
    cr.stroke()


def _g_play(cr, fg, bg) -> None:
    cr.move_to(-3.4, -6.8)
    cr.line_to(6.6, 0)
    cr.line_to(-3.4, 6.8)
    cr.close_path()
    cr.stroke()


def _mic_body(cr) -> None:
    _rounded(cr, -2.9, -9.6, 5.8, 11.4, 2.9)
    cr.stroke()
    cr.new_sub_path()
    cr.arc(0, 0.4, 5.8, 0.36, math.pi - 0.36)             # cradle
    cr.stroke()
    cr.move_to(0, 6.2)
    cr.line_to(0, 9.4)
    cr.stroke()
    cr.move_to(-3.4, 9.4)
    cr.line_to(3.4, 9.4)
    cr.stroke()


def _g_mic(cr, fg, bg) -> None:
    _mic_body(cr)


def _g_mic_off(cr, fg, bg) -> None:
    """The live microphone: a click would cut it, so it wears the slash."""
    _mic_body(cr)
    cr.save()
    cr.set_source_rgba(*bg)                               # gap under the slash
    cr.set_line_width(4.4)
    cr.move_to(-7.6, -8.2)
    cr.line_to(7.6, 8.6)
    cr.stroke()
    cr.set_source_rgba(*fg)
    cr.set_line_width(1.9)
    cr.move_to(-7.0, -7.6)
    cr.line_to(7.0, 8.0)
    cr.stroke()
    cr.restore()


def _g_grid(cr, fg, bg) -> None:
    for x in (-8.6, 1.4):
        for y in (-8.6, 1.4):
            _rounded(cr, x, y, 7.2, 7.2, 1.8)
            cr.stroke()


def _g_monitor(cr, fg, bg) -> None:
    _rounded(cr, -9.6, -7.6, 19.2, 13.4, 2.4)
    cr.stroke()
    cr.move_to(0, 5.8)
    cr.line_to(0, 8.6)
    cr.stroke()
    cr.move_to(-4.4, 8.6)
    cr.line_to(4.4, 8.6)
    cr.stroke()


def _g_minimize(cr, fg, bg) -> None:
    _rounded(cr, -9.4, -8.4, 18.8, 12.0, 2.4)
    cr.stroke()
    cr.move_to(-4.6, 8.2)
    cr.line_to(4.6, 8.2)
    cr.stroke()


def _g_search(cr, fg, bg) -> None:
    cr.arc(-1.4, -1.4, 5.6, 0, TAU)
    cr.stroke()
    cr.move_to(2.8, 2.8)
    cr.line_to(7.8, 7.8)
    cr.stroke()


def _g_folder(cr, fg, bg) -> None:
    cr.move_to(-9.2, 7.0)
    cr.line_to(-9.2, -6.2)
    cr.line_to(-3.0, -6.2)
    cr.line_to(-0.8, -3.4)
    cr.line_to(9.2, -3.4)
    cr.line_to(9.2, 7.0)
    cr.close_path()
    cr.stroke()


def _g_mail(cr, fg, bg) -> None:
    _rounded(cr, -9.6, -7.0, 19.2, 14.0, 2.2)
    cr.stroke()
    cr.move_to(-9.6, -5.4)
    cr.line_to(0, 1.6)
    cr.line_to(9.6, -5.4)
    cr.stroke()


def _g_globe(cr, fg, bg) -> None:
    cr.arc(0, 0, 8.6, 0, TAU)
    cr.stroke()
    cr.move_to(-8.6, 0)
    cr.line_to(8.6, 0)
    cr.stroke()
    for side in (-1, 1):                                  # meridians
        cr.move_to(0, -8.6)
        cr.curve_to(side * 5.6, -4.4, side * 5.6, 4.4, 0, 8.6)
        cr.stroke()


def _g_chat(cr, fg, bg) -> None:
    _rounded(cr, -9.0, -8.0, 18.0, 13.4, 3.4)
    cr.stroke()
    cr.move_to(-3.6, 5.4)
    cr.line_to(-5.2, 9.6)
    cr.line_to(0.8, 5.4)
    cr.stroke()


def _g_skip(cr, fg, bg, back: bool = False) -> None:
    s = -1 if back else 1
    cr.move_to(-6.0 * s, -6.4)
    cr.line_to(2.2 * s, 0)
    cr.line_to(-6.0 * s, 6.4)
    cr.close_path()
    cr.stroke()
    cr.move_to(5.4 * s, -6.4)
    cr.line_to(5.4 * s, 6.4)
    cr.stroke()


def _g_sliders(cr, fg, bg) -> None:
    for y, knob in ((-5.6, -2.4), (0, 3.0), (5.6, -0.6)):
        cr.move_to(-8.6, y)
        cr.line_to(8.6, y)
        cr.stroke()
        cr.set_source_rgba(*bg)
        cr.arc(knob, y, 2.4, 0, TAU)
        cr.fill_preserve()
        cr.set_source_rgba(*fg)
        cr.stroke()


def _g_copy(cr, fg, bg) -> None:
    _rounded(cr, -8.8, -8.8, 12.6, 12.6, 2.2)
    cr.stroke()
    cr.set_source_rgba(*bg)
    _rounded(cr, -3.8, -3.8, 12.6, 12.6, 2.2)
    cr.fill_preserve()
    cr.set_source_rgba(*fg)
    cr.stroke()


def _g_pencil(cr, fg, bg) -> None:
    cr.move_to(-8.4, 8.4)
    cr.line_to(-7.4, 3.6)
    cr.line_to(3.6, -7.4)
    cr.line_to(7.4, -3.6)
    cr.line_to(-3.6, 7.4)
    cr.close_path()
    cr.stroke()
    cr.move_to(1.2, -5.0)
    cr.line_to(5.0, -1.2)
    cr.stroke()


GLYPHS = {
    "$rocket": _g_rocket, "$brightness": _g_sun, "$volume": _g_speaker,
    "$terminal": _g_terminal, "$lock": _g_lock, "$camera": _g_camera,
    "$play": _g_play, "$mic": _g_mic, "$mic-off": _g_mic_off, "$grid": _g_grid,
    "$monitor": _g_monitor, "$minimize": _g_minimize, "$search": _g_search,
    "$folder": _g_folder, "$mail": _g_mail, "$globe": _g_globe, "$chat": _g_chat,
    "$next": _g_skip, "$prev": lambda cr, fg, bg: _g_skip(cr, fg, bg, back=True),
    "$sliders": _g_sliders, "$copy": _g_copy, "$pencil": _g_pencil,
}

# what an entry gets when it doesn't ask for anything: built-ins are known
# outright, everything else is guessed from the label and the command
BUILTIN_ICONS = {
    "workspace-prev": "$prev", "workspace-next": "$next", "overview": "$grid",
    "show-desktop": "$monitor", "minimize": "$minimize", "screenshot": "$camera",
    "play-pause": "$play",
}
KEYWORD_ICONS = (
    (("krunner", "launcher", "spotlight", "run command"), "$rocket"),
    (("rocket", "missile", "launch"), "$rocket"),
    (("bright", "backlight"), "$brightness"),
    (("volume", "sound", "speaker", "audio"), "$volume"),
    (("terminal", "console", "shell", "konsole"), "$terminal"),
    (("lock", "logout", "suspend"), "$lock"),
    (("mic", "microphone"), "$mic"),
    (("screenshot", "capture", "snip", "camera"), "$camera"),
    (("play", "pause", "music", "media"), "$play"),
    (("next", "forward", "skip"), "$next"),
    (("prev", "back", "rewind"), "$prev"),
    (("browser", "web", "chrome", "firefox", "internet"), "$globe"),
    (("mail", "email", "thunderbird"), "$mail"),
    (("file", "folder", "explorer", "dolphin", "nautilus"), "$folder"),
    (("search", "find"), "$search"),
    (("setting", "preference", "config", "tune"), "$sliders"),
    (("copy", "clipboard", "paste"), "$copy"),
    (("workspace", "desktop", "overview"), "$grid"),
    (("window", "minimi"), "$minimize"),
    (("chat", "message", "slack", "teams", "signal", "telegram"), "$chat"),
    (("note", "text", "edit", "write"), "$pencil"),
)
DIAL_ICONS = {"brightness": "$brightness", "volume": "$volume", "mic": "$mic"}


def _icon_exists(name: str) -> bool:
    return bool(name) and Gtk.IconTheme.get_default().has_icon(name)


def guess_icon(label: str, command: str) -> str:
    """A drawn glyph if one fits, else a themed icon, else "" for the letter."""
    if command.startswith("@"):
        name = BUILTIN_ICONS.get(command[1:].strip(), "")
        if name:
            return name
    haystack = f"{label} {command}".lower()
    for words, name in KEYWORD_ICONS:
        if any(word in haystack for word in words):
            return name
    # nothing matched: the command's own binary often ships an icon of its name
    binary = command.strip().split()[0].rsplit("/", 1)[-1] if command.strip() else ""
    return binary if _icon_exists(binary) else ""


@dataclass
class Entry:
    """One button. `dial` makes it a scrollable level, `toggle` a switch whose
    icon reflects `state`; both are filled in by whoever builds the ring."""

    label: str
    icon: str
    command: str
    dial: str = ""
    toggle: str = ""
    level: float | None = None
    state: bool | None = None
    shown: float = 0.0    # the level actually drawn, easing towards `level`
    hover: float = 0.0    # 0 resting, 1 fully highlighted

    def icon_name(self) -> str:
        """A live microphone shows the slash — the icon is what a click does."""
        if self.toggle == "mic" and self.state is not None:
            return "$mic" if self.state else "$mic-off"
        return self.icon

    def caption(self) -> str:
        if self.dial and self.level is not None:
            return f"{self.label} — {round(self.level * 100)}%"
        if self.toggle == "mic" and self.state is not None:
            return f"{self.label} — {'muted' if self.state else 'live'}"
        return self.label


def parse_entry(label: str, value: str) -> Entry:
    """`icon::command` picks the icon by hand — a themed icon name or a drawn
    glyph ("$rocket"). `@brightness`, `@volume` and `@mic-mute` are the ring's
    own entries: the first two dial, the third toggles."""
    icon = ""
    command = value.strip()
    head, sep, tail = command.partition("::")
    if sep and head and all(c.isalnum() or c in "._+-$" for c in head):
        icon, command = head, tail.strip()

    dial = toggle = ""
    if command in ("@brightness", "@volume"):
        dial = command[1:]
        command = levels.settings_command(dial)
    elif command == "@mic-mute":
        toggle = "mic"
        command = levels.Dials.mic_toggle_command()
    if not (icon in GLYPHS or _icon_exists(icon)):
        icon = DIAL_ICONS.get(dial or toggle) or guess_icon(label, command)
    return Entry(label, icon, command, dial, toggle)


# -- shapes ---------------------------------------------------------------------

def _rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _desktop(display) -> Gdk.Rectangle:
    """Every monitor together, as one rectangle."""
    desk = None
    for i in range(display.get_n_monitors()):
        geo = display.get_monitor(i).get_geometry()
        desk = geo if desk is None else Gdk.rectangle_union(desk, geo)
    if desk is None:
        desk = Gdk.Rectangle()
        desk.width = desk.height = 1024
    return desk


def _soft_shadow(cr, path, layers: int = 4, step: float = 1.9, alpha: float = 0.055) -> None:
    """A cheap blur: the outline stroked a few times, wide and faint."""
    cr.save()
    cr.translate(0, 1.5)
    cr.set_source_rgba(0, 0, 0, alpha)
    for i in range(layers, 0, -1):
        path(cr)
        cr.set_line_width(i * step * 2)
        cr.stroke()
    cr.restore()


class ActionRing(Gtk.Window):
    """items: a list of Entry. `on_pick` gets the command of the entry chosen,
    `on_level` an (entry, fraction) pair whenever a dial is scrolled."""

    def __init__(self, items, on_pick, on_level=None, timeout_s: int = 10, scale: float = 1.0):
        # An override-redirect window, the kind menus use: a managed one asking
        # to span four monitors gets clamped to whichever monitor the window
        # manager thinks it belongs to, and the ring lands on the wrong screen.
        super().__init__(type=Gtk.WindowType.POPUP)
        self.items = items
        self.on_pick = on_pick
        self.on_level = on_level
        self.scale = min(max(scale, 0.5), 4.0)  # everything below is unscaled px
        self.hover = None          # int index, "close", or None
        self.picked = False
        self.seat = None
        self.phase = "in"
        self.t0 = time.monotonic()
        self.shown_once = False    # mapping a window takes ~200 ms: see _on_draw
        self.target = None         # where the ring belongs, in screen pixels
        self.frame = 0             # the running frame-clock callback, 0 when idle
        self.timeout_s = timeout_s
        self.timeout_id = 0
        self._buffer = None        # the ring is drawn here, then blitted once
        self._icon_cache: dict[tuple[str, bool], object] = {}

        n = max(len(items), 1)
        self.ring_r = max(104.0, (2 * ITEM_R + ITEM_GAP) * n / TAU)
        self.size = 2 * (self.ring_r + ITEM_R * HOVER_SCALE + PAD)
        self.reach = self.ring_r + ITEM_R * HOVER_SCALE + PAD   # ring half-width
        self.angles = [-math.pi / 2 + TAU * i / n for i in range(n)]
        for entry in items:
            entry.shown = entry.level or 0.0
        width = int(self.size * self.scale)
        height = int((self.size + LABEL_GAP + LABEL_H + PAD) * self.scale)
        self._place(self.size, self.size + LABEL_GAP + LABEL_H + PAD,
                    self.size / 2, self.size / 2)

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        self.set_app_paintable(True)
        self.set_default_size(width, height)
        self.set_size_request(width, height)

        screen = self.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        self.composited = bool(visual) and screen.is_composited()
        if visual:
            self.set_visual(visual)

        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.KEY_PRESS_MASK
                        | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.connect("draw", self._on_draw)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("button-press-event", self._on_click)
        self.connect("scroll-event", self._on_scroll)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)
        self.connect("map-event", lambda *_: self._grab())
        self.connect("configure-event", self._on_configure)
        self.connect("focus-out-event", lambda *_: self.close())

    # -- placement -------------------------------------------------------------

    def _place(self, win_w: float, win_h: float, cx: float, cy: float) -> None:
        """Put the ring's centre at (cx, cy) inside a window this big — all in
        unscaled pixels, the units everything below draws in."""
        self.win_w, self.win_h = win_w, win_h
        self.cx, self.cy = cx, cy
        self.positions = [(cx + self.ring_r * math.cos(a),
                           cy + self.ring_r * math.sin(a)) for a in self.angles]
        self.label_y = min(cy + self.reach + LABEL_GAP, win_h - LABEL_H - 8)

    def _dirty(self):
        """The ring's bounding box in widget pixels. The window covers the
        whole monitor, but repainting all of that every frame would not keep
        up — only this much is ever drawn, so only this much is invalidated."""
        top = (self.cy - self.reach - 8) * self.scale
        bottom = (self.label_y + LABEL_H + 12) * self.scale
        return (int((self.cx - self.reach - 8) * self.scale), int(top),
                int((self.reach + 8) * 2 * self.scale) + 2, int(bottom - top) + 2)

    def _invalidate(self) -> None:
        self.queue_draw_area(*self._dirty())

    def pop_up(self, at: tuple[int, int] | None = None) -> None:
        """Open the ring centred on `at` (screen pixels), on an overlay that
        covers the monitor — that way a click anywhere reaches us, and one
        beyond the buttons can dismiss the ring. Without a point, and without
        a compositor to make the overlay transparent, it opens centred in a
        window of its own size instead."""
        display = self.get_display()
        monitor = None
        if display is not None:
            monitor = (display.get_monitor_at_point(*at) if at is not None
                       else display.get_primary_monitor() or display.get_monitor(0))
        if monitor is not None and self.composited:
            # the overlay spans every monitor, so a click anywhere on the desk
            # lands on it; the ring itself stays inside the one it opened on
            desk = _desktop(display)
            self.set_size_request(desk.width, desk.height)
            self.resize(desk.width, desk.height)
            self.move(desk.x, desk.y)
            here = monitor.get_geometry()
            px = at[0] if at is not None else here.x + here.width / 2
            py = at[1] if at is not None else here.y + here.height / 2
            edge = self.reach + 4                    # keep the whole ring on screen
            px = min(max(px, here.x + edge), max(here.x + edge, here.x + here.width - edge))
            py = min(max(py, here.y + edge),
                     max(here.y + edge, here.y + here.height - edge - LABEL_GAP - LABEL_H))
            self.target = (px, py)
            self._centre(desk.width, desk.height, px - desk.x, py - desk.y)
        elif at is not None:
            self.move(int(at[0] - self.size * self.scale / 2),
                      int(at[1] - self.size * self.scale / 2))
        else:
            self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.t0 = time.monotonic()
        self.show_all()
        self.present()
        self._arm_timeout()
        self._kick()

    def _centre(self, win_w: float, win_h: float, cx: float, cy: float) -> None:
        """Lay the ring out at (cx, cy) of a window this big, all in window
        pixels, nudged so that none of it falls outside."""
        edge = (self.reach + 4) * self.scale
        cx = min(max(cx, edge), max(edge, win_w - edge))
        cy = min(max(cy, edge), max(edge, win_h - edge - (LABEL_GAP + LABEL_H) * self.scale))
        self._place(win_w / self.scale, win_h / self.scale, cx / self.scale, cy / self.scale)

    def _on_configure(self, _w, _event) -> bool:
        """Keep the ring on its target point whatever geometry we ended up
        with — a window manager may not have granted what we asked for."""
        window = self.get_window()
        if self.target is None or window is None:
            return False
        _ok, ox, oy = window.get_origin()
        self._centre(window.get_width(), window.get_height(),
                     self.target[0] - ox, self.target[1] - oy)
        self._invalidate()
        return False

    def _grab(self, tries: int = 0) -> bool:
        """Take the pointer and keyboard the way a menu does, so a click
        outside reaches us (and dismisses) instead of the window below. A
        freshly mapped window is not always grabbable yet, hence the retries."""
        if self.seat is not None or self.phase == "out":
            return False
        display, window = self.get_display(), self.get_window()
        seat = display.get_default_seat() if display else None
        status = None
        if seat is not None and window is not None:
            try:
                status = seat.grab(window, Gdk.SeatCapabilities.ALL, True, None, None, None, None)
            except Exception:
                status = None
        if status == Gdk.GrabStatus.SUCCESS:
            self.seat = seat
        elif tries < 12:
            GLib.timeout_add(25, self._grab, tries + 1)
        return False

    def _arm_timeout(self) -> None:
        """Nobody wants a forgotten ring on screen; the clock restarts on use."""
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
        self.timeout_id = (GLib.timeout_add_seconds(self.timeout_s, self._on_timeout)
                           if self.timeout_s else 0)

    def _on_timeout(self) -> bool:
        self.timeout_id = 0
        self.close()
        return False

    # -- animation ---------------------------------------------------------------

    def refresh(self) -> None:
        """Levels arrived after the ring opened: draw them, arcs sweeping up."""
        self._kick()

    def _kick(self) -> None:
        """Animate off the widget's frame clock, which paces itself to the
        compositor instead of queueing frames it cannot present."""
        if not self.frame:
            self.frame = self.add_tick_callback(self._on_frame)

    def _on_frame(self, _widget, _clock) -> bool:
        if self._tick():
            return GLib.SOURCE_CONTINUE
        self.frame = 0
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        now = time.monotonic()
        busy = self._settle(now)
        if self.phase == "out" and (now - self.t0) * 1000 >= CLOSE_MS:
            self.frame = 0
            self.destroy()
            return False
        self._invalidate()
        if busy:
            return True
        self.frame = 0
        return False

    def _settle(self, now: float) -> bool:
        """Ease every animated value one frame on; True while anything moves."""
        span = OPEN_MS + STAGGER_MS * len(self.items) if self.phase == "in" else CLOSE_MS
        busy = (now - self.t0) * 1000 < span
        for i, entry in enumerate(self.items):
            target = 1.0 if self.hover == i else 0.0
            if entry.hover != target:
                step = FRAME_MS / HOVER_MS
                entry.hover = (min(entry.hover + step, target) if target > entry.hover
                               else max(entry.hover - step, target))
                busy = True
            if entry.level is not None and abs(entry.shown - entry.level) > 0.002:
                entry.shown += (entry.level - entry.shown) * clamp(FRAME_MS / LEVEL_MS * 2)
                busy = True
        return busy

    def _spread(self, i: int) -> float:
        """How far out of the centre button `i` has travelled, 0 to 1."""
        elapsed = (time.monotonic() - self.t0) * 1000
        if self.phase == "out":
            return 1 - ease_in(clamp(elapsed / CLOSE_MS))
        return ease_out_back(clamp((elapsed - i * STAGGER_MS) / OPEN_MS))

    def _fade(self) -> float:
        elapsed = (time.monotonic() - self.t0) * 1000
        if self.phase == "out":
            return 1 - clamp(elapsed / CLOSE_MS)
        return ease_out(clamp(elapsed / (OPEN_MS * 0.6)))

    def close(self) -> None:
        """Fall back into the centre and disappear."""
        if self.phase == "out":
            return
        self.phase = "out"
        self.t0 = time.monotonic()
        self.hover = None
        self._release()
        self._kick()

    def _release(self) -> None:
        """Let go of the pointer and stop swallowing clicks — whatever we just
        launched should get the focus, not a window in the middle of dying."""
        if self.seat is not None:
            self.seat.ungrab()
            self.seat = None
        window = self.get_window()
        if window is not None:
            window.input_shape_combine_region(cairo.Region(), 0, 0)

    # -- input -----------------------------------------------------------------

    def _hit(self, mx: float, my: float):
        """The entry under (or pointed at by) the pointer; None means empty
        space, where a click dismisses the ring."""
        mx, my = mx / self.scale, my / self.scale
        dx, dy = mx - self.cx, my - self.cy
        dist = math.hypot(dx, dy)
        if dist <= CLOSE_R + 8:
            return "close"
        for i, (x, y) in enumerate(self.positions[:len(self.items)]):
            if math.hypot(mx - x, my - y) <= ITEM_R * HOVER_SCALE:
                return i
        if dist < DEADZONE or dist > self.ring_r + SECTOR_REACH or not self.items:
            return None
        n = len(self.items)
        angle = math.atan2(dy, dx) + math.pi / 2
        return int(round(angle / TAU * n)) % n

    def _set_hover(self, hover) -> None:
        if hover != self.hover:
            self.hover = hover
            self._kick()

    def _on_motion(self, _w, event) -> bool:
        if self.phase != "out":
            self._set_hover(self._hit(event.x, event.y))
        return True

    def _on_click(self, _w, event) -> bool:
        if self.phase == "out":
            return True
        if event.button == 1 and isinstance(self.hover, int):
            self._pick(self.hover)
        else:            # the "×", or anywhere off the ring
            self.close()
        return True

    def _on_scroll(self, _w, event) -> bool:
        """Scrolling over a dial moves it; over anything else, nothing."""
        if self.phase == "out" or not isinstance(self.hover, int):
            return True
        entry = self.items[self.hover]
        if not entry.dial or entry.level is None:
            return True
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, _dx, dy = event.get_scroll_deltas()
            step = -dy * 0.05
        elif event.direction == Gdk.ScrollDirection.UP:
            step = 0.05
        elif event.direction == Gdk.ScrollDirection.DOWN:
            step = -0.05
        else:
            return True
        entry.level = clamp(entry.level + step)
        if self.on_level is not None:
            self.on_level(entry, entry.level)
        self._arm_timeout()
        self._kick()
        return True

    def _on_key(self, _w, event) -> bool:
        key = event.keyval
        if self.phase == "out":
            return True
        if key in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.close()
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) and isinstance(self.hover, int):
            self._pick(self.hover)
        elif Gdk.KEY_1 <= key <= Gdk.KEY_9 and key - Gdk.KEY_1 < len(self.items):
            self._pick(key - Gdk.KEY_1)
        elif key in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Tab, Gdk.KEY_Left, Gdk.KEY_Up):
            step = 1 if key in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Tab) else -1
            index = self.hover if isinstance(self.hover, int) else (0 if step > 0 else 1)
            self._set_hover((index + step) % len(self.items))
        return True

    def _pick(self, index: int) -> None:
        if self.picked:
            return
        self.picked = True
        command = self.items[index].command
        self.close()   # ungrabs first, so the command's window can take focus
        if command:
            GLib.idle_add(lambda: (self.on_pick(command), False)[1])

    def _on_destroy(self, _w) -> None:
        if self.seat is not None:
            self.seat.ungrab()
            self.seat = None
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = 0
        if self.frame:
            self.remove_tick_callback(self.frame)
            self.frame = 0

    # -- painting ----------------------------------------------------------------

    def _icon(self, name: str, hovered: bool):
        """A themed icon, for the entries no drawn glyph covers."""
        key = (name, hovered)
        if key in self._icon_cache:
            return self._icon_cache[key]
        pixbuf = None
        info = Gtk.IconTheme.get_default().lookup_icon(name, int(ICON_PX),
                                                       Gtk.IconLookupFlags.FORCE_SIZE)
        if info is not None:
            rgba = Gdk.RGBA(*(FG_ITEM_HOVER if hovered else FG_ITEM))
            try:
                if name.endswith("-symbolic"):
                    pixbuf, _ = info.load_symbolic(rgba, None, None, None)
                else:
                    pixbuf = info.load_icon()
            except GLib.Error:
                pixbuf = None
        self._icon_cache[key] = pixbuf
        return pixbuf

    def _layout(self, cr, text: str, size: float, bold: bool = False):
        layout = PangoCairo.create_layout(cr)
        PangoCairo.context_set_font_options(layout.get_context(), FONT_OPTS)
        desc = Pango.FontDescription()
        desc.set_family("Sans")
        desc.set_absolute_size(size * Pango.SCALE)
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        return layout

    def _text(self, cr, text: str, x: float, y: float, size: float, bold: bool, rgba) -> None:
        layout = self._layout(cr, text, size, bold)
        w, h = layout.get_pixel_size()
        cr.set_source_rgba(*rgba)
        cr.move_to(x - w / 2, y - h / 2)
        PangoCairo.show_layout(cr, layout)

    def _draw_item(self, cr, i: int, entry: Entry) -> None:
        spread = self._spread(i)
        grown = clamp(spread)
        angle = self.angles[i] + (1 - spread) * OPEN_SPIN     # unwinds on the way out
        distance = self.ring_r * spread + entry.hover * ITEM_LIFT
        x = self.cx + distance * math.cos(angle)
        y = self.cy + distance * math.sin(angle)
        r = ITEM_R * (1 + (HOVER_SCALE - 1) * entry.hover) * (0.4 + 0.6 * grown)
        fg = mix(FG_ITEM, FG_ITEM_HOVER, entry.hover)
        bg = mix(BG_ITEM, BG_ITEM_HOVER, entry.hover)

        def circle(c):
            c.new_sub_path()
            c.arc(x, y, r, 0, TAU)

        # the same shadow whatever the state: a heavier one on the highlighted
        # button fights the level arc drawn just outside it
        _soft_shadow(cr, circle)
        cr.set_source_rgba(*bg)
        circle(cr)
        cr.fill_preserve()
        cr.set_source_rgba(*mix(EDGE_ITEM, EDGE_ITEM_HOVER, entry.hover))
        cr.set_line_width(1.0)
        cr.stroke()

        if entry.dial and entry.level is not None:
            self._draw_arc(cr, x, y, r + ARC_GAP, entry, grown)

        cr.save()
        cr.translate(x, y)
        glyph = GLYPHS.get(entry.icon_name())
        pixbuf = None if glyph else (self._icon(entry.icon, entry.hover > 0.5)
                                     if entry.icon else None)
        if grown < 0.02:       # scaling to nothing is not a matrix cairo accepts
            cr.restore()
            return
        if glyph is not None:
            unit = ICON_PX / 24 * grown
            cr.scale(unit, unit)
            _pen(cr)
            cr.set_source_rgba(*fg)
            glyph(cr, fg, bg)
        elif pixbuf is not None:
            cr.scale(grown, grown)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, -pixbuf.get_width() / 2,
                                        -pixbuf.get_height() / 2)
            cr.paint()
        else:
            self._text(cr, (entry.label.strip()[:1] or "•").upper(), 0, 0,
                       21 * grown, True, fg)
        cr.restore()

    def _draw_arc(self, cr, x: float, y: float, radius: float, entry: Entry, grown: float) -> None:
        """The level, as a ring around the button: a full circle at 100%."""
        cr.set_line_width(ARC_W)
        cr.set_line_cap(cairo.LineCap.ROUND)
        cr.set_source_rgba(*mix(ARC_TRACK, ARC_TRACK_HOVER, entry.hover))
        cr.new_sub_path()
        cr.arc(x, y, radius, 0, TAU)
        cr.stroke()
        level = clamp(entry.shown) * grown
        if level <= 0.002:
            return
        cr.set_source_rgba(*mix(ARC_FILL, ARC_FILL_HOVER, entry.hover))
        cr.new_sub_path()
        cr.arc(x, y, radius, -math.pi / 2, -math.pi / 2 + TAU * level)
        cr.stroke()

    def _draw_label(self, cr) -> None:
        if not isinstance(self.hover, int):
            return
        entry = self.items[self.hover]
        layout = self._layout(cr, entry.caption(), 13)
        tw, th = layout.get_pixel_size()
        pill_w = min(tw + 30, self.reach * 2 - 16)
        pill_x = self.cx - pill_w / 2
        pill_y = self.label_y - 6 * (1 - entry.hover)   # slides up into place

        def pill(c):
            _rounded(c, pill_x, pill_y, pill_w, LABEL_H, LABEL_H / 2)

        cr.push_group()
        _soft_shadow(cr, pill)
        cr.set_source_rgba(*BG_LABEL)
        pill(cr)
        cr.fill()
        cr.set_source_rgba(*FG_LABEL)
        cr.move_to(self.cx - tw / 2, pill_y + (LABEL_H - th) / 2)
        PangoCairo.show_layout(cr, layout)
        cr.pop_group_to_source()
        cr.paint_with_alpha(clamp(entry.hover))

    def _on_draw(self, _w, cr) -> bool:
        """Draw the ring into an image buffer and blit that once.

        The window spans the whole desk so that a click anywhere lands on it,
        but painting into it directly means every soft shadow is stroked
        through XRender — 50 ms a frame, far too slow to animate. In an image
        surface the same frame costs about 5 ms, and only the ring's own box
        is ever uploaded."""
        if not self.shown_once:
            # Mapping a window costs a couple of hundred milliseconds — long
            # enough for the whole opening animation to play into a window
            # nobody can see yet. The clock starts on the first real frame.
            self.shown_once = True
            self.t0 = time.monotonic() + START_DELAY / 1000
            self._kick()
        x, y, w, h = self._dirty()
        if w <= 0 or h <= 0:
            return True
        if (self._buffer is None or self._buffer.get_width() != w
                or self._buffer.get_height() != h):
            self._buffer = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        buf = cairo.Context(self._buffer)
        buf.translate(-x, -y)
        self._paint(buf)
        cr.set_operator(cairo.Operator.SOURCE)
        cr.set_source_surface(self._buffer, x, y)
        cr.rectangle(x, y, w, h)
        cr.fill()
        return True

    def _paint(self, cr) -> None:
        cr.set_operator(cairo.Operator.SOURCE)
        if self.composited:
            cr.set_source_rgba(0, 0, 0, 0)
        else:  # no compositor: a plain backdrop beats a black square
            cr.set_source_rgba(0.13, 0.13, 0.13, 1)
        cr.paint()
        cr.set_operator(cairo.Operator.OVER)
        cr.scale(self.scale, self.scale)

        # the group exists only to fade the ring in and out as one; once it is
        # fully there, skipping it saves an allocation and a composite a frame
        fade = clamp(self._fade())
        grouped = fade < 0.999
        if grouped:
            cr.push_group()
        for i, entry in enumerate(self.items):
            self._draw_item(cr, i, entry)

        closing = self.hover == "close"
        shrink = clamp(self._spread(0))
        cr.set_source_rgba(*(BG_CLOSE_HOVER if closing else BG_CLOSE))
        cr.arc(self.cx, self.cy, CLOSE_R * shrink, 0, TAU)
        cr.fill()
        cr.set_source_rgba(*((1, 1, 1, 1) if closing else (0.35, 0.35, 0.35, 1)))
        cr.set_line_width(1.8)
        cr.set_line_cap(cairo.LineCap.ROUND)
        for sx in (-1, 1):
            cr.move_to(self.cx - 4.5 * sx * shrink, self.cy - 4.5 * shrink)
            cr.line_to(self.cx + 4.5 * sx * shrink, self.cy + 4.5 * shrink)
            cr.stroke()

        self._draw_label(cr)
        if grouped:
            cr.pop_group_to_source()
            cr.paint_with_alpha(fade)
