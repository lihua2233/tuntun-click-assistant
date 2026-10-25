"""Windows visual countdown click assistant. F6 calibrate, F8 run, F9 stop."""
import ctypes as C
from ctypes import wintypes as W
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk
from engine import ClockReader, CountdownSync, format_time, parse_time
from autocalibration import AutoCalibration, VisualLocator
from recordings import RecordingList, read_names, decode, same_title

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
USER32 = C.WinDLL("user32", use_last_error=True)
GDI32 = C.WinDLL("gdi32", use_last_error=True)
WINMM = C.WinDLL("winmm")


def declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, argtypes
    return fn


declare(USER32, "GetDC", W.HDC, W.HWND)
declare(USER32, "ReleaseDC", C.c_int, W.HWND, W.HDC)
declare(USER32, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
declare(USER32, "SetCursorPos", W.BOOL, C.c_int, C.c_int)
declare(USER32, "GetAsyncKeyState", C.c_short, C.c_int)
declare(USER32, "GetSystemMetrics", C.c_int, C.c_int)
declare(USER32, "MessageBoxW", C.c_int, W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT)
ENUMWINDOWS = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
declare(USER32, "EnumWindows", W.BOOL, ENUMWINDOWS, W.LPARAM)
declare(USER32, "IsWindowVisible", W.BOOL, W.HWND)
declare(USER32, "GetWindowTextW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
declare(USER32, "GetWindowRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
declare(GDI32, "CreateCompatibleDC", W.HDC, W.HDC)
declare(GDI32, "CreateCompatibleBitmap", W.HBITMAP, W.HDC, C.c_int, C.c_int)
declare(GDI32, "SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
declare(GDI32, "DeleteObject", W.BOOL, W.HGDIOBJ)
declare(GDI32, "DeleteDC", W.BOOL, W.HDC)
declare(GDI32, "BitBlt", W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int,
        W.HDC, C.c_int, C.c_int, W.DWORD)
declare(GDI32, "GetDIBits", C.c_int, W.HDC, W.HBITMAP, W.UINT, W.UINT,
        C.c_void_p, C.c_void_p, W.UINT)


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class INPUTUNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(C.Structure):
    _fields_ = [("type", W.DWORD), ("u", INPUTUNION)]


declare(USER32, "SendInput", W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]


def screen_rect():
    return tuple(USER32.GetSystemMetrics(k) for k in (76, 77, 78, 79))


def recording_window_rect():
    matches = []
    @ENUMWINDOWS
    def visit(hwnd, unused):
        name = C.create_unicode_buffer(512)
        USER32.GetWindowTextW(hwnd, name, len(name))
        if USER32.IsWindowVisible(hwnd) and 'MuMu' in name.value and '操作录制' in name.value:
            rect = W.RECT()
            if USER32.GetWindowRect(hwnd, C.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
                matches.append((rect.left, rect.top, rect.right, rect.bottom))
        return True
    USER32.EnumWindows(visit, 0)
    if len(matches) > 1:
        raise RuntimeError('找到多个操作录制窗口，请只保留需要校准的一个')
    return matches[0] if matches else None


def clamp_roi(center, width, height):
    sx, sy, sw, sh = screen_rect()
    x = max(sx, min(center[0]-width//2, sx+sw-width))
    y = max(sy, min(center[1]-height//2, sy+sh-height))
    return x, y, min(width, sw), min(height, sh)


class ScreenCapture:
    """Capture only the timer rectangle with a reusable Windows GDI bitmap."""
    def __init__(self, rect):
        self.x, self.y, self.w, self.h = rect
        self.screen = USER32.GetDC(None)
        self.dc = GDI32.CreateCompatibleDC(self.screen)
        self.bmp = GDI32.CreateCompatibleBitmap(self.screen, self.w, self.h)
        if not self.screen or not self.dc or not self.bmp:
            self.close()
            raise RuntimeError("无法创建屏幕截图缓冲区")
        self.old = GDI32.SelectObject(self.dc, self.bmp)
        self.header = BITMAPINFOHEADER(C.sizeof(BITMAPINFOHEADER), self.w, -self.h, 1, 32)
        self.buffer = (C.c_ubyte * (self.w*self.h*4))()

    def grab(self):
        before = time.perf_counter()
        if not GDI32.BitBlt(self.dc, 0, 0, self.w, self.h, self.screen, self.x, self.y, 0x40CC0020):
            raise RuntimeError("屏幕截图失败")
        # GetDIBits requires the bitmap to be deselected from its DC.
        GDI32.SelectObject(self.dc, self.old)
        try:
            count = GDI32.GetDIBits(self.dc, self.bmp, 0, self.h, self.buffer, C.byref(self.header), 0)
        finally:
            GDI32.SelectObject(self.dc, self.bmp)
        if count != self.h:
            raise RuntimeError("读取屏幕图像失败")
        after = time.perf_counter()
        bgr = np.frombuffer(self.buffer, dtype=np.uint8).reshape(self.h, self.w, 4)[:, :, :3].copy()
        return bgr, (before+after)/2

    def close(self):
        if getattr(self, "old", None):
            GDI32.SelectObject(self.dc, self.old)
        if getattr(self, "bmp", None):
            GDI32.DeleteObject(self.bmp)
        if getattr(self, "dc", None):
            GDI32.DeleteDC(self.dc)
        if getattr(self, "screen", None):
            USER32.ReleaseDC(None, self.screen)
        self.bmp = self.dc = self.screen = None


class Cancelled(Exception):
    pass


def check_stop(event):
    if event.is_set() or USER32.GetAsyncKeyState(0x78) & 0x8000:  # F9
        raise Cancelled()


def wait_until(deadline, event):
    while True:
        check_stop(event)
        left = deadline-time.perf_counter()
        if left <= 0:
            return
        if left > .003:
            event.wait(min(.015, max(.001, left-.002)))


def move_to(point, event):
    check_stop(event)
    if not USER32.SetCursorPos(*point):
        raise RuntimeError("移动鼠标失败")


def click(point, event, already_moved=False):
    if not already_moved:
        move_to(point, event)
    check_stop(event)
    position = W.POINT()
    USER32.GetCursorPos(C.byref(position))
    if abs(position.x-point[0]) > 3 or abs(position.y-point[1]) > 3:
        raise RuntimeError("鼠标位置被移动，本次已取消，请运行时不要移动鼠标")
    down = INPUT(0, INPUTUNION(mi=MOUSEINPUT(0, 0, 0, 0x0002, 0, 0)))
    up = INPUT(0, INPUTUNION(mi=MOUSEINPUT(0, 0, 0, 0x0004, 0, 0)))
    sent = time.perf_counter()
    if USER32.SendInput(1, C.byref(down), C.sizeof(INPUT)) != 1:
        raise RuntimeError("鼠标输入被系统阻止；若模拟器以管理员运行，请同样以管理员运行助手")
    try:
        event.wait(.012)
    finally:
        if USER32.SendInput(1, C.byref(up), C.sizeof(INPUT)) != 1:
            raise RuntimeError("鼠标松开事件发送失败，请手动点击一次鼠标")
    return sent


NAMES = {"macro": "① 录制开关", "challenge": "② 挑战", "start": "③ 开始／确定", "timer": "④ 计时器中心"}
LEGACY_TIME_PRESET_NAME = '佩恩5926~5940，火影手游i敏：59。2秒开'
TIME_DEFAULTS = dict(target='04:59:15', offset='0', delay='1800', width='160', height='54', mode='full', dry=False)
EXTRA_TIME_PRESETS = [
    ('jasmine5940', dict(TIME_DEFAULTS, name='茉莉5940  录制文件在茉莉群  mumu分辨率1270*720', target='04:59:02', width='168', height='54')),
    ('imin5926', dict(TIME_DEFAULTS, name='i敏稳定5926  分享码ADK1YZ  mumu分辨率1600*900', target='04:59:15', width='163', height='51')),
]


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("豚豚护送 · 定时点击助手 v1.3")
        self.root.geometry("720x820")
        self.root.minsize(700, 800)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.closing = False
        self.pending = None
        self.macro_preview = None
        self.keys = {}
        self.points = {}
        self.reader = ClockReader(ASSETS / "digits.npz")
        self.config = {}
        try:
            self.config = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
            self.points = {k: tuple(v) for k, v in self.config.get("points", {}).items()}
        except (OSError, ValueError, TypeError):
            pass
        self.load_time_presets()
        self.make_ui()
        self.apply_time_preset()
        self.recordings = self.config.get('recordings', [])
        try:
            for record in self.recordings:
                if not isinstance(record['name'],str) or decode(record['image']) is None:
                    raise ValueError('invalid recording')
        except (ValueError,TypeError,KeyError):
            self.recordings = []
            self.config.pop('selected_recording',None)
        self.update_recordings(self.recordings, persist=False)
        if self._preset_import_notice:
            self.say(self._preset_import_notice)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(20, self.poll)

    def make_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=5)
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="豚豚护送 · 定时点击 v1.3", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        footer.columnconfigure(0, weight=1, uniform="footer_side")
        footer.columnconfigure(2, weight=1, uniform="footer_side")
        self.public_notice = ttk.Label(footer, text="公益软件 允许转载 严禁收费", foreground="#666666", font=("Microsoft YaHei UI", 9))
        self.public_notice.grid(row=0, column=1)
        self.watermark = ttk.Label(footer, text="@MinDna", foreground="#888888", font=("Microsoft YaHei UI", 9))
        self.watermark.grid(row=0, column=2, sticky="e")
        ttk.Label(outer, text="先校准位置 → 回到活动页 → F8 执行；F9 随时停止助手").pack(anchor="w", pady=(5, 12))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="x")
        prep_tab = ttk.Frame(tabs, padding=6)
        run_tab = ttk.Frame(tabs, padding=6)
        tabs.add(prep_tab, text="  1. 准备与校准  ")
        tabs.add(run_tab, text="  2. 时间与运行设置  ")
        auto = ttk.LabelFrame(prep_tab, text="自动校准（推荐）", padding=10)
        auto.pack(fill="x", pady=(0, 8))
        auto_actions = ttk.Frame(auto)
        auto_actions.pack(fill="x")
        self.auto_button = ttk.Button(auto_actions, text="一键自动校准 → 进入游戏 → 暂停", command=self.auto_calibrate)
        self.auto_button.pack(side="left", fill="x", expand=True)
        ttk.Button(auto_actions, text="查看箭头定位", command=self.show_macro_preview).pack(side="left", padx=(8,0))
        ttk.Label(auto, text="先打开活动页和操作录制列表，列表滚到顶部，停止所有录制。\n会用当前角色进入一局，可能消耗次数；暂停后滚动识别录制名称。\n默认选择第一项，可在下方改选；扫描和选择均不会启动录制。", wraplength=640).pack(anchor="w", pady=(6, 0))
        choose_row = ttk.Frame(auto)
        choose_row.pack(fill='x', pady=(8,0))
        ttk.Label(choose_row, text='使用录制：').pack(side='left')
        self.recording_choice = ttk.Combobox(choose_row, state='readonly', width=39)
        self.recording_choice.pack(side='left', fill='x', expand=True)
        self.recording_choice.bind('<<ComboboxSelected>>', self.choose_recording)
        self.refresh_button = ttk.Button(choose_row, text='刷新录制列表', command=self.refresh_recordings)
        self.refresh_button.pack(side='left', padx=(6,0))
        group = ttk.LabelFrame(prep_tab, text="手动校准（备用）· 只记录位置，不会点击", padding=10)
        group.pack(fill="x")
        ttk.Label(group, text="点击下方校准按钮，移到目标中央后按 F6。调整窗口后需重新校准。", wraplength=600).pack(anchor="w")
        self.cal_buttons = []
        self.point_labels = {}
        for key, name in NAMES.items():
            row = ttk.Frame(group)
            row.pack(fill="x", pady=3)
            button = ttk.Button(row, text=f"校准{name}", command=lambda k=key: self.calibrate(k), width=22)
            button.pack(side="left")
            self.cal_buttons.append(button)
            label = ttk.Label(row, text=self.point_text(key))
            label.pack(side="left", padx=10)
            self.point_labels[key] = label
        ttk.Label(group, text="① 可从上方选择录制；手动校准①后改用手动位置。\n③ 视频里对应选人界面的“确定”，请先选好角色。", wraplength=640).pack(anchor="w", pady=(5, 0))
        preset_group = ttk.LabelFrame(run_tab, text='使用时间', padding=10)
        preset_group.pack(fill='x', pady=(0,8))
        self.time_choice = ttk.Combobox(preset_group, state='readonly', width=50)
        self.time_choice.pack(fill='x')
        self.time_choice.bind('<<ComboboxSelected>>', self.choose_time_preset)
        preset_actions = ttk.Frame(preset_group)
        preset_actions.pack(fill='x', pady=(6,0))
        self.new_time_button = ttk.Button(preset_actions, text='新建', command=self.new_time_preset)
        self.delete_time_button = ttk.Button(preset_actions, text='删除', command=self.delete_time_preset)
        self.rename_time_button = ttk.Button(preset_actions, text='重命名', command=self.rename_time_preset)
        for button in (self.new_time_button,self.delete_time_button,self.rename_time_button):
            button.pack(side='left',padx=(0,6))
        self.time_count = ttk.Label(preset_actions)
        self.time_count.pack(side='right')
        opts = ttk.LabelFrame(run_tab, text="触发设置", padding=10)
        opts.pack(fill="x", pady=(0, 6))
        self.target = tk.StringVar(value=str(self.config.get("target", "04:59:15")))
        self.offset = tk.StringVar(value=str(self.config.get("offset", 0)))
        self.delay = tk.StringVar(value=str(self.config.get("delay", 1800)))
        self.width = tk.StringVar(value=str(self.config.get("width", 160)))
        self.height = tk.StringVar(value=str(self.config.get("height", 54)))
        self.mode = tk.StringVar(value=self.config.get("mode", "full"))
        self.dry = tk.BooleanVar(value=False)
        for i, (label, variable, tip) in enumerate([
            ("目标倒计时", self.target, "分 : 秒 : 百分之一秒"),
            ("时间微调（毫秒）", self.offset, "负数提前，正数延后；0 = 不调整"),
            ("挑战→开始间隔（毫秒）", self.delay, "等待选人界面出现，默认 1800"),
        ]):
            ttk.Label(opts, text=label).grid(row=i, column=0, sticky="w", pady=4)
            if variable is self.target:
                time_fields = ttk.Frame(opts)
                time_fields.grid(row=i, column=1, padx=10, sticky='w')
                self.target_parts = [tk.StringVar() for _ in range(3)]
                self.target_entries = []
                self._target_sync = False
                validate = (self.root.register(lambda value,action: action=='-1' or value=='' or (len(value)<=2 and value.isascii() and value.isdigit())), '%P', '%d')
                for part, value in enumerate(self.target_parts):
                    if part:
                        ttk.Label(time_fields,text=':').pack(side='left',padx=2)
                    entry = ttk.Entry(time_fields,textvariable=value,width=3,justify='center',validate='key',validatecommand=validate)
                    entry.pack(side='left')
                    entry.bind('<FocusOut>',lambda event,v=value: v.set(v.get().zfill(2)) if v.get().isdigit() else None)
                    self.target_entries.append(entry)
                    value.trace_add('write',self.target_parts_changed)
                self.target.trace_add('write',self.target_text_changed)
                self.target_text_changed()
            else:
                ttk.Entry(opts, textvariable=variable, width=13).grid(row=i, column=1, padx=10)
            ttk.Label(opts, text=tip).grid(row=i, column=2, sticky="w")
        ttk.Label(opts, text="识别框宽 × 高（像素）").grid(row=3, column=0, sticky="w", pady=4)
        size = ttk.Frame(opts)
        size.grid(row=3, column=1, columnspan=2, sticky="w", padx=10)
        ttk.Entry(size, textvariable=self.width, width=6).pack(side="left")
        ttk.Label(size, text=" × ").pack(side="left")
        ttk.Entry(size, textvariable=self.height, width=6).pack(side="left")
        ttk.Button(size, text="预览识别区域", command=self.preview).pack(side="left", padx=8)
        modes = ttk.Frame(run_tab)
        modes.pack(fill="x")
        ttk.Radiobutton(modes, text="完整流程：挑战 → 开始 → 定时点击开关", variable=self.mode, value="full").pack(anchor="w")
        ttk.Radiobutton(modes, text="仅监听计时器：我手动开始挑战", variable=self.mode, value="watch").pack(anchor="w")
        ttk.Checkbutton(modes, text="试运行：所有步骤均不点击，需手动开始挑战", variable=self.dry).pack(anchor="w", pady=4)
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=10)
        self.run_button = ttk.Button(actions, text="开始执行（F8）", command=self.start)
        self.run_button.pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="停止助手（F9）", command=self.cancel).pack(side="left", padx=(10, 0))
        self.status = tk.StringVar(value="先一键自动校准，或手动记录四个位置。F9 只停止本助手。")
        ttk.Label(outer, textvariable=self.status, wraplength=620, foreground="#185b84").pack(anchor="w", pady=5)
        self.log = tk.Text(outer, height=5, font=("Microsoft YaHei UI", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

    def target_text_changed(self, *unused):
        if self._target_sync:
            return
        self._target_sync = True
        try:
            parts = self.target.get().strip().replace('：',':').replace('.',':').split(':')
            for index, variable in enumerate(self.target_parts):
                variable.set(parts[index] if index<len(parts) else '')
        finally:
            self._target_sync = False

    def target_parts_changed(self, *unused):
        if self._target_sync:
            return
        self._target_sync = True
        try:
            parts = [v.get() for v in self.target_parts]
            self.target.set(':'.join(v.zfill(2) if v else '' for v in parts) if any(parts) else '')
        finally:
            self._target_sync = False

    def point_text(self, key):
        return f"已记录 {self.points[key]}" if key in self.points else "尚未校准"

    def say(self, message):
        self.status.set(message)
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def save(self):
        self.store_time_preset()
        self.config.update(points=self.points, target=self.target.get(), offset=self.offset.get(),
                           delay=self.delay.get(), width=self.width.get(), height=self.height.get(), mode=self.mode.get())
        try:
            (ROOT / "settings.json").write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self.say(f"设置无法保存：{error}")

    def load_time_presets(self):
        saved = self.config.get('time_presets')
        self.time_presets = []
        if isinstance(saved,list):
            for item in saved[:10]:
                if isinstance(item,dict) and isinstance(item.get('name'),str) and item['name'].strip():
                    self.time_presets.append(dict(name=item['name'], **{k:item.get(k,v) for k,v in TIME_DEFAULTS.items()}))
        selected = self.config.get('selected_time_preset',0)
        previous = self.time_presets[selected] if isinstance(selected,int) and 0<=selected<len(self.time_presets) else None
        if not self.config.get('removed_legacy_time_preset'):
            self.time_presets = [item for item in self.time_presets if item['name'].strip()!=LEGACY_TIME_PRESET_NAME]
            self.config['removed_legacy_time_preset'] = True
        if not self.time_presets:
            self.time_presets = [dict(preset) for _,preset in EXTRA_TIME_PRESETS]
        # Import each bundled addition once, preserving user edits, deletions and selection.
        imported = self.config.get('imported_time_presets', [])
        imported = set(item for item in imported if isinstance(item,str)) if isinstance(imported,list) else set()
        skipped = 0
        for identity, preset in EXTRA_TIME_PRESETS:
            if identity in imported:
                continue
            name = ' '.join(preset['name'].split())
            if any(' '.join(item['name'].split()) == name for item in self.time_presets):
                imported.add(identity)
            elif len(self.time_presets) < 10:
                self.time_presets.append(dict(preset))
                imported.add(identity)
            else:
                skipped += 1
        self.config['imported_time_presets'] = sorted(imported)
        self._preset_import_notice = f'时间预设已达 10 个，另有 {skipped} 个新增预设未加入；删除不需要的预设后，重启程序即可补充。' if skipped else ''
        self.time_index = next((i for i,item in enumerate(self.time_presets) if item is previous),0)
        self._preset_control_state = None

    def store_time_preset(self):
        self.time_presets[self.time_index].update({key:getattr(self,key).get() for key in TIME_DEFAULTS})
        self.config['time_presets'] = self.time_presets
        self.config['selected_time_preset'] = self.time_index

    def apply_time_preset(self):
        preset = self.time_presets[self.time_index]
        for key in TIME_DEFAULTS:
            getattr(self,key).set(preset[key])
        self.time_choice.configure(values=[f'{i+1}. {item["name"]}' for i,item in enumerate(self.time_presets)])
        self.time_choice.current(self.time_index)
        self.time_count.configure(text=f'{len(self.time_presets)} / 10 个预设')
        self.update_time_controls()

    def update_time_controls(self):
        state = (self.busy, len(self.time_presets))
        if state == self._preset_control_state:
            return
        self._preset_control_state = state
        self.time_choice.configure(state='disabled' if self.busy else 'readonly')
        self.new_time_button.configure(state='disabled' if self.busy or len(self.time_presets)>=10 else 'normal')
        self.delete_time_button.configure(state='disabled' if self.busy or len(self.time_presets)<=1 else 'normal')
        self.rename_time_button.configure(state='disabled' if self.busy else 'normal')

    def choose_time_preset(self, event=None):
        index = self.time_choice.current()
        if self.busy or not 0<=index<len(self.time_presets):
            self.time_choice.current(self.time_index)
            return
        self.store_time_preset()
        self.time_index = index
        self.apply_time_preset()
        self.save()
        self.say(f'已使用时间预设：{self.time_presets[index]["name"]}')

    def new_time_preset(self):
        if self.busy or len(self.time_presets)>=10:
            return
        self.store_time_preset()
        number = 2
        names = {p['name'] for p in self.time_presets}
        while f'预设{number}' in names:
            number += 1
        # Copy preset 1, including its latest edits, but leave only target blank.
        self.time_presets.append(dict(self.time_presets[0], name=f'预设{number}',target=''))
        self.time_index = len(self.time_presets)-1
        self.apply_time_preset()
        self.save()
        self.say('已新建时间预设：请填写目标倒计时，其余设置已复制预设1。')

    def delete_time_preset(self):
        if self.busy or len(self.time_presets)<=1:
            return
        removed = self.time_presets.pop(self.time_index)
        self.time_index = min(self.time_index,len(self.time_presets)-1)
        self.apply_time_preset()
        self.save()
        self.say(f'已删除时间预设：{removed["name"]}')

    def rename_time_preset(self):
        if self.busy:
            return
        preset = self.time_presets[self.time_index]
        name = simpledialog.askstring('重命名时间预设','请输入预设名称：',initialvalue=preset['name'],parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name or any(i!=self.time_index and p['name']==name for i,p in enumerate(self.time_presets)):
            messagebox.showerror('检查名称','名称不能为空，也不能与其他时间预设重复。',parent=self.root)
            return
        self.store_time_preset()
        preset['name'] = name
        self.apply_time_preset()
        self.save()
        self.say(f'时间预设已重命名为：{name}')

    def calibrate(self, key):
        if self.busy:
            return
        self.pending = key
        self.say(f"请把鼠标移到{NAMES[key]}中央，按 F6 记录；F9 取消。")
        self.root.iconify()

    def selected_recording(self):
        index = self.config.get('selected_recording')
        return self.recordings[index] if isinstance(index,int) and 0 <= index < len(self.recordings) else None

    def update_recordings(self, records, persist=True):
        previous = self.selected_recording() if hasattr(self,'recordings') else None
        self.recordings = records
        self.config['recordings'] = records
        self.recording_choice.configure(values=[f'{i+1}. {r["name"]}' for i,r in enumerate(records)])
        index = next((i for i,r in enumerate(records) if previous and same_title(decode(r['image']),decode(previous['image']))), 0)
        if records and (persist or self.config.get('selected_recording') is not None):
            self.config['selected_recording'] = index
            self.recording_choice.current(index)
            self.point_labels['macro'].configure(text='执行前按所选录制重新定位')
        else:
            self.config.pop('selected_recording',None)
            self.recording_choice.set('手动位置' if 'macro' in self.points else '请先自动校准或刷新列表')
        if persist:
            self.macro_preview = None
            self.save()
            self.say(f'已识别 {len(records)} 项录制，可从“使用录制”中选择；名称以原始截图为准。')

    def choose_recording(self, event=None):
        if self.busy:
            return
        index = self.recording_choice.current()
        if not 0 <= index < len(self.recordings):
            return
        self.config['selected_recording'] = index
        self.macro_preview = None
        self.point_labels['macro'].configure(text='执行前按所选录制重新定位')
        self.save()
        self.say(f'已选择：{self.selected_recording()["name"]}。执行前自动找到该行蓝色箭头。')

    def recording_service(self, cap, sx, sy):
        window = recording_window_rect()
        if not window:
            raise RuntimeError('请打开 MuMu 操作录制窗口')
        region = tuple(v-(sx if i%2==0 else sy) for i,v in enumerate(window))
        def check():
            check_stop(self.stop)
        def wait(seconds):
            self.stop.wait(seconds)
            check()
        def scroll(delta):
            check()
            if recording_window_rect() != window:
                raise RuntimeError('录制窗口位置已变化，请重新校准')
            x,y,r,b = window
            move_to((round(x+(r-x)*.35), round(y+(b-y)*.55)),self.stop)
            packet = INPUT(type=0, u=INPUTUNION(mi=MOUSEINPUT(0,0,delta & 0xffffffff,0x0800,0,0)))
            if USER32.SendInput(1,C.byref(packet),C.sizeof(INPUT)) != 1:
                raise RuntimeError('无法滚动录制列表，请检查权限')
        locator = VisualLocator(ASSETS/'calibration.npz',check,region)
        return RecordingList(locator,cap.grab,scroll,wait,check,self.post,
                             lambda images: read_names(images,ASSETS/'recording_ocr.ps1',check))

    def refresh_recordings(self):
        if self.busy or self.pending:
            return
        self.stop.clear()
        self.busy = True
        self.recording_choice.configure(state='disabled')
        self.refresh_button.configure(state='disabled')
        self.run_button.configure(state='disabled')
        self.auto_button.configure(state='disabled')
        for button in self.cal_buttons:
            button.configure(state='disabled')
        self.say('3 秒后滚动识别所有录制名称，不会进入游戏或启动录制。F9 可停止。')
        self.root.iconify()
        threading.Thread(target=self.recordings_worker,daemon=True).start()

    def recordings_worker(self):
        cap = None
        try:
            wait_until(time.perf_counter()+3,self.stop)
            sx,sy,sw,sh = screen_rect()
            cap = ScreenCapture((sx,sy,sw,sh))
            records = self.recording_service(cap,sx,sy).scan()
            check_stop(self.stop)
            self.events.put(('recordings',records))
        except Cancelled:
            self.post('录制列表识别已停止，原选择保留。')
        except Exception as error:
            self.post(f'录制列表识别失败：{error}。原选择保留。')
        finally:
            if cap:
                cap.close()
            self.events.put(('auto_done',None))

    def auto_calibrate(self):
        if self.busy or self.pending:
            return
        if self.dry.get():
            messagebox.showinfo("试运行已开启", "自动校准需要点击挑战、确定和暂停。请先取消勾选试运行。", parent=self.root)
            return
        self.stop.clear()
        self.busy = True
        self.recording_choice.configure(state='disabled')
        self.refresh_button.configure(state='disabled')
        self.run_button.configure(state="disabled")
        self.auto_button.configure(state="disabled")
        for button in self.cal_buttons:
            button.configure(state="disabled")
        self.say("3 秒后自动校准。请保持活动页、录制列表可见，不要移动窗口和鼠标。")
        self.root.iconify()
        threading.Thread(target=self.auto_worker, daemon=True).start()

    def auto_worker(self):
        cap = None
        try:
            wait_until(time.perf_counter()+3, self.stop)
            sx, sy, sw, sh = screen_rect()
            cap = ScreenCapture((sx, sy, sw, sh))
            def check():
                check_stop(self.stop)
            def wait(seconds):
                check()
                self.stop.wait(seconds)
                check()
            def send(point):
                return click((point[0]+sx, point[1]+sy), self.stop)
            window = recording_window_rect()
            region = tuple(v-(sx if i % 2 == 0 else sy) for i,v in enumerate(window)) if window else None
            locator = VisualLocator(ASSETS / "calibration.npz", check, region)
            def preview(frame, match):
                x,y,r,b = match.box
                pad = max(12, round(12*match.scale))
                left,top = max(0,x-pad),max(0,y-pad)
                picture = frame[top:b+pad,left:r+pad].copy()
                cv2.rectangle(picture,(x-left-2,y-top-2),(r-left+2,b-top+2),(0,230,0),2)
                self.events.put(("macro_preview",picture))
            flow = AutoCalibration(locator, self.reader, cap.grab, send, wait, check, self.post, preview=preview)
            result = flow.run()
            check()
            result["points"] = {k: (p[0]+sx, p[1]+sy) for k, p in result["points"].items()}
            self.events.put(("calibrated", result))
            self.post('游戏位置校准完成；开始滚动识别操作录制名称。')
            try:
                records = self.recording_service(cap,sx,sy).scan()
                self.events.put(('recordings',records))
            except Cancelled:
                self.post('名称识别已取消；游戏位置已保存。可稍后刷新录制列表。')
            except Exception as error:
                self.post(f'游戏位置已保存，但名称识别失败：{error}。可稍后刷新录制列表。')
        except Cancelled:
            self.post("自动校准已停止，旧位置保留。若已经进入游戏，请手动暂停。")
        except Exception as e:
            self.post(f"自动校准未完成：{e}。旧位置保留；若已进入游戏，请手动暂停。")
        finally:
            if cap:
                cap.close()
            self.events.put(("auto_done", None))

    def show_macro_preview(self):
        if self.busy:
            return
        record = self.selected_recording()
        if self.macro_preview is None and not record:
            messagebox.showinfo("箭头定位", "自动校准识别到箭头后，这里会显示实际截图与绿色定位框。", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("所选录制 · 名称原图与箭头定位")
        source = decode(record['image']) if record else self.macro_preview
        im = Image.fromarray(cv2.cvtColor(source, cv2.COLOR_BGR2RGB))
        im = im.resize((im.width*2, im.height*2))
        im.thumbnail((850,250))
        photo = ImageTk.PhotoImage(im)
        label = ttk.Label(win, image=photo)
        label.image = photo
        label.pack(padx=16,pady=16)
        text = '所选录制的名称原始截图。执行前重新定位该行的蓝色播放箭头。\n文字识别可能有误；被省略的长名称请在模拟器中核对。' if record else '绿色框内为识别到的蓝色播放箭头，点击位置在框中心。'
        ttk.Label(win,text=text,wraplength=700).pack(padx=16,pady=(0,16))

    def collect_point(self):
        if self.pending is None:
            return
        p = W.POINT()
        if not USER32.GetCursorPos(C.byref(p)):
            return
        key, self.pending = self.pending, None
        self.points[key] = (p.x, p.y)
        if key == 'macro':
            self.config.pop('selected_recording',None)
            self.recording_choice.set('手动位置')
            self.macro_preview = None
        self.config.pop("auto_scale", None)
        self.point_labels[key].configure(text=self.point_text(key))
        self.save()
        self.root.deiconify()
        self.say(f"{NAMES[key]}已校准：({p.x}, {p.y})")

    def roi(self):
        width, height = int(self.width.get()), int(self.height.get())
        if not 60 <= width <= 600 or not 20 <= height <= 200:
            raise ValueError("识别框宽需为 60～600，高需为 20～200")
        return clamp_roi(self.points["timer"], width, height)

    def preview(self):
        if self.busy:
            return
        if "timer" not in self.points:
            messagebox.showinfo("先校准", "请先校准计时器中心。", parent=self.root)
            return
        try:
            roi = self.roi()
        except ValueError as e:
            messagebox.showerror("检查参数", str(e), parent=self.root)
            return
        self.root.iconify()
        self.root.after(500, lambda: self.show_preview(roi))

    def show_preview(self, roi):
        cap = None
        try:
            cap = ScreenCapture(roi)
            frame, _ = cap.grab()
            result = self.reader.read(frame)
            self.root.deiconify()
            win = tk.Toplevel(self.root)
            win.title("计时器区域预览 · 此处不点击")
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            im = Image.fromarray(rgb)
            im.thumbnail((600, 200))
            if im.width < 400:
                im = im.resize((im.width*3, im.height*3))
            photo = ImageTk.PhotoImage(im)
            label = ttk.Label(win, image=photo)
            label.image = photo
            label.pack(padx=12, pady=12)
            text = f"当前识别：{format_time(result.seconds)}" if result else "当前未识别到计时器。请检查区域是否包含完整 MM:SS:CC。"
            ttk.Label(win, text=text, wraplength=580).pack(padx=12, pady=12)
        except Exception as e:
            self.root.deiconify()
            messagebox.showerror("预览失败", str(e), parent=self.root)
        finally:
            if cap:
                cap.close()

    def start(self):
        if self.busy or self.pending:
            return
        try:
            dry = self.dry.get()
            if not all(v.get().strip() for v in self.target_parts):
                raise ValueError('请完整填写目标倒计时的三个小框：分、秒、百分之一秒')
            full = self.mode.get() == "full" and not dry
            needed = ["timer"] + ([] if dry else ["macro"]) + (["challenge", "start"] if full else [])
            missing = [NAMES[k] for k in needed if k not in self.points and not (k=='macro' and self.selected_recording())]
            if missing:
                raise ValueError("请先校准：" + "、".join(missing))
            target = parse_time(self.target.get())
            offset = float(self.offset.get())
            delay = float(self.delay.get())
            if not all(math.isfinite(x) for x in (target, offset, delay)):
                raise ValueError("参数必须是有限数字")
            if not 0 < target < 300 or not -5000 <= offset <= 5000 or not 200 <= delay <= 30000:
                raise ValueError("目标须在 00:00:01～04:59:99；微调 ±5000 毫秒；界面间隔 200～30000 毫秒")
            effective = target-offset/1000
            if not .01 <= effective <= 299.8:
                raise ValueError("微调后的实际触发时间须在 00:00:01～04:59:80，留出对时所需时间")
            cfg = dict(target=target, offset=offset, delay=delay, dry=dry, full=full,
                       roi=self.roi(), points=dict(self.points), auto_scale=self.config.get("auto_scale"), recording=self.selected_recording())
        except (ValueError, KeyError) as e:
            messagebox.showerror("检查设置", str(e), parent=self.root)
            return
        self.save()
        self.stop.clear()
        self.busy = True
        self.recording_choice.configure(state='disabled')
        self.refresh_button.configure(state='disabled')
        self.run_button.configure(state="disabled")
        self.auto_button.configure(state="disabled")
        for button in self.cal_buttons:
            button.configure(state="disabled")
        self.say(f"3 秒后{'试运行' if dry else '执行'}；实际目标 {format_time(effective)}。请勿移动窗口或鼠标。")
        self.root.iconify()
        threading.Thread(target=self.worker, args=(cfg,), daemon=True).start()

    def post(self, text):
        self.events.put(("message", text))

    def verify_auto_button(self, cfg, name):
        scale = cfg.get("auto_scale")
        if not scale:
            return
        locator = VisualLocator(ASSETS / "calibration.npz", lambda: check_stop(self.stop))
        height,width = locator.assets[name].shape[:2]
        point = cfg["points"][name]
        rect = clamp_roi(point, round(width*scale+40), round(height*scale+40))
        cap = ScreenCapture(rect)
        try:
            frame, _ = cap.grab()
        finally:
            cap.close()
        match = locator.find(frame,name,scale=scale)
        if not match or max(abs(match.center[i]+rect[i]-point[i]) for i in range(2)) > 6:
            raise RuntimeError(f'保存位置未显示{NAMES[name]}。校准暂停后请先返回活动页；移动窗口后需重新校准')

    def worker(self, cfg):
        cap = None
        WINMM.timeBeginPeriod(1)
        try:
            wait_until(time.perf_counter()+3, self.stop)
            if cfg.get('recording') and not cfg['dry']:
                self.post(f'正在定位所选录制：{cfg["recording"]["name"]}。请等待提示后再手动开局。')
                sx,sy,sw,sh = screen_rect()
                cap = ScreenCapture((sx,sy,sw,sh))
                match = self.recording_service(cap,sx,sy).locate(cfg['recording'])
                cfg['points']['macro'] = (match.center[0]+sx,match.center[1]+sy)
                cap.close()
                cap = None
                self.post('所选录制已定位。')
            if cfg["full"]:
                if cfg.get("auto_scale"):
                    self.verify_auto_button(cfg, "challenge")
                self.post("点击挑战，等待选人界面。")
                click(cfg["points"]["challenge"], self.stop)
                wait_until(time.perf_counter()+cfg["delay"]/1000, self.stop)
                if cfg.get("auto_scale"):
                    self.verify_auto_button(cfg, "start")
                click(cfg["points"]["start"], self.stop)
                self.post("已点击开始／确定，等待屏幕计时器。")
            else:
                self.post("已开始监听，请手动开始挑战。")
            cap = ScreenCapture(cfg["roi"])
            sync = CountdownSync()
            started = time.perf_counter()
            last_recognized = None
            latest_value = None
            next_report = 0
            deadline = None
            moved = False
            while True:
                check_stop(self.stop)
                now = time.perf_counter()
                if now-started > 420:
                    raise RuntimeError("任务超过 7 分钟，已停止")
                if last_recognized is None and now-started > 60:
                    raise RuntimeError("60 秒内未识别到计时器。请检查窗口、校准位置及游戏是否开始")
                if last_recognized is not None and now-last_recognized > .5:
                    raise RuntimeError("计时器消失或无法识别超过 0.5 秒，已取消点击")
                # Reserve the final 15 ms for scheduling, not another screen capture.
                if deadline is not None and deadline-now <= .015:
                    if now-deadline > .030:
                        raise RuntimeError("已错过目标超过 30 毫秒，未补点；请重试")
                    if not sync.fresh(now) or now-(last_recognized or 0) > .10:
                        raise RuntimeError("临近目标时计时器停止更新，已取消点击")
                    if not cfg["dry"] and not moved:
                        move_to(cfg["points"]["macro"], self.stop)
                        moved = True
                    wait_until(deadline, self.stop)
                    if time.perf_counter()-deadline > .030:
                        raise RuntimeError("系统调度延迟超过 30 毫秒，未点击")
                    sent = time.perf_counter() if cfg["dry"] else click(cfg["points"]["macro"], self.stop, True)
                    record = dict(local_time=time.strftime("%Y-%m-%d %H:%M:%S"), dry_run=cfg["dry"],
                                  target=format_time(cfg["target"]), offset_ms=cfg["offset"],
                                  effective_target=format_time(cfg["target"]-cfg["offset"]/1000),
                                  last_reading=format_time(latest_value),
                                  scheduler_error_ms=round((sent-deadline)*1000, 3),
                                  recording=cfg.get('recording',{}).get('name') if cfg.get('recording') else None,
                                  note="调度误差不等于游戏内实际误差；游戏受理结果未验证")
                    try:
                        with (ROOT / "runs.jsonl").open("a", encoding="utf-8") as f:
                            f.write(json.dumps(record, ensure_ascii=False)+"\n")
                    except OSError as e:
                        self.post(f"日志保存失败：{e}")
                    self.post(f"{'试运行命中，未点击' if cfg['dry'] else '已发送一次录制开关点击'}；调度偏差 {record['scheduler_error_ms']:+.1f} ms。游戏内实际时间需复核。")
                    break
                frame, captured = cap.grab()
                reading = self.reader.read(frame)
                if reading:
                    last_recognized = captured
                    latest_value = reading.seconds
                    sync.update(captured, reading.seconds)
                    deadline = sync.deadline(cfg["target"], cfg["offset"])
                    if deadline is not None:
                        left = deadline-time.perf_counter()
                        if left < -.030:
                            raise RuntimeError("对时完成时目标时间已过，已取消点击。请在挑战开始前启动助手")
                        if not cfg["dry"] and left <= .15 and not moved:
                            move_to(cfg["points"]["macro"], self.stop)
                            moved = True
                        if time.perf_counter() > next_report:
                            self.post(f"已对时：{format_time(reading.seconds)}；距触发约 {max(0,left):.2f} 秒。")
                            next_report = time.perf_counter()+.5
                if sync.last_change is not None and time.perf_counter()-sync.last_change > .35:
                    raise RuntimeError("计时器暂停或连续读数不一致，已取消点击")
                self.stop.wait(.002)
        except Cancelled:
            self.post("已停止助手。若操作录制已经启动，请在模拟器里停止录制。")
        except Exception as e:
            self.post(f"本次停止：{e}")
        finally:
            if cap:
                cap.close()
            WINMM.timeEndPeriod(1)
            self.events.put(("done", None))

    def cancel(self):
        self.stop.set()
        if self.pending:
            self.pending = None
            self.root.deiconify()
            self.say("已取消校准")

    def poll(self):
        self.update_time_controls()
        for key, action in [(0x75, self.collect_point), (0x77, self.start), (0x78, self.cancel)]:
            down = bool(USER32.GetAsyncKeyState(key) & 0x8000)
            if down and not self.keys.get(key, False) and self.root.grab_current() is None:
                action()
            self.keys[key] = down
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "message":
                    self.say(value)
                elif kind == "macro_preview":
                    self.macro_preview = value
                elif kind == 'recordings':
                    self.update_recordings(value)
                elif kind == "calibrated":
                    self.points.update(value["points"])
                    self.config["auto_scale"] = value["scale"]
                    self.width.set(str(value["width"]))
                    self.height.set(str(value["height"]))
                    for key, label in self.point_labels.items():
                        label.configure(text=self.point_text(key))
                    if self.selected_recording():
                        self.point_labels['macro'].configure(text='执行前按所选录制重新定位')
                    self.save()
                    if value["paused"]:
                        self.say("自动校准完成并已检测到暂停。正式执行前请手动返回活动页，再按 F8。")
                    else:
                        self.say("位置已保存，已发送一次暂停点击，但无法确认暂停成功，请检查游戏。正式执行前返回活动页。")
                elif kind in ("done", "auto_done"):
                    self.busy = False
                    self.run_button.configure(state="normal")
                    self.auto_button.configure(state="normal")
                    self.recording_choice.configure(state='readonly')
                    self.refresh_button.configure(state='normal')
                    for button in self.cal_buttons:
                        button.configure(state="normal")
                    if kind == "auto_done" and not self.closing:
                        self.root.deiconify()
        except queue.Empty:
            pass
        if self.closing and not self.busy:
            self.root.destroy()
            return
        self.root.after(20, self.poll)

    def close(self):
        self.closing = True
        self.cancel()
        self.save()


def main():
    try:
        USER32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except (AttributeError, OSError):
        USER32.SetProcessDPIAware()
    cv2.setNumThreads(1)
    if len(sys.argv) in (3,4) and sys.argv[1] == "--self-test":
        # Packaging smoke test: initialize widgets/assets without sending input.
        app = App()
        app.root.withdraw()
        app.root.update_idletasks()
        visual = VisualLocator(ASSETS / "calibration.npz")
        report = dict(ok=True, templates=len(app.reader.labels), calibration_assets=list(visual.assets), input_size=C.sizeof(INPUT),
                      recording_ocr=(ASSETS/'recording_ocr.ps1').is_file(), recording_picker=bool(app.recording_choice),
                      time_preset_picker=bool(app.time_choice), time_preset_count=len(app.time_presets),
                      tkinter=str(tk.TkVersion), opencv=cv2.__version__, numpy=np.__version__)
        if len(sys.argv)==4 and sys.argv[3]=='--ocr':
            sample = np.full((30,250,3),50,np.uint8)
            cv2.putText(sample,'Test5912',(8,23),cv2.FONT_HERSHEY_SIMPLEX,.65,(240,240,240),1,cv2.LINE_AA)
            report['ocr_test'] = read_names([sample],ASSETS/'recording_ocr.ps1')
            report['ok'] = report['ok'] and '5912' in report['ocr_test'][0]
        Path(sys.argv[2]).write_text(json.dumps(report), encoding="utf-8")
        app.root.destroy()
        return
    kernel = C.WinDLL("kernel32", use_last_error=True)
    declare(kernel, "CreateMutexW", W.HANDLE, C.c_void_p, W.BOOL, W.LPCWSTR)
    declare(kernel, "CloseHandle", W.BOOL, W.HANDLE)
    mutex = kernel.CreateMutexW(None, False, "Local\\TuntunCountdownClickAssistantV1")
    if not mutex:
        raise RuntimeError("无法创建单实例保护")
    if C.get_last_error() == 183:
        USER32.MessageBoxW(None, "点击助手已经打开，请从任务栏打开现有窗口。", "点击助手", 0)
        kernel.CloseHandle(mutex)
        return
    app = App()
    try:
        app.root.mainloop()
    finally:
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
