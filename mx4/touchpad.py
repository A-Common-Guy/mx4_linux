"""Pure-stdlib virtual multitouch touchpad via /dev/uinput.

Lets mouse motion drive the compositor's own touchpad gestures: while the
thumb button is held, three virtual fingers follow the mouse, so GNOME runs
its native 1:1 three-finger swipe (workspaces left/right, overview up/down)
with the same tracking and momentum as a real touchpad.
"""

from __future__ import annotations

import fcntl
import os
import struct
import time

# linux/uinput.h ioctls
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_ABS_SETUP = 0x401C5504
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x00005501
UI_DEV_DESTROY = 0x00005502

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0

ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

BTN_LEFT = 0x110
BTN_TOOL_FINGER = 0x145
BTN_TOUCH = 0x14A
BTN_TOOL_DOUBLETAP = 0x14D
BTN_TOOL_TRIPLETAP = 0x14E
BTN_TOOL_QUADTAP = 0x14F

INPUT_PROP_POINTER = 0x00
INPUT_PROP_BUTTONPAD = 0x02

RES = 40  # units per mm
WIDTH, HEIGHT = 12000, 8000  # 300 x 200 mm: room for long swipes
SLOTS = 5
TOOL = {1: BTN_TOOL_FINGER, 2: BTN_TOOL_DOUBLETAP, 3: BTN_TOOL_TRIPLETAP, 4: BTN_TOOL_QUADTAP}
FINGER_GAP = 600  # 15 mm between fingers
# libinput discards a frame as a "touch jump" when a finger moves >20 mm, or
# speeds up by >7 mm, per 12 ms. Motion is therefore queued and released in
# frames paced FRAME_MS apart, each capped well below those limits.
FRAME_MS = 8
FRAME_MAX = 360  # 9 mm per frame (13.5 mm per 12 ms, under the 20 mm limit)
FRAME_RAMP = 160  # 4 mm faster than the previous frame (6 mm per 12 ms, under 7)


class VirtualTouchpad:
    """A uinput clickpad that can put N fingers down, move them, lift them."""

    def __init__(self, name: str = "mx4ctl virtual touchpad"):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for key in (BTN_LEFT, BTN_TOUCH, *TOOL.values()):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            axes = (
                (ABS_X, WIDTH, RES), (ABS_Y, HEIGHT, RES),
                (ABS_MT_SLOT, SLOTS - 1, 0),
                (ABS_MT_POSITION_X, WIDTH, RES), (ABS_MT_POSITION_Y, HEIGHT, RES),
                (ABS_MT_TRACKING_ID, 65535, 0),
            )
            for code, maximum, res in axes:
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
                # struct uinput_abs_setup: code, pad, input_absinfo{value,min,max,fuzz,flat,res}
                fcntl.ioctl(self.fd, UI_ABS_SETUP,
                            struct.pack("=HxxiiiiiI", code, 0, 0, maximum, 0, 0, res))
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_POINTER)
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_BUTTONPAD)
            # bus USB: an external touchpad, so libinput never pairs it with a
            # keyboard for disable-while-typing (which would swallow swipes)
            setup = struct.pack("=HHHH80sI", 0x03, 0x046D, 0xB043, 1, name.encode(), 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self.fingers = 0
        self.x = self.y = 0.0
        self.pending_x = self.pending_y = 0.0
        self.last_step = 0.0
        self.frames = 0
        self.max_step = 0.0
        self.next_id = 1
        time.sleep(0.3)  # give the compositor time to pick up the new device

    @property
    def active(self) -> bool:
        return self.fingers > 0

    @property
    def pending(self) -> float:
        return (self.pending_x ** 2 + self.pending_y ** 2) ** 0.5

    def _emit(self, etype: int, code: int, value: int) -> None:
        os.write(self.fd, struct.pack("qqHHi", 0, 0, etype, code, value))

    def _syn(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    def _positions(self):
        start = -(self.fingers - 1) * FINGER_GAP / 2
        for i in range(self.fingers):
            yield i, int(self.x + start + i * FINGER_GAP), int(self.y)

    def _report(self) -> None:
        for slot, px, py in self._positions():
            self._emit(EV_ABS, ABS_MT_SLOT, slot)
            self._emit(EV_ABS, ABS_MT_POSITION_X, px)
            self._emit(EV_ABS, ABS_MT_POSITION_Y, py)
        self._emit(EV_ABS, ABS_X, int(self.x))
        self._emit(EV_ABS, ABS_Y, int(self.y))
        self._syn()

    def down(self, fingers: int = 3) -> None:
        if self.active:
            return
        self.fingers = fingers
        self.x, self.y = WIDTH / 2, HEIGHT / 2
        self.pending_x = self.pending_y = 0.0
        self.last_step = 0.0
        self.frames = 0
        self.max_step = 0.0
        for slot, px, py in self._positions():
            self._emit(EV_ABS, ABS_MT_SLOT, slot)
            self._emit(EV_ABS, ABS_MT_TRACKING_ID, self.next_id)
            self.next_id = self.next_id % 65000 + 1
            self._emit(EV_ABS, ABS_MT_POSITION_X, px)
            self._emit(EV_ABS, ABS_MT_POSITION_Y, py)
        self._emit(EV_KEY, BTN_TOUCH, 1)
        self._emit(EV_KEY, TOOL[fingers], 1)
        self._emit(EV_ABS, ABS_X, int(self.x))
        self._emit(EV_ABS, ABS_Y, int(self.y))
        self._syn()

    def queue(self, dx: float, dy: float) -> None:
        """Add (dx, dy) touchpad units of motion, released by tick()."""
        if self.active:
            self.pending_x += dx
            self.pending_y += dy

    def tick(self) -> None:
        """Emit one paced frame of the queued motion; call every FRAME_MS."""
        if not self.active:
            return
        dist = (self.pending_x ** 2 + self.pending_y ** 2) ** 0.5
        if dist < 1:
            self.last_step = 0.0
            return
        step = min(dist, FRAME_MAX, self.last_step + FRAME_RAMP)
        fx, fy = self.pending_x * step / dist, self.pending_y * step / dist
        self.pending_x -= fx
        self.pending_y -= fy
        self.last_step = step
        self.frames += 1
        self.max_step = max(self.max_step, step)
        span = (self.fingers - 1) * FINGER_GAP / 2
        self.x = min(max(self.x + fx, span), WIDTH - span)
        self.y = min(max(self.y + fy, 0), HEIGHT)
        self._report()

    def up(self) -> None:
        if not self.active:
            return
        for slot, _, _ in self._positions():
            self._emit(EV_ABS, ABS_MT_SLOT, slot)
            self._emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self._emit(EV_KEY, TOOL[self.fingers], 0)
        self._emit(EV_KEY, BTN_TOUCH, 0)
        self._syn()
        self.fingers = 0
        self.pending_x = self.pending_y = 0.0
        self.last_step = 0.0

    def close(self) -> None:
        self.up()
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)
