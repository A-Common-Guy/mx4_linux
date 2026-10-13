"""Where is the pointer? On Wayland a client may not ask — so ask KWin.

X11 answers this in one call, and under XWayland it answers *stale*: the X
server only learns where the pointer is while it hovers an X window, so on a
Plasma desktop the position is usually minutes old and somewhere else.

KWin knows, and its scripting engine can be made to tell us: a three-line
script, loaded once, registers a global shortcut that reports
`workspace.cursorPos` back over D-Bus. Invoking that shortcut and waiting for
the reply costs a handful of milliseconds — fast enough to do on the way to
opening the actions ring.
"""

from __future__ import annotations

import os
from pathlib import Path

from gi.repository import Gdk, GLib

from . import __version__

BUS_NAME = "org.mx4ctl.Cursor"
OBJ_PATH = "/cursor"
INTERFACE = "org.mx4ctl.Cursor"
# KWin will not re-register the shortcut of a script it already knows, and
# unloading leaves the old action behind — so the name carries the version,
# and a new build simply gets a name KWin has never seen
PREFIX = "mx4ctl-cursor"
SCRIPT_NAME = SHORTCUT = f"{PREFIX}-{__version__}"

# KWin's callDBus takes plain arguments, so everything travels as one string
SCRIPT = """// installed by mx4ctl — reports the pointer position on request
registerShortcut("%(shortcut)s", "mx4ctl: locate the pointer", "", function () {
    var p = workspace.cursorPos, s = workspace.virtualScreenSize;
    callDBus("%(bus)s", "%(path)s", "%(iface)s", "Report",
             p.x + " " + p.y + " " + s.width + " " + s.height);
});
""" % {"shortcut": SHORTCUT, "bus": BUS_NAME, "path": OBJ_PATH, "iface": INTERFACE}


def gdk_pointer() -> tuple[int, int] | None:
    """The pointer according to the display server — right on X11, stale under
    XWayland (see the module docstring)."""
    display = Gdk.Display.get_default()
    seat = display.get_default_seat() if display else None
    pointer = seat.get_pointer() if seat else None
    if pointer is None:
        return None
    _screen, x, y = pointer.get_position()
    return x, y


def _screen_size() -> tuple[int, int]:
    """The X screen in pixels — the union of the monitors."""
    display = Gdk.Display.get_default()
    width = height = 0
    for i in range(display.get_n_monitors() if display else 0):
        geo = display.get_monitor(i).get_geometry()
        width, height = max(width, geo.x + geo.width), max(height, geo.y + geo.height)
    return width, height


