# -*- coding: utf-8 -*-
"""Debugger - JS 断点调试器

提供 WebDriver BiDi 规范之外的断点级调试能力：下断点、暂停、单步、
读调用栈与作用域变量。底层走 Firefox DevTools 的 RDP 通道
（见 :mod:`ruyipage._adapter.rdp`），与 BiDi 连接并行存在。

启用方式::

    opts = FirefoxOptions()
    opts.enable_debugger()          # 写入所需 pref 并启动 devtools server
    page = FirefoxPage(opts)

    page.debugger.start()
    page.get('https://example.com')

    page.debugger.set_breakpoint('https://example.com/app.js', 42)
    state = page.debugger.wait_paused(timeout=30)
    print(state.why, state.line)
    print(page.debugger.scope())
    page.debugger.step_over()
    page.debugger.resume()

重要限制
--------
JS 线程暂停期间，任何依赖 JS 执行的操作（``run_js``、点击、取元素文本等）
都会阻塞到超时。触发断点的调用应放在后台线程，或在暂停期间只使用
``page.debugger`` 自身的接口。
"""

import logging
import threading
from queue import Empty, Queue

from .._adapter.rdp import DEFAULT_RDP_PORT, RdpConnection
from ..errors import DebuggerError

logger = logging.getLogger("ruyipage")

_STEP_LIMITS = {
    "over": "next",
    "into": "step",
    "out": "finish",
}


class Source(object):
    """一个已加载的 JS 源。"""

    __slots__ = ("actor", "url", "introduction_type", "is_black_boxed")

    def __init__(self, form):
        self.actor = form.get("actor", "")
        self.url = form.get("url") or ""
        self.introduction_type = form.get("introductionType")
        self.is_black_boxed = bool(form.get("isBlackBoxed"))

    def __repr__(self):
        return "<Source {}>".format(self.url or self.actor)


class Breakpoint(object):
    """一个已设置的断点。"""

    __slots__ = ("url", "line", "column", "source_actor", "condition")

    def __init__(self, url, line, column, source_actor, condition=None):
        self.url = url
        self.line = line
        self.column = column
        self.source_actor = source_actor
        self.condition = condition

    def __repr__(self):
        suffix = " if {}".format(self.condition) if self.condition else ""
        return "<Breakpoint {}:{}:{}{}>".format(
            self.url, self.line, self.column, suffix
        )


class Frame(object):
    """调用栈中的一帧。"""

    __slots__ = (
        "actor",
        "display_name",
        "type",
        "source_actor",
        "url",
        "line",
        "column",
        "arguments",
        "oldest",
    )

    def __init__(self, form, source_urls=None):
        self.actor = form.get("actor", "")
        self.display_name = form.get("displayName") or "(anonymous)"
        self.type = form.get("type", "")
        self.arguments = form.get("arguments") or []
        self.oldest = bool(form.get("oldest"))

        where = form.get("where") or {}
        self.line = where.get("line")
        self.column = where.get("column")
        # where 只带 source actor，URL 需要用 sources() 的结果换算。
        self.source_actor = where.get("actor") or ""
        self.url = (source_urls or {}).get(self.source_actor) or where.get("url") or ""

    def __repr__(self):
        return "<Frame {} @{}:{}>".format(
            self.display_name, self.url or self.source_actor, self.line
        )


class PausedState(object):
    """一次暂停的快照。"""

    __slots__ = ("why", "message", "frame", "pause_actor", "_raw")

    def __init__(self, packet, source_urls=None):
        self._raw = packet
        why = packet.get("why") or {}
        self.why = why.get("type", "")
        self.message = why.get("message")
        frame_form = packet.get("frame") or {}
        self.frame = Frame(frame_form, source_urls) if frame_form else None
        self.pause_actor = packet.get("actor")

    @property
    def line(self):
        return self.frame.line if self.frame else None

    @property
    def url(self):
        return self.frame.url if self.frame else ""

    @property
    def condition_failed(self):
        """条件表达式求值抛异常导致的暂停。

        Firefox 在条件出错时选择暂停而不是静默跳过，避免调试者误以为断点没命中。
        具体错误在 :attr:`message`。
        """
        return self.why == "breakpointConditionThrown"

    @property
    def raw(self):
        """原始 paused 包，便于访问本类未建模的字段。"""
        return self._raw

    def __repr__(self):
        return "<PausedState {} @{}:{}>".format(self.why, self.url, self.line)


