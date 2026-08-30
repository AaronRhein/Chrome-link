# -*- coding: utf-8 -*-
"""
chrome-window-bridge 自检 (不触碰你的真实 Chrome 窗口):
 1. WebSocket 握手 + 扩展状态接收
 2. 扩展窗口 <-> 系统窗口的 DPI 匹配
 3. 滑出动画 + 优雅关窗 (用一个临时 Win32 测试窗口)
 4. --full: 真实调用 Chrome 打开 about:blank 并滑入, 然后自动清理

用法: python-embed\\python.exe selftest.py [--full]
"""
import base64
import ctypes
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bridge as B  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))


# ---- 纯 Win32 测试窗口 (不依赖 tkinter) ----
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.c_void_p)]


B.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
B.user32.DefWindowProcW.restype = ctypes.c_ssize_t  # LRESULT


_seen_msgs = []
_mm_count = [0]


def _def_proc(hwnd, msg, wp, lp):
    if msg == 0x0200:  # WM_MOUSEMOVE
        _mm_count[0] += 1
    elif msg in (0x0112, 0x00A1, 0x0021):  # WM_SYSCOMMAND / NCLBUTTONDOWN / MOUSEACTIVATE
        if len(_seen_msgs) < 20:
            _seen_msgs.append((hex(msg), hex(wp)))
    return B.user32.DefWindowProcW(hwnd, msg, wp, lp)


_proc_keepalive = WNDPROC(_def_proc)
_classes = set()


def make_test_window(title, x, y, w, h, cls="CBTestWnd", topmost=False):
    if cls not in _classes:
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _proc_keepalive
        wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc.hbrBackground = ctypes.c_void_p(6)  # COLOR_WINDOW + 1
        wc.lpszClassName = cls
        if not B.user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError("RegisterClassExW failed: %s" % cls)
        _classes.add(cls)
    B.user32.CreateWindowExW.restype = wintypes.HWND
    WS_OVERLAPPEDWINDOW = 0x00CF0000
    WS_VISIBLE = 0x10000000
    WS_EX_TOPMOST = 0x8
    return B.user32.CreateWindowExW(
        WS_EX_TOPMOST if topmost else 0, cls, title,
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        x, y, w, h, None, None, None, None)


def pump(ms):
    msg = B.MSG()
    end = time.time() + ms / 1000.0
    while time.time() < end:
        while B.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            B.user32.TranslateMessage(ctypes.byref(msg))
            B.user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def find_hwnd_by_title(title):
    found = []

    def cb(h, lp):
        if B.is_window_visible(h) and B.get_window_text(h) == title:
            found.append(h)
        return True

    proc = B.EnumWindowsProc(cb)
    B.user32.EnumWindows(proc, 0)
    return found[0] if found else None


