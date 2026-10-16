"""Detect scheduler stalls, suspend/resume and wall-clock jumps before decisions."""
import math
import time


class ClockContinuity:
    def __init__(self, *, wall=time.time, monotonic=time.monotonic, max_gap=15, max_skew=2):
        self.wall, self.monotonic = wall, monotonic
        self.max_gap, self.max_skew = max_gap, max_skew
        self.previous = None

    def observe(self):
        current = self.wall(), self.monotonic()
        prior, self.previous = self.previous, current
        if not all(math.isfinite(v) for v in current): return 'nonfinite clock'
        if prior is None: return None
        wall, monotonic = current[0]-prior[0], current[1]-prior[1]
        if not all(math.isfinite(v) for v in (wall,monotonic)): return 'nonfinite clock'
        if wall < 0 or monotonic < 0 or abs(wall-monotonic)>self.max_skew:
            return 'clock discontinuity'
        if max(wall,monotonic)>self.max_gap: return 'scheduler gap or resume'
        return None
