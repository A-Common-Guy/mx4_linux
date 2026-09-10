"""The two things the ring can dial: screen brightness and output volume.

Every desktop exposes these differently, so each dial keeps a list of
backends and settles on the first one that answers. Values are fractions
(0.0 - 1.0) throughout; writes are fire-and-forget, because a scroll must
not wait on a subprocess.
"""

from __future__ import annotations

import re
import shutil
import subprocess

BRIGHTNESS_DBUS = ("org.kde.Solid.PowerManagement",
                   "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
                   "org.kde.Solid.PowerManagement.Actions.BrightnessControl")

# opening the real settings page — first command whose binary exists wins
SETTINGS = {
    "brightness": (("kcmshell6", "kcmshell6 kcm_powerdevilprofilesconfig"),
                   ("kcmshell5", "kcmshell5 kcm_powerdevilprofilesconfig"),
                   ("systemsettings", "systemsettings kcm_powerdevilprofilesconfig"),
                   ("gnome-control-center", "gnome-control-center display"),
                   ("xfce4-power-manager-settings", "xfce4-power-manager-settings")),
    "mic": (("kcmshell6", "kcmshell6 kcm_pulseaudio"),
            ("kcmshell5", "kcmshell5 kcm_pulseaudio"),
            ("systemsettings", "systemsettings kcm_pulseaudio"),
            ("pavucontrol", "pavucontrol"),
            ("gnome-control-center", "gnome-control-center sound")),
    "volume": (("kcmshell6", "kcmshell6 kcm_pulseaudio"),
               ("kcmshell5", "kcmshell5 kcm_pulseaudio"),
               ("systemsettings", "systemsettings kcm_pulseaudio"),
               ("pavucontrol", "pavucontrol"),
               ("gnome-control-center", "gnome-control-center sound")),
}


def settings_command(dial: str) -> str:
    for binary, command in SETTINGS.get(dial, ()):
        if shutil.which(binary):
            return command
    return ""


def _run(*args: str) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _spawn(*args: str) -> None:
    """Fire and forget — nobody waits for a volume change to land."""
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Dials:
    """Read and write the dials. `read` returns None when nothing can answer,
    which is the ring's cue to draw the entry as an ordinary button."""

    def __init__(self, log=print):
        self.log = log
        self.bus = None
        self.backend: dict[str, str] = {}   # dial -> the backend that answered

    # -- brightness --------------------------------------------------------------

    def _kde_brightness(self):
        """PowerDevil, if this is a Plasma session. Returns the proxy or None."""
        if self.bus is None:
            try:
                import dbus
                self.bus = dbus.SessionBus()
            except Exception:
                self.bus = False
        if not self.bus:
            return None
        try:
            return self.bus.get_object(BRIGHTNESS_DBUS[0], BRIGHTNESS_DBUS[1])
        except Exception:
            return None

    def _read_brightness(self) -> float | None:
        proxy = self._kde_brightness()
        if proxy is not None:
            try:
                now = float(proxy.brightness(dbus_interface=BRIGHTNESS_DBUS[2]))
                top = float(proxy.brightnessMax(dbus_interface=BRIGHTNESS_DBUS[2]))
                if top > 0:
                    self.backend["brightness"] = "kde"
                    return now / top
            except Exception:
                pass
        out = _run("brightnessctl", "-m") if shutil.which("brightnessctl") else None
        if out:  # device,class,value,percent%,max
            fields = out.strip().split(",")
            if len(fields) >= 4 and fields[3].endswith("%"):
                self.backend["brightness"] = "brightnessctl"
                return int(fields[3][:-1]) / 100
        return None

    def _write_brightness(self, value: float) -> None:
        if self.backend.get("brightness") == "kde":
            proxy = self._kde_brightness()
            try:
                top = float(proxy.brightnessMax(dbus_interface=BRIGHTNESS_DBUS[2]))
                # ...Silent: we draw our own indicator, no need for the OSD too
                proxy.setBrightnessSilent(int(round(value * top)),
                                          dbus_interface=BRIGHTNESS_DBUS[2])
                return
            except Exception as e:
                self.log(f"brightness not set: {e}")
                self.backend.pop("brightness", None)
        _spawn("brightnessctl", "-q", "set", f"{round(value * 100)}%")

    # -- volume ------------------------------------------------------------------

    def _read_volume(self) -> float | None:
        if shutil.which("wpctl"):
            out = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
            match = re.search(r"([\d.]+)", out or "")
            if match:
                self.backend["volume"] = "wpctl"
                return float(match.group(1))
        if shutil.which("pactl"):
            out = _run("pactl", "get-sink-volume", "@DEFAULT_SINK@")
            match = re.search(r"(\d+)%", out or "")
            if match:
                self.backend["volume"] = "pactl"
                return int(match.group(1)) / 100
        return None

    def _write_volume(self, value: float) -> None:
        if self.backend.get("volume") == "pactl":
            _spawn("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{round(value * 100)}%")
        else:
            _spawn("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{value:.2f}")

    # -- microphone ----------------------------------------------------------------
    #
    # A toggle rather than a dial: the ring shows what a click would do, so it
    # needs to know whether the input is live right now.

    def mic_muted(self) -> bool | None:
        if shutil.which("wpctl"):
            out = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@")
            if out:
                return "MUTED" in out
        if shutil.which("pactl"):
            out = _run("pactl", "get-source-mute", "@DEFAULT_SOURCE@")
            if out:
                return "yes" in out.lower()
        return None

    @staticmethod
    def mic_toggle_command() -> str:
        if shutil.which("wpctl"):
            return "wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle"
        if shutil.which("pactl"):
            return "pactl set-source-mute @DEFAULT_SOURCE@ toggle"
        return "amixer set Capture toggle"

    # -- api ---------------------------------------------------------------------

    def read(self, dial: str) -> float | None:
        try:
            value = self._read_brightness() if dial == "brightness" else self._read_volume()
        except Exception as e:
            self.log(f"{dial} not read: {e}")
            return None
        return None if value is None else min(max(value, 0.0), 1.0)

    def write(self, dial: str, value: float) -> None:
        value = min(max(value, 0.0), 1.0)
        if dial == "brightness":
            self._write_brightness(value)
        else:
            self._write_volume(value)
