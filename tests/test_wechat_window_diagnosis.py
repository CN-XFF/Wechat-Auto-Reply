from pathlib import Path
import ctypes
import inspect
import queue
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "wechatauto-replica"))
sys.path.insert(0, str(ROOT))

from reply_core import wechat_bridge as bridge_module  # noqa: E402
from reply_core.wechat_bridge import WeChatBridge  # noqa: E402
from wechatauto import guia as guia_module  # noqa: E402


def test_preferred_display_and_ocr_name_is_remark_else_wechat_nickname():
    contacts = {
        "with-remark": {
            "username": "with-remark",
            "remark": "班长",
            "nick_name": "联系人B",
        },
        "without-remark": {
            "username": "without-remark",
            "remark": "",
            "nick_name": "Надя",
        },
    }
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.db = SimpleNamespace(search_contact=lambda key: [contacts[key]])

    assert bridge.display_name_for_username("with-remark") == "班长"
    assert bridge.display_name_for_username("without-remark") == "Надя"


def test_bridge_has_read_only_wechat_window_diagnosis():
    source = (ROOT / "reply_core" / "wechat_bridge.py").read_text(encoding="utf-8")
    diagnosis = source.split("def diagnose_wechat_window", 1)[1].split("def send", 1)[0]
    assert "recover" in diagnosis
    assert "recovery_actions" in diagnosis
    assert "restore_window" in diagnosis
    assert "launch_or_show_wechat" in diagnosis
    assert "wechat_gui_created" in diagnosis
    assert "window_rect" in diagnosis
    assert "ensure_visible" in diagnosis
    assert "open_chat" in diagnosis
    assert "chat_is_open" in diagnosis
    assert "_paste_text_win32" not in diagnosis
    assert "_submit_once" not in diagnosis


def test_send_failure_logs_wechat_window_diagnosis():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    delayed = source.split("def _delayed_send", 1)[1].split("def _confirm_dialog", 1)[0]
    assert "self.bridge.diagnose_wechat_window(target_name, recover=True)" in delayed
    assert "发送失败后的微信窗口检测" in delayed


def test_bridge_can_restore_minimized_or_tray_wechat():
    source = (ROOT / "reply_core" / "wechat_bridge.py").read_text(encoding="utf-8")
    assert "def _restore_window" in source
    assert "_restore_keep_maximize(ctypes.windll.user32, hwnd)" in source
    assert "ShowWindowAsync" not in source
    assert "BringWindowToTop(hwnd)" not in source
    assert "def _wechat_exe_candidates" in source
    assert "def _launch_or_show_wechat" in source
    assert "WeChat.exe" in source
    assert r"Tencent\Weixin\Weixin.exe" in source


def test_weixin_installation_path_is_discovered(monkeypatch, tmp_path):
    install_root = tmp_path / "Program Files"
    exe = install_root / "Tencent" / "Weixin" / "Weixin.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(install_root))
    monkeypatch.setenv("ProgramFiles(x86)", "")
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setenv("APPDATA", "")

    bridge = fake_bridge()

    assert str(exe) in bridge._wechat_exe_candidates()


def test_main_window_discovery_includes_hidden_tray_window():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)

    class HiddenTrayWindow:
        def IsWindow(self, hwnd):
            return hwnd == 123

        def IsWindowVisible(self, _hwnd):
            return False

        def GetWindowTextW(self, _hwnd, buffer, _size):
            buffer.value = "微信"
            return 2

        def GetClassNameW(self, _hwnd, buffer, _size):
            buffer.value = "Qt51514QWindowIcon"
            return len(buffer.value)

        def GetWindowRect(self, _hwnd, rect_ptr):
            rect = rect_ptr._obj
            rect.left, rect.top, rect.right, rect.bottom = 100, 100, 1400, 900
            return 1

        def EnumWindows(self, callback, data):
            callback(123, data)

        def FindWindowW(self, *_args):
            return 0

    gui._input = SimpleNamespace(_user32=HiddenTrayWindow())
    gui._get_pid = lambda _hwnd: 456
    gui._process_name = lambda _pid: "weixin.exe"

    assert gui._find_main_window("微信") == 123


def test_hidden_tray_window_uses_tray_activation_instead_of_forced_show(monkeypatch):
    state = {"visible": False, "iconic": False, "activated": 0, "forced": 0}

    class HiddenTrayWindow:
        def IsWindow(self, hwnd):
            return hwnd == 123

        def IsWindowVisible(self, _hwnd):
            return state["visible"]

        def IsIconic(self, _hwnd):
            return state["iconic"]

        def IsHungAppWindow(self, _hwnd):
            return False

        def GetWindowRect(self, _hwnd, rect_ptr):
            rect = rect_ptr._obj
            rect.left, rect.top, rect.right, rect.bottom = 100, 100, 1400, 900
            return 1

        def ShowWindowAsync(self, *_args):
            state["forced"] += 1
            raise AssertionError("托盘隐藏窗口不能用 HWND 强制显示")

    user32 = HiddenTrayWindow()

    def activate_tray(_user32):
        state["activated"] += 1
        state["visible"] = True
        return True

    monkeypatch.setattr(guia_module, "_activate_wechat_tray_icon", activate_tray)
    monkeypatch.setattr(guia_module.time, "sleep", lambda _seconds: None)

    assert guia_module._restore_keep_maximize(user32, 123) is True
    assert state == {"visible": True, "iconic": False, "activated": 1, "forced": 0}


