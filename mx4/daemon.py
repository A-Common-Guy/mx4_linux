"""mx4ctl daemon: notification haptics, thumb-button actions, battery buzz.

Runs a GLib main loop that
  * watches the hidraw fd for diverted-button HID++ events,
  * monitors the session D-Bus for org.freedesktop.Notifications.Notify,
  * polls the battery and buzzes when it gets low,
  * re-applies diversion + haptic level whenever the mouse reconnects.
"""

from __future__ import annotations

import configparser
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# The ring belongs at the cursor, and only an X11/XWayland window can be put
# there by a daemon. PyGObject opens the display the moment gi.repository.Gtk
# is imported, so the backend has to be chosen up here, before that happens.
# GDK_BACKEND=wayland in the environment opts out (the ring then opens centred).
if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
    os.environ.setdefault("GDK_BACKEND", "x11")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import cursor, hidpp, levels, ring, uinput  # noqa: E402
from .mouse import CONTROLS, MX4, parse_waveform  # noqa: E402

DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mx4ctl" / "config.ini"

# "@name" actions in the config resolve per session type: key combos via
# uinput (any compositor), or xdotool on X11 when uinput is unavailable
BUILTIN_KEYS = {
    "workspace-prev": (uinput.KEY_LEFTMETA, uinput.KEY_PAGEUP),
    "workspace-next": (uinput.KEY_LEFTMETA, uinput.KEY_PAGEDOWN),
    "overview": (uinput.KEY_LEFTMETA,),
    "show-desktop": (uinput.KEY_LEFTMETA, uinput.KEY_D),
    "minimize": (uinput.KEY_LEFTMETA, uinput.KEY_H),
    "screenshot": (uinput.KEY_SYSRQ,),
    "play-pause": (uinput.KEY_PLAYPAUSE,),
}
BUILTIN_X11 = {
    "workspace-prev": "xdotool set_desktop --relative -- -1",
    "workspace-next": "xdotool set_desktop --relative 1",
    "overview": "xdotool key super",
    "show-desktop": "xdotool key super+d",
    "minimize": "xdotool getactivewindow windowminimize",
    "screenshot": "xdotool key Print",
    "play-pause": "xdotool key XF86AudioPlay",
}

DEFAULTS = {
    "haptics": {"level": ""},
    "button": {
        "press": "menu",
        "press_command": "",
        "press_waveform": "sharp_state_change",
        "long_press": "command",
        "long_press_command": "",
        "long_press_waveform": "knock",
        "long_press_ms": "450",
        "menu_select_waveform": "damp_state_change",
        "menu_scale": "1.0",
        "force_threshold": "",
    },
    "notifications": {"enabled": "true", "respect_dnd": "true", "default": "subtle_collision"},
    "battery": {"low_percent": "10", "waveform": "mad"},
    "gestures": {
        "enabled": "true",
        "threshold": "100",  # sensor counts; ~2.5 mm at 1000 dpi
        "waveform": "damp_state_change",
        "left": "@workspace-prev",
        "right": "@workspace-next",
        "up": "@show-desktop",
        "down": "@minimize",
        "tap": "@overview",
    },
    "workspace": {"enabled": "true", "waveform": "whisper_collision"},
}