class KWinCursor:
    """Pointer position from KWin, over D-Bus. `start()` once, `position()`
    per use; every failure degrades to None rather than raising."""

    def __init__(self, log=print):
        self.log = log
        self.bus = None
        self.name = None      # the BusName must stay referenced or it is released
        self.service = None
        self.loaded = False
        self.reply: str | None = None
        self.loop: GLib.MainLoop | None = None
        self.timer = 0
        self.ready = False    # set by the warm-up: the shortcut really answers
        self.failures = 0     # three in a row and we stop asking (no KWin here)

    # -- setup ------------------------------------------------------------------

    def start(self) -> bool:
        """Claim our bus name and hand KWin the script. Safe to call again."""
        if self.service is None and not self._serve():
            return False
        if not self.loaded:
            self._load_script()
        return self.loaded

    def _serve(self) -> bool:
        try:
            import dbus
            import dbus.service
            from dbus.mainloop.glib import DBusGMainLoop
        except ImportError:
            self.log("dbus-python not available — the ring cannot follow the cursor on wayland")
            return False
        DBusGMainLoop(set_as_default=True)
        outer = self

        class Service(dbus.service.Object):
            @dbus.service.method(INTERFACE, in_signature="s", out_signature="")
            def Report(self, blob):
                outer._on_report(str(blob))

        try:
            self.bus = dbus.SessionBus()
            # take the name outright: queueing behind another daemon would look
            # like success while every reply went to the process ahead of us
            self.name = dbus.service.BusName(BUS_NAME, self.bus, allow_replacement=True,
                                             replace_existing=True, do_not_queue=True)
            self.service = Service(self.bus, OBJ_PATH)
        except Exception as e:
            self.log(f"cursor service failed: {e}")
            self.bus = self.name = self.service = None
            return False
        return True

    def _script_path(self) -> str:
        """KWin reads the file at load time; keep it somewhere private."""
        base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
        path = base / "mx4ctl-cursor.js"
        path.write_text(SCRIPT)
        return str(path)

    def _load_script(self) -> None:
        try:
            scripting = self.bus.get_object("org.kde.KWin", "/Scripting")
            # our own script from a previous daemon run is still in there and
            # still works — it calls whoever owns the bus name, which is us now
            if not scripting.isScriptLoaded(SCRIPT_NAME,
                                            dbus_interface="org.kde.kwin.Scripting"):
                scripting.loadScript(self._script_path(), SCRIPT_NAME,
                                     dbus_interface="org.kde.kwin.Scripting", signature="ss")
                scripting.start(dbus_interface="org.kde.kwin.Scripting")
            self._forget_old_shortcuts()
        except Exception as e:
            self.log(f"kwin cursor script not loaded ({e}) — the ring will open centred")
            self.loaded = False
            return
        self.loaded = True
        self.ready = False
        # kglobalaccel needs a moment to publish the shortcut the script just
        # registered, so prove the round trip once now rather than on the first
        # press — that one would otherwise time out and open the ring centred
        GLib.timeout_add(800, self._warm_up)
        self.log("kwin cursor script loaded — the ring opens at the pointer")

    def _forget_old_shortcuts(self) -> None:
        """Drop the shortcuts earlier versions left in the Shortcuts KCM."""
        try:
            accel = self.bus.get_object("org.kde.kglobalaccel", "/kglobalaccel")
            component = self.bus.get_object("org.kde.kglobalaccel", "/component/kwin")
            names = component.shortcutNames(dbus_interface="org.kde.kglobalaccel.Component")
            for name in (str(n) for n in names):
                if name.startswith(PREFIX) and name != SHORTCUT:
                    accel.unregister("kwin", name, dbus_interface="org.kde.KGlobalAccel")
        except Exception:
            pass  # tidiness only

    def _warm_up(self) -> bool:
        """Prove the round trip once, so the first press doesn't discover a
        shortcut kglobalaccel had not published yet. `ready` is set by the
        reply itself, in _on_report."""
        self.reply = None
        self._invoke()
        return False

    # -- use --------------------------------------------------------------------

    def position(self, timeout_ms: int = 250) -> tuple[int, int] | None:
        """Ask KWin where the pointer is, in X pixels. None if it won't say."""
        if self.failures >= 3 or self.loop is not None:  # given up, or already asking
            return None
        if not self.start():
            self._failed()
            return None
        self.reply = None
        if not self._invoke():
            return None

        # the answer arrives as an incoming method call, so let the loop run
        self.loop = GLib.MainLoop()
        self.timer = GLib.timeout_add(timeout_ms, self._give_up)
        self.loop.run()
        self.loop = None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.reply is None:
            self.loaded = False
            self._failed()
            return None
        self.failures = 0
        return self._to_x_pixels(self.reply)

    def _invoke(self) -> bool:
        """Poke the shortcut the KWin script registered."""
        try:
            component = self.bus.get_object("org.kde.kglobalaccel", "/component/kwin")
            component.invokeShortcut(SHORTCUT, dbus_interface="org.kde.kglobalaccel.Component")
        except Exception as e:
            self.log(f"cursor query failed: {e}")
            self.loaded = False  # KWin may have restarted; reload on the next try
            self._failed()
            return False
        return True

    def _failed(self) -> None:
        self.failures += 1
        if self.failures == 3:
            self.log("no cursor position from kwin — the ring will open centred from now on")

    def _on_report(self, blob: str) -> None:
        self.reply = blob
        self.ready = True
        if self.loop is not None:
            self.loop.quit()

    def _give_up(self) -> bool:
        self.timer = 0
        self.log("kwin did not report the cursor in time — opening the ring centred")
        if self.loop is not None:
            self.loop.quit()
        return False

    @staticmethod
    def _to_x_pixels(blob: str) -> tuple[int, int] | None:
        """KWin counts in logical pixels; X may count in device ones."""
        try:
            x, y, vw, vh = (float(n) for n in blob.split())
        except ValueError:
            return None
        sw, sh = _screen_size()
        if vw > 0 and sw > 0:
            x *= sw / vw
        if vh > 0 and sh > 0:
            y *= sh / vh
        return int(round(x)), int(round(y))
