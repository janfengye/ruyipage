# -*- coding: utf-8 -*-
"""Debugger 单元：断点列自动解析、暂停状态机、作用域解析。"""

import pytest

from ruyipage._configs.firefox_options import FirefoxOptions
from ruyipage._units.debugger import Debugger, Frame, PausedState
from ruyipage.errors import DebuggerError


class FakeRdp(object):
    """记录请求并按 (actor, type) 返回预设应答。"""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}
        self.connected = True
        self.events = {}

    def request(self, actor, type_, timeout=None, **params):
        self.calls.append((actor, type_, params))
        key = (actor, type_)
        if key in self.replies:
            return self.replies[key]
        if type_ in self.replies:
            return self.replies[type_]
        return {"from": actor}

    def on_event(self, event_type, callback, actor=None):
        self.events[(actor, event_type)] = callback

    def close(self):
        self.connected = False


def _debugger(replies=None, thread_actor="thread1"):
    debugger = Debugger(owner=None)
    debugger._rdp = FakeRdp(replies)
    debugger._thread_actor = thread_actor
    # 真实服务端的 where 只带 source actor，URL 靠这张表换算
    debugger._source_urls = {"src1": "https://x.test/app.js"}
    return debugger


PAUSED_PACKET = {
    "from": "thread1",
    "type": "paused",
    "actor": "pause9",
    "why": {"type": "breakpoint", "actors": ["bp1"]},
    "frame": {
        "actor": "frame7",
        "type": "call",
        "displayName": "computeTotal",
        # where 里是 source actor，不是 url —— 这是真实的包结构
        "where": {"actor": "src1", "line": 6, "column": 16},
        "arguments": [25, 4],
        "oldest": False,
    },
}


# ── options 接线 ──


def test_enable_debugger_sets_prefs_and_bare_flag():
    opts = FirefoxOptions().enable_debugger(port=6001)

    assert opts.debugger_port == 6001
    prefs = opts.preferences
    assert prefs["devtools.debugger.remote-enabled"] is True
    assert prefs["devtools.chrome.enabled"] is True
    assert prefs["devtools.debugger.prompt-connection"] is False
    assert prefs["devtools.debugger.remote-port"] == 6001
    # 必须是裸标志：带值形式要求独立 argv token，本类拼的是 --flag=value
    assert "--start-debugger-server" in opts.arguments
    assert not any(a.startswith("--start-debugger-server=") for a in opts.arguments)


def test_enable_debugger_defaults_to_port_6000():
    assert FirefoxOptions().enable_debugger().debugger_port == 6000


def test_disable_debugger_removes_prefs_and_flag():
    opts = FirefoxOptions().enable_debugger(port=6002)
    opts.enable_debugger(False)

    assert opts.debugger_port is None
    assert "--start-debugger-server" not in opts.arguments
    assert "devtools.debugger.remote-enabled" not in opts.preferences


def test_debugger_port_is_none_until_enabled():
    assert FirefoxOptions().debugger_port is None


# ── 断点列自动解析 ──


def test_set_breakpoint_resolves_column_from_breakable_positions():
    """列号必须取自服务端的有效断点位置，否则断点会被静默忽略。"""
    debugger = _debugger(
        {
            ("thread1", "sources"): {
                "sources": [{"actor": "src1", "url": "https://x.test/app.js"}]
            },
            ("src1", "getBreakpointPositionsCompressed"): {
                "positions": {"3": [2], "6": [16, 24], "7": [2]}
            },
        }
    )

    breakpoint_ = debugger.set_breakpoint("https://x.test/app.js", 6)

    assert breakpoint_.column == 16
    location = next(
        params["location"]
        for actor, type_, params in debugger._rdp.calls
        if type_ == "setBreakpoint"
    )
    assert location == {
        "sourceUrl": "https://x.test/app.js",
        "sourceId": "src1",
        "line": 6,
        "column": 16,
    }


def test_conditional_breakpoint_sends_condition_in_options():
    debugger = _debugger(
        {
            ("thread1", "sources"): {
                "sources": [{"actor": "src1", "url": "https://x.test/app.js"}]
            },
            ("src1", "getBreakpointPositionsCompressed"): {"positions": {"6": [16]}},
        }
    )

    breakpoint_ = debugger.set_breakpoint(
        "https://x.test/app.js", 6, condition="quantity > 3"
    )

    assert breakpoint_.condition == "quantity > 3"
    options = next(
        params["options"]
        for _actor, type_, params in debugger._rdp.calls
        if type_ == "setBreakpoint"
    )
    assert options == {"condition": "quantity > 3"}


