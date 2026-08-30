# -*- coding: utf-8 -*-
"""
Chrome Window Bridge — 把 Chrome 窗口"甩"到另一台电脑。

与 unpacked 扩展 (extension/) 配合工作:
  - 扩展在窗口/标签变化时, 经 WebSocket(127.0.0.1:9422) 推送所有 Chrome 窗口状态
  - 本脚本检测 "按住 Shift 把窗口拖到面向对端的屏幕边缘" -> 播放滑出动画 ->
    优雅关闭本机窗口 -> 把标签列表发给对端
  - 对端收到后启动/复用 Chrome, 让新窗口从相邻边缘滑入打开这些标签

同一份代码在两台机器上运行, 只差 config.json。
资源纪律: 事件驱动、无忙等待; 空闲时 CPU≈0, 内存为 Python 解释器本身 (~20MB)。
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "bridge.log")
LOG_MAX = 1_000_000  # 日志封顶 1MB, 超过即重建

DEFAULT_CONFIG = {
    "transfer_side": "left",       # 本机面向对端电脑的屏幕边缘: left / right
    "peer_ip": "LAPTOP_IP_HERE",   # 对端电脑的局域网 IP (ipconfig 查看)
    "peer_port": 9430,
    "listen": "0.0.0.0",           # 接收端监听地址; 自测可改 127.0.0.1
    "secret": "cwb-7f3a1e9d2c4b6a",
    "ws_port": 9422,               # 本机扩展 WebSocket 端口
    "trigger_mode": "shift",       # shift / fling / both / off
    "modifier_vk": 0x10,           # VK_SHIFT
    "zone_px": 45,                 # 边缘感应区宽度 (物理像素)
    "fling_ratio": 0.45,           # fling 模式: 至少甩出窗口宽度的比例
    "hotkey": {"enabled": True, "mod": 3, "vk": 0x54},  # Ctrl(1)+Alt(2)+T(0x54)
    "chrome_path": "",
    "anim_ms": 240,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("config.json 解析失败, 使用默认配置:", e)
    return cfg


CFG = load_config()

# ---------------- 日志 ----------------
_log_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with _log_lock:
            try:
                if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
                    os.remove(LOG_PATH)
            except OSError:
                pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except OSError:
        pass


# ---------------- Win32 基础 ----------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WM_CLOSE = 0x0010
WM_HOTKEY = 0x0312
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSENDCHANGING = 0x0400
SWP_ASYNCWINDOWPOS = 0x4000
MONITOR_DEFAULTTONEAREST = 2
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
WINEVENT_OUTOFCONTEXT = 0x0000
ERROR_ALREADY_EXISTS = 183


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", POINT)]


EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WinEventProc = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
    ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)

user32.GetForegroundWindow.restype = wintypes.HWND


def get_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def get_rect(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def is_window_visible(hwnd):
    return bool(user32.IsWindowVisible(hwnd))


def is_zoomed(hwnd):
    return bool(user32.IsZoomed(hwnd))


def get_pid(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def chrome_hwnds():
    """可见的 chrome.exe 顶层窗口 (按进程名过滤, 排除 Edge 等 Chromium 系窗口)。"""
    out = []

    def cb(hwnd, lparam):
        if is_window_visible(hwnd) and get_class_name(hwnd) == "Chrome_WidgetWin_1":
            if process_name(get_pid(hwnd)).lower() == "chrome.exe":
                out.append(hwnd)
        return True

    proc = EnumWindowsProc(cb)
    user32.EnumWindows(proc, 0)
    return out


def monitor_workarea_for_hwnd(hwnd):
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    rc = info.rcWork
    return {"left": rc.left, "top": rc.top, "right": rc.right, "bottom": rc.bottom}


_primary_cache = None


def primary_workarea():
    global _primary_cache
    if _primary_cache:
        return _primary_cache
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = user32.MonitorFromPoint(POINT(0, 0), MONITOR_DEFAULTTONEAREST)
    user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    rc = info.rcWork
    _primary_cache = {"left": rc.left, "top": rc.top,
                      "right": rc.right, "bottom": rc.bottom}
    return _primary_cache


def dpi_for_window(hwnd):
    try:
        return user32.GetDpiForWindow(hwnd) or 96
    except Exception:
        return 96


def animate_move(hwnd, x0, y0, x1, y1, w, h, ms=240, ease_in=False):
    """缓动移动窗口。ease_in=True 用于滑出, False 用于滑入。"""
    steps = max(10, int(ms / 16))
    t0 = time.perf_counter()
    for i in range(1, steps + 1):
        p = i / steps
        e = (p ** 3) if ease_in else (1 - (1 - p) ** 3)
        x = int(x0 + (x1 - x0) * e)
        y = int(y0 + (y1 - y0) * e)
        user32.SetWindowPos(hwnd, 0, x, y, w, h,
                            SWP_NOZORDER | SWP_NOACTIVATE |
                            SWP_NOSENDCHANGING | SWP_ASYNCWINDOWPOS)
        target = t0 + (ms / 1000.0) * (i / steps)
        d = target - time.perf_counter()
        if d > 0:
            time.sleep(d)


def find_chrome(cfg):
    p = (cfg.get("chrome_path") or "").strip()
    if p and os.path.exists(p):
        return p
    try:
        import winreg
        keys = (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        )
        for root, path in keys:
            try:
                with winreg.OpenKey(root, path) as k:
                    v, _ = winreg.QueryValueEx(k, None)
                    if v and os.path.exists(v):
                        return v
            except OSError:
                continue
    except Exception:
        pass
    for c in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
              os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(c):
            return c
    return None


_mutex_handle = None


def acquire_single_instance():
    """命名互斥体防止重复启动第二个实例。"""
    global _mutex_handle
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, "ChromeWindowBridge_SingleInstance")
    return not (not _mutex_handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS)


# ---------------- 极简 WebSocket 服务端 (RFC6455 够用子集) ----------------
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(c, n):
    buf = b""
    while len(buf) < n:
        chunk = c.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _ws_send_text(c, text):
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        hdr = struct.pack(">BB", 0x81, n)
    elif n < 65536:
        hdr = struct.pack(">BBH", 0x81, 126, n)
    else:
        hdr = struct.pack(">BBQ", 0x81, 127, n)
    c.sendall(hdr + data)


def _read_ws_frame(c):
    b1, b2 = _recv_exact(c, 2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(c, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(c, 8))[0]
    mask = _recv_exact(c, 4) if masked else None
    payload = _recv_exact(c, ln) if ln else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


# ---------------- Bridge 主类 ----------------
class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.ext_windows = []       # 最近一次扩展推送的窗口状态 (DIP 坐标)
        self.ext_last = 0.0
        self.inflight = set()       # 正在转移中的 hwnd
        self.prev_inside = {}       # hwnd -> 上次是否在感应区
        self.move_active = threading.Event()  # 有窗口正在被拖动
        self.drag_hwnd = None       # 最近一次被拖动的窗口
        self.move_end_time = 0.0    # 上次拖动结束时刻 (宽限判定用)
        self.ws_conn = None         # 当前扩展连接 (用于 focus 命令)
        self.debug = bool(cfg.get("debug"))

    def drag_debug(self, msg):
        if self.debug:
            log("debug: " + msg)

    # ---- 扩展状态 ----
    def on_ext_message(self, msg):
        if isinstance(msg, dict) and msg.get("type") == "state":
            with self.lock:
                self.ext_windows = msg.get("windows", [])
                self.ext_last = time.time()

    def match_window(self, hwnd):
        """系统窗口句柄 -> 扩展上报的窗口 (DPI 感知的最近中心点匹配)。"""
        with self.lock:
            wins = list(self.ext_windows)
        if not wins:
            return None
        r = get_rect(hwnd)
        if not r:
            return None
        l, t, rr, b = r
        cx = (l + rr) / 2.0
        cy = (t + b) / 2.0
        scale = dpi_for_window(hwnd) / 96.0
        best, best_d = None, float("inf")
        for w in wins:
            try:
                wx = w["left"] + w["width"] / 2.0
                wy = w["top"] + w["height"] / 2.0
            except (KeyError, TypeError):
                continue
            d = (cx / scale - wx) ** 2 + (cy / scale - wy) ** 2
            if d < best_d:
                best_d, best = d, w
        tol = (160 * scale) ** 2
        return best if (best is not None and best_d < tol) else None

    # ---- 转移 ----
    def _beep_error(self):
        try:
            user32.MessageBeep(0x00000030)  # MB_ICONWARNING
        except Exception:
            pass

    def transfer(self, hwnd):
        if hwnd in self.inflight:
            return False
        w = self.match_window(hwnd)
        if not w or not w.get("tabs"):
            log("transfer: 窗口未匹配到扩展状态 (扩展未装/未连接?), 放弃")
            self._beep_error()
            return False
        self.inflight.add(hwnd)
        threading.Thread(target=self._do_transfer, args=(hwnd, w), daemon=True).start()
        return True

    def _do_transfer(self, hwnd, w):
        try:
            time.sleep(0.06)  # 等拖动状态稳定
            if not user32.IsWindow(hwnd):
                return
            raw = w.get("tabs", [])
            tabs = [{"url": tb.get("url", ""), "title": tb.get("title", "")}
                    for tb in raw if tb.get("url")]
            if not tabs:
                log("transfer: 无有效标签")
                self._beep_error()
                return
            active_i = 0
            for i, tb in enumerate(raw):
                if tb.get("active"):
                    active_i = min(i, len(tabs) - 1)
                    break
            r = get_rect(hwnd)
            if not r:
                return
            l, t, rr, b = r
            msg = {
                "secret": self.cfg.get("secret", ""),
                "tabs": tabs,
                "active_url": tabs[active_i]["url"],
                "src": {"x": l, "y": t, "w": rr - l, "h": b - t},
                "from": socket.gethostname(),
            }
            if not self.peer_send(msg):
                log("transfer: 对端不可达, 已取消 (窗口保持原样)")
                self._beep_error()
                return
            wa = monitor_workarea_for_hwnd(hwnd)
            W, H = rr - l, b - t
            if self.cfg.get("transfer_side", "left") == "left":
                x1 = wa["left"] - W - 60
            else:
                x1 = wa["right"] + 60
            animate_move(hwnd, l, t, x1, t, W, H,
                         self.cfg.get("anim_ms", 240), ease_in=True)
            time.sleep(0.03)
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            time.sleep(0.3)
            if user32.IsWindow(hwnd):  # 拖动模态循环占用时的兜底重试
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            log("transfer: 已发送 %d 个标签 -> %s" % (len(tabs), self.cfg.get("peer_ip")))
        finally:
            self.inflight.discard(hwnd)

    def peer_send(self, msg):
        ip = self.cfg.get("peer_ip", "")
        port = int(self.cfg.get("peer_port", 9430))
        if not ip or "HERE" in ip:
            log("peer_send: config.json 还没填 peer_ip")
            return False
        try:
            s = socket.create_connection((ip, port), timeout=1.5)
            s.settimeout(2.5)
            s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            ack = b""
            try:
                ack = s.recv(16)
            except socket.timeout:
                pass
            s.close()
            return ack.strip() == b"ok"
        except OSError as e:
            log("peer_send 失败: %s" % e)
            return False

    # ---- 拖动检测 (事件驱动: 仅在真的有窗口被拖动时高频采样) ----
    def hook_loop(self):
        def callback(hhook, event, hwnd, idobj, idchild, thread, time_ms):
            try:
                if not hwnd:
                    return
                if event == EVENT_SYSTEM_MOVESIZESTART:
                    self.move_active.set()
                    self.drag_hwnd = hwnd
                    self.drag_debug("movesize START hwnd=%s class=%s"
                                    % (hwnd, get_class_name(hwnd)))
                else:
                    self.move_active.clear()
                    self.move_end_time = time.time()
                    self.drag_debug("movesize END hwnd=%s" % hwnd)
            except Exception:
                pass

        proc = WinEventProc(callback)
        self._hook_proc = proc  # 防止被 GC
        user32.SetWinEventHook(EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZEEND,
                               None, proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def watcher_loop(self):
        mode = self.cfg.get("trigger_mode", "shift")
        side = self.cfg.get("transfer_side", "left")
        zone = int(self.cfg.get("zone_px", 45))
        vk = int(self.cfg.get("modifier_vk", 0x10))
        fratio = float(self.cfg.get("fling_ratio", 0.45))
        interval = 0.5
        while True:
            try:
                if mode != "off":
                    self._watch_once(mode, side, zone, vk, fratio)
                    interval = 0.03 if self.move_active.is_set() else 0.5
            except Exception as e:
                log("watcher error: %s" % e)
            time.sleep(interval)

    def _watch_once(self, mode, side, zone, vk, fratio):
        # --- 触发路径一: 拖动中把鼠标顶到面向对端的屏幕边缘 ---
        # (拖窗口时手抓在标题栏上, 窗口本身往往够不到边缘, 但鼠标顶到边缘
        #  正是 Input Director 切换的那一瞬间 —— 这才是自然的触发点)
        now = time.time()
        if mode in ("shift", "both") and (
                self.move_active.is_set() or (now - self.move_end_time) < 0.4):
            dh = self.drag_hwnd
            if dh and user32.IsWindow(dh) and dh not in self.inflight \
                    and get_class_name(dh) == "Chrome_WidgetWin_1":
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                wa0 = primary_workarea()
                at_edge = (pt.x <= wa0["left"] + 3) if side == "left" \
                    else (pt.x >= wa0["right"] - 3)
                if at_edge:
                    shift_down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    self.drag_debug("鼠标顶边: x=%d shift=%s" % (pt.x, shift_down))
                    if shift_down:
                        self.transfer(dh)

        # --- 触发路径二 (兜底): 窗口本身进入边缘感应区 ---
        wa = primary_workarea()
        wa_w = wa["right"] - wa["left"]
        seen = set()
        for hwnd in chrome_hwnds():
            seen.add(hwnd)
            if hwnd in self.inflight:
                continue
            r = get_rect(hwnd)
            if not r:
                continue
            l, t, rr, b = r
            if l < -16000 or t < -16000:      # 最小化
                self.prev_inside[hwnd] = False
                continue
            W = rr - l
            if is_zoomed(hwnd) or W <= 0 or W >= wa_w - 80:
                self.prev_inside[hwnd] = False
                continue
            if side == "left":
                inside = (l <= wa["left"] + zone)
            else:
                inside = (rr >= wa["right"] - zone)
            was = self.prev_inside.get(hwnd)
            self.prev_inside[hwnd] = inside
            if was is None:
                continue  # 首次见到不触发 (避免本就贴边的窗口误发)
            if inside and not was:
                ok = False
                if mode in ("shift", "both"):
                    ok = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if not ok and mode in ("fling", "both"):
                    if side == "left":
                        ok = (wa["left"] - l) / float(W) >= fratio
                    else:
                        ok = (rr - wa["right"]) / float(W) >= fratio
                if ok and self.match_window(hwnd):
                    self.transfer(hwnd)
        if len(self.prev_inside) > 128:
            for k in list(self.prev_inside):
                if k not in seen:
                    self.prev_inside.pop(k, None)

    # ---- 全局热键 Ctrl+Alt+T ----
    def hotkey_loop(self):
        hk = self.cfg.get("hotkey") or {}
        if not hk.get("enabled"):
            return
        mod = int(hk.get("mod", 3))
        vk = int(hk.get("vk", 0x54))
        if not user32.RegisterHotKey(None, 1, mod, vk):
            log("热键注册失败 (可能被其他程序占用): mod=%d vk=0x%X" % (mod, vk))
            return
        log("热键已注册 (mod=%d vk=0x%X): 发送当前 Chrome 窗口" % (mod, vk))
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                hwnd = user32.GetForegroundWindow()
                if hwnd and get_class_name(hwnd) == "Chrome_WidgetWin_1":
                    if process_name(get_pid(hwnd)).lower() == "chrome.exe":
                        self.transfer(hwnd)

    # ---- 扩展 WebSocket 服务端 ----
    def ws_server_loop(self):
        port = int(self.cfg.get("ws_port", 9422))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(4)
        log("扩展 WebSocket 已监听: 127.0.0.1:%d" % port)
        while True:
            try:
                c, _ = s.accept()
                threading.Thread(target=self._ws_client, args=(c,), daemon=True).start()
            except OSError:
                break

    def _ws_client(self, c):
        try:
            c.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    return
            text = data.decode("latin1", errors="replace")
            headers = {}
            for line in text.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            key = headers.get("sec-websocket-key")
            if not key or "websocket" not in headers.get("upgrade", "").lower():
                return
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: %s\r\n\r\n" % accept)
            c.sendall(resp.encode())
            c.settimeout(None)
            with self.lock:
                self.ws_conn = c
            log("扩展已连接 (WebSocket)")
            try:
                _ws_send_text(c, json.dumps({"cmd": "refresh"}))
            except OSError:
                pass
            while True:
                try:
                    opcode, payload = _read_ws_frame(c)
                except (ConnectionError, OSError):
                    break
                if opcode == 8:
                    break
                if opcode == 9:
                    try:
                        self._ws_pong(c, payload)
                    except OSError:
                        break
                    continue
                if opcode == 1 and payload:
                    try:
                        self.on_ext_message(json.loads(payload.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        pass
        finally:
            with self.lock:
                if self.ws_conn is c:
                    self.ws_conn = None
            try:
                c.close()
            except OSError:
                pass

    def _ws_pong(self, c, payload):
        n = len(payload)
        if n < 126:
            hdr = struct.pack(">BB", 0x8A, n)
        elif n < 65536:
            hdr = struct.pack(">BBH", 0x8A, 126, n)
        else:
            hdr = struct.pack(">BBQ", 0x8A, 127, n)
        c.sendall(hdr + payload)

    def send_ext_cmd(self, obj):
        with self.lock:
            c = self.ws_conn
        if not c:
            return False
        try:
            _ws_send_text(c, json.dumps(obj))
            return True
        except OSError:
            return False

    # ---- 接收端 ----
    def peer_server_loop(self):
        port = int(self.cfg.get("peer_port", 9430))
        listen = self.cfg.get("listen", "0.0.0.0")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((listen, port))
        s.listen(4)
        log("接收端口已监听: %s:%d (等待对端甩来窗口)" % (listen, port))
        while True:
            try:
                c, addr = s.accept()
                threading.Thread(target=self._handle_peer, args=(c, addr),
                                 daemon=True).start()
            except OSError:
                break

    def _handle_peer(self, c, addr):
        try:
            c.settimeout(5)
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 4 * 1024 * 1024:
                    return
            try:
                msg = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return
            if msg.get("secret") != self.cfg.get("secret", ""):
                log("收到 %s 的请求但密钥不符, 已拒绝" % (addr[0],))
                try:
                    c.sendall(b"denied\n")
                except OSError:
                    pass
                return
            try:
                c.sendall(b"ok\n")
            except OSError:
                pass
            log("收到来自 %s 的 %d 个标签" % (addr[0], len(msg.get("tabs", []))))
            self.open_incoming(msg)
        except OSError as e:
            log("接收错误: %s" % e)
        finally:
            try:
                c.close()
            except OSError:
                pass

    def open_incoming(self, msg):
        urls = [t.get("url") for t in msg.get("tabs", [])
                if re.match(r"^(https?|file|about|chrome):", t.get("url", ""))]
        if not urls:
            urls = ["about:blank"]
        chrome = find_chrome(self.cfg)
        if not chrome:
            log("找不到 chrome.exe, 请在 config.json 指定 chrome_path")
            self._beep_error()
            return
        wa = primary_workarea()
        src = msg.get("src") or {}
        W = int(min(max(src.get("w", 1200), 500), (wa["right"] - wa["left"]) - 40))
        H = int(min(max(src.get("h", 800), 400), (wa["bottom"] - wa["top"]) - 40))
        y = int(max(wa["top"] + 8,
                    min(src.get("y", wa["top"] + 80), wa["bottom"] - H - 8)))
        side = self.cfg.get("transfer_side", "left")
        if side == "right":   # 面向对端的边在右侧 -> 从右边缘滑入
            x0, x1 = wa["right"] + 60, wa["right"] - W
        else:                 # 面向对端的边在左侧 -> 从左边缘滑入
            x0, x1 = wa["left"] - W - 60, wa["left"]
        before = set(chrome_hwnds())
        args = [chrome, "--new-window",
                "--window-position=%d,%d" % (int(x0), y),
                "--window-size=%d,%d" % (W, H),
                "--no-first-run", "--no-default-browser-check"] + urls
        try:
            subprocess.Popen(args, close_fds=True)
        except OSError as e:
            log("启动 Chrome 失败: %s" % e)
            self._beep_error()
            return
        hwnd = self._wait_new_window(before, timeout=10.0)
        if not hwnd:
            log("未能捕获新 Chrome 窗口 (冷启动超时?)")
            return
        r = get_rect(hwnd)
        if r:
            l, t, rr, b = r
            W2, H2 = rr - l, b - t
            if side == "right":
                x0, x1 = wa["right"] + 60, wa["right"] - W2
            else:
                x0, x1 = wa["left"] - W2 - 60, wa["left"]
            user32.SetWindowPos(hwnd, 0, int(x0), y, W2, H2,
                                SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
            time.sleep(0.05)
            animate_move(hwnd, x0, y, x1, y, W2, H2,
                         self.cfg.get("anim_ms", 240), ease_in=False)
        au = msg.get("active_url")
        if au:
            timer = threading.Timer(0.9, self.send_ext_cmd,
                                    args=({"cmd": "focus", "url": au},))
            timer.daemon = True
            timer.start()
        log("已滑入 %d 个标签的新窗口" % len(urls))

    def _wait_new_window(self, before, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for h in chrome_hwnds():
                if h not in before and is_window_visible(h):
                    r = get_rect(h)
                    if r and (r[2] - r[0]) > 300 and (r[3] - r[1]) > 250:
                        return h
            time.sleep(0.1)
        return None


def main():
    if not acquire_single_instance():
        log("已有 bridge 实例在运行, 退出")
        sys.exit(0)
    log("Chrome Window Bridge 启动: side=%s peer=%s:%s trigger=%s" %
        (CFG.get("transfer_side"), CFG.get("peer_ip"),
         CFG.get("peer_port"), CFG.get("trigger_mode")))
    b = Bridge(CFG)
    threading.Thread(target=b.ws_server_loop, daemon=True).start()
    threading.Thread(target=b.peer_server_loop, daemon=True).start()
    threading.Thread(target=b.hook_loop, daemon=True).start()
    threading.Thread(target=b.hotkey_loop, daemon=True).start()
    try:
        b.watcher_loop()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    main()
