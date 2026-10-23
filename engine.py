"""Video-font clock reader and monotonic countdown synchronizer. No input injection."""
from collections import deque
from dataclasses import dataclass
import re
import statistics

import cv2
import numpy as np


def parse_time(text):
    match = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})[:：.](\d{2})\s*", text)
    if not match:
        raise ValueError("时间请填写为 04:58:88（分:秒:百分之一秒）")
    minute, second, hundredth = map(int, match.groups())
    if second >= 60 or minute >= 60:
        raise ValueError("分和秒必须小于 60")
    return minute * 60 + second + hundredth / 100


def format_time(value):
    cs = max(0, round(value * 100))
    return f"{cs // 6000:02d}:{cs // 100 % 60:02d}:{cs % 100:02d}"


@dataclass
class Reading:
    seconds: float
    error: float
    box: tuple


class ClockReader:
    def __init__(self, templates):
        with np.load(templates) as data:
            self.images = data["images"]
            self.labels = data["labels"]

    def digit(self, mask, box):
        x, y, w, h, _ = box
        glyph = cv2.resize(mask[y:y+h, x:x+w].astype(np.float32), (12, 18),
                           interpolation=cv2.INTER_AREA)
        errors = np.mean((self.images - glyph) ** 2, axis=(1, 2))
        k = int(np.argmin(errors))
        return int(self.labels[k]), float(errors[k])

    def read(self, bgr):
        if bgr is None or bgr.size == 0:
            return None
        low = bgr.min(axis=2)
        spread = bgr.max(axis=2).astype(np.int16) - low
        best = None
        for threshold in (170, 145, 195):
            mask = ((low > threshold) & (spread < 75)).astype(np.uint8)
            _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
            candidates = [s for s in stats[1:] if 5 <= s[3] <= bgr.shape[0] * .85
                          and .12 <= s[2] / s[3] <= 1.25
                          and s[4] >= s[2] * s[3] * .2]
            for anchor in candidates:
                height = anchor[3]
                row = sorted([s for s in candidates if abs(s[1]-anchor[1]) <= height*.24
                              and .72 <= s[3]/height <= 1.28], key=lambda s: s[0])
                for start in range(len(row)-5):
                    group = row[start:start+6]
                    centers = np.array([s[0]+s[2]/2 for s in group])
                    gaps = np.diff(centers) / height
                    if np.any(gaps < .30) or np.any(gaps > 1.65):
                        continue
                    # Colons separate MM:SS:CC, making these two gaps wider.
                    if gaps[1] < .8 or gaps[3] < .8:
                        continue
                    digits, errors = zip(*(self.digit(mask, s) for s in group))
                    if max(errors) > .24 or statistics.mean(errors) > .13:
                        continue
                    minute = digits[0]*10+digits[1]
                    second = digits[2]*10+digits[3]
                    if minute > 5 or second >= 60:
                        continue
                    value = minute*60+second+(digits[4]*10+digits[5])/100
                    if value > 300:
                        continue
                    error = statistics.mean(errors)
                    if best is None or error < best.error:
                        x = int(group[0][0]); y = int(min(s[1] for s in group))
                        right = int(group[-1][0]+group[-1][2])
                        bottom = int(max(s[1]+s[3] for s in group))
                        best = Reading(value, error, (x, y, right, bottom))
            if best is not None and best.error < .015:
                break
        return best


class CountdownSync:
    """Estimate time of zero from distinct, consistent screen readings.

    Repeated frames do not move the estimate. A stale or paused clock cannot
    authorize a click. All timestamps must use the same monotonic clock.
    """
    def __init__(self):
        self.samples = deque(maxlen=12)
        self.last_change = None
        self.bad_since = None

    def update(self, now, seconds):
        if self.samples:
            old_t, old_v = self.samples[-1]
            if abs(seconds-old_v) < .004:
                return False
            elapsed = now-old_t
            decrement = old_v-seconds
            if decrement <= 0 or abs(decrement-elapsed) > .12:
                if self.bad_since is None:
                    self.bad_since = now
                if now-self.bad_since > .25:
                    self.samples.clear()
                else:
                    return False
        self.bad_since = None
        self.samples.append((now, seconds))
        self.last_change = now
        return True

    def deadline(self, target, offset_ms=0):
        if len(self.samples) < 4 or self.samples[-1][0]-self.samples[0][0] < .07:
            return None
        zeros = [t+v for t, v in self.samples]
        if max(zeros)-min(zeros) > .09:
            return None
        return statistics.median(zeros)-target+offset_ms/1000

    def fresh(self, now):
        return self.last_change is not None and now-self.last_change < .12