# ---- 测试 1: WebSocket ----
def test_ws(b):
    threading.Thread(target=b.ws_server_loop, daemon=True).start()
    time.sleep(0.4)
    s = socket.create_connection(("127.0.0.1", int(b.cfg["ws_port"])), timeout=3)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET /bridge HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % key)
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    expect = base64.b64encode(
        hashlib.sha1((key + B.WS_GUID).encode()).digest()).decode()
    check("WebSocket 握手 (101 + Accept)",
          resp.startswith(b"HTTP/1.1 101") and expect.encode() in resp)
    payload = json.dumps({"type": "state", "windows": [{
        "id": 1, "left": 100, "top": 100, "width": 900, "height": 650,
        "tabs": [{"url": "https://example.com/", "title": "t", "active": True}]}]}).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        hdr = struct.pack(">BB", 0x81, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack(">BBH", 0x81, 0x80 | 126, n)
    else:
        hdr = struct.pack(">BBQ", 0x81, 0x80 | 127, n)
    s.sendall(hdr + mask + masked)
    time.sleep(0.4)
    ok = (len(b.ext_windows) == 1 and
          b.ext_windows[0]["tabs"][0]["url"] == "https://example.com/")
    check("扩展状态接收 (掩码帧解析)", ok)
    s.close()


# ---- 测试 2: DPI 匹配 ----
def test_match():
    hwnd = make_test_window("CBTEST-MATCH", 150, 150, 600, 400)
    pump(300)
    check("创建测试窗口 (匹配用)", bool(hwnd))
    if not hwnd:
        return
    l, t, r, b = B.get_rect(hwnd)
    dpi = B.dpi_for_window(hwnd)
    scale = dpi / 96.0
    dip = {"id": 9, "left": int(l / scale), "top": int(t / scale),
           "width": int((r - l) / scale), "height": int((b - t) / scale),
           "tabs": [{"url": "https://a.example/", "title": "x", "active": True}]}
    bb = B.Bridge(B.CFG)
    with bb.lock:
        bb.ext_windows = [dip]
    m = bb.match_window(hwnd)
    check("扩展窗口匹配 (DPI=%d)" % dpi, bool(m) and m.get("id") == 9)
    B.user32.DestroyWindow(hwnd)
    pump(100)


# ---- 测试 3: 动画 + 关闭 ----
def test_anim_close():
    hwnd = make_test_window("CBTEST-ANIM", 300, 200, 480, 320)
    pump(300)
    check("创建测试窗口 (动画用)", bool(hwnd))
    if not hwnd:
        return
    wa = B.primary_workarea()
    l, t, r, b = B.get_rect(hwnd)
    B.animate_move(hwnd, l, t, wa["left"] - (r - l) - 40, t,
                   r - l, b - t, 240, ease_in=True)
    l2, t2, r2, b2 = B.get_rect(hwnd)
    check("滑出动画到位", r2 <= wa["left"] + 5,
          "rect=(%d,%d,%d,%d)" % (l2, t2, r2, b2))
    B.user32.PostMessageW(hwnd, B.WM_CLOSE, 0, 0)
    pump(400)
    check("WM_CLOSE 优雅关闭", not B.user32.IsWindow(hwnd))


# ---- 测试 4: 触发路径逻辑测试 (--drag) ----
# 注: 本自动化环境屏蔽输入注入(mouse_event/SendInput), 无法合成真实拖拽;
# 改为直接驱动 watcher 的判定路径: 模拟"拖动中 + 鼠标顶到左边缘 + Shift 按下"。
def test_drag():
    B.CFG["trigger_mode"] = "shift"
    b = B.Bridge(B.CFG)
    with b.lock:  # 清空扩展状态: 即便误匹配也不会真发数据
        b.ext_windows = []
    threading.Thread(target=b.watcher_loop, daemon=True).start()
    time.sleep(0.3)

    hwnd = make_test_window("CBTEST-DRAG", 500, 300, 480, 320,
                            cls="Chrome_WidgetWin_1", topmost=True)
    pump(300)
    check("创建拖拽测试窗口 (Chrome 同名类)", bool(hwnd))
    if not hwnd:
        return

    calls = []

    def spy_transfer(h):
        calls.append(h)
        b.move_active.clear()  # 防止重复触发
        return True

    b.transfer = spy_transfer

    p0 = wintypes.POINT(0, 0)
    B.user32.GetCursorPos(ctypes.byref(p0))  # 记录用户光标位置, 结束后还原
    orig_gaks = B.user32.GetAsyncKeyState
    try:
        B.user32.GetAsyncKeyState = lambda vk: 0x8000  # 模拟 Shift 常按
        b.move_active.set()
        b.drag_hwnd = hwnd
        B.user32.SetCursorPos(2, 400)  # 鼠标顶到左边缘
        time.sleep(1.0)  # 等 watcher 以 30ms 节拍检测
        check("模拟拖拽 + 鼠标顶边 + Shift 触发转移", hwnd in calls,
              "calls=%s" % (calls,))
    finally:
        B.user32.GetAsyncKeyState = orig_gaks
        b.move_active.clear()
        time.sleep(0.5)
        B.user32.SetCursorPos(p0.x, p0.y)  # 还原用户光标
        if B.user32.IsWindow(hwnd):
            B.user32.DestroyWindow(hwnd)
        pump(100)


# ---- 测试 5: 真实 Chrome 开窗 (--full) ----
def test_full():
    cfg = dict(B.CFG)
    cfg["listen"] = "127.0.0.1"
    b = B.Bridge(cfg)
    msg = {"secret": cfg.get("secret"), "active_url": "about:blank",
           "src": {"x": 100, "y": 100, "w": 800, "h": 600},
           "tabs": [{"url": "about:blank", "title": "blank"}],
           "from": "selftest"}
    before = set(B.chrome_hwnds())
    b.open_incoming(msg)
    hwnd = b._wait_new_window(before, timeout=3.0)
    check("真实 Chrome 开窗 + 滑入", bool(hwnd))
    if hwnd:
        time.sleep(0.3)
        B.user32.PostMessageW(hwnd, B.WM_CLOSE, 0, 0)
        time.sleep(0.4)
        check("清理测试 Chrome 窗口", not B.user32.IsWindow(hwnd))


def main():
    full = "--full" in sys.argv
    B.CFG["trigger_mode"] = "off"  # 自检期间禁用真实触发
    print("=== chrome-window-bridge 自检 ===")
    b = B.Bridge(B.CFG)
    test_ws(b)
    test_match()
    test_anim_close()
    if full:
        test_full()
    if "--drag" in sys.argv:
        test_drag()
    ok = sum(1 for _, c in results if c)
    print("=== %d/%d 项通过 ===" % (ok, len(results)))
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