def test_tray_activation_double_clicks_only_one_exact_wechat_icon(monkeypatch):
    events = []

    class FakeRect:
        left, top, right, bottom = 100, 100, 124, 124

    class FakeButton:
        Name = "微信"
        ControlTypeName = "ButtonControl"
        IsOffscreen = False
        BoundingRectangle = FakeRect()

        def GetChildren(self):
            return []

        def DoubleClick(self, **kwargs):
            events.append(("double_click", kwargs))

    class FakeRoot:
        Name = "Taskbar"
        ControlTypeName = "PaneControl"
        BoundingRectangle = FakeRect()
        IsOffscreen = False

        def GetChildren(self):
            return [FakeButton()]

    class FakeUser32:
        def FindWindowW(self, class_name, _title):
            return 456 if class_name == "Shell_TrayWnd" else 0

        def IsWindow(self, hwnd):
            return hwnd == 456

    fake_auto = SimpleNamespace(
        InitializeUIAutomationInCurrentThread=lambda: events.append(("initialize",)),
        UninitializeUIAutomationInCurrentThread=lambda: events.append(("uninitialize",)),
        ControlFromHandle=lambda _hwnd: FakeRoot(),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", fake_auto)

    assert guia_module._activate_wechat_tray_icon(FakeUser32(), timeout=1.0) is True
    assert [event[0] for event in events] == ["initialize", "double_click", "uninitialize"]
    assert events[1][1] == {"simulateMove": False, "waitTime": 0}


def test_ambiguous_wechat_tray_icons_are_not_clicked(monkeypatch):
    events = []

    class FakeRect:
        def __init__(self, left):
            self.left, self.top, self.right, self.bottom = left, 100, left + 24, 124

    class FakeButton:
        ControlTypeName = "ButtonControl"
        IsOffscreen = False

        def __init__(self, name, left):
            self.Name = name
            self.BoundingRectangle = FakeRect(left)

        def GetChildren(self):
            return []

        def DoubleClick(self, **_kwargs):
            events.append("clicked")

    class FakeRoot:
        Name = "Taskbar"
        ControlTypeName = "PaneControl"
        IsOffscreen = False
        BoundingRectangle = FakeRect(0)

        def GetChildren(self):
            return [FakeButton("微信", 100), FakeButton("WeChat", 140)]

    class FakeUser32:
        def FindWindowW(self, class_name, _title):
            return 456 if class_name == "Shell_TrayWnd" else 0

        def IsWindow(self, hwnd):
            return hwnd == 456

    fake_auto = SimpleNamespace(
        InitializeUIAutomationInCurrentThread=lambda: None,
        UninitializeUIAutomationInCurrentThread=lambda: None,
        ControlFromHandle=lambda _hwnd: FakeRoot(),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", fake_auto)

    assert guia_module._activate_wechat_tray_icon(FakeUser32(), timeout=1.0) is False
    assert events == []


def test_tray_activation_opens_overflow_before_clicking_hidden_wechat_icon(monkeypatch):
    events = []
    state = {"overflow_open": False}

    class FakeRect:
        left, top, right, bottom = 100, 100, 124, 124

    class FakeButton:
        ControlTypeName = "ButtonControl"
        IsOffscreen = False
        BoundingRectangle = FakeRect()

        def __init__(self, name):
            self.Name = name

        def GetChildren(self):
            return []

        def Click(self, **kwargs):
            assert self.Name == "显示隐藏的图标"
            assert kwargs == {"simulateMove": False, "waitTime": 0}
            events.append("open_overflow")
            state["overflow_open"] = True

        def DoubleClick(self, **_kwargs):
            assert self.Name == "微信"
            events.append("double_click_wechat")

    overflow_button = FakeButton("显示隐藏的图标")
    wechat_button = FakeButton("微信")

    class FakeRoot:
        Name = "Taskbar"
        ControlTypeName = "PaneControl"
        BoundingRectangle = FakeRect()
        IsOffscreen = False

        def GetChildren(self):
            return [wechat_button] if state["overflow_open"] else [overflow_button]

    class FakeUser32:
        def FindWindowW(self, class_name, _title):
            if class_name == "Shell_TrayWnd":
                return 456
            if class_name == "NotifyIconOverflowWindow" and state["overflow_open"]:
                return 789
            return 0

        def IsWindow(self, hwnd):
            return hwnd in (456, 789)

    fake_auto = SimpleNamespace(
        InitializeUIAutomationInCurrentThread=lambda: None,
        UninitializeUIAutomationInCurrentThread=lambda: None,
        ControlFromHandle=lambda _hwnd: FakeRoot(),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", fake_auto)

    assert guia_module._activate_wechat_tray_icon(FakeUser32(), timeout=1.0) is True
    assert events == ["open_overflow", "double_click_wechat"]


def test_visible_but_hung_wechat_window_is_not_treated_as_ready():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)

    class HungWindow:
        def IsWindow(self, _hwnd):
            return True

        def IsWindowVisible(self, _hwnd):
            return True

        def IsIconic(self, _hwnd):
            return False

        def IsHungAppWindow(self, _hwnd):
            return True

        def GetWindowRect(self, _hwnd, rect_ptr):
            rect = rect_ptr._obj
            rect.left, rect.top, rect.right, rect.bottom = 100, 100, 1400, 900
            return 1

    gui.main_hwnd = 123
    gui._input = SimpleNamespace(_user32=HungWindow())

    assert gui._window_is_displayable() is False


def test_ensure_visible_does_not_trust_cached_but_hidden_window(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    state = {"visible": False, "events": []}
    gui._last_visible_ok = True
    gui._last_visible_ts = bridge_module.time.time()
    gui.is_alive = lambda: True
    gui._window_is_displayable = lambda: state["visible"]

    def restore():
        state["events"].append("restore")
        state["visible"] = True
        return True

    gui._restore_main_window = restore
    gui.bring_to_front = lambda **_kwargs: state["events"].append("foreground") or True
    gui._update_render_rect = lambda: state["events"].append("update_rect")
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.ensure_visible() is True
    assert state["events"] == ["restore", "foreground", "update_rect"]


def test_ensure_visible_minimizes_blockers_only_after_activation_fails(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    state = {"visible": True, "attempts": 0, "events": []}
    gui._last_visible_ok = False
    gui.is_alive = lambda: True
    gui._window_is_displayable = lambda: state["visible"]
    gui._restore_main_window = lambda: state["events"].append("restore") or True

    def activate(**_kwargs):
        state["attempts"] += 1
        state["events"].append("foreground")
        return state["attempts"] > 1

    gui.bring_to_front = activate
    gui._minimize_blockers = lambda: state["events"].append("minimize blockers") or 2
    gui._update_render_rect = lambda: state["events"].append("update rect")
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.ensure_visible() is True
    assert state["events"] == [
        "restore", "foreground", "minimize blockers", "foreground", "update rect"
    ]


def test_bring_to_front_retries_when_windows_rejects_background_activation(monkeypatch):
    state = {"foreground": 900, "attached": False, "events": []}

    class User32:
        def IsWindow(self, hwnd):
            return hwnd == 123

        def GetForegroundWindow(self):
            return state["foreground"]

        def BringWindowToTop(self, hwnd):
            state["events"].append("bring_window_to_top")
            return 1

        def SetWindowPos(self, *_args):
            state["events"].append("set_window_pos")
            return 1

        def SetForegroundWindow(self, hwnd):
            state["events"].append("set_foreground")
            if state["attached"]:
                state["foreground"] = hwnd
                return 1
            return 0

        def GetWindowThreadProcessId(self, _hwnd, _pid):
            return 22

        def AttachThreadInput(self, current, foreground, attach):
            state["events"].append(("attach", current, foreground, attach))
            state["attached"] = attach
            return 1

    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd = 123
    gui._input = SimpleNamespace(_user32=User32())
    gui._window_is_displayable = lambda: True
    gui._restore_main_window = lambda: True
    monkeypatch.setattr(bridge_module.threading, "get_native_id", lambda: 11)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.bring_to_front() is True
    assert state["foreground"] == 123
    assert state["events"] == [
        "bring_window_to_top",
        "set_foreground",
        ("attach", 11, 22, True),
        "set_foreground",
        ("attach", 11, 22, False),
    ]


def test_caption_activation_click_only_targets_exposed_wechat_caption():
    state = {"foreground": 900, "hit_hwnd": 123, "clicks": []}

    class User32:
        def GetWindowRect(self, _hwnd, rect_ptr):
            rect = ctypes.cast(
                rect_ptr, ctypes.POINTER(guia_module.wintypes.RECT)
            ).contents
            rect.left, rect.top, rect.right, rect.bottom = 100, 100, 1100, 800
            return 1

        def GetWindowThreadProcessId(self, hwnd, pid_ptr):
            if pid_ptr:
                pid = ctypes.cast(
                    pid_ptr, ctypes.POINTER(guia_module.wintypes.DWORD)
                ).contents
                pid.value = 55 if hwnd == 123 else 66
            return 22

        def SendMessageTimeoutW(
            self, _hwnd, _message, _wparam, _lparam, _flags, _timeout, result_ptr
        ):
            result = ctypes.cast(
                result_ptr, ctypes.POINTER(ctypes.c_size_t)
            ).contents
            result.value = 2  # HTCAPTION
            return 1

        def WindowFromPoint(self, _point):
            return state["hit_hwnd"]

        def GetForegroundWindow(self):
            return state["foreground"]

    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd = 123
    gui._input = SimpleNamespace(
        _user32=User32(),
        activation_click=lambda x, y, hwnd: (
            state["clicks"].append((x, y, hwnd))
            or state.update(foreground=hwnd)
            or True
        ),
    )

    assert gui._click_caption_to_activate(gui._input._user32) is True
    assert len(state["clicks"]) == 1
    assert state["clicks"][0][2] == 123

    state.update(foreground=900, hit_hwnd=999, clicks=[])
    assert gui._click_caption_to_activate(gui._input._user32) is False
    assert state["clicks"] == []


def test_activation_click_restores_pointer_and_waits_for_target_activation(monkeypatch):
    state = {"cursor": (20, 30), "foreground": 900, "mouse": []}

    class User32:
        def GetCursorPos(self, point_ptr):
            point = ctypes.cast(
                point_ptr, ctypes.POINTER(guia_module.wintypes.POINT)
            ).contents
            point.x, point.y = state["cursor"]
            return 1

        def SetCursorPos(self, x, y):
            state["cursor"] = (x, y)
            return 1

        def mouse_event(self, flag, *_args):
            state["mouse"].append(flag)
            if flag == guia_module.MOUSEEVENTF_LEFTUP:
                state["foreground"] = 123

        def GetForegroundWindow(self):
            return state["foreground"]

    win_input = guia_module.WinInput.__new__(guia_module.WinInput)
    win_input._user32 = User32()
    monkeypatch.setattr(guia_module.time, "sleep", lambda _seconds: None)

    assert win_input.activation_click(400, 500, 123) is True
    assert state["mouse"] == [
        guia_module.MOUSEEVENTF_LEFTDOWN,
        guia_module.MOUSEEVENTF_LEFTUP,
    ]
    assert state["cursor"] == (20, 30)


def test_send_reports_occluded_window_as_foreground_failure_not_invisible():
    bridge = fake_bridge()
    bridge._window_rect = lambda _hwnd: (100, 100, 1400, 900)
    bridge._clear_cached_wechat = lambda: None

    class VisibleButCoveredWindow:
        main_hwnd = 123
        _cached_db = None

        def ensure_visible(self):
            return False

        def _window_is_displayable(self):
            return True

    wx = VisibleButCoveredWindow()
    bridge._reusable_wechat_gui = lambda: wx
    try:
        bridge._send_once("测试账号", "测试内容")
    except RuntimeError as exc:
        assert "仍可见" in str(exc)
        assert "未能切到前台" in str(exc)
    else:
        raise AssertionError("expected foreground activation to fail safely")


def test_open_chat_stops_before_ocr_if_window_cannot_be_restored():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.ensure_visible = lambda: False
    gui.find_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("contact OCR must not run while WeChat is hidden")
    )

    assert gui.open_chat("测试账号") is False


def fake_bridge():
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"dry_run": False, "send_submit_method": "enter"}
    bridge.targets = {"测试账号": "wxid_example_004"}
    bridge.ui_names = {"测试账号": "测试账号.²"}
    bridge.db = object()
    bridge._send_mark = lambda _username: set()
    bridge._verify_sent_db = lambda _username, _text, before: bool(before is not None)
    return bridge


def cacheable_send_bridge(monkeypatch):
    bridge = fake_bridge()
    bridge._send_queue = queue.Queue()
    bridge._send_worker = None
    bridge._send_worker_ident = None
    bridge._send_worker_lock = threading.Lock()
    bridge._send_worker_stopping = False
    bridge.listener = SimpleNamespace(stop=lambda: None)
    state = {
        "tick": 100,
        "rect": (100, 100, 1500, 900),
        "constructed_on": [],
        "opened_on": [],
        "opened": [],
        "visible_checks": [],
        "sent": [],
    }

    class FakeWeChat:
        main_hwnd = 123
        pid = 77
        _cached_db = None

        def __init__(self):
            self.active_chat = "联系人示例"
            state["constructed_on"].append(threading.get_ident())

        def ensure_visible(self):
            state["visible_checks"].append(threading.get_ident())
            return True

        def open_chat(self, name):
            state["opened"].append(name)
            state["opened_on"].append(threading.get_ident())
            self.active_chat = name
            return True

        def _chat_is_open(self, name):
            return self.active_chat == name

    monkeypatch.setattr(bridge_module, "WeChatGUI", lambda **_kwargs: FakeWeChat())
    monkeypatch.setattr(bridge, "_last_input_event_tick", lambda: state["tick"])
    monkeypatch.setattr(bridge, "_window_process_id", lambda _hwnd: 77)
    monkeypatch.setattr(
        bridge,
        "_cached_wechat_window_is_current",
        lambda _wx: state["rect"] == bridge._cached_wechat_rect,
    )
    monkeypatch.setattr(bridge, "_window_rect", lambda _hwnd: state["rect"])
    monkeypatch.setattr(bridge, "_paste_text_win32", lambda _wx, text: state["sent"].append(("paste", text)))
    monkeypatch.setattr(bridge, "_submit_once", lambda _wx, method: state["sent"].append(("submit", method)))
    bridge._send_mark = lambda _username: set()
    bridge._verify_sent_db = lambda _username, _text, before: before is not None
    return bridge, state


def test_send_restores_offscreen_window_and_opens_test_account_not_current_chat(monkeypatch):
    bridge = fake_bridge()
    state = {"rect": (-32000, -32000, -30420, -30959), "opened": [], "sent": []}

    class FakeWeChat:
        main_hwnd = 123
        _cached_db = None
        active_chat = "联系人示例"

        def ensure_visible(self):
            return True

        def open_chat(self, name):
            state["opened"].append(name)
            self.active_chat = name
            return True

        def _chat_is_open(self, name):
            return self.active_chat == name

    fake_wx = FakeWeChat()
    def create_wechat(**_kwargs):
        # Real WeChatGUI restores the tray-hidden window during construction.
        state["rect"] = (100, 100, 1500, 900)
        return fake_wx

    monkeypatch.setattr(bridge_module, "WeChatGUI", create_wechat)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)
    bridge._window_rect = lambda _hwnd: state["rect"]

    bridge._paste_text_win32 = lambda _wx, text: state["sent"].append(("paste", text))
    bridge._submit_once = lambda _wx, method: state["sent"].append(("submit", method))

    bridge.send("测试账号", "测试消息")

    assert state["opened"] == ["测试账号.²"]
    assert state["sent"] == [("paste", "测试消息"), ("submit", "enter")]


def test_send_refuses_when_correct_contact_cannot_be_confirmed(monkeypatch):
    bridge = fake_bridge()
    state = {"sent": []}

    class FakeWeChat:
        main_hwnd = 123
        _cached_db = None

        def ensure_visible(self):
            return True

        def open_chat(self, _name):
            return True

        def _chat_is_open(self, _name):
            return False

    monkeypatch.setattr(bridge_module, "WeChatGUI", lambda **_kwargs: FakeWeChat())
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)
    bridge._window_rect = lambda _hwnd: (100, 100, 1500, 900)
    bridge._restore_window = lambda _hwnd: True
    bridge._paste_text_win32 = lambda *_args: state["sent"].append("paste")
    bridge._submit_once = lambda *_args: state["sent"].append("submit")

    try:
        bridge.send("测试账号", "不应发错人")
    except RuntimeError as exc:
        assert "未执行发送" in str(exc)
    else:
        raise AssertionError("should refuse to send when the active chat cannot be verified")

    assert state["sent"] == []