def test_unconditional_breakpoint_sends_empty_options():
    debugger = _debugger(
        {
            ("thread1", "sources"): {
                "sources": [{"actor": "src1", "url": "https://x.test/app.js"}]
            },
            ("src1", "getBreakpointPositionsCompressed"): {"positions": {"6": [16]}},
        }
    )

    breakpoint_ = debugger.set_breakpoint("https://x.test/app.js", 6)

    assert breakpoint_.condition is None
    options = next(
        params["options"]
        for _actor, type_, params in debugger._rdp.calls
        if type_ == "setBreakpoint"
    )
    assert options == {}


def test_condition_that_throws_is_surfaced_on_paused_state():
    """条件表达式出错时 Firefox 仍会暂停，需要能区分这种暂停。"""
    debugger = _debugger()

    debugger._handle_paused(
        {
            "from": "thread1",
            "type": "paused",
            "why": {
                "type": "breakpointConditionThrown",
                "message": "ReferenceError: nope is not defined",
            },
            "frame": {"actor": "frame7", "where": {"actor": "src1", "line": 6}},
        }
    )
    state = debugger.wait_paused(timeout=1)

    assert state.condition_failed is True
    assert "ReferenceError" in state.message


def test_normal_breakpoint_pause_is_not_flagged_as_condition_failure():
    debugger = _debugger()

    debugger._handle_paused(PAUSED_PACKET)
    state = debugger.wait_paused(timeout=1)

    assert state.condition_failed is False
    assert state.message is None


def test_explicit_column_skips_position_lookup():
    debugger = _debugger(
        {
            ("thread1", "sources"): {
                "sources": [{"actor": "src1", "url": "https://x.test/app.js"}]
            }
        }
    )

    debugger.set_breakpoint("https://x.test/app.js", 6, column=99)

    assert not any(
        type_ == "getBreakpointPositionsCompressed"
        for _actor, type_, _params in debugger._rdp.calls
    )


def test_set_breakpoint_on_unbreakable_line_reports_available_lines():
    debugger = _debugger(
        {
            ("thread1", "sources"): {
                "sources": [{"actor": "src1", "url": "https://x.test/app.js"}]
            },
            ("src1", "getBreakpointPositionsCompressed"): {
                "positions": {"3": [2], "7": [2]}
            },
        }
    )

    with pytest.raises(DebuggerError) as excinfo:
        debugger.set_breakpoint("https://x.test/app.js", 6)

    assert "不可下断点" in str(excinfo.value)
    assert "[3, 7]" in str(excinfo.value)


def test_unknown_source_raises_with_loaded_list():
    debugger = _debugger({("thread1", "sources"): {"sources": []}})

    with pytest.raises(DebuggerError, match="未找到源"):
        debugger.set_breakpoint("https://x.test/missing.js", 1)


def test_source_can_be_matched_by_url_suffix():
    debugger = _debugger(
        {
            ("thread1", "sources"): {
                "sources": [{"actor": "src1", "url": "file:///tmp/deep/app.js"}]
            },
            ("src1", "getBreakpointPositionsCompressed"): {"positions": {"1": [0]}},
        }
    )

    breakpoint_ = debugger.set_breakpoint("app.js", 1)

    assert breakpoint_.url == "file:///tmp/deep/app.js"


# ── 暂停状态机 ──


def test_paused_event_updates_state_and_queue():
    debugger = _debugger()

    debugger._handle_paused(PAUSED_PACKET)

    assert debugger.paused is True
    state = debugger.wait_paused(timeout=1)
    assert isinstance(state, PausedState)
    assert state.why == "breakpoint"
    assert state.line == 6
    assert state.frame.display_name == "computeTotal"
    assert state.frame.arguments == [25, 4]
    assert state.raw is PAUSED_PACKET
    # source actor 必须被换算成 URL
    assert state.frame.source_actor == "src1"
    assert state.url == "https://x.test/app.js"


def test_frame_url_is_empty_when_source_table_is_cold():
    """源表未预热时不能凭空造出 URL，但行号仍可用。"""
    debugger = _debugger()
    debugger._source_urls = {}

    debugger._handle_paused(PAUSED_PACKET)
    state = debugger.wait_paused(timeout=1)

    assert state.url == ""
    assert state.line == 6


def test_wait_paused_returns_none_on_timeout():
    debugger = _debugger()

    assert debugger.wait_paused(timeout=0.05) is None


def test_resume_requires_paused_state():
    debugger = _debugger()

    with pytest.raises(DebuggerError, match="未处于暂停状态"):
        debugger.resume()


def test_step_variants_send_expected_resume_limits():
    for method, expected in (
        ("step_over", "next"),
        ("step_into", "step"),
        ("step_out", "finish"),
    ):
        debugger = _debugger()
        debugger._handle_paused(PAUSED_PACKET)

        getattr(debugger, method)()

        actor, type_, params = debugger._rdp.calls[-1]
        assert (actor, type_) == ("thread1", "resume")
        assert params["resumeLimit"] == {"type": expected}
        assert debugger.paused is False


