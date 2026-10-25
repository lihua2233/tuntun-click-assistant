"""Offline recording names, visual identities and cancellable list traversal."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import cv2
import numpy as np


def encode(image):
    return base64.b64encode(cv2.imencode('.png', image)[1]).decode('ascii')


def decode(value):
    return cv2.imdecode(np.frombuffer(base64.b64decode(value), np.uint8), cv2.IMREAD_COLOR)


def signature(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = (gray > 170).astype(np.uint8)*255
    points = cv2.findNonZero(mask)
    if points is None:
        raise RuntimeError('录制名称被遮挡或为空，请保持录制列表完整可见')
    x,y,w,h = cv2.boundingRect(points)
    return cv2.resize(mask[y:y+h,x:x+w], (480,32), interpolation=cv2.INTER_AREA), w/h


def same_title(a, b):
    aa, ar = signature(a)
    bb, br = signature(b)
    if abs(ar-br)/max(ar,br) > .035:
        return False
    return float(cv2.matchTemplate(aa, bb, cv2.TM_CCOEFF_NORMED)[0,0]) > .94


def read_names(images, script, check=lambda: None):
    """OCR one sheet of enlarged title strips using Windows' local Chinese OCR."""
    if not images:
        return []
    sheet = np.full((len(images)*70, 1400, 3), 255, np.uint8)
    for i, im in enumerate(images):
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        # White-on-dark text becomes dark-on-white; hide row background highlights.
        gray = 255-np.clip((gray.astype(float)-100)*255/155, 0, 255).astype(np.uint8)
        ratio = min(2., 1380/im.shape[1], 50/im.shape[0])
        enlarged = cv2.resize(gray, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_CUBIC)
        h,w = enlarged.shape
        sheet[i*70+10:i*70+10+h,10:10+w] = cv2.cvtColor(enlarged,cv2.COLOR_GRAY2BGR)
    with tempfile.TemporaryDirectory(prefix='tuntun-ocr-') as directory:
        source, output = Path(directory)/'names.png', Path(directory)/'names.json'
        source.write_bytes(cv2.imencode('.png',sheet)[1].tobytes())
        shell = str(Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe')
        with open(Path(directory)/'error.txt','wb') as errors:
            process = subprocess.Popen([shell,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                                        '-File',str(script),'-ImagePath',str(source),'-OutputPath',str(output)],
                                       stdout=subprocess.DEVNULL, stderr=errors,
                                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            end = time.monotonic()+20
            try:
                while process.poll() is None:
                    check()
                    if time.monotonic()>end:
                        raise RuntimeError('录制名称识别超时，请重试')
                    time.sleep(.05)
                check()
                if process.returncode or not output.exists():
                    raise RuntimeError('Windows 中文文字识别不可用；请安装“简体中文”的光学字符识别功能，或使用手动校准')
                lines = json.loads(output.read_text(encoding='utf-8-sig'))
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    words = [[] for _ in images]
    for line in lines:
        for word in line['words']:
            index = int((word['y']+word['h']/2)//70)
            if 0 <= index < len(images):
                words[index].append(word)
    return [''.join(w['text'] for w in sorted(row,key=lambda w:w['x'])) or '（名称未识别，请查看截图）' for row in words]


class RecordingList:
    def __init__(self, locator, capture, scroll, wait, check, post, ocr):
        self.locator, self.capture, self.scroll = locator, capture, scroll
        self.wait, self.check, self.post, self.ocr = wait, check, post, ocr

    def page(self):
        self.check()
        frame, _ = self.capture()
        rows = self.locator.macro_rows(frame)
        if not rows:
            raise RuntimeError('未找到完整的录制名称和播放箭头，请展开列表、停止录制并移开遮挡窗口')
        # Wait until the same rows settle after scrolling.
        for _ in range(8):
            self.wait(.15)
            frame, _ = self.capture()
            fresh = self.locator.macro_rows(frame)
            if len(rows)==len(fresh) and all(same_title(a[1],b[1]) and max(abs(x-y) for x,y in zip(a[0].center,b[0].center))<=2 for a,b in zip(rows,fresh)):
                if not fresh:
                    raise RuntimeError('录制列表被遮挡或没有可用播放箭头')
                self.thumb = self.locator.scrollbar(frame)
                return fresh
            rows = fresh
        raise RuntimeError('录制列表仍在移动或被遮挡，请保持窗口不动后重试')

    def walk(self):
        self.scroll(12000)
        self.wait(.35)
        previous = []
        for _ in range(100):
            rows = self.page()
            if not previous and self.thumb and self.thumb[0] > self.thumb[2]+6*self.thumb[4]:
                raise RuntimeError('未能滚动到列表顶部，请确认操作录制窗口可以接收滚轮输入')
            if previous and len(rows)==len(previous) and all(same_title(a[1],b[1]) and a[0].center==b[0].center for a,b in zip(previous,rows)):
                if self.thumb and self.thumb[1] < self.thumb[3]-12*self.thumb[4]:
                    raise RuntimeError('列表尚未到底，但无法继续滚动，请检查窗口遮挡和滚轮设置')
                return
            overlap = 0
            if previous:
                for count in range(min(len(previous),len(rows)),0,-1):
                    if all(same_title(a[1],b[1]) for a,b in zip(previous[-count:],rows[:count])):
                        overlap = count
                        break
                if overlap == 0:
                    raise RuntimeError('滚动跨度过大或列表内容已变化，请增大录制窗口高度后重新识别')
            yield rows, overlap
            previous = rows
            self.scroll(-120)
            self.wait(.25)
        raise RuntimeError('录制列表超过扫描上限，请使用模拟器搜索缩小列表后重试')

    def scan(self):
        images = []
        try:
            for rows, overlap in self.walk():
                images.extend(title for _,title in rows[overlap:])
                self.post(f'正在识别录制列表：已找到 {len(images)} 项。F9 可停止。')
            if not images:
                raise RuntimeError('录制列表为空')
            # Same-looking truncated names cannot be safely distinguished on later runs.
            for i, title in enumerate(images):
                if any(same_title(title, other) for other in images[:i]):
                    raise RuntimeError('存在显示名称相同的录制，请在模拟器里改为不同名称或使用搜索筛选后重试')
            records = []
            for offset in range(0,len(images),20):
                batch = images[offset:offset+20]
                names = self.ocr(batch)
                records.extend(dict(name=name, image=encode(im)) for name,im in zip(names,batch))
            return records
        finally:
            # Do not send additional input when F9 cancels.
            self.check()
            self.scroll(12000)
            self.wait(.35)

    def locate(self, record):
        target = decode(record['image'])
        for rows, _ in self.walk():
            matches = [match for match,title in rows if same_title(title,target)]
            if len(matches)>1:
                raise RuntimeError('当前列表有多个相同名称，无法唯一定位所选录制')
            if matches:
                return matches[0]
        raise RuntimeError('找不到所选录制，可能已重命名、被遮挡或正在运行；请刷新录制列表')