def test_sequential_sends_reuse_window_on_one_ui_thread_when_input_is_unchanged(monkeypatch):
    bridge, state = cacheable_send_bridge(monkeypatch)
    caller_thread = threading.get_ident()
    try:
        bridge.send("测试账号", "第一条")
        bridge.send("测试账号", "第二条")
    finally:
        bridge.stop()

    assert len(state["constructed_on"]) == 1
    assert len(state["visible_checks"]) == 2
    assert state["opened"] == ["测试账号.²", "测试账号.²"]
    assert len(set(state["constructed_on"] + state["opened_on"])) == 1
    assert state["constructed_on"][0] != caller_thread
    assert state["sent"] == [
        ("paste", "第一条"), ("submit", "enter"),
        ("paste", "第二条"), ("submit", "enter"),
    ]


def test_sequential_send_relocates_after_input_or_window_change(monkeypatch):
    for change in ("input", "position"):
        bridge, state = cacheable_send_bridge(monkeypatch)
        try:
            bridge.send("测试账号", "第一条")
            if change == "input":
                state["tick"] += 1
            else:
                state["rect"] = (120, 100, 1520, 900)
            bridge.send("测试账号", "第二条")
        finally:
            bridge.stop()

        assert len(state["constructed_on"]) == 2
        assert len(state["visible_checks"]) == 2


