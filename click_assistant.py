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
from tkinter import messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk
from engine import ClockReader, CountdownSync, format_time, parse_time

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


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("豚豚护送 · 定时点击助手 (MinDna开发 v1.0)")
        self.root.geometry("680x650")
        self.root.minsize(640, 600)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.closing = False
        self.pending = None
        self.keys = {}
        self.points = {}
        self.reader = ClockReader(ASSETS / "digits.npz")
        self.config = {}
        try:
            self.config = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
            self.points = {k: tuple(v) for k, v in self.config.get("points", {}).items()}
        except (OSError, ValueError, TypeError):
            pass
        self.make_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(20, self.poll)

    def make_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=5)
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="豚豚护送 · 定时点击 (MinDna开发 v1.0)", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="先校准位置 → 回到活动页 → F8 执行；F9 随时停止助手").pack(anchor="w", pady=(5, 12))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="x")
        prep_tab = ttk.Frame(tabs, padding=6)
        run_tab = ttk.Frame(tabs, padding=6)
        tabs.add(prep_tab, text="  1. 准备与校准  ")
        tabs.add(run_tab, text="  2. 时间与运行设置  ")
        group = ttk.LabelFrame(prep_tab, text="准备工作 · 校准时只记录位置，不会点击", padding=10)
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
        ttk.Label(group, text="① 指向“5912豚豚护送58_88左右点-ML”一行最右侧蓝色播放按钮。\n③ 视频里对应选人界面的“确定”，请先选好角色。", wraplength=600).pack(anchor="w", pady=(5, 0))
        opts = ttk.LabelFrame(run_tab, text="触发设置", padding=10)
        opts.pack(fill="x", pady=(0, 6))
        self.target = tk.StringVar(value=str(self.config.get("target", "04:58:95")))
        self.offset = tk.StringVar(value=str(self.config.get("offset", 0)))
        self.delay = tk.StringVar(value=str(self.config.get("delay", 1800)))
        self.width = tk.StringVar(value=str(self.config.get("width", 160)))
        self.height = tk.StringVar(value=str(self.config.get("height", 54)))
        self.mode = tk.StringVar(value=self.config.get("mode", "full"))
        self.dry = tk.BooleanVar(value=False)
        for i, (label, variable, tip) in enumerate([
            ("目标倒计时", self.target, "分:秒:百分之一秒，如 04:58:95"),
            ("时间微调（毫秒）", self.offset, "负数提前，正数延后；0 = 不调整"),
            ("挑战→开始间隔（毫秒）", self.delay, "等待选人界面出现，默认 1800"),
        ]):
            ttk.Label(opts, text=label).grid(row=i, column=0, sticky="w", pady=4)
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
        self.status = tk.StringVar(value="先校准四个位置。F9 只停止本助手，已启动的操作录制请在模拟器里停止。")
        ttk.Label(outer, textvariable=self.status, wraplength=620, foreground="#185b84").pack(anchor="w", pady=5)
        self.log = tk.Text(outer, height=5, font=("Microsoft YaHei UI", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

    def point_text(self, key):
        return f"已记录 {self.points[key]}" if key in self.points else "尚未校准"

    def say(self, message):
        self.status.set(message)
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def save(self):
        self.config.update(points=self.points, target=self.target.get(), offset=self.offset.get(),
                           delay=self.delay.get(), width=self.width.get(), height=self.height.get(), mode=self.mode.get())
        try:
            (ROOT / "settings.json").write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self.say(f"设置无法保存：{error}")

    def calibrate(self, key):
        if self.busy:
            return
        self.pending = key
        self.say(f"请把鼠标移到{NAMES[key]}中央，按 F6 记录；F9 取消。")
        self.root.iconify()

    def collect_point(self):
        if self.pending is None:
            return
        p = W.POINT()
        if not USER32.GetCursorPos(C.byref(p)):
            return
        key, self.pending = self.pending, None
        self.points[key] = (p.x, p.y)
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
            full = self.mode.get() == "full" and not dry
            needed = ["timer"] + ([] if dry else ["macro"]) + (["challenge", "start"] if full else [])
            missing = [NAMES[k] for k in needed if k not in self.points]
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
                       roi=self.roi(), points=dict(self.points))
        except (ValueError, KeyError) as e:
            messagebox.showerror("检查设置", str(e), parent=self.root)
            return
        self.save()
        self.stop.clear()
        self.busy = True
        self.run_button.configure(state="disabled")
        for button in self.cal_buttons:
            button.configure(state="disabled")
        self.say(f"3 秒后{'试运行' if dry else '执行'}；实际目标 {format_time(effective)}。请勿移动窗口或鼠标。")
        self.root.iconify()
        threading.Thread(target=self.worker, args=(cfg,), daemon=True).start()

    def post(self, text):
        self.events.put(("message", text))

    def worker(self, cfg):
        cap = None
        WINMM.timeBeginPeriod(1)
        try:
            wait_until(time.perf_counter()+3, self.stop)
            if cfg["full"]:
                self.post("点击挑战，等待选人界面。")
                click(cfg["points"]["challenge"], self.stop)
                wait_until(time.perf_counter()+cfg["delay"]/1000, self.stop)
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
        for key, action in [(0x75, self.collect_point), (0x77, self.start), (0x78, self.cancel)]:
            down = bool(USER32.GetAsyncKeyState(key) & 0x8000)
            if down and not self.keys.get(key, False):
                action()
            self.keys[key] = down
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "message":
                    self.say(value)
                elif kind == "done":
                    self.busy = False
                    self.run_button.configure(state="normal")
                    for button in self.cal_buttons:
                        button.configure(state="normal")
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
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        # Packaging smoke test: initialize widgets/assets without sending input.
        app = App()
        app.root.withdraw()
        app.root.update_idletasks()
        report = dict(ok=True, templates=len(app.reader.labels), input_size=C.sizeof(INPUT),
                      tkinter=str(tk.TkVersion), opencv=cv2.__version__, numpy=np.__version__)
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