def test_plain_resume_sends_no_limit():
    debugger = _debugger()
    debugger._handle_paused(PAUSED_PACKET)

    debugger.resume()

    _actor, _type, params = debugger._rdp.calls[-1]
    assert "resumeLimit" not in params


def test_pause_uses_immediate_interrupt():
    debugger = _debugger()

    debugger.pause()

    assert debugger._rdp.calls[-1] == ("thread1", "interrupt", {"when": "now"})


def test_resumed_event_clears_state():
    debugger = _debugger()
    debugger._handle_paused(PAUSED_PACKET)

    debugger._handle_resumed({"from": "thread1", "type": "resumed"})

    assert debugger.paused is False


def test_on_paused_callback_receives_state():
    debugger = _debugger()
    seen = []
    debugger.on_paused(seen.append)

    debugger._handle_paused(PAUSED_PACKET)

    assert len(seen) == 1
    assert seen[0].why == "breakpoint"


def test_callback_exception_does_not_break_state_tracking():
    debugger = _debugger()

    def boom(_state):
        raise RuntimeError("callback failed")

    debugger.on_paused(boom)
    debugger._handle_paused(PAUSED_PACKET)

    assert debugger.paused is True


# ── 调用栈与作用域 ──


def test_frames_requires_paused():
    debugger = _debugger()

    with pytest.raises(DebuggerError, match="暂停状态"):
        debugger.frames()


def test_frames_are_parsed_innermost_first():
    debugger = _debugger(
        {
            ("thread1", "frames"): {
                "frames": [
                    {
                        "actor": "f1",
                        "displayName": "inner",
                        "where": {"actor": "src1", "line": 3},
                    },
                    {
                        "actor": "f2",
                        "where": {"actor": "src1", "line": 9},
                        "oldest": True,
                    },
                ]
            }
        }
    )
    debugger._handle_paused(PAUSED_PACKET)

    frames = debugger.frames()

    assert [f.display_name for f in frames] == ["inner", "(anonymous)"]
    assert frames[0].line == 3
    assert frames[0].url == "https://x.test/app.js"
    assert frames[1].oldest is True


def test_scope_reads_variables_and_arguments_of_current_frame():
    debugger = _debugger(
        {
            ("frame7", "getEnvironment"): {
                "type": "function",
                "bindings": {
                    "arguments": [
                        {"price": {"value": {"type": "number", "value": 25}}},
                        {"quantity": {"value": {"type": "number", "value": 4}}},
                    ],
                    "variables": {
                        "subtotal": {"value": {"type": "number", "value": 100}},
                        "label": {"value": {"type": "string", "value": "hi"}},
                        "nothing": {"value": {"type": "undefined"}},
                    },
                },
            }
        }
    )
    debugger._handle_paused(PAUSED_PACKET)

    scope = debugger.scope()

    assert scope == {
        "price": 25,
        "quantity": 4,
        "subtotal": 100,
        "label": "hi",
        "nothing": None,
    }


def test_scope_can_walk_parent_chain_with_inner_shadowing_outer():
    debugger = _debugger(
        {
            ("frame7", "getEnvironment"): {
                "bindings": {"variables": {"x": {"value": {"value": "inner"}}}},
                "parent": {
                    "bindings": {
                        "variables": {
                            "x": {"value": {"value": "outer"}},
                            "y": {"value": {"value": "only-outer"}},
                        }
                    }
                },
            }
        }
    )
    debugger._handle_paused(PAUSED_PACKET)

    assert debugger.scope() == {"x": "inner"}
    assert debugger.scope(include_parents=True) == {"x": "inner", "y": "only-outer"}


def test_scope_keeps_object_grips_it_cannot_downgrade():
    grip = {"type": "object", "class": "Array", "actor": "obj3"}
    debugger = _debugger(
        {
            ("frame7", "getEnvironment"): {
                "bindings": {"variables": {"items": {"value": grip}}}
            }
        }
    )
    debugger._handle_paused(PAUSED_PACKET)

    assert debugger.scope()["items"] == grip


def test_scope_accepts_explicit_frame():
    debugger = _debugger(
        {
            ("frameX", "getEnvironment"): {
                "bindings": {"variables": {"v": {"value": {"value": 1}}}}
            }
        }
    )
    debugger._handle_paused(PAUSED_PACKET)

    frame = Frame({"actor": "frameX", "where": {}}, debugger._source_urls)
    assert debugger.scope(frame=frame) == {"v": 1}


def test_scope_requires_paused():
    debugger = _debugger()

    with pytest.raises(DebuggerError, match="暂停状态"):
        debugger.scope()


# ── 启动前置校验 ──


def test_api_calls_before_start_raise_clear_error():
    debugger = Debugger(owner=None)

    for call in (
        lambda: debugger.sources(),
        lambda: debugger.pause(),
        lambda: debugger.wait_paused(timeout=0.01),
    ):
        with pytest.raises(DebuggerError, match="调试通道未启动"):
            call()