def test_diagnosis_launches_or_shows_wechat_when_no_window_is_available(monkeypatch):
    bridge = fake_bridge()
    attempts = {"count": 0, "launched": 0, "opened": []}

    class FakeWeChat:
        main_hwnd = 456
        _cached_db = None

        def ensure_visible(self):
            return True

        def open_chat(self, name):
            attempts["opened"].append(name)
            return True

        def _chat_is_open(self, name):
            return name == "测试账号.²"

    def create_wechat(**_kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise RuntimeError("微信主窗口暂不可用")
        return FakeWeChat()

    monkeypatch.setattr(bridge_module, "WeChatGUI", create_wechat)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)
    bridge._launch_or_show_wechat = lambda: attempts.update(launched=attempts["launched"] + 1) or {"started": True}
    bridge._window_rect = lambda _hwnd: (100, 100, 1500, 900)
    bridge._restore_window = lambda _hwnd: True

    result = bridge.diagnose_wechat_window("测试账号", recover=True)

    assert attempts["launched"] == 1
    assert result["ensure_visible"] is True
    assert result["open_chat"] is True
    assert result["chat_is_open"] is True
    assert attempts["opened"] == ["测试账号.²"]


def test_short_ocr_contact_names_require_full_match():
    gui_class = bridge_module.WeChatGUI

    assert gui_class._name_matches("逗龙", "逗龙") is True
    assert gui_class._name_matches("聊天 逗龙", "逗龙") is True
    assert gui_class._name_matches("逗", "逗龙") is False
    assert gui_class._name_matches("龙", "逗龙") is False
    assert gui_class._name_matches("件传输助", "文件传输助手") is True


def test_clickable_contact_rows_are_stricter_than_title_ocr():
    gui_class = bridge_module.WeChatGUI

    assert gui_class._contact_row_match_score("小号", "小号") == 100
    assert gui_class._contact_row_match_score("0小号", "小号") == 90
    assert gui_class._contact_row_match_score("小号微信怎么申请", "小号") == 0
    assert gui_class._contact_row_match_score("测试账号 已收到", "测试账号") == 0


def test_chat_title_match_rejects_group_titles_containing_the_person():
    gui_class = bridge_module.WeChatGUI

    assert gui_class._chat_title_matches_contact("联系人B🥟", "联系人B🥟") is True
    assert gui_class._chat_title_matches_contact(
        "联系人B🥟的聊天记录", "联系人B🥟") is True
    assert gui_class._chat_title_matches_contact(
        "联系人B🥟，3人群聊", "联系人B🥟") is False
    assert gui_class._chat_title_matches_contact(
        "文件传输助", "文件传输助手") is True
    assert gui_class._chat_title_matches_contact("李昊", "李昊阳") is False


def test_chat_is_open_does_not_trust_stale_row_or_group_title():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui._current_chat = "联系人B🥟"
    gui._direct_session_trusted_until = bridge_module.time.time() + 60
    gui.right_pane_left = 300
    gui.render_w = 1200
    gui.ocr_zoomed = lambda *_args, **_kwargs: [
        ("联系人B🥟，3人群聊", 400, 20, 150, 30),
    ]

    assert gui._chat_is_open("联系人B🥟") is False


def test_contact_font_gate_rejects_small_or_low_contrast_preview_text():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd = 123
    gui.render_h = 1000
    gui._rel_to_screen = lambda box: box

    def text_patch(foreground):
        image = guia_module.Image.new("RGB", (40, 20), (40, 40, 40))
        for x in range(6, 34, 4):
            for y in range(4, 16):
                image.putpixel((x, y), foreground)
        return image

    gui._grab_screen = lambda _box: text_patch((220, 220, 220))
    assert gui._is_primary_contact_label(
        {"name": "小号", "x": 10, "y": 100, "w": 40, "h": 18}) is True

    gui._grab_screen = lambda _box: text_patch((125, 125, 125))
    assert gui._is_primary_contact_label(
        {"name": "小号", "x": 10, "y": 100, "w": 40, "h": 18}) is False
    assert gui._is_primary_contact_label(
        {"name": "小号", "x": 10, "y": 100, "w": 40, "h": 12}) is False


def test_unchanged_window_reuses_last_verified_input_point():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd, gui.render_hwnd = 100, 101
    gui.origin_x, gui.origin_y = 100, 100
    gui.render_w, gui.render_h = 800, 600
    gui.render_rect = (100, 100, 900, 700)
    gui.sidebar_right, gui.right_pane_left = 176, 176
    gui.layout_profile = "wide"
    gui._last_live_sidebar_ratio = 0.22
    gui._update_render_rect = lambda: None
    gui._probe_input_box = lambda: (_ for _ in ()).throw(
        AssertionError("unchanged geometry should reuse the verified point")
    )
    rect = (100, 100, 920, 720)
    signature = gui._reply_geometry_signature(rect, 0.260, 0.788)
    gui._last_confirmed_reply_geometry = (signature, (0.31, 0.81))

    assert gui.refresh_input_geometry_before_reply(rect) == (0.31, 0.81)
    assert gui.confirm_reply_input_geometry() == (signature, (0.31, 0.81))


def test_changed_window_requires_fresh_input_detection():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd, gui.render_hwnd = 100, 101
    gui.origin_x, gui.origin_y = 100, 100
    gui.render_w, gui.render_h = 800, 600
    gui.render_rect = (100, 100, 900, 700)
    gui.sidebar_right, gui.right_pane_left = 176, 176
    gui.layout_profile = "wide"
    gui._last_live_sidebar_ratio = 0.22
    gui._update_render_rect = lambda: None
    gui._probe_input_box = lambda: None
    old_rect = (100, 100, 920, 720)
    old_signature = gui._reply_geometry_signature(old_rect, 0.260, 0.788)
    gui._last_confirmed_reply_geometry = (old_signature, (0.31, 0.81))

    assert gui.refresh_input_geometry_before_reply(
        (120, 100, 940, 720)) is None


def test_changed_window_uses_freshly_detected_input_point():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd, gui.render_hwnd = 100, 101
    gui.origin_x, gui.origin_y = 100, 100
    gui.render_w, gui.render_h = 800, 600
    gui.render_rect = (100, 100, 900, 700)
    gui.sidebar_right, gui.right_pane_left = 176, 176
    gui.layout_profile = "wide"
    gui._sidebar_ratio = gui._last_live_sidebar_ratio = 0.22
    gui._reply_layout_base_sidebar_ratio = 0.22
    gui._reply_layout_base_render_size = (800, 600)
    gui._reply_layout_base_main_size = (820, 620)
    gui._last_reply_input_box_ratio = None
    gui._last_reply_input_point_ratio = None
    gui._last_input_box = None
    gui._update_render_rect = lambda: None
    gui._probe_input_box = lambda: (176, 450, 800, 580)
    old_rect = (100, 100, 920, 720)
    old_signature = gui._reply_geometry_signature(old_rect, 0.260, 0.788)
    gui._last_confirmed_reply_geometry = (old_signature, (0.31, 0.81))

    point = gui.refresh_input_geometry_before_reply(
        (120, 100, 940, 720))

    assert point is not None
    assert point != (0.31, 0.81)
    assert gui._pending_reply_geometry[0] == gui._reply_geometry_signature(
        (120, 100, 940, 720), 0.260, 0.788)


