# -*- coding: utf-8 -*-
"""page.debugger 在真实 Firefox 上的断点调试回归。

这些测试需要内核带 DevTools（--start-debugger-server），因此标记为 browser。
"""

import threading

import pytest

from ruyipage import FirefoxOptions, FirefoxPage
from ruyipage.errors import DebuggerError


# debugger_target.js 中 `const total = subtotal * (1 + taxRate);` 所在行
TOTAL_LINE = 6
SOURCE_NAME = "debugger_target.js"


def _find_free_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def debug_page(test_browser_path, fixture_page_url):
    opts = FirefoxOptions()
    opts.headless(True)
    if test_browser_path:
        opts.set_browser_path(test_browser_path)
    opts.enable_debugger(port=_find_free_port())

    page = FirefoxPage(opts)
    try:
        page.get(fixture_page_url("debugger_target.html"))
        try:
            page.debugger.start()
        except DebuggerError as exc:
            pytest.skip("devtools server unavailable in this build: {}".format(exc))
        yield page
    finally:
        try:
            page.debugger.stop()
        except Exception:
            pass
        try:
            page.quit()
        except Exception:
            pass


def _trigger_in_background(page, script="return window.runComputation(25, 4);"):
    """在后台线程触发 JS。

    暂停期间 BiDi 调用会一直阻塞，因此不能在主线程触发。
    """
    outcome = {}

    def run():
        try:
            outcome["value"] = page.run_js(script, timeout=120)
        except Exception as exc:
            outcome["error"] = "{}: {}".format(type(exc).__name__, exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return outcome, worker


@pytest.mark.feature
@pytest.mark.browser
def test_sources_include_external_script(debug_page):
    sources = debug_page.debugger.sources(url_contains=SOURCE_NAME)

    assert len(sources) == 1
    assert sources[0].url.endswith(SOURCE_NAME)
    assert sources[0].actor


@pytest.mark.feature
@pytest.mark.browser
def test_breakable_lines_report_real_positions(debug_page):
    lines = debug_page.debugger.breakable_lines(SOURCE_NAME)

    assert TOTAL_LINE in lines
    # 函数声明行本身不是可执行语句
    assert 2 not in lines


@pytest.mark.feature
@pytest.mark.browser
def test_breakpoint_pauses_js_and_exposes_stack_and_scope(debug_page):
    debugger = debug_page.debugger
    breakpoint_ = debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE)
    assert breakpoint_.column is not None

    outcome, worker = _trigger_in_background(debug_page)

    state = debugger.wait_paused(timeout=30)
    assert state is not None, "断点未命中"
    assert state.why == "breakpoint"
    assert state.line == TOTAL_LINE
    assert debugger.paused is True

    # JS 必须仍处于挂起状态
    worker.join(timeout=1.0)
    assert "value" not in outcome

    frames = debugger.frames()
    assert [f.display_name for f in frames][:2] == [
        "computeTotal",
        "window.runComputation",
    ]
    # 服务端只给 source actor，URL 必须被换算出来
    assert frames[0].source_actor
    assert frames[0].url.endswith(SOURCE_NAME)
    assert state.url.endswith(SOURCE_NAME)
    assert frames[0].arguments == [25, 4]

    scope = debugger.scope()
    assert scope["subtotal"] == 100
    assert scope["taxRate"] == 0.08
    assert scope["marker"] == "scope-probe"
    # total 尚未赋值，断点停在该语句之前
    assert scope.get("total") is None

    debugger.resume()
    worker.join(timeout=30)
    assert outcome.get("value") == 108
    assert debugger.paused is False


