"""Video-based visual calibration. All input/capture operations are injected."""
from dataclasses import dataclass
import time
import cv2
import numpy as np
from engine import CountdownSync


@dataclass
class Match:
    box: tuple
    scale: float
    score: float

    @property
    def center(self):
        x, y, r, b = self.box
        return (round((x+r)/2), round((y+b)/2))


def crop(frame, box):
    x, y, r, b = map(int, box)
    x, y = max(0, x), max(0, y)
    r, b = min(frame.shape[1], r), min(frame.shape[0], b)
    return frame[y:b, x:r], (x, y)


class VisualLocator:
    def __init__(self, assets, check=lambda: None, recording_region=None):
        with np.load(assets) as data:
            self.assets = {k: data[k] for k in data.files}
        self.check = check
        self.recording_region = recording_region

    def find(self, frame, name, region=None, scale=None, threshold=.82):
        """Coarse multiscale search followed by local full-resolution refinement."""
        self.check()
        source, origin = crop(frame, region or (0, 0, frame.shape[1], frame.shape[0]))
        if not source.size:
            return None
        gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
        template = cv2.cvtColor(self.assets[name], cv2.COLOR_BGR2GRAY)
        shrink = min(1., 1100 / max(gray.shape))
        small = cv2.resize(gray, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
        scales = np.geomspace(.55, 3.5, 27) if scale is None else np.linspace(scale*.94, scale*1.06, 7)
        best = None
        for s in scales:
            self.check()
            w, h = round(template.shape[1]*s*shrink), round(template.shape[0]*s*shrink)
            if w < 7 or h < 6 or w > small.shape[1] or h > small.shape[0]:
                continue
            target = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA)
            _, score, _, pos = cv2.minMaxLoc(cv2.matchTemplate(small, target, cv2.TM_CCOEFF_NORMED))
            if best is None or score > best[0]:
                best = score, s, (pos[0]/shrink, pos[1]/shrink)
        if best is None or best[0] < threshold-.15:
            return None
        _, s, (x, y) = best
        pad = max(12, round(max(template.shape)*s*.12))
        local, (lx, ly) = crop(source, (x-pad, y-pad, x+template.shape[1]*s+pad, y+template.shape[0]*s+pad))
        local = cv2.cvtColor(local, cv2.COLOR_BGR2GRAY)
        found = None
        for exact in np.linspace(s*.96, s*1.04, 17):
            self.check()
            w, h = round(template.shape[1]*exact), round(template.shape[0]*exact)
            if w > local.shape[1] or h > local.shape[0]:
                continue
            target = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA if exact < 1 else cv2.INTER_CUBIC)
            _, score, _, pos = cv2.minMaxLoc(cv2.matchTemplate(local, target, cv2.TM_CCOEFF_NORMED))
            if found is None or score > found.score:
                px, py = origin[0]+lx+pos[0], origin[1]+ly+pos[1]
                found = Match((px, py, px+w, py+h), exact, score)
        return found if found and found.score >= threshold else None

    def game_box(self, challenge):
        ref = self.assets['challenge_box']
        game = self.assets['game_box']
        x, y = challenge.box[:2]
        return tuple(round((v-ref[i % 2])*challenge.scale+(x if i % 2 == 0 else y))
                     for i, v in enumerate(game))

    def top_macro(self, frame):
        if self.recording_region is not None:
            x, y, r, b = self.recording_region
            s = (r-x)/400
            region = (r-65*s, y+25*s, r-4*s, y+62*s)
        else:
            panel = self.find(frame, 'panel', threshold=.80)
            if not panel:
                return None
            x, y, r, b = panel.box
            s = panel.scale
            region = (r-48*s, b+3*s, r, b+34*s)
        return self._macro_in_region(frame, region, s)

    def macro_rows(self, frame):
        """Return fully visible playable rows; never treat a stop square as play."""
        if self.recording_region is None:
            raise RuntimeError('请打开 MuMu 操作录制窗口，再识别录制名称')
        x, y, r, b = self.recording_region
        s = (r-x)/400
        image, (ox, oy) = crop(frame, (r-65*s, y+25*s, r-4*s, b-4*s))
        if not image.size:
            return []
        blue, green, red = cv2.split(image.astype(np.int16))
        mask = ((blue>150)&(green>90)&(red<120)&(green-red>70)&(blue-red>100)).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rows = []
        for contour in contours:
            bx, by, bw, bh = cv2.boundingRect(contour)
            if not (12*s <= bw <= 29*s and 7*s <= bh <= 21*s and 1.3 <= bw/bh <= 2.3):
                continue
            cy = oy+by+bh/2
            if cy-13*s < y+24*s or cy+12*s > b-3*s:
                continue
            match = self._macro_in_region(frame, (ox+bx-3*s, oy+by-3*s, ox+bx+bw+3*s, oy+by+bh+3*s), s)
            if match:
                title, _ = crop(frame, (x+12*s, cy-13*s, r-190*s, cy+2*s))
                rows.append((match, title.copy()))
        return sorted(rows, key=lambda row: row[0].center[1])

    def scrollbar(self, frame):
        """Visible MuMu scrollbar thumb, used to reject ineffective scrolling."""
        if not self.recording_region:
            return None
        x,y,r,b = self.recording_region
        s = (r-x)/400
        strip, (_,oy) = crop(frame,(r-5*s,y+25*s,r-3*s,b-3*s))
        if not strip.size:
            return None
        pixels = np.median(strip,axis=1)
        neutral = (pixels.max(axis=1)-pixels.min(axis=1)<12) & (pixels.mean(axis=1)>75) & (pixels.mean(axis=1)<150)
        edges = np.diff(np.r_[False,neutral,False].astype(int))
        runs = [(a+oy,z+oy) for a,z in zip(np.where(edges==1)[0],np.where(edges==-1)[0]) if z-a>12*s]
        return (*max(runs,key=lambda p:p[1]-p[0]),y+32*s,b-8*s,s) if runs else None

    def _macro_in_region(self, frame, region, s):
        # Require the blue button AND its white right-pointing triangle.
        # A stopped/missing first row never falls back to a lower row.
        image, (ox, oy) = crop(frame, region)
        if not image.size:
            return None
        blue, green, red = cv2.split(image.astype(np.int16))
        mask = ((blue>150)&(green>90)&(red<120)&(green-red>70)&(blue-red>100)).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        found = []
        for contour in contours:
            self.check()
            bx, by, bw, bh = cv2.boundingRect(contour)
            if not (12*s <= bw <= 29*s and 7*s <= bh <= 21*s and 1.3 <= bw/bh <= 2.3):
                continue
            # Full-arrow matching also handles anti-aliased tiny triangles.
            matched = self.find(frame, 'macro', region, s, threshold=.90)
            if matched and max(abs(matched.center[i]-(ox+bx+bw/2 if i==0 else oy+by+bh/2)) for i in range(2)) <= 3*s:
                found.append(matched)
                continue
            button = image[by:by+bh, bx:bx+bw]
            white = ((button.min(axis=2)>205)&(button.max(axis=2)-button.min(axis=2)<65)).astype(np.uint8)
            arrows, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for arrow in arrows:
                if cv2.contourArea(arrow) < max(2, bw*bh*.04):
                    continue
                polygon = cv2.approxPolyDP(arrow, .09*cv2.arcLength(arrow,True), True).reshape(-1,2)
                if len(polygon) != 3:
                    continue
                order = polygon[np.argsort(polygon[:,0])]
                if abs(int(order[0,0])-int(order[1,0])) > bw*.2:
                    continue
                if order[2,0]-order[1,0] < bw*.15:
                    continue
                if not min(order[0,1],order[1,1]) < order[2,1] < max(order[0,1],order[1,1]):
                    continue
                found.append(Match((ox+bx,oy+by,ox+bx+bw,oy+by+bh),s,1.))
        return min(found,key=lambda m:m.center[1]) if len(found)==1 else None


