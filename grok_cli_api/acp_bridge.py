"""acp_bridge — 把 grok CLI 的 agent stdio（ACP）会话桥接为 OpenAI 工具调用。

方案 B 的核心：网关维持一个**有状态**的 grok ACP 会话，把 HeySure 传来的
OpenAI ``tools[]`` 作为一个 HTTP MCP server 注册给 grok（``session/new`` 的
``mcpServers``）。grok 以真实工具调用的方式使用平台工具：

    grok 发起 MCP tools/call ──► 调用在网关处**阻塞挂起**
        ──► 网关把它翻译成 OpenAI ``tool_calls`` 返回给 HeySure（finish_reason
            = "tool_calls"），本次 HTTP 请求结束，但 grok 进程与 ACP 会话存活
    HeySure 编排器执行工具（权限/设备下发/记账全在服务端）
        ──► 下一次 chat/completions 请求带回 ``role:"tool"`` 结果
        ──► 网关按 tool_call_id 找回挂起的 MCP 调用并作答
        ──► grok 在同一会话内继续推进，直到 EndTurn（= 真正的最终回答）

会话相关性靠 tool_call id 编码：``call_<会话token>-<序号>``。HeySure 会原样
回传该 id（native 工具协议本身要求 tool 响应携带 tool_call_id），网关据此
找回会话。找不到（网关重启 / 会话过期 / 上下文被压缩重写）时退回**全量重放**：
把整个对话序列化成 prompt 开一个新会话——退化但正确。

本模块自成一体，不 import server.py（避免环）；进程管理、JSON-RPC、MCP 调用
挂起/作答、会话注册与回收都在这里，HTTP 路由与 OpenAI 编解码留在 server.py。
纯 Python 标准库。
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

ACP_DEBUG = str(os.environ.get("GROK_CLI_ACP_DEBUG", "") or "").strip() in ("1", "true", "yes")

# tool_call id 形态：call_<token8>-<seq>
CALL_ID_RE = re.compile(r"^call_([0-9a-f]{8})-(\d+)$")

# ACP 权限选项 kind 里代表"允许"的前缀。本项目 7×24 全自动运行，工具调用
# 不设确认环节：spawn 时带 --always-approve，兜底的权限请求也一律放行。
# 平台工具的真正治理（DevicePermissionPolicy 等）在 HeySure 服务端，不在这里。
_ALLOW_KIND_PREFIX = "allow"


def _dbg(msg: str) -> None:
    if ACP_DEBUG:
        print(f"[acp] {msg}")


class AcpError(RuntimeError):
    pass


class PendingToolCall:
    """一次挂起中的 MCP tools/call：grok 的 HTTP 线程阻塞在 event 上等答案。"""

    __slots__ = ("call_id", "name", "arguments", "event", "result_text", "is_error")

    def __init__(self, call_id: str, name: str, arguments: Dict[str, Any]):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments if isinstance(arguments, dict) else {}
        self.event = threading.Event()
        self.result_text = ""
        self.is_error = False


class AcpSession:
    """一个 grok ``agent stdio`` 子进程 + 它的一个 ACP 会话。

    turn 事件经 ``queue`` 输出，元素为 ``(kind, data)``：
      ("thought", str)       推理增量
      ("text", str)          正文增量
      ("mcp_call", PendingToolCall)  grok 发起了平台工具调用（已挂起）
      ("end", stop_reason)   本 turn 结束（真正的最终回答）
      ("error", str)         会话级错误（进程死亡 / prompt 被拒 …）
    """

    def __init__(self, token: str, model: str):
        self.token = token
        self.model = model
        self.queue: "Queue[tuple]" = Queue()
        self.tools: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.pending: "OrderedDict[str, PendingToolCall]" = OrderedDict()
        self.temp_paths: List[str] = []
        self.session_id = ""
        self.closed = False
        self.created_at = time.time()
        self.last_used = time.time()
        self.busy = threading.Lock()

        self.proc: Optional[subprocess.Popen] = None
        self.stderr_tail: List[str] = []
        self._call_seq = 0
        self._rpc_seq = 0
        self._turn_rpc_id: Optional[int] = None
        self._inflight: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        exe: str,
        model: str,
        tools: Dict[str, Dict[str, Any]],
        mcp_url_base: str,
        cwd: str,
        registry: "SessionRegistry",
        init_timeout: float = 60.0,
    ) -> "AcpSession":
        """spawn + initialize + session/new。失败抛 AcpError（进程已清理）。

        MCP 端点 URL 含会话 token（``<base>/<token>``），因此必须**先**入注册表
        再做 session/new——grok 建会话时就会连过来 initialize/tools/list。
        """
        token = uuid.uuid4().hex[:8]
        sess = cls(token, model)
        sess.busy.acquire()  # 创建者持有 busy，请求结束时 release/close
        sess.update_tools(tools)
        mcp_url = f"{mcp_url_base.rstrip('/')}/{token}"
        registry.add(sess)

        argv = [exe, "agent"]
        if str(model or "").strip():
            argv += ["-m", str(model).strip()]
        # 7×24 全自动项目：不要任何工具确认环节，全部自动放行。
        argv += ["--always-approve", "--no-leader", "stdio"]

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            sess.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                creationflags=creationflags,
            )
        except OSError as exc:
            registry.drop(sess)
            raise AcpError(f"ACP 进程启动失败：{exc}") from exc

        threading.Thread(target=sess._read_loop, daemon=True, name=f"acp-r-{token}").start()
        threading.Thread(target=sess._stderr_loop, daemon=True, name=f"acp-e-{token}").start()

        try:
            sess._rpc_request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        # 不提供 fs/terminal 能力：网关机器上的本地动作不该由
                        # grok 自主发起，平台动作一律走注册的 heysure MCP 工具。
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
                timeout=init_timeout,
            )
            result = sess._rpc_request(
                "session/new",
                {
                    "cwd": os.path.abspath(cwd),
                    "mcpServers": [
                        {"type": "http", "name": "heysure", "url": mcp_url, "headers": []}
                    ],
                },
                timeout=init_timeout,
            )
            sess.session_id = str(result.get("sessionId") or "")
            if not sess.session_id:
                raise AcpError(f"session/new 未返回 sessionId：{result}")
        except AcpError:
            registry.drop(sess)
            raise
        except Exception as exc:
            registry.drop(sess)
            raise AcpError(f"ACP 会话初始化失败：{exc}") from exc
        _dbg(f"session {token} ready (acp sid={sess.session_id})")
        return sess

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            pending = list(self.pending.values())
            self.pending.clear()
            inflight = list(self._inflight.values())
            self._inflight.clear()
        for pc in pending:
            pc.is_error = True
            pc.result_text = "会话已关闭，调用未执行。"
            pc.event.set()
        for slot in inflight:
            slot["msg"] = {"error": {"message": "session closed"}}
            slot["evt"].set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        for path in self.temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.temp_paths.clear()
        _dbg(f"session {self.token} closed")

    def cancel_turn(self) -> None:
        """尽力通知 grok 取消当前 turn（客户端断开时用），随后仍应 close()。"""
        try:
            self._rpc_notify("session/cancel", {"sessionId": self.session_id})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # turn 驱动
    # ------------------------------------------------------------------

    def start_turn(self, prompt_text: str) -> None:
        """发起 session/prompt（异步）；结束经 queue 的 ("end", ...) 到达。"""
        rpc_id = self._rpc_send(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": prompt_text}],
            },
        )
        self._turn_rpc_id = rpc_id
        self.last_used = time.time()

    def matches_results(self, results: Dict[str, str]) -> bool:
        """恢复条件：结果非空，且每个 tool_call_id 都还挂在本会话上。"""
        if self.closed or not results:
            return False
        with self._lock:
            return all(cid in self.pending for cid in results)

    def answer_calls(self, results: Dict[str, str], extra_tail: str = "") -> None:
        """按 id 回答挂起的 MCP 调用；extra_tail（插入消息等）附在最后一个答案后。

        只回答 ``results`` 里有的调用；其余仍保持挂起（属于上一轮 grace 窗口
        之外、尚未上报给 HeySure 的调用，会被消费循环重新上报）。
        """
        with self._lock:
            answer_ids = [cid for cid in self.pending if cid in results]
            targets = [(cid, self.pending.pop(cid)) for cid in answer_ids]
        for index, (cid, pc) in enumerate(targets):
            text = str(results.get(cid) or "")
            if extra_tail and index == len(targets) - 1:
                text = f"{text}\n\n[本轮追加消息]\n{extra_tail}" if text else extra_tail
            pc.result_text = text
            pc.event.set()
        self.last_used = time.time()

    def update_tools(self, tools: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self.tools = OrderedDict(tools or {})

    def adopt_temp_paths(self, paths: List[str]) -> None:
        """图片等临时文件的生命周期跟随会话（grok 可能稍后才 read_file）。"""
        self.temp_paths.extend(paths or [])

    # ------------------------------------------------------------------
    # MCP server 侧（由 server.py 的 HTTP 线程调用）
    # ------------------------------------------------------------------

    def mcp_tools_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": name,
                    "description": str(spec.get("description") or ""),
                    "inputSchema": spec.get("inputSchema")
                    or {"type": "object", "properties": {}},
                }
                for name, spec in self.tools.items()
            ]

    def mcp_tools_call(
        self, name: str, arguments: Dict[str, Any], timeout: float = 3600.0
    ) -> PendingToolCall:
        """挂起一次工具调用，阻塞到 HeySure 送回结果（或会话关闭/超时）。"""
        with self._lock:
            if self.closed:
                raise AcpError("session closed")
            self._call_seq += 1
            pc = PendingToolCall(f"call_{self.token}-{self._call_seq}", name, arguments)
            self.pending[pc.call_id] = pc
        self.queue.put(("mcp_call", pc))
        _dbg(f"session {self.token} tools/call {name} → {pc.call_id}")

        deadline = time.time() + timeout
        while not pc.event.wait(timeout=5.0):
            if self.closed or time.time() > deadline:
                with self._lock:
                    self.pending.pop(pc.call_id, None)
                pc.is_error = True
                pc.result_text = "调用超时或会话已关闭，未取得结果。"
                pc.event.set()
                break
        return pc

    # ------------------------------------------------------------------
    # JSON-RPC 底层
    # ------------------------------------------------------------------

    def _rpc_send(self, method: str, params: Dict[str, Any]) -> int:
        with self._lock:
            if self.closed:
                raise AcpError("session closed")
            self._rpc_seq += 1
            rpc_id = self._rpc_seq
        self._write({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
        return rpc_id

    def _rpc_notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc_request(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        slot: Dict[str, Any] = {"evt": threading.Event(), "msg": None}
        with self._lock:
            if self.closed:
                raise AcpError("session closed")
            self._rpc_seq += 1
            rpc_id = self._rpc_seq
            self._inflight[rpc_id] = slot
        self._write({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
        if not slot["evt"].wait(timeout=timeout):
            with self._lock:
                self._inflight.pop(rpc_id, None)
            raise AcpError(f"ACP {method} 超时（{timeout:.0f}s）：{self._stderr_text()}")
        msg = slot["msg"] or {}
        if msg.get("error"):
            raise AcpError(f"ACP {method} 出错：{msg['error']}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def _write(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        _dbg(f">> {data[:400]!r}")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except Exception as exc:
            raise AcpError(f"ACP 进程写入失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 读取线程
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                _dbg(f"<< {line[:400]}")
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    threading.Thread(
                        target=self._handle_agent_request, args=(msg,), daemon=True
                    ).start()
                elif "method" in msg:
                    self._handle_notification(msg)
                elif "id" in msg:
                    self._handle_response(msg)
        except Exception:
            pass
        finally:
            # 进程 stdout EOF：turn 未结束就死了 → 报错给消费方
            if not self.closed:
                self.queue.put(
                    ("error", f"grok ACP 进程退出：{self._stderr_text() or '（无错误输出）'}")
                )

    def _stderr_loop(self) -> None:
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
                    if len(self.stderr_tail) > 40:
                        del self.stderr_tail[:-40]
        except Exception:
            pass

    def _stderr_text(self) -> str:
        return "\n".join(self.stderr_tail[-8:]).strip()

    def _handle_response(self, msg: Dict[str, Any]) -> None:
        rpc_id = msg.get("id")
        with self._lock:
            slot = self._inflight.pop(rpc_id, None)
        if slot is not None:
            slot["msg"] = msg
            slot["evt"].set()
            return
        if rpc_id == self._turn_rpc_id:
            self._turn_rpc_id = None
            if msg.get("error"):
                self.queue.put(("error", f"session/prompt 出错：{msg['error']}"))
            else:
                result = msg.get("result") or {}
                self.queue.put(("end", str(result.get("stopReason") or "end_turn")))

    def _handle_notification(self, msg: Dict[str, Any]) -> None:
        method = str(msg.get("method") or "")
        if method != "session/update":
            return  # _x.ai/* 等系统通知一律忽略
        params = msg.get("params") or {}
        update = params.get("update") or {}
        kind = str(update.get("sessionUpdate") or "")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content") or {}
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text") or "")
                if text:
                    self.queue.put(
                        ("text" if kind == "agent_message_chunk" else "thought", text)
                    )
        # tool_call / tool_call_update / plan 等：grok 自身工具的执行播报，忽略。

    def _handle_agent_request(self, msg: Dict[str, Any]) -> None:
        """处理 grok 发来的反向请求（主要是权限确认）。"""
        method = str(msg.get("method") or "")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}
        try:
            if method == "session/request_permission":
                option_id = self._pick_permission_option(params)
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
                    }
                )
            else:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32601, "message": f"method not supported: {method}"},
                    }
                )
        except Exception:
            pass

    def _pick_permission_option(self, params: Dict[str, Any]) -> Any:
        """无确认策略：一律选"允许"（--always-approve 的兜底，正常不会走到这）。"""
        options = params.get("options") or []
        for opt in options:
            if str(opt.get("kind") or "").lower().startswith(_ALLOW_KIND_PREFIX):
                return opt.get("optionId")
        return options[0].get("optionId") if options else None


class SessionRegistry:
    """token → AcpSession，带 TTL 回收与容量上限（LRU 淘汰）。"""

    def __init__(self, ttl: float = 1800.0, max_sessions: int = 6):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._sessions: "OrderedDict[str, AcpSession]" = OrderedDict()
        self._lock = threading.Lock()
        self._gc_started = False

    def configure(self, ttl: float, max_sessions: int) -> None:
        self.ttl = ttl
        self.max_sessions = max_sessions

    def get(self, token: str) -> Optional[AcpSession]:
        with self._lock:
            return self._sessions.get(token)

    def add(self, sess: AcpSession) -> None:
        evicted: List[AcpSession] = []
        with self._lock:
            self._sessions[sess.token] = sess
            self._sessions.move_to_end(sess.token)
            while len(self._sessions) > self.max_sessions:
                _, old = self._sessions.popitem(last=False)
                evicted.append(old)
        for old in evicted:
            old.close()
        self._ensure_gc()

    def drop(self, sess: AcpSession) -> None:
        with self._lock:
            self._sessions.pop(sess.token, None)
        sess.close()

    def touch(self, sess: AcpSession) -> None:
        sess.last_used = time.time()
        with self._lock:
            if sess.token in self._sessions:
                self._sessions.move_to_end(sess.token)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _ensure_gc(self) -> None:
        with self._lock:
            if self._gc_started:
                return
            self._gc_started = True
        threading.Thread(target=self._gc_loop, daemon=True, name="acp-gc").start()

    def _gc_loop(self) -> None:
        while True:
            time.sleep(60)
            now = time.time()
            stale: List[AcpSession] = []
            with self._lock:
                for token, sess in list(self._sessions.items()):
                    if now - sess.last_used > self.ttl or sess.closed:
                        self._sessions.pop(token, None)
                        stale.append(sess)
            for sess in stale:
                sess.close()


REGISTRY = SessionRegistry()


# ---------------------------------------------------------------------------
# OpenAI 消息 → 会话恢复信息
# ---------------------------------------------------------------------------

def extract_resume_info(messages: List[Dict[str, Any]]):
    """从 OpenAI messages 中提取 (token, {tool_call_id: 结果原文}, 尾部消息列表)。

    定位**最后一个**带 tool_calls 的 assistant 消息（= 最近一轮的调用批次），
    其后的 ``role:"tool"`` 是本轮结果；其后的非 tool 消息（用户插入、系统通知、
    截图等）作为尾部原样返回，由调用方文本化后附进最后一个答案。
    结果值保持原始 content（可能是分块列表），由调用方按需文本化/落盘图片。
    """
    last_idx = -1
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_idx = index
            break
    if last_idx < 0:
        return None, {}, []

    token = None
    for tc in messages[last_idx].get("tool_calls") or []:
        match = CALL_ID_RE.match(str((tc or {}).get("id") or ""))
        if match:
            token = match.group(1)
            break
    if not token:
        return None, {}, []

    results: Dict[str, Any] = {}
    tail: List[Dict[str, Any]] = []
    for msg in messages[last_idx + 1:]:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id:
                results[call_id] = msg.get("content")
        else:
            tail.append(msg)
    return token, results, tail


def tools_registry_from_payload(tools: Any) -> Dict[str, Dict[str, Any]]:
    """OpenAI ``tools[]`` → {name: {description, inputSchema}}（原名透传）。"""
    registry: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        registry[name] = {
            "description": str(fn.get("description") or ""),
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
    return registry
