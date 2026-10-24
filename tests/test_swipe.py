"""Thumb swipe detection and touchpad frame pacing.

Motion is fed the way the mouse sends it: one raw-XY report every ~8 ms.
Several cases are taken from real MX Master 4 logs.

    python3 -m unittest discover tests
"""

import unittest

from mx4 import touchpad
from mx4.swipe import SwipeConfig, ThumbSwipe

REPORT_S = 0.008


def hold(reports, cfg=None):
    """Press, feed (dx, dy) reports 8 ms apart; return the swipe and its results."""
    s = ThumbSwipe(cfg or SwipeConfig())
    s.press(0.0)
    results = [s.motion((i + 1) * REPORT_S, dx, dy) for i, (dx, dy) in enumerate(reports)]
    return s, [r for r in results if r]


class SwipeStart(unittest.TestCase):
    def test_still_press_is_a_tap(self):
        s, results = hold([(0, -1), (0, 0)])
        self.assertEqual(results, [])
        self.assertEqual(s.release()[0], "tap")

    def test_quick_flick_released_before_start(self):
        # logged: 664 counts left inside the first report window, then released
        s, results = hold([(-664, -74)])
        self.assertEqual(results, [])
        kind, x, y = s.release()
        self.assertEqual(kind, "flick")
        self.assertLess(x, 0)
        self.assertEqual(y, 0)  # axis lock drops the cross motion

    def test_big_diagonal_flick_is_not_a_tap(self):
        # logged: a 45-degree-ish flick released before the direction was clear
        s, results = hold([(-321, 250)])
        self.assertEqual(results, [])
        kind, x, y = s.release()
        self.assertEqual(kind, "flick")
        self.assertLess(x, 0)
        self.assertEqual(y, 0)

    def test_press_nudge_does_not_pick_the_direction(self):
        # thumb press nudges the mouse down, then the hand swipes right
        s, results = hold([(2, 30)] * 3 + [(80, -5)] * 10)
        kind, x, y = results[0]
        self.assertEqual(kind, "begin")
        self.assertEqual(s.axis, "x")
        self.assertGreater(x, 0)

    def test_vertical_swipe_up(self):
        s, results = hold([(-3, -60)] * 10)
        self.assertEqual(results[0][0], "begin")
        self.assertEqual(s.axis, "y")
        self.assertLess(results[0][2], 0)

    def test_slow_swipe_starts(self):
        s, results = hold([(15, 1)] * 40)
        self.assertTrue(results)
        self.assertEqual(s.axis, "x")

    def test_thirty_degree_swipe_counts(self):
        s, results = hold([(60, 35)] * 15)
        self.assertTrue(results)
        self.assertEqual(s.axis, "x")

    def test_ambiguous_diagonal_waits(self):
        s, results = hold([(50, 48)] * 6)
        self.assertEqual(results, [])
        self.assertFalse(s.active)

    def test_axis_lock_drops_cross_motion(self):
        s, results = hold([(80, 0)] * 6 + [(10, 50)])
        self.assertEqual(results[-1], ("move", 10 * s.cfg.gain, 0))

    def test_free_axis_when_unlocked(self):
        s, results = hold([(80, 0)] * 6 + [(10, 50)], SwipeConfig(axis_lock=False))
        self.assertEqual(results[-1], ("move", 10 * s.cfg.gain, 50 * s.cfg.gain))

    def test_running_swipe_ends_on_release(self):
        s, _ = hold([(80, 0)] * 6)
        self.assertEqual(s.release()[0], "end")

    def test_press_resets(self):
        s, _ = hold([(80, 0)] * 6)
        s.press(1.0)
        self.assertFalse(s.active)
        self.assertEqual(s.axis, "")


class FramePacing(unittest.TestCase):
    """Frames must stay under libinput's touch-jump limits, or it discards them:
    >20 mm in one 12 ms frame, or >7 mm faster than the previous frame."""

    def make_pad(self):
        pad = touchpad.VirtualTouchpad.__new__(touchpad.VirtualTouchpad)
        pad.fingers = 3
        pad.x, pad.y = touchpad.WIDTH / 2, touchpad.HEIGHT / 2
        pad.pending_x = pad.pending_y = 0.0
        pad.last_step = pad.max_step = 0.0
        pad.frames = 0
        pad._report = lambda: None
        return pad

    def test_big_burst_is_paced_and_drains(self):
        pad = self.make_pad()
        pad.queue(4000, 0)  # 100 mm arriving at once
        to_12ms = 12 / touchpad.FRAME_MS
        prev_mm = 0.0
        while pad.pending >= 1:
            before = pad.pending
            pad.tick()
            step_mm = (before - pad.pending) / touchpad.RES
            self.assertLess(step_mm * to_12ms, 20)
            self.assertLess((step_mm - prev_mm) * to_12ms, 7)
            prev_mm = step_mm
        self.assertLess(pad.frames, 60)

    def test_fingers_stay_on_the_pad(self):
        pad = self.make_pad()
        pad.queue(-10 ** 6, 10 ** 6)
        for _ in range(5000):
            pad.tick()
        span = (pad.fingers - 1) * touchpad.FINGER_GAP / 2
        self.assertGreaterEqual(pad.x, span)
        self.assertLessEqual(pad.y, touchpad.HEIGHT)


if __name__ == "__main__":
    unittest.main()