@pytest.mark.feature
@pytest.mark.browser
def test_step_over_advances_one_statement(debug_page):
    debugger = debug_page.debugger
    debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE)

    outcome, worker = _trigger_in_background(debug_page)
    assert debugger.wait_paused(timeout=30) is not None

    debugger.step_over()
    stepped = debugger.wait_paused(timeout=20)

    assert stepped is not None
    assert stepped.why == "resumeLimit"
    assert stepped.line == TOTAL_LINE + 1
    # 单步后 total 已完成赋值
    assert debugger.scope()["total"] == 108

    debugger.resume()
    worker.join(timeout=30)
    assert outcome.get("value") == 108


@pytest.mark.feature
@pytest.mark.browser
def test_step_out_returns_to_caller(debug_page):
    debugger = debug_page.debugger
    debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE)

    outcome, worker = _trigger_in_background(debug_page)
    assert debugger.wait_paused(timeout=30) is not None

    debugger.step_out()
    stepped = debugger.wait_paused(timeout=20)

    assert stepped is not None
    assert stepped.frame.display_name == "window.runComputation"

    debugger.resume()
    worker.join(timeout=30)
    assert outcome.get("value") == 108


@pytest.mark.feature
@pytest.mark.browser
def test_false_condition_does_not_pause(debug_page):
    debugger = debug_page.debugger
    debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE, condition="quantity > 999")

    # 条件不成立，JS 应正常跑完，不需要后台线程
    assert debug_page.run_js("return window.runComputation(25, 4);") == 108
    assert debugger.paused is False


@pytest.mark.feature
@pytest.mark.browser
def test_true_condition_pauses_with_expected_scope(debug_page):
    debugger = debug_page.debugger
    debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE, condition="subtotal === 100")

    outcome, worker = _trigger_in_background(debug_page)

    state = debugger.wait_paused(timeout=30)
    assert state is not None, "条件成立时应当暂停"
    assert state.why == "breakpoint"
    assert state.condition_failed is False
    assert debugger.scope()["subtotal"] == 100

    debugger.resume()
    worker.join(timeout=30)
    assert outcome.get("value") == 108


@pytest.mark.feature
@pytest.mark.browser
def test_broken_condition_pauses_and_reports_the_error(debug_page):
    """条件表达式出错时 Firefox 选择暂停，避免调试者误判断点没命中。"""
    debugger = debug_page.debugger
    debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE, condition="undefinedVar.x")

    outcome, worker = _trigger_in_background(debug_page)

    state = debugger.wait_paused(timeout=30)
    assert state is not None
    assert state.condition_failed is True
    assert state.message

    debugger.resume()
    worker.join(timeout=30)
    assert outcome.get("value") == 108


@pytest.mark.feature
@pytest.mark.browser
def test_removed_breakpoint_no_longer_pauses(debug_page):
    debugger = debug_page.debugger
    breakpoint_ = debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE)
    debugger.remove_breakpoint(breakpoint_)

    assert debugger.breakpoints == []
    assert debug_page.run_js("return window.runComputation(25, 4);") == 108
    assert debugger.paused is False


@pytest.mark.feature
@pytest.mark.browser
def test_stop_resumes_a_paused_page(debug_page):
    """stop() 必须先恢复执行，否则页面会永久卡住。"""
    debugger = debug_page.debugger
    debugger.set_breakpoint(SOURCE_NAME, TOTAL_LINE)

    outcome, worker = _trigger_in_background(debug_page)
    assert debugger.wait_paused(timeout=30) is not None

    debugger.stop()

    worker.join(timeout=30)
    assert outcome.get("value") == 108
    assert debugger.started is False


@pytest.mark.feature
@pytest.mark.browser
def test_start_without_enable_debugger_raises_actionable_error(
    test_browser_path, fixture_page_url
):
    opts = FirefoxOptions()
    opts.headless(True)
    if test_browser_path:
        opts.set_browser_path(test_browser_path)

    page = FirefoxPage(opts)
    try:
        page.get(fixture_page_url("debugger_target.html"))
        with pytest.raises(DebuggerError, match="enable_debugger"):
            page.debugger.start(port=_find_free_port())
    finally:
        page.quit()