def test_ocr_line_rows_recovers_short_name_before_merged_timestamp():
    rect = lambda x, y, width, height: SimpleNamespace(
        x=x, y=y, width=width, height=height)
    line = SimpleNamespace(
        text="小号18:00",
        words=[
            SimpleNamespace(text="小", bounding_rect=rect(40, 230, 10, 16)),
            SimpleNamespace(text="号", bounding_rect=rect(51, 230, 10, 16)),
            SimpleNamespace(text="18:00", bounding_rect=rect(90, 230, 38, 16)),
        ],
    )

    rows = guia_module.ScreenOCR._line_rows(line)

    assert ("小号", 40, 230, 21, 16) in rows
    assert not any(y == 0 for _text, _x, y, _w, _h in rows)


def test_top_session_scan_is_generic_and_uses_stable_row_click_point():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.sidebar_right = 240
    gui.render_h = 1000
    seen = []
    gui.ocr_zoomed = lambda region, scale: seen.append((region, scale)) or [
        ("赵薇", 28, 264, 36, 18),
    ]

    point = gui._scan_top_session("赵薇")

    assert point == (124, 273)
    assert seen == [((0, 50, 240, 450), 4)]


def test_top_session_scan_rejects_multiple_matching_rows():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.sidebar_right = 240
    gui.render_h = 1000
    gui.ocr_zoomed = lambda *_args, **_kwargs: [
        ("逗龙", 28, 230, 36, 18),
        ("逗龙", 28, 330, 36, 18),
    ]

    assert gui._scan_top_session("逗龙") is None