class Daemon:
    def __init__(self, config_path: str | None, verbose: bool):
        self.verbose = verbose
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.optionxform = str  # keep menu label case
        for section, values in DEFAULTS.items():
            self.cfg[section] = dict(values)
        path = Path(config_path) if config_path else DEFAULT_CONFIG
        if not path.exists() and not config_path:
            self._seed_config(path)
        if path.exists():
            self.cfg.read(path)
            self.log(f"config: {path}")
        else:
            self.log(f"config: {path} not found, using defaults")

        self.mouse: MX4 | None = None
        self.ctl_idx = 0
        self.watch_id = 0
        self.keys_down: set[int] = set()
        self.long_timer = 0
        self.long_fired = False
        self.last_play = 0.0
        self.battery_warned = False
        self.gesturing = False
        self.gest_x = 0
        self.gest_y = 0
        self.dnd_settings = None
        self.keyboard = None  # lazily created uinput device; False = unavailable
        self._ring = None     # the open actions ring, if any
        self.dials = levels.Dials(self.log)
        display = Gdk.Display.get_default()
        # the session type decides how keys are injected; the GDK backend (which
        # may be XWayland — see run()) decides whether the ring can be placed
        self.is_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or bool(
            display and "wayland" in type(display).__name__.lower())
        self.gdk_wayland = bool(display and "wayland" in type(display).__name__.lower())
        # on wayland only the compositor knows where the pointer is (and only
        # an X11/XWayland window can be put there) — see mx4/cursor.py
        self.cursor = None
        if self.is_wayland and not self.gdk_wayland:
            self.cursor = cursor.KWinCursor(self.log)
        elif self.gdk_wayland:
            self.log("actions ring: centred — a native wayland client cannot place a window")
        self.loop = GLib.MainLoop()

    def _seed_config(self, path: Path) -> None:
        """First run: give the user a commented config to edit."""
        for example in (Path("/usr/share/mx4ctl/config.example.ini"),
                        Path(__file__).resolve().parent.parent / "config.example.ini"):
            if example.exists():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(example, path)
                    self.log(f"created {path} from {example}")
                except OSError:
                    pass
                return

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def debug(self, msg: str) -> None:
        if self.verbose:
            self.log(msg)

    # -- device ---------------------------------------------------------------

    def connect_mouse(self) -> bool:
        try:
            self.mouse = MX4(hidpp.find_device())
        except hidpp.HidppError as e:
            self.debug(f"discovery failed: {e}")
            return False
        m = self.mouse
        self.ctl_idx = m.controls_feature_index()
        self.apply_device_config()
        self.watch_id = GLib.io_add_watch(
            m.dev.channel.fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_fd
        )
        self.log(f"connected: {m.dev.channel.path} index {m.dev.index}")
        return True

    def apply_device_config(self) -> None:
        """(Re-)apply everything that resets when the mouse reconnects."""
        m = self.mouse
        level = self.cfg["haptics"]["level"].strip()
        if level:
            m.set_haptic_level(int(level))
        force = self.cfg["button"]["force_threshold"].strip()
        if force:
            try:
                m.set_force(int(force))
            except (ValueError, hidpp.HidppError) as e:
                self.log(f"force_threshold not applied: {e}")
        m.divert(CONTROLS["haptic"], True)
        if self.cfg["gestures"].getboolean("enabled"):
            m.divert(CONTROLS["gesture"], True, raw_xy=True)
        self.debug("buttons diverted, settings applied")

    def _reinit(self, attempt: int = 0) -> bool:
        """Re-apply volatile settings after the mouse reconnects. The first
        packets on a freshly woken link are often dropped, so retry with
        backoff instead of giving up."""
        if not self.mouse:
            return False
        try:
            self.apply_device_config()
            self.log("mouse reconnected — settings re-applied")
        except OSError:
            self._reconnect_later()
        except hidpp.HidppError as e:
            if attempt >= 5:
                self.log(f"re-apply failed repeatedly ({e}) — will retry on the periodic check")
                return False
            delay = (2, 3, 5, 10, 20)[attempt]
            self.log(f"re-apply failed ({e}) — retrying in {delay}s")
            GLib.timeout_add_seconds(delay, self._reinit, attempt + 1)
        return False  # one-shot; retries are scheduled explicitly

    def _reconnect_later(self) -> None:
        if self.watch_id:
            GLib.source_remove(self.watch_id)
            self.watch_id = 0
        if self.mouse:
            self.mouse.dev.channel.close()
            self.mouse = None
        self.log("mouse lost — retrying every 5s")
        GLib.timeout_add_seconds(5, lambda: not self.connect_mouse())

    # -- HID++ events -----------------------------------------------------------

    def _on_fd(self, _fd, cond) -> bool:
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            self._reconnect_later()
            return False
        try:
            reports = self.mouse.dev.channel.read_pending()
        except OSError:
            self._reconnect_later()
            return False
        for data in reports:
            self._on_report(data)
        return True

    def _on_report(self, data: bytes) -> None:
        dev = self.mouse.dev
        if data[1] != dev.index:
            return
        # receiver "device connection" notification: diversion was reset
        if data[0] == hidpp.REPORT_SHORT and data[2] == 0x41:
            GLib.timeout_add(1500, self._reinit)
            return
        if data[0] != hidpp.REPORT_LONG or (data[3] & 0x0F) != 0x00:
            return
        if data[2] == self.ctl_idx and (data[3] >> 4) == 0x00:  # divertedButtonsEvent
            self._on_keys(MX4.decode_keys_down(data[4:]))
        elif data[2] == self.ctl_idx and (data[3] >> 4) == 0x01:  # divertedRawMouseXY
            if self.gesturing:
                dx, dy = MX4.decode_raw_xy(data[4:])
                self.gest_x += dx
                self.gest_y += dy

    def _on_keys(self, now_down: set[int]) -> None:
        for cid in now_down - self.keys_down:
            if cid == CONTROLS["haptic"]:
                self._on_press()
            elif cid == CONTROLS["gesture"]:
                self.gesturing = True
                self.gest_x = self.gest_y = 0
        for cid in self.keys_down - now_down:
            if cid == CONTROLS["haptic"]:
                self._on_release()
            elif cid == CONTROLS["gesture"]:
                self.gesturing = False
                self._on_gesture()
        self.keys_down = now_down

    def _on_gesture(self) -> None:
        gx, gy, cfg = self.gest_x, self.gest_y, self.cfg["gestures"]
        threshold = cfg.getint("threshold")
        if abs(gx) < threshold and abs(gy) < threshold:
            direction = "tap"
        elif abs(gx) >= abs(gy):
            direction = "right" if gx > 0 else "left"
        else:
            direction = "down" if gy > 0 else "up"
        self.debug(f"gesture {direction} (dx={gx} dy={gy})")
        command = cfg[direction].strip()
        if command:
            self.play(cfg["waveform"])
            self._do(command)

    def _on_press(self) -> None:
        self.long_fired = False
        ms = self.cfg["button"].getint("long_press_ms")
        self.long_timer = GLib.timeout_add(ms, self._on_long_press)
        self.debug("haptic button pressed")

    def _on_release(self) -> None:
        if self.long_timer:
            GLib.source_remove(self.long_timer)
            self.long_timer = 0
        if not self.long_fired:
            self._run_action(self.cfg["button"]["press"], self.cfg["button"]["press_waveform"],
                             self.cfg["button"]["press_command"])
        self.debug("haptic button released")

    def _on_long_press(self) -> bool:
        self.long_timer = 0
        self.long_fired = True
        self._run_action(self.cfg["button"]["long_press"], self.cfg["button"]["long_press_waveform"],
                         self.cfg["button"]["long_press_command"])
        return False

    # -- actions ---------------------------------------------------------------

    def play(self, waveform: str) -> None:
        if not waveform or not self.mouse:
            return
        now = time.monotonic()
        if now - self.last_play < 0.25:  # don't machine-gun the haptic engine
            return
        self.last_play = now
        try:
            self.mouse.play(parse_waveform(waveform))
        except (ValueError, hidpp.HidppError) as e:
            self.debug(f"play failed: {e}")

    def _run_action(self, kind: str, waveform: str, command: str) -> None:
        self.play(waveform)
        if kind == "menu":
            self._show_menu()
        elif kind == "command":
            self._do(command)
        self.debug(f"action: {kind} {command}".rstrip())

    def _do(self, action: str) -> None:
        """Run a config action: '@name' built-in or a shell command."""
        action = action.strip()
        if not action:
            return
        if action.startswith("@"):
            self._builtin(action[1:])
        else:
            self._spawn(action)

    def _builtin(self, name: str) -> None:
        keys = BUILTIN_KEYS.get(name)
        if keys is None:
            self.log(f"unknown built-in action @{name} (known: {', '.join(sorted(BUILTIN_KEYS))})")
            return
        if self.keyboard is None:
            try:
                self.keyboard = uinput.VirtualKeyboard()
                self.log("virtual keyboard created (uinput)")
            except OSError as e:
                self.keyboard = False
                if self.is_wayland:
                    self.log(f"cannot open /dev/uinput ({e}) — built-in actions need it on Wayland; "
                             "is the mx4ctl udev rule installed?")
        if self.keyboard:
            self.keyboard.combo(*keys)
        elif not self.is_wayland:
            self._spawn(BUILTIN_X11[name])
        else:
            self.log(f"@{name} skipped — no uinput access")

    def _spawn(self, command: str) -> None:
        try:
            subprocess.Popen(command, shell=True, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.log(f"command failed: {e}")

    def _show_menu(self) -> None:
        raw = list(self.cfg["menu"].items()) if self.cfg.has_section("menu") else []
        if not raw:
            self.log("menu action but no [menu] entries in config")
            return
        if Gdk.Display.get_default() is None:
            self.log("menu: no display available")
            return
        if getattr(self, "_ring", None):
            self._ring.destroy()
            self._ring = None
        items = [ring.parse_entry(label, value) for label, value in raw]
        try:
            scale = float(self.cfg["button"]["menu_scale"] or 1.0)
        except ValueError:
            scale = 1.0
        self._ring = ring.ActionRing(items, lambda command: self._on_menu_pick(None, command),
                                     on_level=self._on_menu_level, scale=scale)
        self._ring.connect("destroy", lambda _w: setattr(self, "_ring", None))
        self._ring.pop_up(self._pointer())
        # reading a level blocks on a subprocess or two; doing that while the
        # ring is spiralling open costs it frames, so it waits for the landing
        GLib.timeout_add(int(ring.OPEN_MS) + 60, self._read_levels, self._ring)

    def _read_levels(self, opened) -> bool:
        """What a dial or toggle stands at costs a subprocess or two to find
        out — ask once the ring is already up, so it opens without the wait
        and the arcs sweep up to their level instead of appearing at it."""
        if opened is not self._ring:
            return False
        for entry in opened.items:
            if entry.dial:
                entry.level = self.dials.read(entry.dial)
            elif entry.toggle == "mic":
                entry.state = self.dials.mic_muted()
        opened.refresh()
        return False

    def _pointer(self) -> tuple[int, int] | None:
        """Where to centre the ring: X11 knows; on Wayland KWin has to be
        asked; anywhere else it opens centred."""
        if self.cursor is not None:
            return self.cursor.position()
        if self.gdk_wayland:
            return None
        return cursor.gdk_pointer()

    def _on_menu_level(self, entry, value: float) -> None:
        """A dial was scrolled: move it, and tick under the thumb."""
        self.dials.write(entry.dial, value)
        self.play(self.cfg["button"]["menu_select_waveform"])

    def _on_menu_pick(self, _item, command: str) -> None:
        self.play(self.cfg["button"]["menu_select_waveform"])
        self._do(command)

    # -- desktop notifications ---------------------------------------------------

    def start_notification_monitor(self) -> None:
        if not self.cfg["notifications"].getboolean("enabled"):
            return
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
        except ImportError:
            self.log("dbus-python not available — notification haptics disabled")
            return
        DBusGMainLoop(set_as_default=True)
        rule = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
        try:
            # keep the connection referenced — a GC'd bus stops delivering
            self._monitor_bus = dbus.SessionBus(private=True)
            self._monitor_bus.add_message_filter(self._on_dbus_message)
            try:
                self._monitor_bus.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                "org.freedesktop.DBus.Monitoring", "BecomeMonitor", "asu",
                                                ([rule], 0))
            except dbus.DBusException:
                self._monitor_bus.add_match_string(f"eavesdrop=true,{rule}")
            self.log("notification haptics on")
        except Exception as e:  # bus not available (headless, etc.)
            self.log(f"notification monitor failed: {e}")

    def _dnd_active(self) -> bool:
        if not self.cfg["notifications"].getboolean("respect_dnd"):
            return False
        try:
            if self.dnd_settings is None:
                self.dnd_settings = Gio.Settings.new("org.gnome.desktop.notifications")
            return not self.dnd_settings.get_boolean("show-banners")
        except Exception:  # not GNOME / schema missing
            return False

    def _on_dbus_message(self, _bus, message) -> None:
        try:
            if message.get_interface() != "org.freedesktop.Notifications" or message.get_member() != "Notify":
                return
            app = str(message.get_args_list()[0]).lower()
        except Exception:
            return
        if self._dnd_active():
            self.debug(f"notification from {app!r} suppressed (do not disturb)")
            return
        waveform = self.cfg["notifications"]["default"]
        for key, value in self.cfg["notifications"].items():
            if key in ("enabled", "default"):
                continue
            if key.lower() in app:
                waveform = value
                break
        self.debug(f"notification from {app!r} → {waveform}")
        self.play(waveform)

    # -- workspace switch tick --------------------------------------------------

    def start_workspace_watch(self) -> None:
        if not self.cfg["workspace"].getboolean("enabled") or not os.environ.get("DISPLAY"):
            return
        try:
            proc = subprocess.Popen(["xprop", "-root", "-spy", "_NET_CURRENT_DESKTOP"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.log("xprop not available — workspace tick disabled")
            return
        self._xprop = proc  # keep referenced
        self._workspace_seen = False
        GLib.io_add_watch(proc.stdout.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP,
                          self._on_workspace_line)
        self.log("workspace tick on")

    def _on_workspace_line(self, fd, cond) -> bool:
        if cond & GLib.IO_HUP:
            return False
        try:
            os.read(fd, 4096)
        except OSError:
            return False
        if self._workspace_seen:  # skip the initial value xprop prints on start
            self.debug("workspace changed")
            self.play(self.cfg["workspace"]["waveform"])
        self._workspace_seen = True
        return True

    # -- periodic diversion re-assert -----------------------------------------------

    def _assert_diversion(self) -> bool:
        """Diversion flags are volatile on the mouse and can be lost without a
        connection notification we see (deep sleep, Solaar re-applying its own
        settings). Re-writing them is idempotent and cheap, so do it steadily.
        Doubles as a health check: a dead node lands in the reconnect loop."""
        if not self.mouse or self.keys_down or self.gesturing:
            return True  # don't rewrite flags mid-press
        try:
            self.mouse.divert(CONTROLS["haptic"], True)
            if self.cfg["gestures"].getboolean("enabled"):
                self.mouse.divert(CONTROLS["gesture"], True, raw_xy=True)
        except OSError:
            self._reconnect_later()
        except hidpp.HidppError as e:
            self.debug(f"diversion re-assert failed: {e}")
        return True

    # -- battery -------------------------------------------------------------------

    def _check_battery(self) -> bool:
        if not self.mouse:
            return True
        try:
            percent, state = self.mouse.battery()
        except hidpp.HidppError:
            return True
        low = self.cfg["battery"].getint("low_percent")
        if percent <= low and state == "discharging":
            if not self.battery_warned:
                self.battery_warned = True
                self.play(self.cfg["battery"]["waveform"])
                self._spawn(f"notify-send -i battery-caution 'MX Master 4' 'Battery at {percent}%'")
        elif percent > low + 10 or state != "discharging":
            self.battery_warned = False
        return True

    # -- lifecycle -------------------------------------------------------------------

    def run(self) -> None:
        if not self.connect_mouse():
            self.log("mouse not found — retrying every 5s")
            GLib.timeout_add_seconds(5, lambda: not self.connect_mouse())
        self.start_notification_monitor()
        self.start_workspace_watch()
        GLib.timeout_add_seconds(60, self._assert_diversion)
        GLib.timeout_add_seconds(300, self._check_battery)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._quit)
        if self.cursor is not None:
            self.cursor.start()
        self.log("mx4ctl daemon running")
        try:
            self.loop.run()
        finally:
            if self.mouse:
                for name in ("haptic", "gesture"):
                    try:
                        self.mouse.divert(CONTROLS[name], False)
                    except hidpp.HidppError:
                        pass
                self.mouse.dev.channel.close()
            if self.keyboard:
                self.keyboard.close()
            self.log("stopped, button restored")

    def _quit(self) -> bool:
        self.loop.quit()
        return False


def run(config_path: str | None = None, verbose: bool = False) -> None:
    try:
        ok = Gtk.init_check()
        ok = ok[0] if isinstance(ok, tuple) else ok
    except Exception:
        ok = False
    if not ok:
        print("warning: no display — menu action unavailable", file=sys.stderr)
    Daemon(config_path, verbose).run()