class Debugger(object):
    """JS 断点调试器（``page.debugger``）。"""

    def __init__(self, owner):
        self._owner = owner
        self._rdp = None
        self._thread_actor = None
        self._target_actor = None
        self._breakpoints = []
        self._paused = None
        self._paused_queue = Queue()
        self._state_lock = threading.Lock()
        self._on_paused_cb = None
        self._source_cache = {}  # {url: Source}
        self._source_urls = {}  # {source actor: url}

    # ── 状态 ──

    @property
    def started(self):
        """调试通道是否已连接。"""
        return bool(self._rdp and self._rdp.connected)

    @property
    def paused(self):
        """当前是否处于暂停状态。"""
        with self._state_lock:
            return self._paused is not None

    @property
    def paused_state(self):
        """当前暂停快照；未暂停时为 None。"""
        with self._state_lock:
            return self._paused

    @property
    def breakpoints(self):
        return list(self._breakpoints)

    @property
    def thread_actor(self):
        """底层 thread actor id，便于发送本类未封装的 RDP 请求。"""
        return self._thread_actor

    @property
    def connection(self):
        """底层 :class:`RdpConnection`，便于扩展。"""
        return self._rdp

    # ── 生命周期 ──

    def start(self, port=None, host="127.0.0.1", timeout=20):
        """连接 devtools server 并 attach 到当前标签页的 thread actor。

        Args:
            port: RDP 端口。默认取 ``opts.enable_debugger()`` 配置的端口。
            host: RDP 主机。
            timeout: 单次 RDP 请求超时（秒）。

        Raises:
            DebuggerError: 无法连接（通常是没有调用 ``opts.enable_debugger()``）
        """
        if self.started:
            return self

        port = self._resolve_port(port)
        connection = RdpConnection(host=host, port=port, timeout=timeout)
        try:
            connection.connect()
        except OSError as exc:
            raise DebuggerError(
                "无法连接 devtools server {}:{}（{}）。"
                "请在创建页面前调用 opts.enable_debugger()，"
                "它会写入所需 pref 并以 --start-debugger-server 启动 Firefox。".format(
                    host, port, exc
                )
            )

        self._rdp = connection
        try:
            self._attach_current_tab()
        except Exception:
            connection.close()
            self._rdp = None
            raise

        connection.on_event("paused", self._handle_paused, actor=self._thread_actor)
        connection.on_event("resumed", self._handle_resumed, actor=self._thread_actor)

        # 预热源表：paused 事件在 RDP 读线程上处理，不能在那里再发请求，
        # 因此帧的 URL 只能从缓存换算。
        try:
            self.sources()
        except Exception as exc:
            logger.debug("预加载 JS 源列表失败: %s", exc)
        return self

    def stop(self):
        """断开调试通道。若当前处于暂停状态，先恢复执行避免页面卡死。"""
        if not self._rdp:
            return self

        if self.paused:
            try:
                self.resume()
            except Exception:
                pass

        self._rdp.close()
        self._rdp = None
        self._thread_actor = None
        self._target_actor = None
        self._breakpoints = []
        self._source_cache = {}
        self._source_urls = {}
        with self._state_lock:
            self._paused = None
        return self

    def _resolve_port(self, port):
        if port is not None:
            return int(port)
        options = getattr(getattr(self._owner, "_browser", None), "options", None)
        configured = getattr(options, "debugger_port", None)
        return int(configured or DEFAULT_RDP_PORT)

    def _attach_current_tab(self):
        tabs = self._rdp.request("root", "listTabs").get("tabs") or []
        if not tabs:
            raise DebuggerError("devtools server 未报告任何标签页")

        # BiDi context id 与 RDP actor 无法直接互换，用 URL 对齐当前页面，
        # 匹配不到时回退到 selected 标签页。
        current_url = ""
        try:
            current_url = self._owner.url or ""
        except Exception:
            pass

        tab = None
        if current_url:
            tab = next((t for t in tabs if (t.get("url") or "") == current_url), None)
        if tab is None:
            tab = next((t for t in tabs if t.get("selected")), tabs[0])

        target = self._rdp.request(tab["actor"], "getTarget").get("frame") or {}
        thread_actor = target.get("threadActor")
        if not thread_actor:
            raise DebuggerError("目标未提供 threadActor，无法进行断点调试")

        self._target_actor = target.get("actor")
        self._thread_actor = thread_actor
        self._rdp.request(thread_actor, "attach", options={})

    def _require_started(self):
        if not self.started:
            raise DebuggerError("调试通道未启动，请先调用 page.debugger.start()")

    # ── 源与断点 ──

    def sources(self, url_contains=None):
        """列出已加载的 JS 源。

        Args:
            url_contains: 只返回 URL 含该子串的源。

        Returns:
            list[Source]
        """
        self._require_started()
        forms = self._rdp.request(self._thread_actor, "sources").get("sources") or []
        result = []
        for form in forms:
            source = Source(form)
            if source.url:
                self._source_cache[source.url] = source
                self._source_urls[source.actor] = source.url
            if url_contains and url_contains not in source.url:
                continue
            result.append(source)
        return result

    def breakable_lines(self, url):
        """返回某个源可下断点的行号列表。"""
        self._require_started()
        positions = self._breakpoint_positions(self._resolve_source(url))
        return sorted(int(line) for line in positions)

    def set_breakpoint(self, url, line, column=None, condition=None):
        """在指定源的指定行下断点。

        Firefox 只接受落在真实断点位置上的断点，行列不匹配时会被静默忽略。
        因此 ``column`` 省略时会自动查询该行的有效列。

        Args:
            url: 源 URL，需与 ``sources()`` 中的 URL 一致。
            line: 行号（从 1 开始）。
            column: 列号。省略时自动取该行第一个有效列。
            condition: 条件表达式，在断点所在帧中求值，结果为真才暂停。
                表达式抛异常时**仍会暂停**，此时
                ``PausedState.why`` 为 ``'breakpointConditionThrown'``，
                ``PausedState.message`` 是抛出的信息。

        Returns:
            Breakpoint

        Raises:
            DebuggerError: 源不存在或该行不可下断点
        """
        self._require_started()
        source = self._resolve_source(url)

        if column is None:
            positions = self._breakpoint_positions(source)
            columns = positions.get(str(int(line))) or positions.get(int(line))
            if not columns:
                raise DebuggerError(
                    "{} 第 {} 行不可下断点，可用行: {}".format(
                        source.url, line, sorted(int(k) for k in positions)
                    )
                )
            column = sorted(columns)[0]

        options = {}
        if condition:
            options["condition"] = condition

        self._rdp.request(
            self._thread_actor,
            "setBreakpoint",
            location={
                "sourceUrl": source.url,
                "sourceId": source.actor,
                "line": int(line),
                "column": int(column),
            },
            options=options,
        )

        breakpoint_ = Breakpoint(
            source.url, int(line), int(column), source.actor, condition
        )
        self._breakpoints.append(breakpoint_)
        return breakpoint_

    def remove_breakpoint(self, breakpoint_):
        """移除一个断点。"""
        self._require_started()
        self._rdp.request(
            self._thread_actor,
            "removeBreakpoint",
            location={
                "sourceUrl": breakpoint_.url,
                "sourceId": breakpoint_.source_actor,
                "line": breakpoint_.line,
                "column": breakpoint_.column,
            },
        )
        self._breakpoints = [b for b in self._breakpoints if b is not breakpoint_]
        return self

    def clear_breakpoints(self):
        """移除所有由本实例设置的断点。"""
        for breakpoint_ in list(self._breakpoints):
            try:
                self.remove_breakpoint(breakpoint_)
            except DebuggerError as exc:
                logger.warning("移除断点失败 %s: %s", breakpoint_, exc)
        return self

    def _resolve_source(self, url):
        source = self._source_cache.get(url)
        if source is not None:
            return source

        for candidate in self.sources():
            if candidate.url == url:
                return candidate

        # 允许传入后缀，方便 file:// 这类冗长 URL
        matches = [s for s in self._source_cache.values() if s.url.endswith(url)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DebuggerError(
                "源 URL '{}' 匹配到多个结果: {}".format(
                    url, [m.url for m in matches]
                )
            )
        raise DebuggerError(
            "未找到源 '{}'，已加载: {}".format(
                url, sorted(self._source_cache)
            )
        )

    def _breakpoint_positions(self, source):
        return (
            self._rdp.request(
                source.actor, "getBreakpointPositionsCompressed", query=None
            ).get("positions")
            or {}
        )

    # ── 暂停与恢复 ──

    def wait_paused(self, timeout=30):
        """等待下一次暂停。

        Returns:
            PausedState，超时返回 None
        """
        self._require_started()
        try:
            return self._paused_queue.get(timeout=timeout)
        except Empty:
            return None

    def on_paused(self, callback):
        """注册暂停回调；``None`` 表示移除。

        回调在 RDP 读线程上执行，不要在其中调用会阻塞的页面操作。
        """
        self._on_paused_cb = callback
        return self

    def pause(self):
        """立即暂停 JS 执行。"""
        self._require_started()
        self._rdp.request(self._thread_actor, "interrupt", when="now")
        return self

    def resume(self):
        """恢复执行。"""
        return self._resume()

    def step_over(self):
        """单步跳过（不进入函数调用）。"""
        return self._resume(limit="over")

    def step_into(self):
        """单步进入函数调用。"""
        return self._resume(limit="into")

    def step_out(self):
        """跳出当前函数。"""
        return self._resume(limit="out")

    def _resume(self, limit=None):
        self._require_started()
        if not self.paused:
            raise DebuggerError("当前未处于暂停状态")

        params = {}
        if limit is not None:
            params["resumeLimit"] = {"type": _STEP_LIMITS[limit]}

        with self._state_lock:
            self._paused = None
        self._rdp.request(self._thread_actor, "resume", **params)
        return self

    def _handle_paused(self, packet):
        state = PausedState(packet, self._source_urls)
        with self._state_lock:
            self._paused = state
        self._paused_queue.put(state)
        callback = self._on_paused_cb
        if callback:
            try:
                callback(state)
            except Exception as exc:
                logger.warning("暂停回调异常: %s", exc)

    def _handle_resumed(self, _packet):
        with self._state_lock:
            self._paused = None

    # ── 调用栈与作用域 ──

    def frames(self, count=50):
        """读取当前调用栈。

        Returns:
            list[Frame]，最内层在前

        Raises:
            DebuggerError: 当前未暂停
        """
        self._require_started()
        if not self.paused:
            raise DebuggerError("只能在暂停状态下读取调用栈")
        forms = (
            self._rdp.request(
                self._thread_actor, "frames", start=0, count=int(count)
            ).get("frames")
            or []
        )
        return [Frame(form, self._source_urls) for form in forms]

    def scope(self, frame=None, include_parents=False):
        """读取某一帧的作用域变量。

        Args:
            frame: :class:`Frame` 或 frame actor id。默认用当前暂停的帧。
            include_parents: 是否沿作用域链向上合并外层变量。
                内层同名变量优先。

        Returns:
            dict: 变量名到值的映射。无法序列化为原生值的对象保留其 RDP grip。

        Raises:
            DebuggerError: 当前未暂停
        """
        self._require_started()
        state = self.paused_state
        if state is None:
            raise DebuggerError("只能在暂停状态下读取作用域")

        if frame is None:
            if state.frame is None:
                raise DebuggerError("当前暂停状态没有可用的调用帧")
            actor = state.frame.actor
        elif isinstance(frame, Frame):
            actor = frame.actor
        else:
            actor = frame

        environment = self._rdp.request(actor, "getEnvironment")
        merged = {}
        chain = []
        node = environment
        while node:
            chain.append(node)
            node = node.get("parent") if include_parents else None

        # 由外到内合并，内层同名变量覆盖外层
        for node in reversed(chain):
            merged.update(self._bindings_to_dict(node.get("bindings") or {}))
        return merged

    @staticmethod
    def _bindings_to_dict(bindings):
        result = {}

        for entry in bindings.get("arguments") or []:
            if isinstance(entry, dict):
                for name, descriptor in entry.items():
                    result[name] = Debugger._grip_to_value(
                        (descriptor or {}).get("value")
                    )

        variables = bindings.get("variables") or {}
        for name, descriptor in variables.items():
            result[name] = Debugger._grip_to_value((descriptor or {}).get("value"))
        return result

    @staticmethod
    def _grip_to_value(grip):
        """把 RDP grip 转成 Python 值；无法降级的对象原样返回。"""
        if not isinstance(grip, dict):
            return grip
        if "value" in grip:
            return grip["value"]
        grip_type = grip.get("type")
        if grip_type in ("null", "undefined"):
            return None
        if grip_type in ("Infinity", "-Infinity", "NaN"):
            return float(grip_type.replace("Infinity", "inf"))
        return grip