def test_switching_wechat_window_resets_reply_coordinate_reference(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui._input = SimpleNamespace(
        _user32=SimpleNamespace(IsWindow=lambda hwnd: hwnd == 200),
    )
    gui.main_hwnd, gui.render_hwnd = 100, 101
    gui._find_render_window = lambda hwnd: hwnd + 1
    gui.layout_profile = "wide"
    gui._sidebar_ratio = 0.24
    gui._portrait_sidebar_ratio = 1.0
    gui._current_chat = "旧会话"
    gui._direct_session_trusted_until = 10.0
    gui._last_input_box = (400, 700, 1500, 850)
    gui._last_reply_input_box_ratio = (0.2, 0.7, 0.9, 0.9)
    gui._last_reply_input_point_ratio = (0.26, 0.78)
    gui._reply_layout_size_changed = True

    def update_render_rect():
        gui.render_w, gui.render_h = 760, 560
        gui._sidebar_ratio = 0.24
        gui.layout_profile = "wide"

    gui._update_render_rect = update_render_rect

    class FakeUser32:
        @staticmethod
        def GetWindowRect(hwnd, rect_ptr):
            rect = rect_ptr._obj
            rect.left, rect.top = 20, 30
            rect.right, rect.bottom = 820, 630
            return 1

    monkeypatch.setattr(
        guia_module.ctypes, "windll", SimpleNamespace(user32=FakeUser32),
        raising=False,
    )

    assert gui.use_window(200) is True
    assert (gui._reply_layout_base_main_size == (800, 600))
    assert gui._reply_layout_base_render_size == (760, 560)
    assert gui._reply_layout_base_sidebar_ratio == pytest.approx(0.24)
    assert gui._last_reply_input_box_ratio is None
    assert gui._last_reply_input_point_ratio is None
    assert gui._last_input_box is None
    assert gui._reply_layout_size_changed is False


def test_visible_session_uses_row_center_not_ocr_text_center(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.sidebar_right = 400
    gui.get_sessions = lambda **_kwargs: [
        {"name": "小号", "x": 120, "y": 250, "w": 40, "h": 30},
    ]
    gui._input = SimpleNamespace(_user32=SimpleNamespace())
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.find_session("小号", max_scroll=0) == (208, 265)


def test_get_sessions_keeps_contact_name_when_render_crop_shifts_it_left():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.sidebar_right = 366
    gui.render_h = 1000
    gui.ocr = lambda _box: [
        ("小号", 40, 235, 18, 16),  # 截图实测：相对 x=40，旧 25% 阈值会误删。
        ("头像噪声", 20, 235, 24, 16),
        ("联系人示例", 120, 335, 48, 16),
    ]

    rows = gui.get_sessions()

    assert [row["name"] for row in rows] == ["小号", "联系人示例"]


def test_find_session_progressively_scrolls_list_before_giving_up(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    state = {"page": 0, "scrolls": []}
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 300
    gui.render_h = 800
    gui._input = SimpleNamespace(_user32=SimpleNamespace(SetCursorPos=lambda *_args: None))

    def get_sessions(zoomed=False):
        if state["page"] >= 3:
            return [{"name": "小号", "x": 120, "y": 200, "w": 40, "h": 20}]
        return [{"name": "其他会话", "x": 120, "y": 100, "w": 60, "h": 20}]

    def wheel(delta):
        state["scrolls"].append(delta)
        distance = max(1, abs(delta) // 360)
        state["page"] = max(0, state["page"] - distance) if delta > 0 else state["page"] + distance

    gui.get_sessions = get_sessions
    gui._contact_row_match_score = lambda candidate, target: 100 if candidate == target else 0
    gui._session_row_click_point = lambda row: (208, int(row["y"]) + 10)
    gui.wx_wheel = wheel
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.find_session("小号", max_scroll=3) == (208, 210)
    assert state["scrolls"][:2] == [1200, 1200]
    assert state["scrolls"][2:] == [-360, -360, -360]


def test_find_session_returns_to_top_before_searching_for_first_contact(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    state = {"page": 5, "scrolls": []}
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 300
    gui.render_h = 800
    gui._input = SimpleNamespace(_user32=SimpleNamespace(SetCursorPos=lambda *_args: None))

    def get_sessions(zoomed=False):
        name = "小号" if state["page"] == 0 else f"其他会话{state['page']}"
        return [{"name": name, "x": 120, "y": 80, "w": 40, "h": 20}]

    def wheel(delta):
        state["scrolls"].append(delta)
        if delta > 0:
            state["page"] = max(0, state["page"] - 1)
        else:
            state["page"] += 1

    gui.get_sessions = get_sessions
    gui._contact_row_match_score = lambda candidate, target: 100 if candidate == target else 0
    gui._session_row_click_point = lambda row: (208, int(row["y"]) + 10)
    gui.wx_wheel = wheel
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.find_session("小号", max_scroll=3) == (208, 90)
    assert state["page"] == 0
    assert len(state["scrolls"]) >= 7
    assert all(delta > 0 for delta in state["scrolls"])


def test_visible_session_does_not_fall_back_to_search_after_row_is_selected():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd = 123
    gui.origin_x = 0
    gui.origin_y = 0
    scans = []
    gui.ensure_visible = lambda: True
    gui._update_render_rect = lambda: None
    gui._chat_is_open = lambda _name: False
    gui._chat_open_confirmed = lambda _name: True
    gui._pane_has_content = lambda: True

    def find_session(name, max_scroll=3):
        scans.append((name, max_scroll))
        return (208, 265)

    gui.find_session = find_session
    gui.use_window = lambda _hwnd: True
    gui._row_is_active = lambda _y: True
    gui._search_chat = lambda _name: (_ for _ in ()).throw(
        AssertionError("a visible matched row must not trigger search")
    )

    assert gui.open_chat("小号") is True
    assert scans == [("小号", 3)]
    assert gui._current_chat == "小号"


def test_message_automation_never_initializes_uia(monkeypatch):
    gui_class = bridge_module.WeChatGUI
    gui = gui_class.__new__(gui_class)

    original_import = __import__

    def reject_uia_import(name, *args, **kwargs):
        if name == "wechatauto.uia_driver":
            raise AssertionError("OCR-only message automation must not import UIA")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", reject_uia_import)
    assert gui._get_uia(refresh=True) is None
    for method in (gui_class.open_chat, gui_class._search_chat, gui_class.send_msg):
        assert "_get_uia" not in inspect.getsource(method)


def test_search_mistarget_guard_uses_ocr_and_clears_exact_draft():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_h = 1000
    gui.render_w = 1600
    gui.right_pane_left = 300
    seen = []
    keys = []
    gui.ocr_zoomed = lambda box, scale: seen.append((box, scale)) or [
        ("联系人A", 500, 800, 80, 24)
    ]
    gui._input = SimpleNamespace(key=lambda *args, **kwargs: keys.append((args, kwargs)))

    assert gui._typed_into_chat_input("联系人A") is True
    assert seen == [((312, 720, 1588, 940), 2)]
    assert len(keys) == 2


def test_search_mistarget_guard_does_not_clear_unconfirmed_draft():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_h = 1000
    gui.render_w = 1600
    gui.right_pane_left = 300
    keys = []
    gui.ocr_zoomed = lambda *_args, **_kwargs: [("联系人A你好", 500, 800, 120, 24)]
    gui._input = SimpleNamespace(key=lambda *args, **kwargs: keys.append((args, kwargs)))

    assert gui._typed_into_chat_input("联系人A") is False
    assert keys == []


def test_nonempty_old_chat_pane_does_not_confirm_target(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui._chat_is_open = lambda _name: False
    gui._pane_has_content = lambda: True
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._chat_open_confirmed("逗龙") is False


def test_open_chat_scrolls_chat_list_before_search_fallback():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    events = []
    gui._get_uia = lambda: None
    gui.ensure_visible = lambda: True
    gui._update_render_rect = lambda: None
    gui._chat_is_open = lambda _name: False

    def find_session(name, max_scroll=3):
        events.append(("scan", name, max_scroll))
        return None

    def search_chat(name):
        events.append(("search", name))
        return False

    gui.find_session = find_session
    gui._search_chat = search_chat

    assert gui.open_chat("逗龙") is False
    assert events == [("scan", "逗龙", 3), ("search", "逗龙")]


def test_find_session_scrolls_to_top_before_matching_contact_rows(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    state = {"view": "scrolled"}
    events = []
    gui._input = SimpleNamespace(
        _user32=SimpleNamespace(SetCursorPos=lambda *_args: events.append("cursor"))
    )
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 200
    gui.render_h = 400

    def get_sessions(zoomed=False):
        events.append(("view", state["view"], zoomed))
        name = "小号" if state["view"] == "top" else "其他会话"
        return [{"name": name, "x": 90, "y": 80, "w": 40, "h": 20}]

    def wheel(delta):
        events.append(("wheel", delta))
        if delta > 0:
            state["view"] = "top"

    gui.get_sessions = get_sessions
    gui.wx_wheel = wheel
    gui._contact_row_match_score = lambda row, target: (
        events.append(("match", row)) or (100 if row == target else 0)
    )
    gui._session_row_click_point = lambda _row: (110, 90)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.find_session("小号", max_scroll=3) == (110, 90)
    first_match = next(i for i, event in enumerate(events)
                       if isinstance(event, tuple) and event[0] == "match")
    first_up = next(i for i, event in enumerate(events)
                    if isinstance(event, tuple) and event == ("wheel", 1200))
    assert first_up < first_match
    assert not any(event == ("wheel", -360) for event in events)


def test_find_session_avoids_duplicate_top_scan_and_bounds_zoom_vote_retries(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    calls = []
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 200
    gui.render_h = 400
    gui._input = SimpleNamespace(
        _user32=SimpleNamespace(SetCursorPos=lambda *_args: None)
    )

    def get_sessions(zoomed=False):
        calls.append(zoomed)
        return [{"name": "其他会话", "x": 90, "y": 80, "w": 40, "h": 20}]

    gui.get_sessions = get_sessions
    gui.wx_wheel = lambda _delta: None
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.find_session("小号", max_scroll=1) is None
    # 初始视口 + 两次归顶确认 + 一次普通 OCR；放大 OCR 最多补到四轮。
    assert calls[:8] == [False, False, False, False, True, True, True, True]


def test_find_session_recovers_short_name_after_intermittent_zoom_ocr_miss(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    state = {"zoomed_calls": 0}
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 300
    gui.render_h = 800
    gui._input = SimpleNamespace(
        _user32=SimpleNamespace(SetCursorPos=lambda *_args: None)
    )

    def get_sessions(zoomed=False):
        if not zoomed:
            return [{"name": "其他会话", "x": 120, "y": 100, "w": 60, "h": 20}]
        state["zoomed_calls"] += 1
        if state["zoomed_calls"] in (1, 4):
            return [{"name": "小号", "x": 147, "y": 444, "w": 18, "h": 16}]
        return []

    gui.get_sessions = get_sessions
    gui.wx_wheel = lambda _delta: None
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui.find_session("小号", max_scroll=1) == (156, 452)
    assert state["zoomed_calls"] == 4


def test_search_chat_only_uses_search_box_without_scanning_chat_list():
    gui = guia_module.WeChatGUI.__new__(guia_module.WeChatGUI)
    calls = []
    gui.main_hwnd = 1
    gui._current_chat = None
    gui.ensure_visible = lambda: calls.append("visible") or True
    gui._update_render_rect = lambda: calls.append("refresh")
    gui._search_chat = lambda name, *, use_ctrl_f=False: calls.append(
        ("search_box", name, use_ctrl_f)
    ) or True
    gui._find_chat_window = lambda _name: 0
    gui._chat_open_confirmed = lambda name: calls.append(("confirm", name)) or True
    gui.find_session = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("search-only test must not scan the chat list")
    )

    assert gui.search_chat_only("小号") is True
    assert calls == [
        "visible", "refresh", ("search_box", "小号", True), ("confirm", "小号")
    ]
    assert gui._current_chat == "小号"


def test_search_only_uses_ctrl_f_and_enter_without_result_coordinates(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    key_calls = []
    clicks = []
    refreshes = []
    gui.render_hwnd = 10
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 400
    gui.render_h = 1000
    gui._input = SimpleNamespace(
        key=lambda vk, ctrl=False, shift=False: key_calls.append((vk, ctrl, shift))
    )
    gui._update_render_rect = lambda: refreshes.append(True)
    gui._search_field_click_point = lambda _name: (_ for _ in ()).throw(
        AssertionError("Ctrl+F route must not depend on the OCR search-box anchor")
    )
    gui.set_clipboard = lambda text: key_calls.append(("clipboard", text))
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: (_ for _ in ()).throw(
        AssertionError("Ctrl+F Enter route must not be gated by query OCR")
    )
    gui.ocr_zoomed = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Enter route must not OCR or click result-row coordinates")
    )
    gui.wx_click = lambda *args: clicks.append(args)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._search_chat("小号", use_ctrl_f=True) is True
    assert refreshes == [True]
    assert key_calls == [
        (guia_module.VK_F, True, False),
        (guia_module.VK_A, True, False),
        ("clipboard", "小号"),
        (guia_module.VK_V, True, False),
        (guia_module.VK_RETURN, False, False),
    ]
    assert clicks == []


def test_ctrl_f_search_enters_first_result_when_search_query_ocr_is_unavailable(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    clicks = []
    key_calls = []
    gui.render_hwnd = 0
    gui.origin_x = 0
    gui.origin_y = 0
    gui.sidebar_right = 400
    gui.render_h = 1000
    gui._input = SimpleNamespace(
        key=lambda vk, ctrl=False, shift=False: key_calls.append((vk, ctrl, shift))
    )
    gui.set_clipboard = lambda _text: None
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: (_ for _ in ()).throw(
        AssertionError("Ctrl+F Enter route must not require query OCR")
    )
    gui.wx_click = lambda *args: clicks.append(args)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._search_chat("小号", use_ctrl_f=True) is True
    assert clicks == []
    assert key_calls.count((guia_module.VK_RETURN, False, False)) == 1


def test_ctrl_f_search_does_not_enter_if_query_was_pasted_into_chat_draft(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    key_calls = []
    gui.render_hwnd = 0
    gui._input = SimpleNamespace(
        key=lambda vk, ctrl=False, shift=False: key_calls.append((vk, ctrl, shift))
    )
    gui.set_clipboard = lambda _text: None
    gui._typed_into_chat_input = lambda _name: True
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._search_chat("小号", use_ctrl_f=True) is False
    assert (guia_module.VK_RETURN, False, False) not in key_calls


def test_search_only_rejects_enter_result_when_chat_title_is_not_exact():
    gui = guia_module.WeChatGUI.__new__(guia_module.WeChatGUI)
    gui.main_hwnd = 1
    gui._current_chat = None
    gui.ensure_visible = lambda: True
    gui._update_render_rect = lambda: None
    gui._search_chat = lambda _name, *, use_ctrl_f=False: use_ctrl_f
    gui._find_chat_window = lambda _name: 0
    gui._chat_open_confirmed = lambda _name: False

    assert gui.search_chat_only("小号") is False
    assert gui._current_chat is None


def test_search_refuses_to_click_results_when_search_field_did_not_receive_query(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    clicks = []
    gui.search_box = (20, 20, 100, 40)
    gui._rel_to_screen = lambda _box: (10, 10, 110, 30)
    gui.wx_click = lambda *args: clicks.append(args)
    gui._input = SimpleNamespace(key=lambda *_args, **_kwargs: None)
    gui.set_clipboard = lambda _text: None
    gui._search_field_click_point = lambda _name: (60, 20)
    gui.origin_x = 0
    gui.origin_y = 0
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: False
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._search_chat("小号") is False
    assert clicks == [(60, 20)]  # 只点搜索框；没有点任何搜索结果。


def test_search_retries_unconfirmed_field_anchor_before_clicking(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    anchors = []
    refreshes = []
    clicks = []
    gui.origin_x = 0
    gui.origin_y = 0
    gui.render_hwnd = 1
    gui._update_render_rect = lambda: refreshes.append(True)

    def locate(_name):
        anchors.append(True)
        return (60, 20) if len(anchors) == 3 else None

    gui._search_field_click_point = locate
    gui.wx_click = lambda *args: clicks.append(args)
    gui._input = SimpleNamespace(key=lambda *_args, **_kwargs: None)
    gui.set_clipboard = lambda _text: None
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: False
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._search_chat("小号") is False
    assert len(anchors) == 3
    assert len(refreshes) == 3
    assert clicks == [(60, 20)]  # 只有 OCR 锚定成功后才点击。


def test_search_refuses_to_click_when_ocr_sees_mini_program_not_search_field():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    clicks = []
    gui.search_box = (70, 40, 340, 80)
    gui.sidebar_right = 400
    gui.render_h = 1000
    gui.origin_x = 0
    gui.origin_y = 0
    gui.ocr_zoomed = lambda *_args, **_kwargs: [("小程序", 110, 48, 60, 20)]
    gui.wx_click = lambda *args: clicks.append(args)
    gui._input = SimpleNamespace(key=lambda *_args, **_kwargs: None)
    gui.set_clipboard = lambda _text: None

    assert gui._search_chat("小号") is False
    assert clicks == []


def test_search_field_anchor_accepts_only_placeholder_or_current_query():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.search_box = (70, 40, 340, 80)
    gui.ocr_zoomed = lambda *_args, **_kwargs: [("搜索", 100, 50, 40, 20)]

    assert gui._search_field_click_point("小号") == (120, 60)

    gui.ocr_zoomed = lambda *_args, **_kwargs: [("0小号", 100, 50, 40, 20)]
    assert gui._search_field_click_point("小号") == (120, 60)

    gui.ocr_zoomed = lambda *_args, **_kwargs: [("搜索网络结果", 100, 50, 80, 20)]
    assert gui._search_field_click_point("小号") is None


def test_search_field_anchor_recovers_small_layout_drift_from_sidebar_header():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.search_box = (70, 40, 340, 80)
    gui.sidebar_right = 400
    gui.render_h = 1000
    header = gui._search_header_band()
    gui.ocr_zoomed = lambda box, **_kwargs: (
        [("搜索", 100, 50, 40, 20)] if box == header else []
    )

    assert gui._search_field_click_point("小号") == (120, 60)


def test_sidebar_ratio_refresh_reads_only_the_top_search_anchor():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_w, gui.render_h = 1600, 900
    boxes = []

    def ocr(box):
        boxes.append(box)
        return [("搜索", 76, 50, 20, 20)]

    gui.ocr = ocr

    assert gui._detect_sidebar_ratio() == pytest.approx(86 / 0.28 / 1600)
    assert boxes == [(0, 0, 960, 270)]


def test_reply_preflight_updates_sidebar_ratio_and_invalidates_cached_input():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_w, gui.render_h = 1600, 900
    gui.layout_profile = "wide"
    gui._sidebar_ratio = 0.22
    gui._send_button_ratio = guia_module.SEND_BUTTON_RATIO
    gui._last_input_box = (352, 700, 1600, 850)
    gui._last_reply_render_size = (1600, 900)
    gui._last_live_sidebar_ratio = 0.22
    gui._update_render_rect = lambda: None
    gui._detect_sidebar_ratio = lambda: 0.26
    gui._update_layout = lambda: None

    assert gui.refresh_sidebar_layout_before_reply() is True
    assert gui._sidebar_ratio == 0.26
    assert gui._last_input_box is None
    assert gui._last_live_sidebar_ratio == 0.26


def test_reply_preflight_cancels_after_resize_when_sidebar_anchor_is_missing():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_w, gui.render_h = 1600, 1000
    gui.layout_profile = "wide"
    gui._sidebar_ratio = 0.22
    gui._last_input_box = (352, 700, 1600, 850)
    gui._last_reply_render_size = (1600, 900)
    gui._last_live_sidebar_ratio = 0.22
    gui._update_render_rect = lambda: None
    gui._detect_sidebar_ratio = lambda: None

    assert gui.refresh_sidebar_layout_before_reply() is False


def test_reply_input_point_tracks_live_sidebar_and_window_bottom():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_w, gui.render_h = 1600, 1000
    gui.origin_x, gui.origin_y = 100, 100
    gui.sidebar_right = 416
    gui.right_pane_left = 416
    gui.layout_profile = "wide"
    gui._sidebar_ratio = 0.26
    gui._reply_layout_base_sidebar_ratio = 0.22
    gui._reply_layout_base_render_size = (1600, 1000)
    gui._reply_layout_base_main_size = (1620, 1020)
    gui._last_live_sidebar_ratio = 0.26
    gui._last_reply_input_box_ratio = None
    gui._last_reply_input_point_ratio = None
    gui._last_input_box = (352, 700, 1600, 850)
    gui._update_render_rect = lambda: None
    gui._probe_input_box = lambda: None  # 深色主题常见：用实时窗口/侧栏比例换算

    point = gui.refresh_input_geometry_before_reply(
        (90, 80, 1710, 1080), input_x_ratio=0.260, input_y_ratio=0.788)

    assert point is not None
    assert point[0] == pytest.approx((100 + 416 - 90 + 64) / 1620)
    assert point[1] == pytest.approx(1 - (0.212 * 1020 / 1000))
    assert gui._last_input_box is None


def test_saved_layout_height_drift_over_two_percent_triggers_recalibration(tmp_path, monkeypatch):
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        '{"profiles":{"wide":{"profile":"wide","sidebar_ratio":0.22,'
        '"send_button_ratio":[0.78,0.92,0.995,0.99],"render_w":1600,'
        '"render_h":900}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(guia_module, "_layout_path", lambda: str(layout_path))
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.render_w, gui.render_h = 1600, 1000
    gui._update_layout = lambda: None

    assert gui._load_layout() is False


def test_bridge_refreshes_layout_before_contact_lookup_and_input_before_paste():
    send_once = inspect.getsource(WeChatBridge._send_once)
    sidebar_check = send_once.index("refresh_sidebar_layout_before_reply")
    contact_lookup = send_once.index("wx.open_chat(ui_name)")
    seed_geometry = send_once.index("seed_reply_input_geometry")
    input_check = send_once.index("refresh_input_geometry_before_reply")
    paste = send_once.index("self._paste_text_win32(wx, text)")
    confirm_geometry = send_once.index("confirm_reply_input_geometry")
    verify_sent = send_once.index("if self._verify_sent_db")

    assert sidebar_check < contact_lookup < input_check < paste
    assert input_check < confirm_geometry
    assert contact_lookup < seed_geometry < input_check
    assert verify_sent < confirm_geometry
    assert send_once.index("if not wx._chat_is_open(ui_name):", input_check) < paste
    assert send_once.index("if not wx._chat_is_open(ui_name):", paste) < send_once.index(
        "self._submit_once(wx", paste
    )
    assert "_reply_input_point_ratio" in inspect.getsource(
        WeChatBridge._paste_text_win32)


@pytest.mark.parametrize(
    ("chat_states", "expected_pastes"),
    [([True, False], 0), ([True, True, False], 1)],
)
def test_bridge_aborts_if_contact_is_not_confirmed_before_submit(
    chat_states, expected_pastes
):
    states = iter(chat_states)
    wx = SimpleNamespace(
        main_hwnd=123,
        ensure_visible=lambda: True,
        refresh_sidebar_layout_before_reply=lambda: True,
        open_chat=lambda _name: True,
        _chat_is_open=lambda _name: next(states),
        refresh_input_geometry_before_reply=lambda *_args, **_kwargs: (0.26, 0.78),
    )
    bridge = WeChatBridge.__new__(WeChatBridge)
    bridge.config = {"win32_input_x_ratio": 0.26, "win32_input_y_ratio": 0.78}
    bridge.db = object()
    bridge.targets = {"target": "stable-target-id"}
    bridge.ui_names = {"target": "李昊阳"}
    bridge._reusable_wechat_gui = lambda: wx
    bridge._send_mark = lambda _username: set()
    bridge._window_rect = lambda _hwnd: (0, 0, 1000, 800)
    bridge._clear_cached_wechat = lambda: None
    pastes = []
    submits = []
    bridge._paste_text_win32 = lambda *_args: pastes.append("paste")
    bridge._submit_once = lambda *_args: submits.append("submit")

    with pytest.raises(bridge_module.SendCancelled, match="会话"):
        bridge._send_once("target", "禁止发错会话")

    assert len(pastes) == expected_pastes
    assert submits == []


def test_search_query_verification_uses_verified_header_fallback():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.search_box = (70, 40, 340, 80)
    gui.sidebar_right = 400
    gui.render_h = 1000
    header = gui._search_header_band()
    gui.ocr_zoomed = lambda box, **_kwargs: (
        [("小号", 100, 50, 40, 20)] if box == header else []
    )

    assert gui._search_query_visible("小号") is True


def test_search_never_clicks_a_small_program_result_for_contact_name():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    clicks = []
    gui.search_box = (70, 40, 340, 80)
    gui._search_field_click_point = lambda _name: (100, 55)
    gui._rel_to_screen = lambda _box: (10, 10, 110, 30)
    gui.wx_click = lambda *args: clicks.append(args)
    gui._input = SimpleNamespace(key=lambda *_args, **_kwargs: None)
    gui.set_clipboard = lambda _text: None
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: True
    gui.render_h = 1000
    gui.sidebar_right = 400
    gui.origin_x = 0
    gui.origin_y = 0
    gui.ocr_zoomed = lambda *_args, **_kwargs: [
        ("小程序", 110, 120, 60, 24),
        ("小号", 120, 160, 50, 24),
    ]

    assert gui._search_chat("小号") is False
    assert all(y == 55 for _x, y in clicks)


def test_search_can_select_contact_before_small_program_section():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    clicks = []
    gui.search_box = (70, 40, 340, 80)
    gui._search_field_click_point = lambda _name: (100, 55)
    gui.wx_click = lambda *args: clicks.append(args)
    gui._input = SimpleNamespace(key=lambda *_args, **_kwargs: None)
    gui.set_clipboard = lambda _text: None
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: True
    gui.render_h = 1000
    gui.sidebar_right = 400
    gui.origin_x = 0
    gui.origin_y = 0
    gui.ocr_zoomed = lambda *_args, **_kwargs: [
        ("联系人", 110, 100, 60, 24),
        ("小号", 120, 140, 50, 24),
        ("小程序", 110, 200, 60, 24),
        ("小号", 120, 240, 50, 24),
    ]

    assert gui._search_chat("小号") is True
    assert clicks == [(100, 55), (208, 152)]


def test_search_ignores_every_result_below_network_search_section(monkeypatch):
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    clicks = []
    gui.search_box = (20, 20, 100, 40)
    gui._rel_to_screen = lambda _box: (10, 10, 110, 30)
    gui.wx_click = lambda *args: clicks.append(args)
    gui._input = SimpleNamespace(key=lambda *_args, **_kwargs: None)
    gui.set_clipboard = lambda _text: None
    gui._search_field_click_point = lambda _name: (40, 30)
    gui._typed_into_chat_input = lambda _name: False
    gui._search_query_visible = lambda _name: True
    gui.render_h = 1000
    gui.sidebar_right = 400
    gui.origin_x = 0
    gui.origin_y = 0
    gui.ocr_zoomed = lambda *_args, **_kwargs: [
        ("搜索网络结果", 30, 120, 140, 24),
        ("小号", 30, 160, 60, 24),
    ]
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    assert gui._search_chat("小号") is False
    assert clicks == [(40, 30)]


def test_active_visible_row_without_verified_title_is_not_trusted():
    gui = bridge_module.WeChatGUI.__new__(bridge_module.WeChatGUI)
    gui.main_hwnd = 123
    gui.origin_x = 0
    gui.origin_y = 0
    clicked = []
    gui._get_uia = lambda: None
    gui.ensure_visible = lambda: True
    gui._update_render_rect = lambda: None
    gui._chat_is_open = lambda _name: False
    gui._chat_open_confirmed = lambda _name: False
    gui.find_session = lambda _name, max_scroll=3: (10, 20)
    gui.use_window = lambda _hwnd: None
    gui._row_is_active = lambda _y: True
    gui._pane_has_content = lambda: True
    gui.wx_click = lambda *_args: clicked.append(_args)
    gui._search_chat = lambda _name: False

    assert gui.open_chat("逗龙") is False
    assert clicked == []