class AutoCalibration:
    def __init__(self, locator, reader, capture, click, wait, check, post, now=time.perf_counter, preview=None):
        self.locator, self.reader = locator, reader
        self.capture, self.click = capture, click
        self.wait, self.check, self.post, self.now = wait, check, post, now
        self.preview = preview

    def stable_button(self, name, region=None, scale=None, timeout=20):
        end = self.now()+timeout
        previous = None
        while self.now() < end:
            self.check()
            frame, stamp = self.capture()
            match = self.locator.find(frame, name, region, scale)
            if match and previous and max(abs(a-b) for a,b in zip(match.center, previous.center)) <= 4:
                return match
            previous = match
            self.wait(.15)
        raise RuntimeError(f'未能稳定识别{name}按钮，请保持视频中的游戏界面可见，或使用手动校准')

    def run(self):
        self.post('识别操作录制列表最上面一行的播放开关（只记录，不启动）。')
        frame, _ = self.capture()
        macro = self.locator.top_macro(frame)
        if not macro:
            raise RuntimeError('未找到最上面的蓝色播放开关。请打开操作录制列表、滚到顶部，并停止该录制')
        self.wait(.2)
        frame, _ = self.capture()
        again = self.locator.top_macro(frame)
        if not again or max(abs(a-b) for a,b in zip(macro.center, again.center)) > 4:
            raise RuntimeError('录制列表位置不稳定，请保持窗口不动后重试')
        macro = again
        if self.preview:
            self.preview(frame, macro)
        self.post('识别挑战按钮，进入选人页。')
        challenge = self.stable_button('challenge')
        game = self.locator.game_box(challenge)
        self.check()
        self.click(challenge.center)
        self.post('等待选人页，识别并点击开始／确定，使用当前已选角色。')
        start = self.stable_button('start', game, challenge.scale, timeout=30)
        self.check()
        self.click(start.center)
        self.post('等待进入游戏；识别连续倒计时和右上角暂停按钮。')
        x, y, r, b = game
        w, h = r-x, b-y
        timer_region = (x+w*.30, y+h*.02, x+w*.70, y+h*.23)
        pause_region = (x+w*.82, y, r, y+h*.22)
        sync = CountdownSync()
        end = self.now()+75
        previous_center = None
        while self.now() < end:
            self.check()
            frame, stamp = self.capture()
            timer, (tx, ty) = crop(frame, timer_region)
            reading = self.reader.read(timer)
            if reading:
                left, top, right, bottom = reading.box
                center = (round(tx+(left+right)/2), round(ty+(top+bottom)/2))
                if previous_center and max(abs(a-b) for a,b in zip(center, previous_center)) > 5:
                    sync = CountdownSync()
                previous_center = center
                sync.update(stamp, reading.seconds)
                if sync.deadline(0) is not None and sync.fresh(stamp):
                    pause = self.locator.find(frame, 'pause', pause_region, challenge.scale, threshold=.80)
                    if pause:
                        # Recheck the button against a fresh screenshot before input.
                        fresh, _ = self.capture()
                        confirmed = self.locator.find(fresh, 'pause', pause_region, pause.scale, threshold=.80)
                        if not confirmed or max(abs(a-b) for a,b in zip(pause.center, confirmed.center)) > 4:
                            raise RuntimeError('暂停按钮位置发生变化，已停止自动校准')
                        self.check()
                        self.click(confirmed.center)
                        points = dict(macro=macro.center, challenge=challenge.center,
                                      start=start.center, timer=center, pause=confirmed.center)
                        roi = (max(100, round((right-left)*1.7)), max(40, round((bottom-top)*3)))
                        paused = self.verify_pause(fresh, timer_region, game, pause_region, confirmed.scale)
                        return dict(points=points, width=min(600, roi[0]), height=min(200, roi[1]), paused=paused, scale=challenge.scale)
            self.wait(.08)
        raise RuntimeError('75 秒内没有识别到连续变化的倒计时及暂停按钮；未保存本次位置，请检查游戏是否进入战斗')

    def verify_pause(self, before, timer_region, game, pause_region, scale):
        """Never click pause twice: a second click might resume the game."""
        self.post('已点击右上角暂停，正在检查画面是否停止计时。')
        end = self.now()+4
        stable_since, last_value = None, None
        before_game, _ = crop(before, game)
        while self.now() < end:
            self.wait(.2)
            self.check()
            frame, stamp = self.capture()
            timer, _ = crop(frame, timer_region)
            reading = self.reader.read(timer)
            value = reading.seconds if reading else None
            if reading:
                stopped = last_value is not None and value == last_value
            else:
                current, _ = crop(frame, game)
                changed = current.shape == before_game.shape and np.mean(cv2.absdiff(current, before_game)) > 18
                stopped = changed and self.locator.find(frame, 'pause', pause_region, scale, threshold=.80) is None
            if stopped:
                if stable_since is None:
                    stable_since = stamp
                if stamp-stable_since >= .65:
                    return True
            else:
                stable_since = None
            last_value = value
        return False
