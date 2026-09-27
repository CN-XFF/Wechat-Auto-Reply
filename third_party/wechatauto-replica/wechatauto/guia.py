# -*- coding: utf-8 -*-
"""wechatauto.guia —— 微信 4.x 自绘界面「坐标 + OCR」自动化发送模块

背景
----
微信 4.1.12+ 的聊天区域改用自绘渲染（``MMUIRenderSubWindow*``，不同版本
后缀不同，如 ``MMUIRenderSubWindowHW`` / ``MMUIRenderSubWindow``），
对 UIAutomation / MSAA 不再暴露任何无障碍节点，因此原 wechatauto 的 UIA
方案在 4.1.x 上失效。本项目已通过「本地数据库解密」（``wechatauto/db.py``）
解决读消息；本模块针对**发送消息**补充一条「坐标 + OCR」路线：

    * 通过 Win32 定位微信主窗口与其内的 QtQuick 渲染子窗口（多特征兜底：
      标题 / 类名前缀 / 进程名 weixin.exe / 可见 / 大尺寸联合评分，类名从
      「硬条件」降级为「软条件」，Qt 升级改名也不失效；找不到渲染子窗口
      时直接回退用主窗口矩形计算坐标）；
    * 布局动态校准：首次运行时实测侧栏边界 / 搜索框 / 发送按钮锚点，保存到
      ``~/.wechatauto/layout-<机器>.json``，之后自动加载，DPI/布局漂移时
      输入框探测连续失败会自动重校准；
    * 用 Windows OCR（WinRT ``Windows.Media.Ocr``）识别会话列表 /
      输入框 / 发送按钮在屏幕上的位置；
    * 用真实鼠标事件 + 剪贴板粘贴（Ctrl+V）输入文字（避免中文输入法拦截）；
    * 点击「发送」按钮完成发送。

坐标系说明
----------
微信最大化时渲染子窗口与屏幕重合（本机 3072x1920）。本模块一律以
**渲染子窗口坐标系**（相对坐标）描述布局，运行时通过渲染窗口的屏幕
矩形换算成屏幕绝对坐标，从而兼容窗口缩放/未最大化的情况。

使用前提：
    1. 微信 4.x 已登录并**解锁桌面**（锁屏状态下无法 GUI 操作）；
    2. ``pip install -e .`` 安装依赖（含 ``winsdk``）。

示例：
    from wechatauto.guia import WeChatGUI
    wx = WeChatGUI()
    wx.bring_to_front()
    wx.open_chat('文件传输助手')
    wx.send_msg('你好，这是自动化测试')
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import re
import tempfile
import threading
import time
import unicodedata
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

from PIL import Image

from wechatauto import rhythm
from wechatauto.logger import wxlog
from wechatauto.param import WxResponse

# ---------------------------------------------------------------------------
# DPI：模块加载时立即设为 PER_MONITOR_AWARE_V2，保证后续所有
# GetWindowRect / GetSystemMetrics / ImageGrab 截图 / SetCursorPos 点击
# 都处于同一坐标系（物理像素）。
# ---------------------------------------------------------------------------
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Win32 原生常量与结构
# ---------------------------------------------------------------------------

# keyboard / mouse 输入标志
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

# 虚拟键
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_RETURN = 0x0D
VK_SPACE = 0x20
VK_ESCAPE = 0x1B
VK_DELETE = 0x2E
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSE_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class MOUSE_INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _MOUSE_UNION)]


# 微信 4.x 窗口类名（不同版本/机型存在差异，统一用前缀匹配兼容所有变体）
# 主窗口：Qt51514QWindowIcon；渲染子窗口：MMUIRenderSubWindow /
# MMUIRenderSubWindowHW / 未来其他后缀。按前缀识别可覆盖全部已知/未知变体。
WX_MAIN_WIN_CLASS_PREFIX = "Qt51514QWindowIcon"
WX_MAIN_WIN_TITLE = "微信"
WX_RENDER_WIN_CLASS_PREFIX = "MMUIRenderSubWindow"

# 布局比例常量（相对渲染窗口尺寸，跨分辨率自适应）
# 侧栏宽度约占窗口 22%（微信 4.x 侧栏为固定逻辑宽度，最大化时≈0.22）；
# 其余布局元素按其与侧栏宽/窗口高的实测比例换算，保证不同 DPI/分辨率
# 与窗口尺寸下无需改动即可工作。
SIDEBAR_LEFT = 0
SIDEBAR_RATIO = 0.22                     # 侧栏宽度 / 窗口宽度
SIDEBAR_TOP = 0.05                       # 会话列表顶部起始（相对窗口高）
SEARCH_BOX_RATIO = (0.18, 0.041, 0.86, 0.079)  # (x0,y0,x1,y1)，x 相对侧栏宽、y 相对窗口高
SEND_BUTTON_RATIO = (0.78, 0.92, 0.995, 0.99)  # 「发送」按钮检索区（相对窗口）
LAYOUT_RATIO_TOLERANCE = 0.02          # 每次回复前的布局漂移容差（相对比例 2%）

# 「代码本身坏了」和「这次没测出来」不是一回事：前者必须上屏，后者可以静默回落。
# 少了这层区分时，一个缺 import 的 NameError 会被宽 except 伪装成 OCR 未命中。
CODE_DEFECT_ERRORS = (NameError, UnboundLocalError, AttributeError,
                      TypeError, ImportError)


def _log_swallowed(where: str, e: BaseException) -> None:
    """宽 ``except Exception`` 吞掉异常时按性质分级记录。

    控制台默认 INFO，所以 debug 等于只进日志文件：「DB 这次查不到」这类可恢复
    失败不该刷屏，代码缺陷则必须上屏——否则又能藏成一个版本。
    """
    if isinstance(e, CODE_DEFECT_ERRORS):
        wxlog.error(f'{where}：代码缺陷，不是这次没测出来 —— '
                    f'{type(e).__name__}: {e}')
    else:
        wxlog.debug(f'{where}：{type(e).__name__}: {e}')

# 竖屏（手机式窄窗口）布局：微信新版支持把窗口缩到手机比例，界面切换为
# 单列布局——会话列表占满窗口宽度，打开会话后聊天区同样占满窗口宽度。
# 两档位（wide / portrait）各自独立校准，分别存于布局文件。
PORTRAIT_MIN_HW = 1.2            # 高/宽 ≥ 此值 → 视为竖屏（手机式）布局
PORTRAIT_SIDEBAR_RATIO = 1.0     # 竖屏下「侧栏」= 整窗宽（单列）
MIN_WINDOW_PORTRAIT = 600        # 竖屏主窗口的最小高度（像素）
# 会话列表名字列最小 x（相对侧栏宽）。备注文字可能左移到约 11%（短名称样例）；
# 过滤最左侧 8% 可排除头像 OCR 噪声（约 5%），同时保留左移后的联系人名。
# 候选仍必须通过严格名称匹配，位置本身不会触发点击。
NAME_COL_MIN_RATIO = 0.08

# 多特征兜底：类名只是「软条件」之一，还需 进程名/可见/大尺寸/标题 等特征
# 联合判断，避免 Qt 升级改名（Qt51514 → Qt6xxx）后主窗口定位失效。
PROCESS_NAME = 'weixin.exe'              # 微信进程名（小写）
MIN_WINDOW_SIZE = 800                    # 主窗口最小边长（像素），小于此视为非主窗
MAIN_TITLE_KEYWORDS = ('微信', 'Weixin', 'WeChat')

# 布局校准配置目录：~/.wechatauto/layout-<机器标识>.json
LAYOUT_CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.wechatauto')


def _layout_profile(w: int, h: int) -> str:
    """按窗口长宽比判定布局档位：``'portrait'``（手机式竖屏）或 ``'wide'``。"""
    if w > 0 and h > 0 and h / w >= PORTRAIT_MIN_HW:
        return 'portrait'
    return 'wide'


def _machine_id() -> str:
    """机器标识：主机名 + 屏幕分辨率，用于区分不同机器/DPI 的布局配置。"""
    try:
        node = platform.node() or 'unknown'
    except Exception:
        node = 'unknown'
    cx = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
    cy = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
    safe = re.sub(r'[^\w\-]', '_', node)
    return f'{safe}_{cx}x{cy}'


def _layout_path() -> str:
    """返回本机布局校准文件路径（不存在则创建目录）。"""
    if not os.path.isdir(LAYOUT_CONFIG_DIR):
        try:
            os.makedirs(LAYOUT_CONFIG_DIR, exist_ok=True)
        except Exception:
            pass
    return os.path.join(LAYOUT_CONFIG_DIR, f'layout-{_machine_id()}.json')


def _window_is_displayable(hwnd: int, user32) -> bool:
    """Return true only after the top-level HWND is actually on screen."""
    try:
        if (not hwnd or not user32.IsWindow(hwnd)
                or not user32.IsWindowVisible(hwnd)
                or user32.IsIconic(hwnd)):
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        return rect.right > rect.left and rect.bottom > rect.top
    except Exception as exc:
        wxlog.debug(f'微信窗口可见性校验失败：{exc!r}')
        return False


def _window_is_responsive(hwnd: int, user32) -> bool:
    """用 Windows 的无响应标志过滤“看得见但点不动”的微信窗口。"""
    check = getattr(user32, 'IsHungAppWindow', None)
    if check is None:
        return True
    try:
        return not bool(check(hwnd))
    except Exception as exc:
        wxlog.debug(f'微信窗口响应状态检查失败：{exc!r}')
        return True


def _control_bounds_are_visible(control) -> bool:
    try:
        if bool(control.IsOffscreen):
            return False
        rect = control.BoundingRectangle
        return rect.right > rect.left and rect.bottom > rect.top
    except Exception:
        return False


def _walk_controls(root, max_depth: int = 8, max_nodes: int = 400):
    """只遍历任务栏/托盘控件的小范围 UIA 树，绝不扫描微信聊天树。"""
    pending = [(root, 0)]
    visited = 0
    while pending and visited < max_nodes:
        control, depth = pending.pop(0)
        visited += 1
        yield control
        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            continue
        pending.extend((child, depth + 1) for child in children)


def _is_tray_button(control) -> bool:
    try:
        return str(control.ControlTypeName) == 'ButtonControl'
    except Exception:
        return False


def _is_wechat_tray_button(control) -> bool:
    if not _is_tray_button(control) or not _control_bounds_are_visible(control):
        return False
    try:
        name = unicodedata.normalize('NFKC', str(control.Name)).strip().casefold()
    except Exception:
        return False
    return any(
        name == alias or name.startswith((alias + ' ', alias + '(', alias + ' -'))
        for alias in ('微信', 'wechat', 'weixin')
    )


def _is_tray_overflow_button(control) -> bool:
    if not _is_tray_button(control) or not _control_bounds_are_visible(control):
        return False
    try:
        name = unicodedata.normalize('NFKC', str(control.Name)).strip().casefold()
    except Exception:
        return False
    labels = (
        'show hidden icons', '显示隐藏的图标', '显示隐藏图标',
        '隐藏的图标', '更多托盘图标', '显示其他图标',
    )
    return any(name == label or name.startswith(label + ' ') for label in labels)


def _tray_roots(user32, auto):
    roots = []
    for class_name in (
        'Shell_TrayWnd',
        'NotifyIconOverflowWindow',
        'TopLevelWindowForOverflowXamlIsland',
    ):
        try:
            hwnd = user32.FindWindowW(class_name, None)
            if hwnd and user32.IsWindow(hwnd):
                root = auto.ControlFromHandle(int(hwnd))
                if root is not None:
                    roots.append(root)
        except Exception as exc:
            wxlog.debug(f'读取系统托盘控件失败（{class_name}）：{exc!r}')
    return roots


def _find_tray_buttons(roots, predicate):
    found = []
    seen = set()
    for root in roots:
        for control in _walk_controls(root):
            if not predicate(control):
                continue
            try:
                rect = control.BoundingRectangle
                key = (
                    str(control.Name).strip().casefold(),
                    rect.left, rect.top, rect.right, rect.bottom,
                )
            except Exception:
                key = id(control)
            if key not in seen:
                seen.add(key)
                found.append(control)
    return found


def _activate_wechat_tray_icon(user32, timeout: float = 3.0) -> bool:
    """通过任务栏的微信托盘按钮模拟双击，而不是强行显示微信 HWND。

    UIA 仅用于 Windows Shell 的托盘按钮；聊天窗口仍然完全使用 OCR。
    Shell/UIA 请求被隔离在线程中并有超时，避免其异常拖住自动回复发送线程。
    """
    outcome = {'ok': False, 'detail': ''}
    completed = threading.Event()

    def activate():
        initialized = False
        try:
            import uiautomation as auto

            auto.InitializeUIAutomationInCurrentThread()
            initialized = True
            roots = _tray_roots(user32, auto)
            if not roots:
                outcome['detail'] = '未找到 Windows 任务栏托盘控件'
                return

            buttons = _find_tray_buttons(roots, _is_wechat_tray_button)
            if not buttons:
                overflow_buttons = _find_tray_buttons(roots, _is_tray_overflow_button)
                if len(overflow_buttons) == 1:
                    # 微信图标可能被 Windows 收进“显示隐藏的图标”面板。
                    overflow_buttons[0].Click(simulateMove=False, waitTime=0)
                    time.sleep(0.2)
                    buttons = _find_tray_buttons(_tray_roots(user32, auto), _is_wechat_tray_button)
                elif len(overflow_buttons) > 1:
                    outcome['detail'] = '检测到多个托盘溢出按钮，停止以免点错'
                    return

            if len(buttons) != 1:
                outcome['detail'] = (
                    '未找到唯一微信托盘按钮' if not buttons
                    else f'检测到 {len(buttons)} 个微信托盘按钮，停止以免点错'
                )
                return

            buttons[0].DoubleClick(simulateMove=False, waitTime=0)
            outcome['ok'] = True
        except Exception as exc:
            outcome['detail'] = str(exc)
        finally:
            if initialized:
                try:
                    auto.UninitializeUIAutomationInCurrentThread()
                except Exception:
                    pass
            completed.set()

    worker = threading.Thread(target=activate, name='wechat-tray-activate', daemon=True)
    worker.start()
    if not completed.wait(timeout):
        wxlog.warning('系统托盘微信图标自动化超时；未改用 HWND 强制显示')
        return False
    if not outcome['ok']:
        wxlog.warning('无法安全双击微信托盘图标：%s', outcome['detail'])
        return False
    wxlog.info('已通过 Windows 系统托盘控件模拟双击微信图标')
    return True


def _restore_keep_maximize(user32, hwnd: int):
    """恢复普通最小化窗口；托盘隐藏/无响应时走微信托盘图标双击路径。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if (_window_is_displayable(hwnd, user32)
            and _window_is_responsive(hwnd, user32)):
        return True

    def wait_until_ready(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (_window_is_displayable(hwnd, user32)
                    and _window_is_responsive(hwnd, user32)):
                return True
            time.sleep(0.05)
        return (_window_is_displayable(hwnd, user32)
                and _window_is_responsive(hwnd, user32))

    hidden = not bool(user32.IsWindowVisible(hwnd))
    if hidden:
        if not _activate_wechat_tray_icon(user32):
            return False
        if wait_until_ready(2.5):
            wxlog.info('微信托盘图标双击后已恢复为可响应窗口')
            return True
        wxlog.warning('双击微信托盘图标后窗口仍不可响应；取消 OCR 和发送')
        return False

    if user32.IsIconic(hwnd):
        is_maximized = getattr(user32, "IsZoomed", lambda _hwnd: False)(hwnd)
        command = 3 if is_maximized else 9  # SW_SHOWMAXIMIZED / SW_RESTORE
        show_async = getattr(user32, "ShowWindowAsync", None)
        if show_async is not None:
            show_async(hwnd, command)
        else:  # compatibility fallback for test doubles and older wrappers
            user32.ShowWindow(hwnd, command)
        if wait_until_ready(1.0):
            wxlog.info('已通过 Win32 异步恢复普通最小化窗口')
            return True

    # 已显示但无响应，或普通恢复失败，都通过微信自己的托盘回调再试一次；
    # 不再用 SW_SHOW / weixin:// 伪装成“窗口恢复成功”。
    if _activate_wechat_tray_icon(user32) and wait_until_ready(2.5):
        wxlog.info('微信托盘图标双击后已恢复为可响应窗口')
        return True
    wxlog.warning('微信窗口恢复或响应校验失败；取消 OCR 和发送')
    return False


# ---------------------------------------------------------------------------
# 底层输入封装
# ---------------------------------------------------------------------------

class WinInput:
    """基于 Win32 的真实鼠标/键盘输入（DPI 感知）。

    所有停顿/光标移动都过 :mod:`wechatauto.rhythm`：真人不会两次点同一个像素、
    也不会每 0.15s 动一次。档位用 ``rhythm.set_profile('natural'|'calm'|'fast')``
    或环境变量 ``WECHATAUTO_RHYTHM`` 调。
    """

    def __init__(self):
        user32 = ctypes.windll.user32
        # 进程已设为 DPI-aware（PER_MONITOR_AWARE_V2），
        # GetSystemMetrics 返回物理像素，与 UIA BoundingRectangle 一致。
        self._user32 = user32
        self.screen_w = user32.GetSystemMetrics(0)
        self.screen_h = user32.GetSystemMetrics(1)
        wxlog.debug(f'WinInput 初始化，屏幕尺寸：{self.screen_w}x{self.screen_h}')

    # -- 鼠标 ----------------------------------------------------------
    def real_click(self, x: int, y: int, right: bool = False):
        """SetCursorPos + mouse_event 的「真实」点击。

        进程在模块加载时已设为 DPI-aware（PER_MONITOR_AWARE_V2），
        UIA BoundingRectangle 返回物理像素坐标，SetCursorPos 也使用
        物理像素，两者在同一坐标系，无需额外缩放。
        """
        u = self._user32
        rhythm.move_to(u, int(x), int(y))
        rhythm.nap(0.15)
        down = MOUSEEVENTF_RIGHTDOWN if right else MOUSEEVENTF_LEFTDOWN
        up = MOUSEEVENTF_RIGHTUP if right else MOUSEEVENTF_LEFTUP
        u.mouse_event(down, 0, 0, 0, 0)
        u.mouse_event(up, 0, 0, 0, 0)
        rhythm.nap(0.3)

    def activation_click(self, x: int, y: int, expected_hwnd: int) -> bool:
        """Click once and restore the cursor, for normal window activation only."""
        u = self._user32
        origin = wintypes.POINT()
        has_origin = bool(u.GetCursorPos(ctypes.byref(origin)))
        if not has_origin:
            return False
        try:
            if not u.SetCursorPos(int(x), int(y)):
                return False
            u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                if u.GetForegroundWindow() == expected_hwnd:
                    return True
                time.sleep(0.02)
            return False
        finally:
            # Do not leave the pointer on the title bar (the app's optional
            # mouse-movement cancellation checks the cursor position). If the
            # user moved it during the click, preserve their new position.
            current = wintypes.POINT()
            if (u.GetCursorPos(ctypes.byref(current))
                    and (current.x, current.y) == (int(x), int(y))):
                u.SetCursorPos(origin.x, origin.y)

    def send_input_click(self, x: int, y: int, right: bool = False):
        """SendInput 绝对坐标点击（按真实屏幕尺寸缩放）。

        先 SetCursorPos 移动可见光标（方便观察/确保悬停状态），
        再用 SendInput 注入点击。右键走 SendInput：微信 4.x 渲染
        子窗口对 mouse_event 模拟的右键不响应（原图打开预览那次
        同因改用 SendInput），SendInput 可直接命中弹出右键菜单。
        """
        u = self._user32
        rhythm.move_to(u, int(x), int(y))
        rhythm.nap(0.15)
        n = int(x * 65535 // self.screen_w)
        m = int(y * 65535 // self.screen_h)
        down = MOUSEEVENTF_ABSOLUTE | (MOUSEEVENTF_RIGHTDOWN if right else MOUSEEVENTF_LEFTDOWN)
        up = MOUSEEVENTF_ABSOLUTE | (MOUSEEVENTF_RIGHTUP if right else MOUSEEVENTF_LEFTUP)
        for flags in (down, up):
            inp = MOUSE_INPUT()
            inp.type = 0
            inp.u.mi.dx = n
            inp.u.mi.dy = m
            inp.u.mi.dwFlags = flags
            u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(MOUSE_INPUT))
            rhythm.nap(0.06)
        rhythm.nap(0.3)

    def wheel(self, delta: int = -300):
        """滚轮滚动（delta 为正向上，负向下）。"""
        u = self._user32
        u.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        rhythm.nap(0.4)

    # -- 键盘 ----------------------------------------------------------
    def key(self, vk: int, ctrl: bool = False, shift: bool = False):
        """发送一次虚拟键（可带 Ctrl/Shift）。"""
        mods = [(VK_CONTROL, ctrl), (VK_SHIFT, shift)]
        for vk_mod, on in mods:
            if on:
                self._raw_key(vk_mod, down=True)
        self._raw_key(vk, down=True)
        self._raw_key(vk, down=False)
        for vk_mod, on in reversed(mods):
            if on:
                self._raw_key(vk_mod, down=False)

    def _raw_key(self, vk: int, down: bool):
        """发送一次虚拟键事件。

        优先使用 ``keybd_event``（老式 API）：在部分远程/虚拟化会话中
        ``SendInput`` 键盘事件会被系统丢弃（返回 0），而 ``keybd_event``
        与 ``mouse_event`` 走同一套老式输入管线，与鼠标点击一致可用。
        """
        flags = KEYEVENTF_KEYUP if not down else 0
        ctypes.windll.user32.keybd_event(vk & 0xFFFF, 0, flags, 0)
        rhythm.key_hold()

    def type_unicode(self, text: str):
        """以 SendInput Unicode 方式键入文本（绕开键盘布局/大小写问题）。"""
        u = self._user32
        for ch in text:
            down = INPUT()
            down.type = 1
            down.u.ki.wScan = ord(ch)
            down.u.ki.dwFlags = KEYEVENTF_UNICODE
            up = INPUT()
            up.type = 1
            up.u.ki.wScan = ord(ch)
            up.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            u.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
            rhythm.key_hold()
            u.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
            rhythm.type_gap()

    def type_pinyin(self, pinyin: str):
        """以虚拟键逐字母输入拼音（供中文输入法组合，如 ``ceshi`` → 测试）。"""
        for ch in pinyin.lower():
            if 'a' <= ch <= 'z':
                self.key(ord(ch) - 32)
            elif ch == ' ':
                self.key(VK_SPACE)
            rhythm.type_gap()


# ---------------------------------------------------------------------------
# OCR 封装（WinRT）
# ---------------------------------------------------------------------------

class ScreenOCR:
    """Windows 自带 OCR（WinRT Windows.Media.Ocr）封装。

    用法：``ScreenOCR.recognize(pil_image)`` 返回
    ``[(text, x, y, w, h), ...]``，坐标为图像内相对坐标。
    """

    @staticmethod
    def recognize(image: Image.Image) -> List[Tuple[str, int, int, int, int]]:
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage import StorageFile

        async def _run() -> List[Tuple[str, int, int, int, int]]:
            tmp = os.path.join(tempfile.gettempdir(), 'wechatauto_ocr_tmp.png')
            image.save(tmp)
            f = await StorageFile.get_file_from_path_async(tmp)
            s = await f.open_async(0)
            dec = await BitmapDecoder.create_async(s)
            bmp = await dec.get_software_bitmap_async()
            eng = OcrEngine.try_create_from_user_profile_languages()
            if eng is None:
                return []
            res = await eng.recognize_async(bmp)
            out = []
            for line in res.lines:
                out.extend(ScreenOCR._line_rows(line))
            return out

        try:
            return asyncio.run(asyncio.wait_for(_run(), timeout=8))
        except asyncio.TimeoutError:
            wxlog.debug('OCR 识别超时（8s）')
            return []
        except Exception as e:
            wxlog.debug(f'OCR 识别失败：{e}')
            return []

    @staticmethod
    def _line_rows(line) -> List[Tuple[str, int, int, int, int]]:
        """保留 OCR 整行结果，同时补充独立词/紧邻汉字词组及准确包围框。

        微信窄聊天列表有时把“昵称 + 时间/预览”合成一行。旧实现把整行
        文本只配到第一个词的矩形，短昵称因此既无法精确匹配，坐标也偏左。
        """
        line_text = str(getattr(line, 'text', '') or '').replace(' ', '').strip()
        word_rows = []
        for word in getattr(line, 'words', ()) or ():
            text = str(getattr(word, 'text', '') or '').replace(' ', '').strip()
            rect = getattr(word, 'bounding_rect', None)
            if not text or rect is None:
                continue
            word_rows.append((text, int(rect.x), int(rect.y),
                              int(rect.width), int(rect.height)))

        out = []
        seen = set()

        def add(text, x, y, w, h):
            text = str(text or '').replace(' ', '').strip()
            row = (text, int(x), int(y), int(w), int(h))
            if text and row not in seen:
                seen.add(row)
                out.append(row)

        if word_rows:
            left = min(row[1] for row in word_rows)
            top = min(row[2] for row in word_rows)
            right = max(row[1] + row[3] for row in word_rows)
            bottom = max(row[2] + row[4] for row in word_rows)
            if line_text:
                add(line_text, left, top, right - left, bottom - top)
            for row in word_rows:
                add(*row)

            # 某些中文 OCR 引擎会把短昵称拆成逐字词。只合并紧邻的汉字
            # token，避免把远处的时间/消息预览拼成可点击的联系人名。
            for start in range(len(word_rows)):
                parts = ''
                x0 = y0 = x1 = y1 = None
                previous_right = None
                for end in range(start, len(word_rows)):
                    text, x, y, w, h = word_rows[end]
                    if not text or not all('\u3400' <= ch <= '\u9fff' for ch in text):
                        break
                    if previous_right is not None and x - previous_right > max(3, h // 3):
                        break
                    parts += text
                    if len(parts) > 4:
                        break
                    x0 = x if x0 is None else min(x0, x)
                    y0 = y if y0 is None else min(y0, y)
                    x1 = x + w if x1 is None else max(x1, x + w)
                    y1 = y + h if y1 is None else max(y1, y + h)
                    previous_right = x + w
                    if len(parts) >= 2:
                        add(parts, x0, y0, x1 - x0, y1 - y0)
        # 时间与昵称偶尔被 OCR 合并为一个 token；只剥离明确的末尾时间/日期。
        if line_text:
            suffix = re.match(
                r'^(.*?)(?:(?:[01]?\d|2[0-3])[:：][0-5]\d|'
                r'昨天|星期[一二三四五六日天]|周[一二三四五六日天]|'
                r'\d{1,2}[/-]\d{1,2})$', line_text)
            if suffix and suffix.group(1):
                if word_rows:
                    left = min(row[1] for row in word_rows)
                    top = min(row[2] for row in word_rows)
                    right = max(row[1] + row[3] for row in word_rows)
                    bottom = max(row[2] + row[4] for row in word_rows)
                    add(suffix.group(1), left, top, right - left, bottom - top)
                # 没有有效词框时不产生 y=0 的伪点击坐标。
        return out


# ---------------------------------------------------------------------------
# 微信主流程
# ---------------------------------------------------------------------------

class WeChatGUI:
    """基于坐标 + OCR 的微信 4.x 自动化实例。

    Attributes:
        main_hwnd: 微信主窗口句柄
        render_hwnd: QtQuick 渲染子窗口句柄
        render_rect: 渲染窗口屏幕矩形 (left, top, right, bottom)
    """

    def __init__(self, title: str = WX_MAIN_WIN_TITLE, hwnd: int = None,
                 calibrate: bool = False):
        self._input = WinInput()
        self._sidebar_ratio = SIDEBAR_RATIO
        self._portrait_sidebar_ratio = PORTRAIT_SIDEBAR_RATIO
        self._send_button_ratio = SEND_BUTTON_RATIO
        self.layout_profile = 'wide'      # 由 _update_layout() 按窗口比例刷新
        self.main_hwnd = hwnd or self._find_main_window(title)
        if not self.main_hwnd:
            raise RuntimeError('未找到微信主窗口，请确认微信已登录并运行')
        # 微信“最小化到托盘”时主窗口仍可能存在，但不可见。先显式恢复，
        # 否则后面的渲染窗口定位和 OCR 校准会在不可见界面上进行。
        if not self._restore_main_window():
            raise RuntimeError('已找到微信主窗口，但无法从托盘恢复并显示')
        self.render_hwnd = self._find_render_window(self.main_hwnd)
        if not self.render_hwnd:
            # 找不到渲染子窗口（Qt 改版等）→ 直接用主窗口矩形计算坐标
            wxlog.warning('未找到微信渲染子窗口，回退用主窗口矩形定位坐标')
            self.render_hwnd = self.main_hwnd
        self._update_render_rect()
        self.pid = self._get_pid(self.main_hwnd)
        self._current_chat = None  # 最近打开的会话名，用于连续发送时复用
        self._direct_session_trusted_until = 0.0
        self._last_input_box = None  # 最近一次成功发送的输入框位置，连续发送复用
        wxlog.info('微信消息自动化使用 OCR + 坐标，不创建 UIA 控件树')
        # 布局校准：显式 calibrate=True 强制重校准；否则加载本机已校准配置，
        # 没有配置则自动校准一次（OCR 可用时），实现「跑一次永久兼容」。
        if not calibrate:
            calibrate = not self._load_layout()
        if calibrate:
            self.calibrate_layout()
        self._reset_reply_layout_reference()
        wxlog.info(
            f'WeChatGUI 初始化成功：hwnd={self.main_hwnd}, '
            f'render={self.render_hwnd}, rect={self.render_rect}')

    # ------------------------------------------------------------------
    # 窗口定位
    # ------------------------------------------------------------------
    def _process_name(self, pid: int) -> str:
        """返回 pid 对应进程的可执行文件名（小写），失败返回空串。"""
        if not pid:
            return ''
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size))
            if ok:
                return os.path.basename(buf.value).lower()
        except Exception:
            return ''
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
        return ''

    def _looks_like_main_window(self, hwnd: int) -> bool:
        """多特征联合判断是否为微信主窗口（类名只是软条件之一）。

        特征：标题含微信关键字 / 类名前缀 Qt51514QWindowIcon* /
        进程名为 weixin.exe / 尺寸 >= MIN_WINDOW_SIZE。任一项都不
        单独成立，评分（权重和）>=5 才认可，防止 Qt 升级改名后失效。

        不要求窗口当前可见：微信隐藏到托盘时，顶层窗口通常仍然存在，
        需要先找到它并恢复，而不是把“隐藏”误判成“微信未运行”。
        """
        u = self._input._user32
        if not u.IsWindow(hwnd):
            return False
        buf = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, buf, 256)
        u.GetClassNameW(hwnd, cls, 256)
        title, cls_name = buf.value.strip(), cls.value
        score = 0
        if any(k in title for k in MAIN_TITLE_KEYWORDS):
            score += 5
        if cls_name.startswith(WX_MAIN_WIN_CLASS_PREFIX):
            score += 4
        if self._process_name(self._get_pid(hwnd)) == PROCESS_NAME:
            score += 3
        rr = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rr))
        w_, h_ = rr.right - rr.left, rr.bottom - rr.top
        if w_ >= MIN_WINDOW_SIZE or h_ >= MIN_WINDOW_SIZE \
                or (0 < w_ < h_ and h_ >= MIN_WINDOW_PORTRAIT):
            score += 2
        return score >= 5

    def _find_main_window(self, title: str) -> int:
        """多特征兜底定位微信主窗口。

        类名前缀不再是硬条件（Qt 升级 Qt51514→Qt6xxx 后会改名），改为在
        所有顶层窗口（包括隐藏到托盘的窗口）中按「标题/类名/进程名/尺寸」
        评分，取最高分；可见性只用于同类候选的排序。找不到时退回按标题
        精确查找。
        """
        u = self._input._user32
        scored = []
        cb_ref = []

        def _cb(h, lp):
            if not u.IsWindow(h):
                return True
            buf = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, buf, 256)
            u.GetClassNameW(h, cls, 256)
            title_text, cls_name = buf.value.strip(), cls.value
            if not (title_text or cls_name):
                return True
            score = 0
            if any(k in title_text for k in MAIN_TITLE_KEYWORDS):
                score += 5
            if cls_name.startswith(WX_MAIN_WIN_CLASS_PREFIX):
                score += 4
            if self._process_name(self._get_pid(h)) == PROCESS_NAME:
                score += 3
            if u.IsWindowVisible(h):
                score += 1
            rr = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(rr))
            w = rr.right - rr.left
            ht = rr.bottom - rr.top
            if w > 0 and ht > 0:
                if w >= MIN_WINDOW_SIZE or ht >= MIN_WINDOW_SIZE \
                        or (w < ht and ht >= MIN_WINDOW_PORTRAIT):
                    score += 2
                if score >= 5:
                    scored.append((score, w * ht, h))
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        cb_ref.append(CB(_cb))
        u.EnumWindows(cb_ref[0], 0)
        if scored:
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return scored[0][2]
        # 完全找不到时退回按标题精确查找
        hwnd = u.FindWindowW(None, title)
        if hwnd and self._looks_like_main_window(hwnd):
            return hwnd
        return 0

    def _find_render_window(self, main_hwnd: int) -> int:
        u = self._input._user32
        found = []
        cb_ref = []

        def _cb(h, lp):
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cls, 256)
            if not cls.value.startswith(WX_RENDER_WIN_CLASS_PREFIX):
                return True
            rr = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(rr))
            w = rr.right - rr.left
            ht = rr.bottom - rr.top
            if w > 0 and ht > 0:
                found.append((w * ht, h))
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        cb_ref.append(CB(_cb))
        u.EnumChildWindows(main_hwnd, cb_ref[0], 0)
        if not found:
            return 0
        found.sort(key=lambda t: t[0], reverse=True)
        return found[0][1]

    def _get_pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _update_render_rect(self):
        rr = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(self.render_hwnd, ctypes.byref(rr))
        self.render_rect = (rr.left, rr.top, rr.right, rr.bottom)
        self.origin_x, self.origin_y = rr.left, rr.top
        self.render_w = rr.right - rr.left
        self.render_h = rr.bottom - rr.top
        self._update_layout()

    def _update_layout(self):
        """根据当前渲染窗口尺寸换算各布局区域（渲染相对坐标）。

        所有硬编码像素坐标替换为按比例计算，保证跨 DPI/分辨率/窗口
        尺寸一致：``sidebar_right``（右面板左边界）、``search_box``
        （左侧搜索框）、``send_button_region``（发送按钮检索区）。

        比例优先取布局校准结果（``_sidebar_ratio`` / ``_send_button_ratio``），
        未校准时回落到模块默认常量。
        """
        self.layout_profile = _layout_profile(self.render_w, self.render_h)
        send_ratio = getattr(self, '_send_button_ratio', SEND_BUTTON_RATIO)
        if self.layout_profile == 'portrait':
            # 手机式竖屏：单列——列表占满窗宽；打开会话后聊天区同样占满
            sb_ratio = getattr(self, '_portrait_sidebar_ratio',
                               PORTRAIT_SIDEBAR_RATIO)
            self.sidebar_right = max(120, int(self.render_w * sb_ratio))
            self.right_pane_left = 0
        else:
            sb_ratio = getattr(self, '_sidebar_ratio', SIDEBAR_RATIO)
            self.sidebar_right = max(120, int(self.render_w * sb_ratio))
            self.right_pane_left = self.sidebar_right
        sx0, sy0, sx1, sy1 = SEARCH_BOX_RATIO
        self.search_box = (
            int(self.sidebar_right * sx0), int(self.render_h * sy0),
            int(self.sidebar_right * sx1), int(self.render_h * sy1))
        bx0, by0, bx1, by1 = send_ratio
        self.send_button_region = (
            int(self.render_w * bx0), int(self.render_h * by0),
            int(self.render_w * bx1), int(self.render_h * by1))

    def _reset_reply_layout_reference(self):
        """以当前顶层窗口和渲染窗口重新建立回复坐标基准。"""
        sidebar_ratio = (
            self._portrait_sidebar_ratio if self.layout_profile == 'portrait'
            else self._sidebar_ratio)
        self._reply_layout_base_sidebar_ratio = sidebar_ratio
        self._reply_layout_base_render_size = (self.render_w, self.render_h)
        main_rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(
            self.main_hwnd, ctypes.byref(main_rect))
        self._reply_layout_base_main_size = (
            max(1, main_rect.right - main_rect.left),
            max(1, main_rect.bottom - main_rect.top))
        self._last_reply_render_size = (self.render_w, self.render_h)
        self._last_reply_input_box_ratio = None
        self._last_reply_input_point_ratio = None
        self._reply_input_point_ratio = None
        self._last_live_sidebar_ratio = sidebar_ratio
        self._reply_layout_size_changed = False
        self._last_input_box = None
        self._last_confirmed_reply_geometry = None
        self._pending_reply_geometry = None

    @staticmethod
    def _chat_name_identity(value: str) -> str:
        """Normalize a displayed chat/contact name, ignoring decorative symbols."""
        text = unicodedata.normalize('NFKC', str(value or '')).casefold()
        return ''.join(
            ch for ch in text
            if not ch.isspace()
            and unicodedata.category(ch)[0] not in {'P', 'Z', 'C', 'S'}
        )

    @classmethod
    def _chat_title_matches_contact(cls, title: str, target: str) -> bool:
        """Match one chat title to one person; never accept a group title containing them."""
        candidate = cls._chat_name_identity(title)
        expected = cls._chat_name_identity(target)
        if not candidate or not expected:
            return False
        if candidate == expected:
            return True
        # Some WeChat detached-chat captions append only a direct-chat suffix.
        for suffix in ('的聊天记录', '与我的聊天记录', '与我聊天记录', '聊天记录'):
            normalized_suffix = cls._chat_name_identity(suffix)
            if candidate.endswith(normalized_suffix):
                head = candidate[:-len(normalized_suffix)]
                if head in {expected, '与' + expected}:
                    return True
        # Only tolerate one omitted edge character on longer names. Short names
        # (for example a three-character Chinese name) must match exactly: a
        # two-character OCR fragment is not strong enough to authorize sending.
        return (len(expected) >= 6 and len(candidate) >= len(expected) - 1
                and len(candidate) < len(expected) and candidate in expected)

    def _is_primary_contact_label(self, row: Dict[str, object]) -> bool:
        """Reject tiny/low-contrast preview text before it can select a chat row.

        Contact names are larger and higher-contrast than gray message previews.
        The test is performed only for an OCR candidate that already matched the
        requested name, so normal list OCR remains inexpensive.
        """
        try:
            x, y, w, h = (int(row[key]) for key in ('x', 'y', 'w', 'h'))
            render_h = int(getattr(self, 'render_h', 0) or 0)
            min_height = max(8, int(round(render_h * 0.015))) if render_h else 8
            if w <= 0 or h < min_height:
                return False

            grab = getattr(self, '_grab_screen', None)
            rel_to_screen = getattr(self, '_rel_to_screen', None)
            if (not getattr(self, 'main_hwnd', None)
                    or not callable(grab) or not callable(rel_to_screen)):
                # Test doubles and older integrations may not expose pixels; keep
                # their prior OCR behavior while production uses the contrast gate.
                return True
            image = grab(rel_to_screen((x, y, x + w, y + h))).convert('RGB')
            raw_pixels = image.tobytes()
            if not raw_pixels:
                return False
            luminance = sorted(
                int(0.2126 * r + 0.7152 * g + 0.0722 * b)
                for r, g, b in zip(raw_pixels[0::3], raw_pixels[1::3],
                                   raw_pixels[2::3])
            )
            if not luminance:
                return False
            background = luminance[len(luminance) // 2]
            strong_contrast = sum(abs(value - background) >= 120
                                  for value in luminance)
            readable_fraction = strong_contrast / len(luminance)
            if readable_fraction < 0.025:
                wxlog.debug(
                    '联系人候选为低对比度/灰色文本，忽略：text=%r h=%s contrast=%.3f',
                    row.get('name', ''), h, readable_fraction,
                )
                return False
            return True
        except Exception as exc:
            wxlog.debug(f'联系人候选字体信号检测失败，按不匹配处理：{exc!r}')
            return False

    def _reply_geometry_signature(
            self, main_rect: Tuple[int, int, int, int],
            input_x_ratio: float, input_y_ratio: float) -> tuple:
        """Position/size/layout key for a previously confirmed input click point."""
        return (
            int(getattr(self, 'main_hwnd', 0) or 0),
            int(getattr(self, 'render_hwnd', 0) or 0),
            tuple(int(value) for value in main_rect),
            tuple(int(value) for value in getattr(
                self, 'render_rect', (self.origin_x, self.origin_y,
                                      self.origin_x + self.render_w,
                                      self.origin_y + self.render_h))),
            int(self.render_w), int(self.render_h),
            int(getattr(self, 'sidebar_right', 0)),
            int(getattr(self, 'right_pane_left', 0)),
            str(getattr(self, 'layout_profile', 'wide')),
            round(float(getattr(self, '_last_live_sidebar_ratio',
                                getattr(self, '_sidebar_ratio', SIDEBAR_RATIO))), 6),
            round(float(input_x_ratio), 6), round(float(input_y_ratio), 6),
        )

    def seed_reply_input_geometry(self, geometry) -> None:
        """Carry the last successful point across a safe WeChatGUI re-creation."""
        if (isinstance(geometry, tuple) and len(geometry) == 2
                and isinstance(geometry[0], tuple)
                and isinstance(geometry[1], (tuple, list))
                and len(geometry[1]) == 2):
            self._last_confirmed_reply_geometry = (
                geometry[0], tuple(float(value) for value in geometry[1]))

    def confirm_reply_input_geometry(self):
        """Commit this input point as reusable only after the send was verified."""
        pending = getattr(self, '_pending_reply_geometry', None)
        if pending is None:
            return None
        self._last_confirmed_reply_geometry = pending
        self._pending_reply_geometry = None
        return pending

    # ------------------------------------------------------------------
    # 布局动态校准（防 DPI/布局漂移）
    # ------------------------------------------------------------------
    def _detect_sidebar_ratio(self) -> Optional[float]:
        """OCR 锚点法实测侧栏宽度比例，失败返回 None。

        微信 4.x 侧栏背景与消息区同为白色，像素边界几乎不可见，唯一稳定
        的结构锚点是搜索框内的「搜索」占位文本（文本中心 ≈ 侧栏宽的 0.28，
        实测于本机 3072x1920）。结果钳制在 [0.14, 0.30]，超出正常范围视为
        误检（如 OCR 读到别处的「搜索」），返回 None 由调用方回退默认比例。
        """
        try:
            # 搜索占位词位于侧栏左上角；每条回复前只扫描这一小块，避免
            # 对整个聊天区做全屏 OCR。坐标仍由 ocr() 返回为渲染窗口坐标。
            anchor_box = (0, 0, int(self.render_w * 0.60),
                          int(self.render_h * 0.30))
            for _ in range(2):
                lines = self.ocr(anchor_box)
                for text, x, y, w, h in lines:
                    t = (text or '').strip()
                    if '搜索' in t and x < self.render_w * 0.35 \
                            and y < self.render_h * 0.3:
                        cx = x + w // 2
                        ratio = (cx / 0.28) / self.render_w
                        if 0.14 <= ratio <= 0.30:
                            return ratio
                time.sleep(0.8)
            return None
        except Exception:
            return None

    def calibrate_layout(self, save: bool = True) -> bool:
        """检测布局锚点并实测布局比例，写入 ``~/.wechatauto/layout-<机器>.json``。

        锚点（实测驱动，所有结果都钳制在合理范围，防误检破坏可用布局）：
            * 侧栏宽度：OCR 找「搜索」占位文本，反推侧栏右边界；
            * 发送按钮：OCR 在右下角找「发送」文本，反推检索区比例
              （发送按钮仅输入框有内容时可见，找不到就保持默认）。
        校准采用「保守优先」策略：任一项检测失败即用模块默认比例，宁可
        不做调整也不产生错误的坐标。返回是否成功。
        """
        def _run_with_timeout(fn, timeout=5):
            result = [None]
            def _target():
                try:
                    result[0] = fn()
                except Exception as ex:
                    # 探针内部出错只说明这一项测不出来（回落默认比例是对的），
                    # 但必须留痕：这里静默 pass 过一次，缺 import 的 NameError
                    # 一路伪装成「OCR 未命中」，藏了整整一个版本。
                    wxlog.debug(f'校准探针 {getattr(fn, "__name__", fn)} 异常：'
                                f'{type(ex).__name__}: {ex}')
            t = threading.Thread(target=_target, daemon=True)
            t.start()
            t.join(timeout)
            return result[0]

        try:
            self._update_render_rect()
            self.bring_to_front()
            time.sleep(0.8)
            self._update_render_rect()
            profile = _layout_profile(self.render_w, self.render_h)
            entry: Dict[str, object] = {'profile': profile}
            # 1) 侧栏宽度
            #    wide：OCR「搜索」锚点（限制 5s 超时）反推侧栏右边界；
            #    portrait：手机式单列布局，列表即整窗宽，比例恒为 1.0
            #    （该锚点的 0.28 经验值只适用于宽屏侧栏，竖屏下不适用）
            if profile == 'portrait':
                entry['sidebar_ratio'] = PORTRAIT_SIDEBAR_RATIO
            else:
                sb = _run_with_timeout(self._detect_sidebar_ratio, timeout=5)
                entry['sidebar_ratio'] = float(sb) if sb else SIDEBAR_RATIO
            # 2) 发送按钮：OCR「发送」（仅右下角检索区）
            #    OCR 会偶发漏检（按钮只在输入框有内容时可见），重试一次；
            #    两次都没抓到就保持默认比例（实测默认区已能覆盖「发送」）
            send = None
            for _attempt in range(2):
                try:
                    lines = _run_with_timeout(
                        lambda: self.ocr((int(self.render_w * 0.5),
                                          int(self.render_h * 0.7),
                                          self.render_w, self.render_h)),
                        timeout=5)
                except Exception:
                    lines = None
                for text, x, y, w, h in (lines or []):
                    if (text or '').strip() == '发送':
                        send = (x, y, w, h)
                        break
                if send:
                    break
                time.sleep(0.6)
            if send:
                sx, sy, sw, sh = send
                # 检索区需略大于按钮本体，OCR 才能稳定命中；四周留边距
                pad_x, pad_y = 40, 20
                x0 = max(0, sx - pad_x)
                y0 = max(0, sy - pad_y)
                x1 = min(self.render_w, sx + sw + pad_x)
                y1 = min(self.render_h, sy + sh + pad_y)
                entry['send_button_ratio'] = [
                    x0 / self.render_w, y0 / self.render_h,
                    x1 / self.render_w, y1 / self.render_h]
            else:
                entry['send_button_ratio'] = list(SEND_BUTTON_RATIO)
            entry['render_w'] = self.render_w
            entry['render_h'] = self.render_h
            entry['date'] = time.strftime('%Y-%m-%d %H:%M:%S')
            layout = self._merge_layout_file(profile, entry)
            if save:
                try:
                    with open(_layout_path(), 'w', encoding='utf-8') as f:
                        json.dump(layout, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    wxlog.debug(f'保存布局校准文件失败：{e}')
            self._apply_layout(entry)
            wxlog.info(
                f'布局校准完成（{profile}）：sidebar_ratio={entry["sidebar_ratio"]:.3f}, '
                f'send_button_ratio={entry["send_button_ratio"]}')
            return True
        except Exception as e:
            # 编程错误和「OCR 没认到锚点」不是一回事：前者说明这段代码本身坏了，
            # 落到默认比例只是碰巧没炸，必须上屏；后者是可恢复的，静默回落即可。
            if isinstance(e, CODE_DEFECT_ERRORS):
                wxlog.error(f'布局校准异常（代码缺陷，不是识别失败）：'
                            f'{type(e).__name__}: {e}')
            else:
                wxlog.warning(f'布局校准失败，回落默认比例：{type(e).__name__}: {e}')
            return False

    @staticmethod
    def _merge_layout_file(profile: str, entry: dict) -> dict:
        """把本次校准结果并入布局文件（保留另一档位的配置）。

        新格式：``{"version": 2, "machine": ..., "profiles": {"wide": {...},
        "portrait": {...}}}``；旧的扁平格式（只有 sidebar_ratio 等）视为
        ``wide`` 档位，迁移后不再丢失。
        """
        data: Dict[str, object] = {'version': 2, 'machine': _machine_id(),
                                   'profiles': {}}
        p = _layout_path()
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8') as f:
                    old = json.load(f)
            except Exception:
                old = {}
            if isinstance(old, dict):
                if isinstance(old.get('profiles'), dict):
                    data['profiles'] = dict(old['profiles'])
                elif 'sidebar_ratio' in old:          # v1 扁平格式 → 宽屏档
                    data['profiles']['wide'] = old
        data['profiles'][profile] = entry
        return data

    def _apply_layout(self, d: dict) -> None:
        """应用某一档位的布局校准结果并重算各布局区域。"""
        prof = d.get('profile') or _layout_profile(self.render_w, self.render_h)
        if prof == 'portrait':
            self._portrait_sidebar_ratio = float(
                d.get('sidebar_ratio', PORTRAIT_SIDEBAR_RATIO))
        else:
            self._sidebar_ratio = float(d.get('sidebar_ratio', SIDEBAR_RATIO))
        sb = d.get('send_button_ratio')
        if isinstance(sb, (list, tuple)) and len(sb) == 4:
            try:
                self._send_button_ratio = tuple(float(v) for v in sb)
            except Exception:
                self._send_button_ratio = SEND_BUTTON_RATIO
        try:
            ref_w = int(d.get('render_w', 0) or 0)
            ref_h = int(d.get('render_h', 0) or 0)
            if ref_w > 0 and ref_h > 0:
                self._layout_reference_render_size = (ref_w, ref_h)
        except (TypeError, ValueError):
            pass
        self._update_layout()

    def _load_layout(self) -> bool:
        """运行前自动加载本机已校准的**当前档位**布局配置，返回是否成功采用。

        新格式按 profiles 分档；旧扁平格式视为 wide 档。当前档位没有配置
        （例如第一次把窗口缩成手机比例）时返回 False → 触发该档位自动校准。
        窗口宽度或高度与校准当时差异过大（>2%，如窗口缩放/未最大化）时
        同样视为布局不匹配，拒绝采用。
        """
        p = _layout_path()
        if not os.path.isfile(p):
            return False
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            return False
        profile = _layout_profile(self.render_w, self.render_h)
        entry = None
        if isinstance(d.get('profiles'), dict):
            entry = d['profiles'].get(profile)
        elif 'sidebar_ratio' in d:
            entry = d if profile == 'wide' else None
        if not isinstance(entry, dict):
            wxlog.info(f'布局校准缺少 {profile} 档位配置，触发该档位校准')
            return False
        saved_w = float(entry.get('render_w', 0) or 0)
        saved_h = float(entry.get('render_h', 0) or 0)
        width_drift = abs(saved_w - self.render_w) / max(self.render_w, 1)
        height_drift = abs(saved_h - self.render_h) / max(self.render_h, 1)
        if (saved_w <= 0 or saved_h <= 0
                or width_drift > LAYOUT_RATIO_TOLERANCE
                or height_drift > LAYOUT_RATIO_TOLERANCE):
            wxlog.info(
                f'布局配置与当前窗口尺寸差异过大，忽略并重新校准'
                f'（{profile} 校准={saved_w:.0f}x{saved_h:.0f} '
                f'vs 当前={self.render_w}x{self.render_h}，'
                f'宽高漂移={width_drift:.1%}/{height_drift:.1%}）')
            return False
        self._apply_layout(entry)
        ratio = (self._portrait_sidebar_ratio if profile == 'portrait'
                 else self._sidebar_ratio)
        wxlog.info(f'已加载布局校准（{profile}）：sidebar_ratio={ratio:.3f}')
        return True

    @staticmethod
    def _ratio_drifted(old: float, new: float,
                       tolerance: float = LAYOUT_RATIO_TOLERANCE) -> bool:
        """比较两个比例的相对漂移，避免零值/小区域导致除零。"""
        return abs(float(new) - float(old)) / max(abs(float(old)), 0.01) > tolerance

    def refresh_sidebar_layout_before_reply(self) -> bool:
        """每条回复定位联系人前刷新窗口和侧栏比例；无法确认重大变化时停发。"""
        try:
            self._update_render_rect()
            if self.render_w <= 0 or self.render_h <= 0:
                wxlog.warning('回复前布局检查失败：微信渲染窗口尺寸无效')
                return False
            previous_size = getattr(
                self, '_last_reply_render_size', (self.render_w, self.render_h))
            size_changed = any(
                self._ratio_drifted(old, new)
                for old, new in zip(previous_size, (self.render_w, self.render_h)))
            self._reply_layout_size_changed = size_changed
            if size_changed:
                wxlog.info(
                    f'回复前检测到微信窗口尺寸变化：{previous_size[0]}x'
                    f'{previous_size[1]} → {self.render_w}x{self.render_h}；'
                    '刷新布局并清除旧输入框坐标')
                self._last_input_box = None

            profile = _layout_profile(self.render_w, self.render_h)
            if profile == 'portrait':
                live_ratio = PORTRAIT_SIDEBAR_RATIO
            else:
                live_ratio = self._detect_sidebar_ratio()
            if live_ratio is None:
                wxlog.warning(
                    '回复前未识别到微信侧栏搜索锚点；沿用上次比例 '
                    f'{getattr(self, "_last_live_sidebar_ratio", SIDEBAR_RATIO):.3f}')
                # 外框尺寸变了但侧栏边界无法复核，不能放心用旧坐标点击。
                if size_changed:
                    wxlog.error('窗口尺寸变化且侧栏比例无法复核，取消本次回复')
                    return False
                live_ratio = getattr(
                    self, '_last_live_sidebar_ratio',
                    self._portrait_sidebar_ratio if profile == 'portrait'
                    else self._sidebar_ratio)

            attr = ('_portrait_sidebar_ratio' if profile == 'portrait'
                    else '_sidebar_ratio')
            old_ratio = float(getattr(self, attr, live_ratio))
            if self._ratio_drifted(old_ratio, live_ratio):
                setattr(self, attr, float(live_ratio))
                self._update_layout()
                self._last_input_box = None
                wxlog.info(
                    f'回复前侧栏比例变化超过 2%：{old_ratio:.4f} → '
                    f'{live_ratio:.4f}；已重算聊天列表坐标')
            self._last_live_sidebar_ratio = float(live_ratio)
            self._last_reply_render_size = (self.render_w, self.render_h)
            return True
        except Exception as exc:
            wxlog.warning(f'回复前侧栏布局检查异常，取消坐标定位：{exc}')
            return False

    def refresh_input_geometry_before_reply(
            self, main_rect: Tuple[int, int, int, int],
            input_x_ratio: float = 0.260,
            input_y_ratio: float = 0.788) -> Optional[Tuple[float, float]]:
        """发送前确认输入框点击比例；同几何复用成功点位，变化后重新检测。

        深色主题下像素边界可能不可见，因此窗口几何变化时优先用输入框探针；
        已验证的同窗口位置/尺寸则直接复用比例，不重复探测和误点。
        """
        try:
            self._update_render_rect()
            left, top, right, bottom = (int(v) for v in main_rect)
            main_w, main_h = right - left, bottom - top
            if min(main_w, main_h, self.render_w, self.render_h) <= 0:
                return None

            signature = self._reply_geometry_signature(
                (left, top, right, bottom), input_x_ratio, input_y_ratio)
            self._pending_reply_geometry = None
            previous_geometry = getattr(
                self, '_last_confirmed_reply_geometry', None)
            previous_signature = previous_geometry[0] if previous_geometry else None
            previous_point = previous_geometry[1] if previous_geometry else None
            if signature == previous_signature and previous_point:
                wxlog.info(
                    '微信窗口位置、大小和布局未变，复用上次已验证的输入框点位：'
                    f'({previous_point[0]:.4f},{previous_point[1]:.4f})')
                self._pending_reply_geometry = (signature, previous_point)
                self._last_reply_input_point_ratio = previous_point
                return previous_point

            base_sidebar = float(getattr(
                self, '_reply_layout_base_sidebar_ratio', SIDEBAR_RATIO))
            profile = getattr(self, 'layout_profile', 'wide')
            fallback_sidebar = getattr(
                self, '_portrait_sidebar_ratio', PORTRAIT_SIDEBAR_RATIO
            ) if profile == 'portrait' else getattr(
                self, '_sidebar_ratio', SIDEBAR_RATIO)
            live_sidebar = float(getattr(
                self, '_last_live_sidebar_ratio', fallback_sidebar))
            base_render_w, _base_render_h = getattr(
                self, '_reply_layout_base_render_size',
                getattr(self, '_layout_reference_render_size',
                        (self.render_w, self.render_h)))
            base_main_w, base_main_h = getattr(
                self, '_reply_layout_base_main_size', (main_w, main_h))

            if profile == 'portrait':
                fallback_x = float(input_x_ratio)
            else:
                gap_px = max(8.0, (float(input_x_ratio) - base_sidebar)
                             * max(1, int(base_render_w)))
                sidebar_screen_x = self.origin_x + self.sidebar_right
                fallback_x = (sidebar_screen_x - left + gap_px) / main_w
            bottom_gap_px = (1.0 - float(input_y_ratio)) * max(1, int(base_main_h))
            fallback_y = 1.0 - bottom_gap_px / main_h
            point_x, point_y = fallback_x, fallback_y

            box = self._probe_input_box()
            box_ratio = None
            probe_accepted = False
            if box:
                x0, y0, x1, y1 = box
                box_ratio = (x0 / self.render_w, y0 / self.render_h,
                             x1 / self.render_w, y1 / self.render_h)
                # 只有输入框顶部落在合理区域、且与布局推算位置接近时才
                # 把探针当作可用锚点；否则只记日志，不用可疑坐标发送。
                probe_screen_x = self.origin_x + x0 + max(
                    24, min(160, int((x1 - x0) * 0.05)))
                probe_screen_y = self.origin_y + y0 + min(
                    24, max(8, (y1 - y0) // 8))
                probe_x = (probe_screen_x - left) / main_w
                probe_y = (probe_screen_y - top) / main_h
                if (0.20 <= probe_x <= 0.60 and 0.60 <= probe_y <= 0.92
                        and abs(probe_x - fallback_x) <= 0.12
                        and abs(probe_y - fallback_y) <= 0.12):
                    point_x, point_y = probe_x, probe_y
                    probe_accepted = True
            else:
                # 探针未能确认当前输入框时，不让旧的快速发送矩形继续生效。
                self._last_input_box = None

            previous_box = getattr(self, '_last_reply_input_box_ratio', None)
            if box_ratio and previous_box and any(
                    self._ratio_drifted(old, new)
                    for old, new in zip(previous_box, box_ratio)):
                wxlog.info(
                    '回复前检测到输入框比例变化超过 2%；已丢弃缓存坐标并使用新位置')
                self._last_input_box = None
            self._last_reply_input_box_ratio = box_ratio

            previous_point = getattr(self, '_last_reply_input_point_ratio', None)
            if previous_point and any(
                    self._ratio_drifted(old, new)
                    for old, new in zip(previous_point, (point_x, point_y))):
                wxlog.info(
                    '回复前输入点击比例变化超过 2%：'
                    f'{previous_point} → {(point_x, point_y)}；刷新输入框位置')
                self._last_input_box = None
            self._last_reply_input_point_ratio = (point_x, point_y)
            # 未识别到输入框锚点时，点位仍由当前窗口矩形/当前侧栏边界重新
            # 计算；绝不复用上次屏幕绝对坐标。若窗口尺寸变化且比例无效则停发。
            if not (0.20 <= point_x <= 0.70 and 0.60 <= point_y <= 0.94):
                wxlog.error(
                    f'回复前输入框比例超出安全区域：{point_x:.3f}, {point_y:.3f}')
                return None
            if previous_signature is not None and not probe_accepted:
                wxlog.error(
                    '微信窗口位置、大小或布局已变化，且重新检测未能确认输入框；'
                    '为避免点错取消发送')
                return None
            self._pending_reply_geometry = (signature, (point_x, point_y))
            wxlog.debug(
                f'回复前布局已复核：sidebar={live_sidebar:.4f}, '
                f'input=({point_x:.4f},{point_y:.4f}), '
                f'anchor={"pixel" if box_ratio else "window-relative"}')
            return point_x, point_y
        except Exception as exc:
            wxlog.warning(f'回复前输入框比例检查失败，取消发送：{exc}')
            return None

    def use_window(self, top_hwnd: int) -> bool:
        """把 GUI 操作目标切换到指定顶层微信窗口（主窗或独立聊天窗）。

        微信 4.x 通过搜索打开会话时会生成独立聊天窗口（标题如
        “昵称与X的聊天记录”）。切换后 render_rect/origin 等随新窗口更新，
        click/paste/OCR 等原语无需改动即可在新窗口内工作。找不到渲染
        子窗口时直接用该顶层窗口矩形计算坐标。
        """
        if not top_hwnd or not self._input._user32.IsWindow(top_hwnd):
            return False
        previous_window = (self.main_hwnd, self.render_hwnd)
        render = self._find_render_window(top_hwnd)
        if not render:
            render = top_hwnd  # 渲染窗口缺失时回退用窗口矩形
        self.main_hwnd = top_hwnd
        self.render_hwnd = render
        self._current_chat = None
        self._direct_session_trusted_until = 0.0
        self._update_render_rect()
        if previous_window != (top_hwnd, render):
            # 搜索结果可能打开独立聊天窗；输入框点击必须以新窗口尺寸为基准，
            # 不能沿用主窗口（或上一个联系人窗口）的缓存比例/输入框坐标。
            self._reset_reply_layout_reference()
        return True

    def _find_chat_window(self, name: str) -> int:
        """只找标题身份与目标联系人一致的独立聊天窗，避免命中群聊。"""
        u = self._input._user32
        wx_pid = self._get_pid(self.main_hwnd)
        found = 0
        cb_ref = []

        def _cb(h, lp):
            nonlocal found
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value != wx_pid or not u.IsWindowVisible(h):
                return True
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, buf, 256)
            title = buf.value
            if title and self._chat_title_matches_contact(title, name):
                found = h
                return False
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        cb_ref.append(CB(_cb))
        u.EnumWindows(cb_ref[0], 0)
        return found

    def is_alive(self) -> bool:
        return bool(self._input._user32.IsWindow(self.main_hwnd))

    # ------------------------------------------------------------------
    # 前台与可用性
    # ------------------------------------------------------------------
    def bring_to_front(self, keep_topmost: bool = False) -> bool:
        """显示微信并按普通窗口激活流程请求前台，不设置 topmost。

        ``keep_topmost`` 为兼容旧调用方保留；窗口不会被设成置顶，因为
        普通窗口激活即可。先将微信提到普通窗口 Z 序前方并请求前台；若
        Windows 拒绝后台线程切换，则临时共享前台输入队列重试。仍失败时，
        只在命中微信标题栏的安全空白区域时模拟单击，不点聊天内容。
        """
        u = self._input._user32
        if not u.IsWindow(self.main_hwnd):
            return False
        if not self._window_is_displayable() and not self._restore_main_window():
            return False
        if u.GetForegroundWindow() != self.main_hwnd:
            # BringWindowToTop 只调整普通窗口的 Z 序，不会设置 HWND_TOPMOST。
            try:
                u.BringWindowToTop(self.main_hwnd)
            except Exception as exc:
                wxlog.debug(f'普通窗口前移请求失败：{exc!r}')
            try:
                u.SetForegroundWindow(self.main_hwnd)
            except Exception as exc:
                wxlog.debug(f'普通前台激活请求失败：{exc!r}')
            if u.GetForegroundWindow() != self.main_hwnd:
                self._activate_with_foreground_input(u)
            if u.GetForegroundWindow() != self.main_hwnd:
                self._click_caption_to_activate(u)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if (self._window_is_displayable()
                    and u.GetForegroundWindow() == self.main_hwnd):
                return True
            time.sleep(0.05)
        return (self._window_is_displayable()
                and u.GetForegroundWindow() == self.main_hwnd)

    def _click_caption_to_activate(self, user32) -> bool:
        """Safely click an exposed WeChat caption area as a final activation try.

        A point is used only when Windows reports HTCAPTION for WeChat and
        WindowFromPoint confirms the visible hit belongs to the WeChat process.
        This prevents clicking an overlay or a chat/control area.
        """
        WM_NCHITTEST = 0x0084
        HTCAPTION = 2
        SMTO_ABORTIFHUNG = 0x0002
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(self.main_hwnd, ctypes.byref(rect)):
                return False
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < 160 or height < 80:
                return False

            target_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(self.main_hwnd, ctypes.byref(target_pid))
            if not target_pid.value:
                return False

            # Probe only the upper caption band, away from the window controls.
            xs = (rect.left + width // 2, rect.left + width * 2 // 5,
                  rect.left + width * 3 // 5)
            ys = (rect.top + 12, rect.top + 20, rect.top + 28)
            for y in ys:
                for x in xs:
                    packed = (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)
                    if packed & 0x80000000:
                        packed -= 0x100000000
                    result = ctypes.c_size_t()
                    sent = user32.SendMessageTimeoutW(
                        self.main_hwnd, WM_NCHITTEST, 0,
                        ctypes.c_ssize_t(packed).value,
                        SMTO_ABORTIFHUNG, 250, ctypes.byref(result),
                    )
                    if not sent or result.value != HTCAPTION:
                        continue

                    hit_hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
                    if not hit_hwnd:
                        continue
                    hit_pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hit_hwnd, ctypes.byref(hit_pid))
                    if hit_pid.value != target_pid.value:
                        continue

                    clicked = self._input.activation_click(
                        int(x), int(y), self.main_hwnd
                    )
                    if clicked:
                        wxlog.info('已在微信标题栏执行一次普通单击以激活窗口')
                    return bool(clicked and user32.GetForegroundWindow() == self.main_hwnd)
        except Exception as exc:
            wxlog.debug(f'微信标题栏安全激活点击失败：{exc!r}')
        return False

    def _activate_with_foreground_input(self, user32) -> bool:
        """Retry SetForegroundWindow while temporarily sharing the foreground queue."""
        foreground_hwnd = user32.GetForegroundWindow()
        if not foreground_hwnd:
            return False
        current_thread = threading.get_native_id()
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground_hwnd, None) or 0)
        attached = False
        if foreground_thread and foreground_thread != current_thread:
            try:
                attached = bool(
                    user32.AttachThreadInput(current_thread, foreground_thread, True)
                )
            except Exception as exc:
                wxlog.debug(f'临时共享前台输入队列失败：{exc!r}')
        try:
            for _ in range(3):
                try:
                    user32.SetForegroundWindow(self.main_hwnd)
                except Exception as exc:
                    wxlog.debug(f'前台激活重试失败：{exc!r}')
                if user32.GetForegroundWindow() == self.main_hwnd:
                    return True
                time.sleep(0.05)
            return user32.GetForegroundWindow() == self.main_hwnd
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(current_thread, foreground_thread, False)
                except Exception as exc:
                    wxlog.debug(f'解除前台输入队列共享失败：{exc!r}')

    def restore_zorder(self):
        """兼容旧调用方；窗口激活不再设置 topmost，因此无需恢复 Z 序。"""
        return None

    def _minimize_blockers(self) -> int:
        """最小化 Z 序中位于微信上方、并与微信重叠的非微信窗口。

        微信被 Chrome/OpenCode 等窗口覆盖时，鼠标点击的命中测试按 Z 序
        会落到覆盖层上，必须先把遮挡窗口最小化才能操作微信。
        只处理微信上方「可见 + 与微信重叠 + 非微信进程 + 非系统装饰窗口」
        的窗口；微信以下的窗口不是遮挡物，不动。
        """
        u = self._input._user32
        wx_pid = self._get_pid(self.main_hwnd)
        rect = wintypes.RECT()
        u.GetWindowRect(self.main_hwnd, ctypes.byref(rect))
        wx = (rect.left, rect.top, rect.right, rect.bottom)
        skip_classes = ("Progman", "WorkerW", "Shell_TrayWnd", "kugou_ui",
                        "MSCTFIME UI", "IME")
        targets = []
        reached_wechat = False

        def _cb(h, lp):
            nonlocal reached_wechat
            # EnumWindows 按 Z 序从上到下枚举；到微信为止的窗口才可能挡住它。
            if h == self.main_hwnd:
                reached_wechat = True
                return False
            if not u.IsWindowVisible(h) or u.IsIconic(h):
                return True
            cls_buf = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cls_buf, 256)
            cls = cls_buf.value
            if cls in skip_classes or cls.startswith("Windows.UI.Core"):
                return True
            if self._get_pid(h) == wx_pid:
                return True  # 微信自身窗口不动
            r2 = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(r2))
            if not (r2.right > wx[0] and r2.left < wx[2]
                    and r2.bottom > wx[1] and r2.top < wx[3]):
                return True  # 与微信不重叠
            title_buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, title_buf, 256)
            if cls == "Chrome_WidgetWin_1" or title_buf.value.strip():
                targets.append(h)
            return True

        cb_ref = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        cb_ref.append(CB(_cb))
        u.EnumWindows(cb_ref[0], 0)
        if not reached_wechat:
            wxlog.warning('窗口顺序枚举未找到微信主窗口，跳过最小化以免误动其他窗口')
            return 0
        for h in targets:
            u.ShowWindow(h, 6)  # SW_MINIMIZE
        if targets:
            wxlog.info(f'自动最小化微信上方的遮挡窗口 {len(targets)} 个')
        return len(targets)

    def ensure_visible(self) -> bool:
        """恢复微信主窗口并请求前台激活。

        发送类操作前调用。先尝试普通窗口激活，不设置 topmost；若仍被
        其他窗口挡住，则只最小化 Z 序中实际位于微信上方的遮挡窗并重试。

        批量发送（三件套等）会逐条调用本方法，为避免每条都重复
        枚举窗口 / 置顶（上轮实测每次开销 20s+），15 秒内已成功
        前置过且窗口仍存活则直接复用，不再重复扫描。

        缓存命中也必须确认主窗口仍然可见、未最小化且仍在前台；否则先
        异步恢复并重新激活。消息控件定位使用 OCR 和坐标点击。
        """
        if (getattr(self, '_last_visible_ok', False)
                and getattr(self, '_last_visible_ts', 0) > time.time() - 15
                and self.is_alive() and self._window_is_displayable()
                and getattr(self, '_input', None) is not None
                and self._input._user32.GetForegroundWindow() == self.main_hwnd):
            return True
        if not self._restore_main_window():
            self._last_visible_ok = False
            return False
        brought_to_front = self.bring_to_front()
        if not brought_to_front:
            minimized = self._minimize_blockers()
            if minimized:
                wxlog.info('微信普通前台激活未成功，已清除遮挡并重试激活')
                brought_to_front = self.bring_to_front()
        self._update_render_rect()
        ok = brought_to_front and self._window_is_displayable()
        self._last_visible_ok = ok
        self._last_visible_ts = time.time()
        return ok

    def _window_is_displayable(self) -> bool:
        """确认主窗口存在、处于可见/非最小化状态且矩形有效。"""
        u = self._input._user32
        try:
            if (not u.IsWindow(self.main_hwnd)
                    or not u.IsWindowVisible(self.main_hwnd)
                    or u.IsIconic(self.main_hwnd)):
                return False
            if not _window_is_responsive(self.main_hwnd, u):
                return False
            rect = wintypes.RECT()
            if not u.GetWindowRect(self.main_hwnd, ctypes.byref(rect)):
                return False
            return rect.right > rect.left and rect.bottom > rect.top
        except Exception as exc:
            wxlog.debug(f'微信窗口显示状态检查失败：{exc!r}')
            return False

    def _restore_main_window(self) -> bool:
        """必要时从托盘/最小化状态恢复微信主窗口，并验证它已经显示。"""
        u = self._input._user32
        if not u.IsWindow(self.main_hwnd):
            return False
        if self._window_is_displayable():
            return True
        wxlog.info('检测到微信主窗口隐藏或最小化，正在恢复窗口')
        if not _restore_keep_maximize(u, self.main_hwnd):
            self._last_visible_ok = False
            return False
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self._window_is_displayable():
                time.sleep(0.15)  # let WeChat repaint before the first OCR capture
                return True
            time.sleep(0.05)
        wxlog.warning('异步恢复请求超时，微信仍不可见；取消 OCR 会话定位')
        return False

    def wx_click(self, x: int, y: int, right: bool = False):
        """点击微信渲染窗口内的坐标。

        微信渲染子窗口（MMUIRenderSubWindow*）设置了
        ``WS_EX_LAYERED | WS_EX_TRANSPARENT``，导致 mouse_event
        的点击会穿透到主窗口而无法到达渲染层。此方法在点击前后
        临时去掉 ``WS_EX_TRANSPARENT``，使点击能被渲染窗口接收，
        随即恢复原样式以保证画面正常合成。
        """
        u = self._input._user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        old_ex = u.GetWindowLongW(self.render_hwnd, GWL_EXSTYLE)
        if old_ex & WS_EX_TRANSPARENT:
            u.SetWindowLongW(self.render_hwnd, GWL_EXSTYLE,
                             old_ex & ~WS_EX_TRANSPARENT)
            time.sleep(0.05)
        try:
            self._input.real_click(x, y, right=right)
        finally:
            if old_ex & WS_EX_TRANSPARENT:
                u.SetWindowLongW(self.render_hwnd, GWL_EXSTYLE, old_ex)
                time.sleep(0.05)

    def wx_wheel(self, delta: int):
        """滚轮滚动渲染窗口内的内容。

        与 wx_click 同理：渲染子窗口带 WS_EX_TRANSPARENT，mouse_event 的
        滚轮事件会穿透到下层窗口，必须临时去掉该样式再滚动。
        """
        u = self._input._user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        old_ex = u.GetWindowLongW(self.render_hwnd, GWL_EXSTYLE)
        if old_ex & WS_EX_TRANSPARENT:
            u.SetWindowLongW(self.render_hwnd, GWL_EXSTYLE,
                             old_ex & ~WS_EX_TRANSPARENT)
            time.sleep(0.05)
        try:
            self._input.wheel(delta)
        finally:
            if old_ex & WS_EX_TRANSPARENT:
                u.SetWindowLongW(self.render_hwnd, GWL_EXSTYLE, old_ex)
                time.sleep(0.05)

    # ------------------------------------------------------------------
    # 截图与 OCR
    # ------------------------------------------------------------------
    def _grab_screen(self, box: Optional[Tuple[int, int, int, int]] = None) -> Image.Image:
        """截取屏幕区域（带重试）。box 为屏幕绝对坐标。"""
        from PIL import ImageGrab
        last = None
        for _ in range(6):
            try:
                return ImageGrab.grab(bbox=box) if box else ImageGrab.grab()
            except Exception as e:
                last = e
                time.sleep(0.5)
        raise RuntimeError(f'屏幕截图失败：{last}')

    def _rel_to_screen(self, rel: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        x0, y0, x1, y1 = rel
        return (self.origin_x + x0, self.origin_y + y0,
                self.origin_x + x1, self.origin_y + y1)

    def ocr(self, rel_box: Tuple[int, int, int, int]) -> List[Tuple[str, int, int, int, int]]:
        """对渲染窗口相对区域做 OCR，返回 (text, rel_x, rel_y, w, h)。"""
        screen_box = self._rel_to_screen(rel_box)
        img = self._grab_screen(screen_box)
        res = ScreenOCR.recognize(img)
        out = []
        for text, x, y, w, h in res:
            out.append((text, rel_box[0] + x, rel_box[1] + y, w, h))
        return out

    def ocr_zoomed(self, rel_box: Tuple[int, int, int, int],
                   scale: int = 3) -> List[Tuple[str, int, int, int, int]]:
        """对渲染窗口相对区域放大 scale 倍后 OCR，返回渲染相对坐标。

        微信 4.x 的小字号标题（尤其含生僻字的标题）原尺寸 OCR 常
        漏识别或读出乱码，放大后识别率显著提升。坐标按 1/scale 还原。
        """
        screen_box = self._rel_to_screen(rel_box)
        img = self._grab_screen(screen_box)
        w, h = img.size
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
        res = ScreenOCR.recognize(img)
        out = []
        for text, x, y, w, h in res:
            out.append((text, rel_box[0] + x // scale,
                        rel_box[1] + y // scale, w // scale, h // scale))
        return out

    # ------------------------------------------------------------------
    # 会话列表
    # ------------------------------------------------------------------
    def get_sessions(self, zoomed: bool = False) -> List[Dict[str, object]]:
        """OCR 识别会话列表（渲染相对坐标），返回 [{name, x, y, w, h}]。

        名字列通常从侧栏左侧 8% 之后开始；短名称联系人的 OCR 可左移到约 11%，
        因此只过滤最左侧头像/角标区，跨分辨率/档位一致（竖屏档下“侧栏”=
        整窗宽，阈值同以此为基准）。

        zoomed=True 时对整块侧栏放大 3 倍后 OCR，用于原尺寸扫不到
        （生僻字/小字号）时兜底；速度较慢，仅按需启用。
        """
        rel = (SIDEBAR_LEFT, int(self.render_h * SIDEBAR_TOP),
               self.sidebar_right, self.render_h)
        lines = self.ocr_zoomed(rel, scale=3) if zoomed else self.ocr(rel)
        rows = []
        for text, x, y, w, h in lines:
            t = (text or '').strip()
            if not t:
                continue
            if x < self.sidebar_right * NAME_COL_MIN_RATIO:   # 头像/图标/角标区
                continue
            if any(k in t for k in ('搜索', '聊天', '通讯录')):
                continue
            rows.append({'name': t, 'x': x, 'y': y, 'w': w, 'h': h})
        return rows

    @staticmethod
    def _normalize_contact_text(value: str) -> str:
        """Normalize OCR/contact labels without collapsing distinct short names."""
        text = unicodedata.normalize('NFKC', str(value or '')).casefold()
        return ''.join(
            ch for ch in text
            if not ch.isspace() and unicodedata.category(ch)[0] not in {'P', 'Z', 'C'}
        )

    @staticmethod
    def _name_matches(ocr_name: str, target: str) -> bool:
        """OCR 会话名的韧性匹配，容忍首尾字符截断/粘连。

        微信 4.x 标题 OCR 常把首字截掉（'文件传输助手'→'件传输助'），
        因此长名称可接受至少两个字符的片段；一到两个字符的联系人名
        则必须完整匹配，避免单字 OCR 误识后点到别的联系人。
        """
        a = WeChatGUI._normalize_contact_text(ocr_name)
        b = WeChatGUI._normalize_contact_text(target)
        if not a or not b:
            return False
        if a == b:
            return True
        # OCR 框偶尔把联系人名和相邻状态文字合成一段；接受完整目标名被包含。
        if b in a and len(a) > len(b):
            return True
        # 长名称才允许 OCR 截断；禁止单字/双字目标的部分命中。
        if len(b) > 2 and a in b and len(a) >= 2:
            return True
        return False

    @staticmethod
    def _contact_row_match_score(ocr_name: str, target: str) -> int:
        """Return a strict confidence score for a contact-list/search-result row.

        The title matcher intentionally tolerates fragments because WeChat's
        title OCR often truncates characters.  That tolerance is unsafe for a
        clickable row: a two-character target such as ``测试`` would otherwise
        also match web-result text like ``测试微信怎么申请``.  Row selection is
        therefore exact-first, allowing at most one leading/trailing OCR
        artifact (for example ``0测试``) for short names.
        """
        a = WeChatGUI._normalize_contact_text(ocr_name)
        b = WeChatGUI._normalize_contact_text(target)
        if not a or not b:
            return 0
        if a == b:
            return 100
        padding = len(a) - len(b)
        if len(b) <= 2:
            if padding == 1 and (a.startswith(b) or a.endswith(b)):
                return 90
            return 0
        if padding in (1, 2) and (a.startswith(b) or a.endswith(b)):
            return 85
        if len(a) < len(b) and len(a) >= max(2, len(b) - 1) and a in b:
            return 70
        return 0

    def _session_row_click_point(self, row: Dict[str, object]) -> Tuple[int, int]:
        """Return a stable click point inside a side-list row, not OCR text.

        OCR boxes can include a preview, timestamp, or an unread badge; their
        center is not reliably over the row's hit target.  The horizontal
        midpoint of the side-list is the same selectable row but is immune to
        text-box drift.  The OCR text supplies only the row's vertical center.
        """
        x = max(40, min(self.sidebar_right - 24, int(self.sidebar_right * 0.52)))
        y = int(row['y']) + max(1, int(row['h']) // 2)
        return x, y

    def _matchable_session_rows(self, rows, name: str):
        """Keep only strict name matches rendered like primary, readable labels."""
        matches = []
        for row in rows:
            score = self._contact_row_match_score(str(row['name']), name)
            if score and self._is_primary_contact_label(row):
                matches.append((score, row))
        return matches

    def _scan_top_session(self, name: str) -> Optional[Tuple[int, int]]:
        """列表确认归顶后，单独放大首屏联系人行，补救全侧栏 OCR 漏字。"""
        top = max(0, int(self.render_h * SIDEBAR_TOP))
        # 微信顶端除了搜索框还有工具栏；首个会话行在不同高度/DPI 下
        # 可能落到 20%-35%。多取几行并靠严格名字匹配与歧义拒绝控误点。
        bottom = min(self.render_h, max(top + 1, int(self.render_h * 0.45)))
        region = (SIDEBAR_LEFT, top, self.sidebar_right, bottom)
        try:
            lines = self.ocr_zoomed(region, scale=4)
        except Exception as exc:
            wxlog.debug(f'首屏联系人 OCR 探测失败：{exc}')
            return None
        matches = []
        for text, x, y, w, h in lines:
            label = (text or '').strip()
            if (not label or x < self.sidebar_right * NAME_COL_MIN_RATIO
                    or any(k in label for k in ('搜索', '聊天', '通讯录'))):
                continue
            score = self._contact_row_match_score(label, name)
            row = {'score': score, 'name': label, 'x': x, 'y': y, 'w': w, 'h': h}
            if score and self._is_primary_contact_label(row):
                matches.append(row)
        if not matches:
            return None

        # 同一首行可能同时得到整行、词级及去时间候选；按纵坐标聚成行，
        # 若首屏真的存在多个同名行则拒绝猜测，不误点其他联系人。
        matches.sort(key=lambda row: (row['y'], -row['score']))
        clusters = []
        for row in matches:
            cluster = next((c for c in clusters
                            if abs(row['y'] - c['y']) <= 28), None)
            if cluster is None:
                clusters.append({'y': row['y'], 'rows': [row]})
            else:
                cluster['rows'].append(row)
                cluster['y'] = min(cluster['y'], row['y'])
        if len(clusters) != 1:
            wxlog.warning(
                '首屏联系人 OCR 命中多个不同位置，拒绝猜测：target=%r rows=%s',
                name, [int(c['y']) for c in clusters[:6]],
            )
            return None
        row = max(clusters[0]['rows'], key=lambda item: (item['score'], -item['x']))
        wxlog.info(
            '首屏联系人 OCR 补充命中：target=%r recognized=%r y=%s scale=4',
            name, row['name'], row['y'],
        )
        return self._session_row_click_point(row)

    def find_session(self, name: str, max_scroll: int = 3) -> Optional[Tuple[int, int]]:
        """在会话列表中查找指定会话，返回可点击的 (相对x, 相对y)。

        点击点取 OCR 文本框中心（而非硬编码列坐标），自动适配任何
        窗口宽度/DPI。渲染可能在窗口置前台后短暂未刷新，先重复纯 OCR
        再滚动；找不到时悬停列表滚动重试。

        若当前视口没有命中，先向上滚到顶部（通过视口 OCR 稳定确认），
        再从顶部向下扫描。不能确认顶部时停止，不用不确定的列表位置猜联系人。
        """
        u = self._input._user32

        def _scan(zoomed: bool = False) -> Optional[Tuple[int, int]]:
            # 按 y 排序，OCR 返回顺序不可靠。行匹配使用比标题严格得多的
            # 规则，避免把同名网页结果/消息预览误当联系人。
            rows = sorted(self.get_sessions(zoomed=zoomed), key=lambda r: r['y'])
            matches = self._matchable_session_rows(rows, name)
            if matches:
                matches.sort(key=lambda item: (-item[0], item[1]['y']))
                best_score, best_row = matches[0]
                competing = [row for score, row in matches[1:]
                             if score == best_score
                             and abs(int(row['y']) - int(best_row['y'])) > 24]
                if competing:
                    wxlog.warning(
                        '侧栏 OCR 有多个同等可信联系人，拒绝猜测：target=%r candidates=%s',
                        name, [best_row['name']] + [row['name'] for row in competing[:4]],
                    )
                    return None
                wxlog.debug(
                    '侧栏 OCR 命中：target=%r recognized=%r score=%s zoomed=%s',
                    name, best_row['name'], best_score, zoomed,
                )
                return self._session_row_click_point(best_row)
            if rows:
                wxlog.debug(
                    '侧栏 OCR 未匹配：target=%r zoomed=%s candidates=%s',
                    name, zoomed, [str(row['name'])[:24] for row in rows[:12]],
                )
            return None

        def _scan_vote(zoomed: bool = False, rounds: int = 4,
                       min_votes: int = 2,
                       tol: int = 30) -> Optional[Tuple[int, int]]:
            """多轮 OCR 投票找会话行，抗单轮识别抖动。

            WinRT OCR 对生僻字/小字号存在抖动：同一行
            不同轮次可能漏识或误识成形近字。最多扫描 rounds 轮；
            一旦同一行达到 min_votes 即提前返回，稳定时不增加延迟，
            抖动时再补扫，避免短名称联系人偶发漏一帧就直接退到搜索。
            """
            hits = []  # (y, x, w, h)

            def _best_cluster(samples):
                if not samples:
                    return None
                clusters = []  # [y均值, x均值, w均值, h均值, 票数]
                for y, x, w, h in sorted(samples):
                    for c in clusters:
                        if abs(c[0] - y) <= tol:
                            c[4] += 1
                            n = c[4]
                            c[0] = (c[0] * (n - 1) + y) / n
                            c[1] = (c[1] * (n - 1) + x) / n
                            c[2] = max(c[2], w)
                            c[3] = max(c[3], h)
                            break
                    else:
                        clusters.append([float(y), float(x), w, h, 1])
                return max(clusters, key=lambda c: c[4])

            for round_index in range(rounds):
                # 每轮只取 y 最靠前的一个命中，避免同轮多个误配行干扰
                for row in sorted(self.get_sessions(zoomed=zoomed),
                                  key=lambda r: r['y']):
                    if (self._contact_row_match_score(str(row['name']), name)
                            and self._is_primary_contact_label(row)):
                        hits.append((row['y'], row['x'], row['w'], row['h']))
                        break
                best = _best_cluster(hits)
                if best and best[4] >= min_votes:
                    wxlog.info(
                        '侧栏放大 OCR 多轮确认命中：target=%r votes=%s rounds=%s y=%s',
                        name, best[4], round_index + 1, int(best[0]),
                    )
                    return self._session_row_click_point({
                        'x': int(best[1]), 'y': int(best[0]),
                        'w': int(best[2]), 'h': int(best[3]),
                    })
                if round_index + 1 < rounds:
                    time.sleep(0.15)

            best = _best_cluster(hits)
            votes = best[4] if best else 0
            wxlog.warning(
                '侧栏放大 OCR 投票未达到命中阈值：target=%r votes=%s/%s',
                name, votes, rounds,
            )
            return None

        if max_scroll <= 0:
            return _scan()

        # 正滚轮值代表向上：先把列表归顶。用连续两次相同的 OCR 视口
        # 作为到顶信号；不会在不确定的起点上反向滚动后猜测目标。
        wxlog.info('联系人定位：先将聊天列表滚到顶部，再从顶部向下查找 target=%r', name)
        u.SetCursorPos(self.origin_x + self.sidebar_right // 2,
                       self.origin_y + int(self.render_h * 0.5))
        time.sleep(0.2)
        def _view_signature(rows) -> tuple:
            return tuple(
                (self._normalize_contact_text(str(row['name'])),
                 int(row['y']) // 12)
                for row in sorted(rows, key=lambda item: item['y'])
            )

        previous_signature = _view_signature(self.get_sessions())
        stable_views = 0
        at_top = False
        max_top_scrolls = max(6, min(12, max_scroll * 4))
        for _ in range(max_top_scrolls):
            self.wx_wheel(1200)  # 向上快速归顶
            time.sleep(0.25)
            signature = _view_signature(self.get_sessions())
            if signature and signature == previous_signature:
                stable_views += 1
            else:
                stable_views = 0
            if signature:
                previous_signature = signature
            if stable_views >= 2:
                at_top = True
                break
        if not at_top:
            wxlog.warning(
                '会话列表滚到顶部未能通过 OCR 确认，停止列表扫描：target=%r', name
            )
            return None
        wxlog.debug('会话列表已确认归顶，开始从顶部向下查找：target=%r', name)

        hit = _scan()
        if hit:
            return hit
        # 首行昵称与时间常被 OCR 合并；单独裁切首屏并放大到 4x，避免
        # 联系人明明在列表第一位，却因整侧栏文字布局导致 OCR 投票为 0。
        hit = self._scan_top_session(name)
        if hit:
            return hit
        # 顶部的短昵称/小字号目标再用放大 OCR 双轮确认。普通 OCR 已经
        # 扫过一次，不重复截取同一视图；仍要求两轮命中，避免降低防误配。
        hit = _scan_vote(zoomed=True)
        if hit:
            return hit

        for _ in range(max_scroll * 2):
            self.wx_wheel(-360)  # 从顶部向下扫近期会话
            time.sleep(0.6)
            hit = _scan()
            if hit:
                return hit
            # 小字号/生僻名在每个新视口再用一次放大 OCR；匹配规则仍
            # 是联系人行专用的严格匹配，不接受消息预览或网络搜索结果。
            hit = _scan(zoomed=True)
            if hit:
                return hit
        return None

    def _row_is_active(self, rel_y: int) -> bool:
        """判断侧栏指定行是否为当前高亮（已打开）的会话。

        微信 4.x 活动会话行背景为绿色 (≈21,172,112)，普通行为浅灰
        (238,238,240)。取会话名右侧空白横向条带统计绿色像素即可判断。
        """
        try:
            y0 = max(0, rel_y - 6)
            x1 = max(self.right_pane_left - 40, 360)
            rel = (360, y0, x1, rel_y + 6)
            img = self._grab_screen(self._rel_to_screen(rel))
            px = img.load()
            w, h = img.size
            green = 0
            for yy in range(0, h, 2):
                for xx in range(0, w, 2):
                    r, g, b = px[xx, yy][:3]
                    if g > r + 25 and g > b + 25 and g > 100:
                        green += 1
            return green > 20
        except Exception as exc:
            wxlog.debug(f'绿色像素探测失败：{exc!r}')
            return False

    def _get_uia(self, refresh: bool = False):
        """保留旧调用接口，但消息自动化固定使用 OCR，不再创建 UIA 树。

        ``refresh`` 仅为兼容旧调用方保留；此路径不会导入 UIA 驱动，也不会
        热激活微信的 Qt accessibility gate。
        """
        return None

    def open_chat(self, name: str, exact: bool = False) -> bool:
        """通过 OCR 识别和坐标操作打开指定会话。

        微信 4.x 行为：点侧栏会话在主窗右侧面板打开；搜索下拉点联系人则
        打开独立聊天窗口（标题“昵称与X的聊天记录”）。本方法两条路都处理，
        打开后把 GUI 操作目标切到对应窗口，并验证会话真正打开。
        """
        if not self.ensure_visible():
            wxlog.warning('无法显示微信主窗口，取消联系人 OCR 搜索')
            return False
        # 优先：右侧面板当前已打开目标会话 → 直接成功
        # （微信 4.x 已打开的会话才渲染右侧，侧栏查找反而慢且易误配）
        self._update_render_rect()
        if self._chat_is_open(name) and self._pane_has_content():
            return True
        # 第一优先级是聊天列表：先扫描当前视图并有限滚动查找，只有列表滚动
        # 查找仍无结果时才使用搜索框兜底。搜索结果仍需通过搜索框文字和联系人行双重校验。
        pos = self.find_session(name, max_scroll=3)
        if pos:
            self.use_window(self.main_hwnd)
            rx, ry = pos
            # 行高亮只代表选中了某个会话，不代表它是目标私聊：群聊预览也
            # 可能出现联系人名字。必须再由标题精确确认，失败才走过滤群聊的搜索。
            if self._row_is_active(ry):
                if (self._chat_open_confirmed(name)
                        and self._pane_has_content()):
                    self._current_chat = name
                    return True
                wxlog.warning(
                    '侧栏行虽已高亮，但聊天标题不能精确确认为个人会话；'
                    '不沿用当前群聊，改用严格联系人搜索：%r', name)
            else:
                self.wx_click(self.origin_x + rx, self.origin_y + ry)
                if self._chat_open_confirmed(name):
                    self._current_chat = name
                    return True
                if self._row_is_active(ry):
                    wxlog.warning(
                        '点击后侧栏行已高亮，但标题不能确认是目标个人会话；'
                        '不继续向当前会话输入：%r', name)
                else:
                    wxlog.warning(
                        '点击侧栏目标行后未确认选中：%r；改用严格联系人搜索', name)
        else:
            wxlog.info(
                '当前可见聊天列表未找到 %r，改用微信搜索框兜底；只接受联系人精确匹配',
                name,
            )

        # 侧栏找不到或点击后无法确认 → 搜索框回退。
        if self._search_chat(name):
            sub = self._find_chat_window(name)
            if sub:
                _restore_keep_maximize(self._input._user32, sub)
                self._input._user32.SetForegroundWindow(sub)
                time.sleep(0.5)
                self.use_window(sub)
                if self._chat_open_confirmed(name):
                    return True
                wxlog.warning(f'搜索结果已打开但标题无法确认：{name!r}；放弃发送')
            if self._chat_open_confirmed(name):
                return True
        return False

    def _chat_open_confirmed(self, name: str) -> bool:
        """点击会话后轮询确认已打开。

        微信 4.x 聊天标题为浅灰渲染、OCR 常读不到。即使消息区非空也不能
        证明它属于目标联系人，因此只接受标题 OCR 的目标匹配；宁可停止，
        也不把旧会话内容当作确认。
        """
        for _ in range(5):
            time.sleep(0.6)
            if self._chat_is_open(name):
                return True
        for _ in range(3):
            time.sleep(0.4)
            if self._chat_is_open(name):
                return True
        return False

    def _pane_has_content(self) -> bool:
        """右侧消息区是否渲染了内容（非全白空白页）。"""
        try:
            y0 = int(self.render_h * 0.18)
            y1 = int(self.render_h * 0.72)
            rel = (self.right_pane_left + 40, y0, self.render_w - 40, y1)
            img = self._grab_screen(self._rel_to_screen(rel))
            px = img.load()
            w, h = img.size
            non_white = sum(1 for y in range(0, h, 6) for x in range(0, w, 6)
                            if sum(px[x, y][:3]) / 3 <= 235)
            return non_white > 30
        except Exception as exc:
            wxlog.debug(f'非白像素探测失败：{exc!r}')
            return False

    def _chat_is_open(self, name: str) -> bool:
        """检测右侧面板是否打开了指定会话（标题 OCR）。

        一行标题必须等于目标联系人（仅容忍长备注 OCR 少字）；不能因为
        群聊标题里包含联系人名，或上次点击过某一行，就把群聊当成私聊。
        """
        try:
            # 标题文本贴右面板左边缘（可能略越过侧栏边界），OCR 区向左多留
            x0 = max(0, self.right_pane_left - 60)
            res = self.ocr_zoomed((x0, 0, self.render_w, 185), scale=3)
            raw_titles = [str(row[0]).strip() for row in res if row[0]]
            matched_titles = [title for title in raw_titles
                              if self._chat_title_matches_contact(title, name)]
            wxlog.debug(
                '聊天标题严格校验：target=%r recognized=%r matched=%s',
                name, raw_titles, bool(matched_titles),
            )
            return bool(matched_titles)
        except Exception as exc:
            wxlog.debug(f'标题片段匹配失败：{exc!r}')
            return False

    def _typed_into_chat_input(self, expect: str) -> bool:
        """自检：兜底粘贴有没有落进聊天输入框（点击没抢到焦点时就会这样）。

        纯 OCR 检查输入框区域；若确认搜索词误入聊天草稿，则清掉该词并放弃
        搜索。识别不确定时不做清理，避免误删用户正在编辑的草稿。
        """
        try:
            # 聊天编辑区位于渲染窗口下方；避开工具栏，并放大文字区域以
            # 提高短联系人名的可读性。只在 OCR 整行与搜索词完全相同时清理。
            y0 = int(self.render_h * 0.72)
            y1 = int(self.render_h * 0.94)
            x0 = max(0, self.right_pane_left + 12)
            x1 = max(x0 + 1, self.render_w - 12)
            lines = self.ocr_zoomed((x0, y0, x1, y1), scale=2)
            expected = self._normalize_contact_text(expect)
            matched_line = next(
                (line for line in lines
                 if self._normalize_contact_text(line[0]) == expected),
                None,
            )
            if not expected or matched_line is None:
                return False
            self._input.key(VK_A, ctrl=True)
            self._input.key(VK_DELETE)
            wxlog.warning('搜索词 %r 被 OCR 确认误粘入聊天草稿，已清空并放弃搜索兜底', expect)
            return True
        except Exception as ex:
            wxlog.debug('OCR 聊天草稿自检失败：%s: %s', type(ex).__name__, ex)
            return False

    def _search_query_visible(self, expect: str) -> bool:
        """只有 OCR 确认搜索词确实出现在微信搜索框后，才解析搜索结果。"""
        expected = self._normalize_contact_text(expect)
        if not expected:
            return False
        try:
            for box in (self.search_box, self._search_header_band()):
                lines = self.ocr_zoomed(box, scale=3)
                if any(
                    self._normalize_contact_text(str(line[0])) == expected
                    and self._search_field_line_is_plausible(line)
                    for line in lines
                ):
                    return True
            return False
        except Exception as exc:
            wxlog.debug('搜索框文字 OCR 校验失败：%s: %s', type(exc).__name__, exc)
            return False

    def _search_header_band(self) -> Tuple[int, int, int, int]:
        """搜索框 OCR 锚点的轻微布局漂移兜底区域（仅限侧栏顶部）。"""
        sidebar = max(120, int(getattr(self, 'sidebar_right', self.search_box[2])))
        render_h = max(int(self.search_box[3]), int(getattr(self, 'render_h', 1000)))
        return (int(sidebar * 0.04), 0, int(sidebar * 0.98), int(render_h * 0.11))

    def _search_field_line_is_plausible(self, line) -> bool:
        """扩展 OCR 区域内只接受落在搜索框附近的文字，避免把聊天行当输入框。"""
        _text, x, y, w, h = line
        sidebar = max(120, int(getattr(self, 'sidebar_right', self.search_box[2])))
        render_h = max(1, int(getattr(self, 'render_h', self.search_box[3] * 10)))
        margin_x = max(8, int(sidebar * 0.04))
        margin_y = max(8, int(render_h * 0.018))
        cx, cy = int(x) + int(w) // 2, int(y) + int(h) // 2
        return (
            int(self.search_box[0]) - margin_x <= cx <= int(self.search_box[2]) + margin_x
            and int(self.search_box[1]) - margin_y <= cy <= int(self.search_box[3]) + margin_y
        )

    def _search_field_click_point(self, target: str) -> Optional[Tuple[int, int]]:
        """Return a click point only when OCR confirms the search field itself.

        The cached proportional rectangle can drift after a WeChat layout change.
        Clicking it blindly once landed on a navigation item; accept only the
        visible placeholder or the requested query inside that rectangle.
        """
        expected = self._normalize_contact_text(target)
        try:
            for region_index, box in enumerate((self.search_box, self._search_header_band())):
                rows = self.ocr_zoomed(box, scale=3)
                candidates = []
                for text, x, y, w, h in rows:
                    normalized = self._normalize_contact_text(text)
                    if not normalized or not self._search_field_line_is_plausible(
                        (text, x, y, w, h)
                    ):
                        continue
                    is_placeholder = normalized == '搜索'
                    # 扩展区域只接受精确搜索占位词；目标名只允许在原搜索框
                    # 矩形内通过，防止把顶部会话行误当成当前搜索查询。
                    is_current_query = bool(
                        region_index == 0 and expected
                        and self._contact_row_match_score(normalized, expected)
                    )
                    if not is_placeholder and not is_current_query:
                        continue
                    candidates.append((int(x + w // 2), int(y + h // 2), normalized))
                if candidates:
                    cx, cy, _text = min(
                        candidates,
                        key=lambda item: abs(
                            item[0] - (self.search_box[0] + self.search_box[2]) // 2
                        ),
                    )
                    if region_index:
                        wxlog.info('搜索框 OCR 锚点轻微偏移，已通过侧栏顶部区域重新定位')
                    return cx, cy
        except Exception as exc:
            wxlog.debug('搜索框位置 OCR 校验失败：%s: %s', type(exc).__name__, exc)
        wxlog.warning('搜索框区域 OCR 未确认是联系人搜索输入框，取消点击：target=%r', target)
        return None

    def _search_chat(self, name: str) -> bool:
        """退路：搜索框 + 剪贴板粘贴搜索，点选名称匹配的第一条联系人。

        搜索下拉结果排布：联系人在最上方，下面是「搜索网络结果 / 搜一搜」
        等节标题。OCR 结果按 y 排序后，跳过节标题/提示行，点选视觉上
        第一条匹配名称的联系人行。

        群聊的成员预览行（如「00，包含：某好友」）也含目标名片段，若不
        排除会误点群聊而非联系人。群聊节标题「群聊」以下的行优先排除，
        含「包含」的成员预览行直接跳过。
        """
        for attempt in range(3):
            # 窗口位置/DPI 可能在联系人列表扫描期间变化。重读渲染区域并
            # 重试 OCR 锚点；只有识别到搜索框本身后才点击，绝不退化为盲点。
            refresh_rect = getattr(self, '_update_render_rect', None)
            if callable(refresh_rect) and getattr(self, 'render_hwnd', None):
                refresh_rect()
            point = self._search_field_click_point(name)
            if not point:
                if attempt < 2:
                    wxlog.info(
                        '联系人搜索框 OCR 锚点未确认，刷新坐标后重试 %s/2：target=%r',
                        attempt + 1, name,
                    )
                    time.sleep(0.2)
                    continue
                wxlog.warning('联系人搜索框 OCR 锚点重试后仍未确认，取消搜索：target=%r', name)
                return False
            cx, cy = self.origin_x + point[0], self.origin_y + point[1]
            self.wx_click(cx, cy)
            time.sleep(0.3)
            self._input.key(VK_A, ctrl=True)
            self._input.key(VK_DELETE)
            self.set_clipboard(name)
            self._input.key(VK_V, ctrl=True)
            time.sleep(0.8)
            if self._typed_into_chat_input(name):
                return False
            if not self._search_query_visible(name):
                wxlog.warning(
                    '搜索框未确认收到联系人名 %r，取消搜索结果点击以免误入网络搜索或聊天草稿',
                    name,
                )
                return False
            res = self.ocr_zoomed((SIDEBAR_LEFT, int(self.render_h * 0.08),
                                   self.sidebar_right, self.render_h), scale=2)
            rows = sorted(res, key=lambda r: (r[2], r[1]))
            blocked_section = False
            saw_web_section = False
            matches = []
            for t, x, y, w, h in rows:
                tt = (t.strip() or '')
                if not tt:
                    continue
                if '网络' in tt or '搜一搜' in tt or '网页' in tt:
                    blocked_section = True
                    saw_web_section = True
                    continue
                if tt.startswith(('联系人', '好友', '朋友')):
                    blocked_section = False
                    continue
                if tt == '群聊' or any(
                    section in tt for section in ('小程序', '公众号', '视频号', '订阅号', '服务号')
                ):
                    blocked_section = True
                    continue
                if tt.startswith('聊天记录') or tt.startswith('查看全部'):
                    break
                if blocked_section:
                    continue
                # 跳过“搜索/网络/搜一搜”等节标题与提示行
                if (tt.startswith('搜索') or tt.startswith('搜一搜')
                        or '网络' in tt or '暂无' in tt):
                    continue
                # 搜索候选必须位于会话搜索列，排除左侧导航栏图标/标签。
                if x + max(0, w) <= int(self.sidebar_right * 0.14):
                    continue
                # 群聊成员预览行，跳过以免误点群聊
                if '包含' in tt or tt.endswith('群') or tt.endswith('群聊'):
                    continue
                score = self._contact_row_match_score(tt, name)
                if score and self._is_primary_contact_label(
                        {'name': tt, 'x': x, 'y': y, 'w': w, 'h': h}):
                    matches.append((score, tt, x, y, w, h))
            if matches:
                # 优先严格度最高的联系人行；短名称的 ``0测试`` 可作为单个 OCR
                # 前缀伪影通过，但“测试微信怎么申请”一类网页结果不会进入候选。
                best_score = max(row[0] for row in matches)
                candidates = [row for row in matches if row[0] == best_score]
                by_row = []
                for row in sorted(candidates, key=lambda item: (item[3], item[2])):
                    if not any(abs(row[3] - existing[3]) <= 24 for existing in by_row):
                        by_row.append(row)
                if len(by_row) == 1:
                    _score, tt, x, y, w, h = by_row[0]
                    # 搜索结果也点结果行中央，避免 OCR 框被网页结果文字拉偏。
                    click_x = max(40, min(self.sidebar_right - 24,
                                          int(self.sidebar_right * 0.52)))
                    click_y = y + max(1, h // 2)
                    wxlog.debug('搜索 OCR 唯一命中：target=%r recognized=%r score=%s',
                                name, tt, best_score)
                    self.wx_click(self.origin_x + click_x,
                                  self.origin_y + click_y)
                    time.sleep(0.8)
                    return True
                wxlog.warning(
                    '搜索 OCR 命中不唯一，拒绝猜测：target=%r candidates=%s',
                    name, [row[1] for row in by_row[:8]],
                )
                return False
            wxlog.debug(
                '搜索 OCR 未匹配联系人：target=%r candidates=%s',
                name, [str(row[0])[:24] for row in rows[:12]],
            )
            if saw_web_section:
                wxlog.warning(
                    '搜索结果只有网络搜索区且未命中联系人：target=%r；停止，不点网络结果',
                    name,
                )
                return False
        return False

    # ------------------------------------------------------------------
    # 输入框
    # ------------------------------------------------------------------
    def get_input_box(self) -> Optional[Tuple[int, int, int, int]]:
        """自适应输入框检测，返回渲染相对矩形 (x0,y0,x1,y1)。

        原理：输入框是右侧面板底部一块**全宽浅色区**，与上方消息区以一条
        约 2px 的浅灰边框线 ((224,224,224)) 分隔。先取若干探针行（自下而上
        在输入框下半部找一行，要求该行在 right_pane_left..render_w 范围内
        白度 >=0.8），再从探针行向上/向下扫描中心竖线确定输入框上下边界。
        探测失败返回 None（不再回退到默认布局，避免在错误坐标上误点）。
        """
        for _ in range(6):
            box = self._probe_input_box()
            if box:
                return box
            time.sleep(0.5)
            self._update_render_rect()
        wxlog.debug('未检测到输入框')
        # 布局异常自动重校准：本机校准配置可能已不匹配（DPI/布局漂移），
        # 重新检测锚点并校准一次后再次探测（每会话只自动触发一次）。
        if not getattr(self, '_auto_recalibrated', False):
            self._auto_recalibrated = True
            wxlog.info('输入框探测连续失败，自动重新校准布局…')
            self.calibrate_layout()
            for _ in range(3):
                box = self._probe_input_box()
                if box:
                    return box
                time.sleep(0.5)
                self._update_render_rect()
        wxlog.debug('未检测到输入框')
        # 兜底：输入框与底部工具栏高度固定，窗口缩放只改变消息区高度。
        # 实测本机输入框上边 ≈ render_h-391、下边 ≈ render_h-150。
        return (self.right_pane_left, self.render_h - int(self.render_h * 0.214),
                self.render_w, self.render_h - int(self.render_h * 0.082))

    def _probe_input_box(self) -> Optional[Tuple[int, int, int, int]]:
        """探针法定位输入框，失败返回 None。

        输入框是右侧面板底部一块全宽浅色区，顶边是一条约 2px 的浅灰
        （224,224,224）分界线，与上方消息区（白底）分隔。消息区在渲染
        未稳定时可能整体发白，需校验分界线是「细线」而非消息区里的
        粗灰元素，否则会把消息区误判为输入框。
        """
        cx = (self.right_pane_left + self.render_w) // 2
        sx = self.origin_x + cx
        for probe_off in (150, 120, 200, 250, 350, 100, 450):
            probe_y = self.render_h - probe_off
            if probe_y <= int(self.render_h * 0.50):
                continue
            sy = self.origin_y + probe_y
            row_img = self._grab_screen(
                (self.origin_x + self.right_pane_left, sy,
                 self.origin_x + self.render_w, sy + 1))
            pxx = row_img.load()
            w = row_img.size[0]
            white = sum(1 for x in range(0, w, 2) if sum(pxx[x, 0]) / 3 > 240)
            if white / max(1, (w + 1) // 2) < 0.8:
                continue  # 该行不在输入框内（如工具条/空白）
            scan_top = self.origin_y + int(self.render_h * 0.50)
            col = self._grab_screen((sx, scan_top, sx + 1, sy + 1))
            pc = col.load()
            dy_probe = sy - scan_top
            y0 = dy_probe
            while y0 > 0 and sum(pc[0, y0]) / 3 > 240:
                y0 -= 1
            y0 += 1
            y1 = dy_probe
            while y1 < col.size[1] - 1 and sum(pc[0, y1]) / 3 > 240:
                y1 += 1
            # 输入框顶边上方必须是 1-4px 的细灰分界线；若是消息区里的
            # 粗灰元素（如系统消息/时间分隔），说明渲染未稳定，判为失败。
            g = y0 - 1
            while g >= 0 and sum(pc[0, g]) / 3 <= 240:
                g -= 1
            divider_h = (y0 - 1) - g
            y0_abs = scan_top + y0 - self.origin_y
            y1_abs = scan_top + y1 - self.origin_y
            if (1 <= divider_h <= 4
                    and y1_abs - y0_abs >= 150
                    and y0_abs >= int(self.render_h * 0.45)
                    and y1_abs >= int(self.render_h * 0.85)):
                return (self.right_pane_left, y0_abs, self.render_w, y1_abs)
        return None

    def focus_input(self, box: Optional[Tuple[int, int, int, int]] = None) -> bool:
        """点击输入框文本区使其获得焦点。返回是否检测到输入框。

        box 可传入已探测好的输入框，避免重复探测在会话切换时偶发失败。
        点击点取输入框顶部文本行附近（而非中心），避免点到下方
        表情/工具栏而无法聚焦。
        """
        if box is None:
            box = self.get_input_box()
        if not box:
            return False
        x0, y0, x1, y1 = box
        cx = (x0 + x1) // 2
        cy = y0 + min(24, (y1 - y0) // 4)   # 文本行贴近输入框上沿
        self.wx_click(self.origin_x + cx, self.origin_y + cy)
        time.sleep(0.6)
        return True

    # ------------------------------------------------------------------
    # 文字输入
    # ------------------------------------------------------------------
    def set_clipboard(self, text: str):
        """写入系统剪贴板（pyperclip，兼容中文）。"""
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.2)

    def input_text(self, text: str,
                   box: Optional[Tuple[int, int, int, int]] = None,
                   fast: bool = False) -> bool:
        """向当前聚焦的输入框输入文字。

        优先走「剪贴板 + Ctrl+V」并多次重试确认（输入框探测 / 焦点 /
        粘贴都可能偶发失败，整体循环重试）；均失败后回退到「拼音 +
        回车提交」的输入法组合。

        fast=True 时复用传入 box 走单次快速路径（分段连续发送用），
        失败即返回 False 由 send_msg 回退到完整流程。
        """
        if fast and box:
            if self.focus_input(box):
                self.set_clipboard(text)
                self._input.key(VK_A, ctrl=True)
                self._input.key(VK_DELETE)
                self._input.key(VK_V, ctrl=True)
                time.sleep(0.35)
                if self._input_box_has_text(box):
                    return True
            return False
        box = None
        for attempt in range(1, 7):
            box = self.get_input_box()
            if not box:
                wxlog.debug(f'未探测到输入框（attempt={attempt}），重试')
                time.sleep(0.5)
                continue
            if not self.focus_input(box):
                time.sleep(0.3)
                continue
            self.set_clipboard(text)
            self._input.key(VK_A, ctrl=True)   # 清空既有内容
            self._input.key(VK_DELETE)
            self._input.key(VK_V, ctrl=True)
            time.sleep(0.8)
            if self._input_box_has_text():
                self._last_input_box = box
                wxlog.debug(f'输入文字（剪贴板粘贴）成功，attempt={attempt}')
                return True
            wxlog.debug(f'剪贴板粘贴未确认（attempt={attempt}），重试')
            time.sleep(0.3)
        # 尝试 2：中文输入法拼音（逐字）
        wxlog.debug('剪贴板粘贴多次未生效，尝试拼音组合输入')
        self.focus_input(box)
        self._input.key(VK_A, ctrl=True)
        self._input.key(VK_DELETE)
        self._input.type_pinyin(self._to_pinyin(text))
        self._input.key(VK_RETURN)   # 提交候选
        time.sleep(0.8)
        return self._input_box_has_text()

    @staticmethod
    def _to_pinyin(text: str) -> str:
        """极简中文 → 拼音映射（仅内置少量常用词，供回退路径使用）。

        正式方案建议接入 ``pypinyin``（pip install pypinyin）。
        """
        table = {
            '测试': 'ceshi', '你好': 'nihao', '你好世界': 'nihaoshijie',
            '你好，世界': 'nihaoshijie', '消息': 'xiaoxi', '发送': 'fasong',
            '自动化': 'zidonghua', '成功': 'chenggong', '文件': 'wenjian',
            '文件传输助手': 'wenjianchuanshuzhushou',
        }
        if text in table:
            return table[text]
        try:
            import pypinyin
            return ''.join(pypinyin.lazy_pinyin(text))
        except ImportError:
            return ''.join(ch for ch in text if '\u4e00' <= ch <= '\u9fff') or text

    def _input_box_has_text(self, box=None) -> bool:
        """检测输入框文本区是否有深色像素（文字/光标），确认输入生效。

        box 可传入已探测好的输入框，避免连续发送时重复探测。

        阈值收紧到 sum<450：空输入框的浅灰占位符约为 (200,200,200) 即
        600，避免把占位符误判为未发送的文字导致消息重复发送。
        """
        if box is None:
            box = self.get_input_box()
        if not box:
            return False
        x0, y0, x1, y1 = box
        rel = (x0 + 20, y0 + 10, x1 - 20, y0 + 200)
        img = self._grab_screen(self._rel_to_screen(rel))
        px = img.load()
        dark = sum(1 for y in range(0, img.size[1], 2) for x in range(0, img.size[0], 2)
                   if sum(px[x, y]) < 450)
        return dark > 20

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def click_send(self, fast: bool = False) -> bool:
        """发送消息并确认输入框已清空。

        优先回车键（输入框刚粘贴完必已聚焦，回车最可靠）；回车后输入框
        仍有内容则回退到 OCR 定位「发送」按钮点击。每次操作后都必须确认
        输入框文本已清空（即消息真正发出），否则重试。

        关键防护：发送前若输入框本无文字，回车等于空发，必须判定失败
        让上层重试（否则消息未发出却被误判成功）。

        fast=True 时仅回车 + 短等待（分段连续发送用），失败返回 False
        由 send_msg 回退到完整流程。
        """
        rhythm.gate('send')
        box = getattr(self, '_last_input_box', None)
        if fast:
            if not self._input_box_has_text(box):
                return False
            self._input.key(VK_RETURN)
            rhythm.nap(0.5)
            if not self._input_box_has_text(box):
                return True
            return False
        for attempt in range(3):
            if not self._input_box_has_text(box):
                wxlog.debug('发送前输入框无文字，跳过空发')
                return False
            self._input.key(VK_RETURN)
            rhythm.nap(1.0)
            if not self._input_box_has_text(box):
                return True
            wxlog.debug(f'回车发送未生效（attempt={attempt}），改用「发送」按钮')
            res = self.ocr(self.send_button_region)
            clicked = False
            for text, x, y, w, h in res:
                if '发送' in text:
                    px, py = rhythm.point((self.origin_x + x, self.origin_y + y,
                                           self.origin_x + x + w,
                                           self.origin_y + y + h))
                    self.wx_click(px, py)
                    clicked = True
                    break
            if not clicked:
                return False
            rhythm.nap(1.0)
            if not self._input_box_has_text(box):
                return True
        return False

    def send_msg(self, text: str, who: Optional[str] = None,
                 verify: bool = False) -> WxResponse:
        """发送一条文本消息。

        Args:
            text: 消息内容
            who: 目标会话名称（默认发送到当前打开的会话）
            verify: 是否用数据库读取器回读确认发送成功

        Returns:
            WxResponse

        整条流程带总时限（默认 75s）：打开会话 / 输入 / 发送任一步偶发
        失败时重试；已发送但 DB 未落库时轮询等待确认，不重复发送。
        """
        # 快速路径：连续发送同一会话（如分段回复）时，复用已探测的输入框，
        # 跳过 ensure_visible / 会话重检 / 重复探测与截图确认，失败自动回退完整流程。
        if (not verify and who
                and who == getattr(self, '_current_chat', None)
                and getattr(self, '_last_input_box', None) is not None
                and self.input_text(text, box=self._last_input_box, fast=True)
                and self.click_send(fast=True)):
            return WxResponse.success(f'消息已发送：{text}', data={'content': text})
        # 水位必须在任何 UI 动作之前拍：一旦发出去，DB 顶部就已经包含新行了。
        mark = self._send_watermark(who) if verify else None
        if not self.ensure_visible():
            return WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        self._last_input_box = None
        deadline = time.time() + 75
        for attempt in range(3):
            if who:
                if time.time() > deadline:
                    break
                # 会话复用：目标仍是当前已打开会话时跳过 open_chat（每次
                # open_chat 都重扫侧栏/点击，是逐条发送的主要耗时点）
                if not (who == getattr(self, '_current_chat', None)
                        and self._chat_is_open(who)):
                    if not self.open_chat(who):
                        wxlog.debug(f'open_chat 未确认（attempt={attempt}），重试')
                        continue
                    self._current_chat = who
                    time.sleep(0.8)   # 等右侧面板/输入框渲染稳定，避免探测误判
            if time.time() > deadline:
                break
            if not self.input_text(text):
                wxlog.debug(f'输入文字失败（attempt={attempt}），重试')
                continue
            if not self.click_send():
                wxlog.debug(f'点击发送未确认清空输入框（attempt={attempt}），重试')
                continue
            if not verify:
                return WxResponse.success(f'消息已发送：{text}', data={'content': text})
            if self._verify_sent(text, who, after=mark):
                return WxResponse.success(f'消息已发送并确认：{text}', data={'content': text})
            # 可能已发送但 DB 异步落库，轮询确认，不重发避免重复
            wait_until = time.time() + 8
            while time.time() < wait_until:
                time.sleep(1.0)
                if self._verify_sent(text, who, after=mark):
                    return WxResponse.success(f'消息已发送并确认：{text}', data={'content': text})
            return WxResponse.failure('消息已操作发送，但数据库未确认', data={'content': text})
        return WxResponse.failure('发送失败：多次重试未完成')

    def _get_db(self):
        """惰性创建并复用 WeChatDB（密钥提取/解密较慢，避免每次校验都重建）。"""
        if getattr(self, '_cached_db', None) is None:
            from wechatauto.db import WeChatDB
            self._cached_db = WeChatDB()
        return self._cached_db

    def _verify_usernames(self, who: Optional[str]) -> List[str]:
        """把 ``who`` 解析成候选 username 列表（消息表按 username 键）。

        直接拿显示名查消息表是**静默失败**的：查不到就返回空列表，看起来像
        「没发出去」。所以先按通讯录解析一遍，再把原名留在后面兜底。
        水位和回读必须走同一个口径，否则两边量的不是同一个会话。
        """
        try:
            db = self._get_db()
            if not who:
                return [db.get_self_info()['username']]
            names = [h['username'] for h in db.search_contact(who)
                     if h.get('username')]
            names.append(who)
            seen, out = set(), []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            return out
        except Exception as e:
            _log_swallowed('发送校验解析会话', e)
            return [who] if who else []

    def _send_watermark(self, who: Optional[str]) -> Optional[dict]:
        """发送**前**拍一个落库水位，交给 :meth:`_verify_sent` 当门槛。

        没有水位时「最近几条里有一条含目标文本」会被旧消息满足：同一段话昨天
        发过、今天这次其实没发出去，校验照样返回成功。取顶部若干行的
        ``(sort_seq, local_id)`` 身份集合 + 最大 sort_seq：真实 sort_seq 大量
        并列（同会话实测最多 8 行同值），只比 ``>`` 会把刚发出去那条判成旧消息，
        所以并列时再按身份排除。拍不到（新会话、DB 不可用）返回 None，
        校验退回不带水位的旧行为——宁可不加门槛，不能因为门槛误判成失败。
        """
        try:
            db = self._get_db()
            for uname in self._verify_usernames(who):
                rows = db.get_messages(uname, limit=5)
                if rows:
                    return {
                        'username': uname,
                        'seq': max(int(r.get('sort_seq') or 0) for r in rows),
                        'ids': {(int(r.get('sort_seq') or 0),
                                 int(r.get('local_id') or 0)) for r in rows},
                    }
        except Exception as e:
            _log_swallowed('发送水位读取', e)
        return None

    def _verify_sent(self, text: str, who: Optional[str], mode: str = 'exact',
                     after: Optional[dict] = None) -> bool:
        """回读数据库确认这条消息真的发出去了。

        Args:
            text: 期望的正文
            who: 目标会话（空=当前会话按「自己」解析，与旧行为一致）
            mode: ``exact`` 正文逐字相等（普通文本）；``contains`` 包含匹配
                （引用/回复/@ 的正文会被微信包进 XML 或加前缀，只能包含匹配）
            after: 发送前 :meth:`_send_watermark` 拍的水位，只认比它新的行

        普通文本为什么不能是子串匹配：输入框里留着草稿时，粘贴会接在草稿后面，
        实际发出去的是「校准wechatauto 部署自检 OK」这类拼接正文——库里查得到、
        内容却是错的，子串匹配照样返回成功。逐字相等才拦得住。
        """
        if not text:
            return False
        try:
            db = self._get_db()
            marked = (after or {}).get('username')
            names = [marked] if marked else self._verify_usernames(who)
            for uname in names:
                for m in db.get_messages(uname, limit=5):
                    seq = int(m.get('sort_seq') or 0)
                    if after:
                        if seq < after['seq']:
                            continue   # 比水位旧的一定不是这次发的
                        if (seq, int(m.get('local_id') or 0)) in after['ids']:
                            continue
                    if m.get('sender_id') != 2:
                        continue
                    content = m.get('content') or ''
                    if content == text if mode == 'exact' else text in content:
                        return True
        except Exception as e:
            _log_swallowed('发送回读校验', e)
        return False

    # ------------------------------------------------------------------
    # 文件 / 图片发送（剪贴板 CF_HDROP + Ctrl+V）
    # ------------------------------------------------------------------
    @staticmethod
    def copy_files_to_clipboard(paths: List[str]) -> bool:
        """把本地文件以 CF_HDROP 格式写入剪贴板，供微信粘贴为附件/图片。

        相比走「+ 菜单 → 文件对话框」，该路线不依赖自绘界面图标定位，
        兼容性最好：在聊天输入框 Ctrl+V 后，微信会把文件/图片插入草稿。
        """
        paths = [os.path.abspath(p) for p in paths]
        if not paths:
            return False
        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", ctypes.c_uint),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
                ("fNC", ctypes.c_int),
                ("fWide", ctypes.c_int),
            ]
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        CF_HDROP = 15
        try:
            if not u32.OpenClipboard(None):
                return False
            u32.EmptyClipboard()
            df = DROPFILES()
            df.pFiles = ctypes.sizeof(DROPFILES)
            df.fWide = 1
            raw = (ctypes.string_at(ctypes.byref(df), ctypes.sizeof(DROPFILES))
                   + ("\0".join(paths) + "\0").encode("utf-16-le") + b"\0\0")
            h = k32.GlobalAlloc(0x0042, len(raw))
            if not h:
                u32.CloseClipboard()
                return False
            dst = k32.GlobalLock(h)
            ctypes.memmove(dst, raw, len(raw))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(CF_HDROP, h)
            u32.CloseClipboard()
            return True
        except Exception as e:
            wxlog.debug(f'写入 CF_HDROP 剪贴板失败：{e}')
            return False

    def _paste_attachment_and_send(self, path: str,
                                   box: Optional[Tuple[int, int, int, int]] = None) -> bool:
        """聚焦输入框 → 粘贴文件 → 检测草稿就绪 → 回车发送。

        图片草稿为彩色像素、文件为灰色卡片。图片粘贴偶发失效，
        采用「重新聚焦 + 重新粘贴」重试循环提升可靠性。

        box 可传入已探测好的输入框（连续发送附件时复用，避免重复探测）。
        """
        if not os.path.isfile(path):
            wxlog.debug(f'文件不存在：{path}')
            return False
        is_image = path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))
        if box is None:
            box = self.get_input_box()
        wxlog.debug(f'粘贴前输入框探测：{box}')
        for attempt in range(3):
            if not self.focus_input(box):
                return False
            if not self.copy_files_to_clipboard([path]):
                return False
            time.sleep(0.3)
            self._input.key(VK_A, ctrl=True)
            self._input.key(VK_DELETE)
            self._input.key(VK_V, ctrl=True)
            if is_image:
                draft_ok = self._input_box_has_color_draft(box, wait=6.0)
                wxlog.debug(f'粘贴尝试 {attempt + 1} 草稿检测：{draft_ok}')
                if draft_ok:
                    break
            else:
                time.sleep(2.0)
                break
        else:
            wxlog.debug('多次粘贴仍未检测到图片草稿，仍尝试回车')
        rhythm.gate('send-file')
        self._input.key(VK_RETURN)
        rhythm.nap(1.5)
        return True

    def _input_box_has_color_draft(self, box: Optional[Tuple[int, int, int, int]] = None,
                                   wait: float = 8.0) -> bool:
        """轮询输入框区域是否存在彩色像素（图片缩略图草稿已渲染）。

        图片粘贴后输入框会向上扩展容纳缩略图，因此扫描区域向上多
        覆盖一段（y0-150），避免草稿出现在探测基线之上而漏检。
        """
        if box is None:
            box = self.get_input_box()
        if not box:
            return False
        x0, y0, x1, y1 = box
        scan_y0 = max(100, y0 - 150)
        scan_y1 = min(self.render_h - 15, y1 + 80)
        rel = (x0 + 10, scan_y0, x1 - 10, scan_y1)
        screen_rect = self._rel_to_screen(rel)
        deadline = time.time() + wait
        last_colored = 0
        while time.time() < deadline:
            try:
                img = self._grab_screen(screen_rect)
                px = img.load()
                colored = 0
                for yy in range(0, img.size[1], 4):
                    for xx in range(0, img.size[0], 4):
                        r, g, b = px[xx, yy][:3]
                        if abs(r - g) > 20 or abs(g - b) > 20 or abs(r - b) > 20:
                            colored += 1
                last_colored = colored
                if colored > 30:
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        wxlog.debug(f'草稿检测失败：box={box} rel={rel} colored={last_colored}')
        return False

    def _open_chat_and_settle(self, who: str) -> bool:
        """打开会话并等待切换动画完成，返回输入框是否可探测。

        会话消息区若正显示大图，输入框探测会失败（返回 None）。
        重复点击会话会改变消息区滚动状态，重试直到探测成功。
        """
        for _ in range(5):
            self.open_chat(who)
            time.sleep(1.2)
            if self.get_input_box():
                return True
            wxlog.debug(f'打开会话后未检测到输入框，重试 open_chat({who})')
        return bool(self.get_input_box())

    def send_file(self, path: str, who: Optional[str] = None,
                  verify: bool = False) -> WxResponse:
        """发送本地文件（剪贴板粘贴路线）。"""
        if not self.ensure_visible():
            return WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        # 会话复用：目标仍是当前已打开会话且输入框可探测时跳过 open_chat
        # （open_chat 重扫侧栏/点击是附件发送的主要耗时点）
        if who and not (who == getattr(self, '_current_chat', None)
                        and self.get_input_box()):
            if not self._open_chat_and_settle(who):
                return WxResponse.failure(f'无法打开会话：{who}')
            self._current_chat = who
        box = self.get_input_box()
        before_seq = self._target_seq(who)
        if not self._paste_attachment_and_send(path, box):
            return WxResponse.failure(f'文件发送失败：{path}')
        if verify:
            ok = self._verify_attachment_sent(path, who, before_seq)
            return (WxResponse.success(f'文件已发送并确认：{path}', data={'path': path})
                    if ok else WxResponse.failure('文件已操作发送，但数据库未确认', data={'path': path}))
        return WxResponse.success(f'文件已发送：{path}', data={'path': path})

    def send_image(self, path: str, who: Optional[str] = None,
                   verify: bool = False) -> WxResponse:
        """发送本地图片（剪贴板粘贴路线，微信自动作为图片消息插入）。"""
        if not self.ensure_visible():
            return WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        if who and not (who == getattr(self, '_current_chat', None)
                        and self.get_input_box()):
            if not self._open_chat_and_settle(who):
                return WxResponse.failure(f'无法打开会话：{who}')
            self._current_chat = who
        box = self.get_input_box()
        before_seq = self._target_seq(who)
        if not self._paste_attachment_and_send(path, box):
            return WxResponse.failure(f'图片发送失败：{path}')
        if verify:
            ok = self._verify_attachment_sent(path, who, before_seq)
            return (WxResponse.success(f'图片已发送并确认：{path}', data={'path': path})
                    if ok else WxResponse.failure('图片已操作发送，但数据库未确认', data={'path': path}))
        return WxResponse.success(f'图片已发送：{path}', data={'path': path})

    def _target_seq(self, who: Optional[str]) -> int:
        """取目标会话当前最大 sort_seq，作为发送后校验的基线。"""
        try:
            db = self._get_db()
            if not who:
                who = db.get_self_info()['username']
            else:
                hits = db.search_contact(who)
                if hits:
                    who = hits[0]["username"]
            msgs = db.get_messages(who, limit=1)
            return msgs[0]['sort_seq'] if msgs else 0
        except Exception:
            return 0

    def _verify_attachment_sent(self, path: str, who: Optional[str],
                                before_seq: int = 0) -> bool:
        try:
            db = self._get_db()
            if not who:
                who = db.get_self_info()['username']
            else:
                hits = db.search_contact(who)
                if hits:
                    who = hits[0]["username"]
            fname = os.path.basename(path)
            fname_bytes = fname.encode('utf-8')
            # 微信会自动重名文件为 名称(n).ext，匹配文件名主干
            stem = os.path.splitext(fname)[0]
            stem_bytes = stem.encode('utf-8')
            is_image = path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))
            expect_type = '图片' if is_image else '文件/链接/卡片'
            # 微信发送后 DB 异步落库，文件消息落库更慢，轮询放宽到 10s
            deadline = time.time() + (6.0 if is_image else 10.0)
            while time.time() < deadline:
                # 优先：发送后出现了 sort_seq 更大的同类型消息（图片消息无文件名，只能靠它）
                for m in db.get_new_messages(who, since_seq=before_seq, limit=8):
                    if m.get('sender_id') != 2 or m.get('type') != expect_type:
                        continue
                    # 文件消息再做文件名匹配，图片消息仅凭类型+时序
                    if is_image:
                        return True
                    if fname in (m.get('content') or ''):
                        return True
                    row = db.get_message_row(who, m.get('local_id'))
                    if row and row.get('packed_info') and isinstance(row['packed_info'], bytes):
                        if fname_bytes in row['packed_info']:
                            return True
                        # 兼容重命名：主干匹配 + packed_info 内同一文件（主干名出现即可）
                        if stem_bytes in row['packed_info']:
                            return True
                time.sleep(0.5)
        except Exception as e:
            wxlog.debug(f'附件校验失败：{e}')
        return False

    # ------------------------------------------------------------------
    # 回复 / 引用
    # ------------------------------------------------------------------
    def _last_message_y(self) -> Optional[int]:
        """估算最近一条消息的位置（输入框上沿往上一点）。"""
        box = self.get_input_box()
        if not box:
            return None
        return max(100, box[1] - 80)

    def reply_msg(self, text: str, who: Optional[str] = None,
                  target_text: Optional[str] = None, verify: bool = False) -> WxResponse:
        """回复最近一条消息（悬停消息 → 点击「回复」→ 输入 → 发送）。

        target_text 用于在 OCR 结果中匹配目标消息（可选）。
        """
        # 回复落库的正文带有被回复消息的包装，逐字相等判不了，用包含匹配；
        # 水位保证「上一次发过的同一句话」不会被当成这一次的确认。
        mark = self._send_watermark(who) if verify else None
        if not self.ensure_visible():
            return WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        if who:
            self.open_chat(who)
            time.sleep(0.8)
        y = self._last_message_y()
        if y is None:
            return WxResponse.failure('未检测到消息区域')
        u = self._input._user32
        u.SetCursorPos(self.origin_x + (self.render_w + self.right_pane_left) // 2,
                       self.origin_y + y)
        time.sleep(0.8)
        # OCR 悬停工具栏（回复/引用等图标）
        region = (self.right_pane_left, y - 60, self.render_w, y + 40)
        items = self.ocr(region)
        click_pt = None
        for text, x, yy, w, h in items:
            if '回复' in text or '引用' in text or '转发' in text:
                click_pt = (x + w // 2, yy + h // 2)
                break
        if not click_pt:
            wxlog.debug('未识别到回复工具栏，尝试右键菜单')
            self.wx_click(self.origin_x + (self.render_w + self.right_pane_left) // 2,
                                    self.origin_y + y, right=True)
            time.sleep(0.6)
            menu = self.ocr((self.right_pane_left, y, self.render_w, self.render_h))
            for text, x, yy, w, h in menu:
                if '回复' in text:
                    click_pt = (x + w // 2, yy + h // 2)
                    break
        if not click_pt:
            return WxResponse.failure('未找到回复入口')
        self.wx_click(self.origin_x + click_pt[0], self.origin_y + click_pt[1])
        time.sleep(0.6)
        if not self.input_text(text):
            return WxResponse.failure('输入回复内容失败')
        self.click_send()
        if verify:
            ok = self._verify_sent(text, who, mode='contains', after=mark)
            return (WxResponse.success(f'回复已发送并确认：{text}', data={'content': text})
                    if ok else WxResponse.failure('回复已操作发送，但数据库未确认', data={'content': text}))
        return WxResponse.success(f'回复已发送：{text}', data={'content': text})

    def _locate_last_message_pt(self, y: int) -> Optional[Tuple[int, int]]:
        """OCR 实测最近一条消息的文本中心（横坐标实测而不是估算中心）。

        参照 previous 修复（原图下载/打开原图）：控件实际位置不能按
        消息区几何中心估算，必须用识别到的文本真实位置，否则自己消息
        气泡偏右时点击坐标会横向偏移。
        """
        band = (self.right_pane_left, max(80, y - 40),
                self.render_w, min(self.render_h, y + 40))
        items = self.ocr(band)
        best = None
        best_d = 1 << 30
        for t, x, yy, w, h in items:
            tt = (t or '').strip()
            if not tt:
                continue
            cy = yy + h // 2
            d = abs(cy - y)
            if d < best_d:
                best_d = d
                best = (x + w // 2, cy)
        return best

    def quote_msg(self, text: str, who: Optional[str] = None,
                  target_text: Optional[str] = None, verify: bool = False) -> WxResponse:
        """引用指定/最近一条消息（右键 → 菜单「引用」→ 输入 → 发送）。

        target_text 用于 OCR 定位要引用的消息文案（可选）；省略时引用最近一条。
        """
        # 引用消息落库的正文里还包着被引用那条，只能包含匹配；水位见 _send_watermark。
        mark = self._send_watermark(who) if verify else None
        if not self.ensure_visible():
            return WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        if who:
            self.open_chat(who)
            time.sleep(0.8)
        box = self.get_input_box()
        msg_bottom = box[1] if box else int(self.render_h * 0.8)
        if target_text:
            items = self.ocr((self.right_pane_left, 100, self.render_w, msg_bottom))
            pt = None
            for t, x, yy, w, h in items:
                if target_text in t:
                    pt = (x + w // 2, yy + h // 2)
                    break
            if not pt:
                return WxResponse.failure(f'未找到要引用的消息：{target_text}')
        else:
            y = self._last_message_y()
            if y is None:
                return WxResponse.failure('未检测到消息区域')
            hit = self._locate_last_message_pt(y)
            if hit is None:
                return WxResponse.failure('未定位到最近一条消息')
            pt = hit
        # 右键点击目标消息，弹出操作菜单
        # 用 SendInput（而非 mouse_event）：微信渲染窗口对 mouse_event 的
        # 右键不响应，SendInput 可直接命中弹出菜单（原图那次的同类修复）。
        self._input.send_input_click(self.origin_x + pt[0], self.origin_y + pt[1], right=True)
        time.sleep(0.7)
        menu = self.ocr((self.right_pane_left, max(80, pt[1] - 200), self.render_w, self.render_h))
        click_pt = None
        for t, x, yy, w, h in menu:
            if '引用' in t:
                click_pt = (x + w // 2, yy + h // 2)
                break
        if not click_pt:
            return WxResponse.failure('未找到「引用」菜单项')
        self.wx_click(self.origin_x + click_pt[0], self.origin_y + click_pt[1])
        time.sleep(0.8)
        if not self.input_text(text):
            return WxResponse.failure('输入引用内容失败')
        self.click_send()
        if verify:
            ok = self._verify_sent(text, who, mode='contains', after=mark)
            return (WxResponse.success(f'引用已发送并确认：{text}', data={'content': text})
                    if ok else WxResponse.failure('引用已操作发送，但数据库未确认', data={'content': text}))
        return WxResponse.success(f'引用已发送：{text}', data={'content': text})

    # ------------------------------------------------------------------
    # 艾特成员（群聊）
    # ------------------------------------------------------------------
    def at_member(self, member: str, text: str, who: Optional[str] = None,
                  verify: bool = False) -> WxResponse:
        """在群聊中 @ 成员后追加发送 text。

        流程：输入框键入 '@' → OCR 成员选择弹层定位成员 → 点击 → 输入正文 → 发送。
        """
        # @ 消息落库正文带有「@昵称」包装，只能包含匹配；水位见 _send_watermark。
        mark = self._send_watermark(who) if verify else None
        if not self.ensure_visible():
            return WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        if who:
            self.open_chat(who)
            time.sleep(0.8)
        if not self.focus_input():
            return WxResponse.failure('输入框不可用')
        self._input.type_unicode('@')
        time.sleep(0.9)
        box = self.get_input_box()
        popup = (self.right_pane_left, max(80, (box[1] if box else int(self.render_h * 0.49)) - 500),
                 self.render_w, (box[1] if box else int(self.render_h * 0.77)))
        items = self.ocr(popup)
        target = None
        for t, x, yy, w, h in items:
            if member in t or t.startswith(member):
                target = (x + w // 2, yy + h // 2)
                break
        if not target:
            return WxResponse.failure(f'未在成员列表中找到：{member}')
        self.wx_click(self.origin_x + target[0], self.origin_y + target[1])
        time.sleep(0.6)
        if text and not self.input_text(text):
            return WxResponse.failure('输入消息正文失败')
        self.click_send()
        if verify:
            ok = self._verify_sent(text, who, mode='contains', after=mark)
            return (WxResponse.success(f'@成员消息已发送并确认', data={'member': member, 'content': text})
                    if ok else WxResponse.failure('@消息已操作发送，但数据库未确认', data={'member': member}))
        return WxResponse.success(f'@成员消息已发送', data={'member': member, 'content': text})


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def quick_send(text: str, who: str = None, verify: bool = False) -> WxResponse:
    """一行式发送消息。

    >>> from wechatauto.guia import quick_send
    >>> quick_send('你好', '文件传输助手')
    """
    wx = WeChatGUI()
    return wx.send_msg(text, who, verify)


def quick_send_file(path: str, who: str = None, verify: bool = False) -> WxResponse:
    """一行式发送文件。"""
    wx = WeChatGUI()
    return wx.send_file(path, who, verify)


def quick_send_image(path: str, who: str = None, verify: bool = False) -> WxResponse:
    """一行式发送图片。"""
    wx = WeChatGUI()
    return wx.send_image(path, who, verify)


def quick_reply(text: str, who: str = None, verify: bool = False) -> WxResponse:
    """一行式回复最近一条消息。"""
    wx = WeChatGUI()
    return wx.reply_msg(text, who, verify=verify)


def quick_quote(text: str, who: str = None, target_text: str = None,
                verify: bool = False) -> WxResponse:
    """一行式引用消息并发送。

    >>> from wechatauto.guia import quick_quote
    >>> quick_quote('收到', '文件传输助手', target_text='要引用的原文')
    """
    wx = WeChatGUI()
    return wx.quote_msg(text, who, target_text=target_text, verify=verify)
