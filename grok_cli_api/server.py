"""grok_cli — 本地 OpenAI 兼容 API 服务，把 grok CLI 包装成标准聊天接口。

把 grok CLI（或任何输出 streaming-json 的同类 agent CLI）暴露为
``POST /v1/chat/completions``（支持 stream / 非 stream）+ ``GET /v1/models``，
这样任何按 OpenAI 格式调用的客户端（包括 HeySure 服务器的普通 API 模型预设）
都能直接使用本机 CLI 的订阅额度，服务器端无需任何 CLI 特判。

一次请求 = 启动一个 CLI 进程：

    <command> --prompt-file <tmp> --output-format streaming-json -m <model> ...

CLI stdout 输出 JSON Lines：
    {"type":"thought","data":"..."}   推理增量  → delta.reasoning_content
    {"type":"text","data":"..."}      正文增量  → delta.content
    {"type":"end","stopReason":...}   本轮结束  → finish_reason=stop

对话是无状态的：每轮把完整对话（含 system prompt）序列化进 prompt 文件，
命令行长度与 prompt 大小无关。

纯 Python 标准库实现，无第三方依赖。直接运行：

    python server.py --command C:\\Users\\admin\\.grok\\bin\\grok.exe --port 8100

环境变量（命令行参数优先）：
    GROK_CLI_COMMAND   CLI 命令或完整路径（默认 "grok"）
    GROK_CLI_HOST      监听地址（默认 127.0.0.1）
    GROK_CLI_PORT      监听端口（默认 8100）
    GROK_CLI_TIMEOUT   单次推理超时秒数（默认 600）
    GROK_CLI_API_KEY   可选；设置后要求请求携带 Bearer <key>
    GROK_CLI_MODELS    /v1/models 返回的模型 id 列表（逗号分隔，仅展示用）
"""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty
from typing import Any, Dict, List, Optional

import acp_bridge
from acp_bridge import REGISTRY as ACP_REGISTRY

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
IMAGE_MAX_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024

# grok 拒绝创建零内置工具的会话。保留无害的 todo 工具 + read_file：
# 图片输入会落盘为 RUNTIME_DIR 下的临时文件，多模态 Grok 模型用 read_file
# 查看像素。Web 搜索与子代理保持关闭；平台自身的 MCP 工具走文本协议。
CLI_FIXED_ARGS = [
    "--output-format",
    "streaming-json",
    "--verbatim",
    "--tools",
    "todo_write,read_file",
    "--disable-web-search",
    "--no-subagents",
]

# 真正的（可能很大的）system prompt 放在 prompt 文件里；命令行上只带这个短包装。
#
# 末尾那条「说了就要做」的规则不是废话：本网关是无状态的，每轮请求都是一个全新
# 的 CLI 进程，模型很容易把一轮当成「汇报进度」——写下"接下来我要点击提交"然后
# 就结束了。而服务端把「一轮没有工具调用」判定为最终回答，于是任务从中间断掉。
# 必须明确要求它在同一轮里把话和动作一起给出来。
CLI_SYSTEM_WRAPPER = (
    "你不是编程助手。接下来的输入由两部分组成：[系统设定] 与 [对话记录]。"
    "请完全遵循 [系统设定] 中的全部要求与角色设定，以助手身份继续"
    " [对话记录] 中的对话与尚未完成的工作。若最后一条是 Assistant，表示同一任务"
    "正在连续生成，请直接从该输出之后继续，不要把它当成另一位用户来回复。"
    "不要输出角色前缀，不要复述对话记录。"
    "\n\n"
    "重要：如果你判断下一步需要执行某个动作（点击、输入、提交、运行、读取、搜索、"
    "发送等），必须在**本轮回复里直接输出对应的工具调用块**，把动作真正执行掉。"
    "严禁只用文字描述计划（例如「接下来我要向输入框写入提示词并提交」）然后就结束——"
    "系统会把没有工具调用的一轮当作任务已完成而收尾，你的动作将永远不会被执行。"
    "特别注意：**只在思考/推理里写出工具调用是无效的**——思考内容不会被当作动作解析，"
    "真正要执行的工具调用块必须出现在**正文回复**中。"
    "确实无法执行时（缺工具、缺参数、需用户补充信息），直接说明原因，不要假装已执行。"
)

_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "tool": "Tool Result"}

# ACP（agent stdio）路径的提示包装。与 headless 不同：平台工具此时是**真实注册**
# 的 MCP 工具（heysure server），必须引导模型直接调用而不是手写文本标记；agent
# 模式没有 --system-prompt-override，包装直接置于 prompt 文本顶部。
ACP_SYSTEM_WRAPPER = (
    "你不是编程助手。接下来的输入由两部分组成：[系统设定] 与 [对话记录]。"
    "请完全遵循 [系统设定] 中的全部要求与角色设定，以助手身份继续"
    " [对话记录] 中的对话与尚未完成的工作。若最后一条是 Assistant，表示同一任务"
    "正在连续生成，请直接从该输出之后继续，不要把它当成另一位用户来回复。"
    "不要输出角色前缀，不要复述对话记录。"
    "\n\n"
    "平台的 MCP 工具已注册为你的真实工具（服务器名 heysure）。需要执行动作"
    "（发送、查询、点击、读取、创建、运行、检查等）时，直接发起对应的工具调用"
    "并等待结果，在本会话内持续推进，直到任务完成或确实需要用户补充信息。"
    "严禁在文本里手写 <mcp-call>、<xai:function_call> 之类的调用标记——"
    "它们不会被执行；一律使用真实工具调用。"
    "工具目录可能随对话增补：结果文本中提到的新工具可直接按名称调用。"
)

GATEWAY_FINGERPRINT = "grok-cli-gateway"


# ---------------------------------------------------------------------------
# grok 私有工具语法归一化
# ---------------------------------------------------------------------------
# grok 系模型即使被提示词要求输出 <mcp-call>，也会滑回预训练的私有格式：
#
#     <xai:function_call name="tool"><parameter name="x">v</parameter></xai:function_call>
#
# HeySure 服务端只认 <mcp-call> / <invoke> / <tool_call> 语法；私有格式会被
# 前端剥离但不会执行——表现为"说了要做什么然后戛然而止"。网关最清楚自己
# 包的是 grok，所以在这里把私有格式重写为规范 <mcp-call> 块，服务端保持通用。

_FC_OPEN_RE = re.compile(
    r"<[^<>]*?\bfunction[_-]?call\b[^<>]*?\bname\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^<>]*?>",
    re.IGNORECASE,
)
_FC_BLOCK_RE = re.compile(
    r"<[^<>]*?\bfunction[_-]?call\b[^<>]*?\bname\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^<>]*?>"
    r"([\s\S]*?)"
    r"</[^<>]*?\bfunction[_-]?call\b[^<>]*?>",
    re.IGNORECASE,
)
_FC_PARAM_RE = re.compile(
    r"<[^<>]*?\bparameter\b[^<>]*?\bname\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^<>]*?>"
    r"([\s\S]*?)"
    r"</[^<>]*?\bparameter\b[^<>]*?>",
    re.IGNORECASE,
)


def _coerce_param_value(raw: str) -> Any:
    """XML 参数体的尽力类型化：JSON 字面量按 JSON 解析，其余保留字符串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        pass
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d*\.\d+", text):
        return float(text)
    return text


def _fc_block_to_mcp_call(match: "re.Match[str]") -> str:
    tool = str(match.group(1) or "").strip()
    if not tool:
        return match.group(0)
    inner = str(match.group(2) or "")
    args: Dict[str, Any] = {}
    for pm in _FC_PARAM_RE.finditer(inner):
        key = str(pm.group(1) or "").strip()
        if key:
            args[key] = _coerce_param_value(pm.group(2))
    if not args:
        # 无 <parameter> 子标签时，块体本身可能就是一个 JSON 参数对象。
        stripped = inner.strip()
        if stripped:
            try:
                maybe = json.loads(stripped)
                if isinstance(maybe, dict):
                    args = maybe
            except Exception:
                pass
    payload = json.dumps({"tool": tool, "arguments": args}, ensure_ascii=False)
    return f"<mcp-call>{payload}</mcp-call>"


def normalize_tool_markup(text: str) -> str:
    """把完整的 grok 私有工具块重写为规范 <mcp-call> 块，其余文本原样保留。"""
    return _FC_BLOCK_RE.sub(_fc_block_to_mcp_call, str(text or ""))


class StreamingToolMarkupNormalizer:
    """`normalize_tool_markup` 的流式增量版。

    完整的私有块即时重写；疑似"块开了个头"的尾部（未闭合的 ``<...`` 或未闭合
    的 function_call 块）先扣住不发，等后续增量补完或推理结束 flush 再定夺。
    被扣住的正文只会延迟、不会丢失。
    """

    # 私有块的参数可能很大（例如整段脚本），扣住上限放宽，避免块被从中间切断
    # 后 normalize 失配、半截私有语法漏给调用方。
    _HOLDBACK_LIMIT = 4000

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += str(chunk or "")
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        buf = normalize_tool_markup(self._buf)
        if final:
            self._buf = ""
            return buf
        hold = len(buf)
        open_match = _FC_OPEN_RE.search(buf)
        if open_match:
            # 完整块已在 normalize 中被替换，这里剩下的只可能是未闭合的开头。
            hold = open_match.start()
        else:
            tail = re.search(r"<[^<>]*$", buf)
            if tail and len(buf) - tail.start() <= self._HOLDBACK_LIMIT:
                hold = tail.start()
        out, self._buf = buf[:hold], buf[hold:]
        return out


class StreamingThoughtNormalizer:
    """把 grok 的思考（``thought``）流拆成"纯推理"与"落在思考里的工具调用"两路。

    grok 系模型常常在**思考流**里就把决定好的动作写成私有
    ``<xai:function_call>…</xai:function_call>`` 块，然后正文（``text``）不再重复。
    而服务端只从 ``content`` 解析工具调用，从不解析 ``reasoning_content`` ——
    于是这一轮在服务端看来"没有工具调用"，被判为最终回答，任务从中间戛然而止。

    本归一器把思考流里**每个完整的私有块**抽出来重写成 ``<mcp-call>`` 交给
    ``content`` 路（真正会被执行的地方），其余思考文本仍作为 ``reasoning_content``。
    尾部疑似"块开了个头"的片段先扣住，等补完或 flush 再定夺，只会延迟不会丢失。

    ``feed`` / ``flush`` 均返回 ``(reasoning_out, content_out)`` 两段增量。
    """

    _HOLDBACK_LIMIT = 4000

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str):
        self._buf += str(chunk or "")
        return self._drain(final=False)

    def flush(self):
        return self._drain(final=True)

    def _drain(self, final: bool):
        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        pos = 0
        for match in _FC_BLOCK_RE.finditer(self._buf):
            reasoning_parts.append(self._buf[pos:match.start()])
            content_parts.append(_fc_block_to_mcp_call(match))
            pos = match.end()
        rest = self._buf[pos:]
        if final:
            reasoning_parts.append(rest)
            self._buf = ""
            return "".join(reasoning_parts), "".join(content_parts)

        hold = len(rest)
        open_match = _FC_OPEN_RE.search(rest)
        if open_match:
            # 已开头但未闭合的私有块：整段扣住（参数可能很长），等闭合再抽取。
            hold = open_match.start()
        else:
            tail = re.search(r"<[^<>]*$", rest)
            if tail and len(rest) - tail.start() <= self._HOLDBACK_LIMIT:
                hold = tail.start()
        reasoning_parts.append(rest[:hold])
        self._buf = rest[hold:]
        return "".join(reasoning_parts), "".join(content_parts)


_MCP_CALL_DEDUP_RE = re.compile(r"<mcp-call>([\s\S]*?)</mcp-call>", re.IGNORECASE)


def _dedup_mcp_calls(text: str, seen: set) -> str:
    """丢弃 ``text`` 中 payload 已在 ``seen`` 出现过的 ``<mcp-call>`` 块。

    思考流与正文流可能各自给出同一个工具调用（grok 先在思考里决定、正文又复述）。
    两路都经此过滤，保证同一调用只交给服务端一次，避免被执行两遍。
    """
    def _filter(match: "re.Match[str]") -> str:
        payload = str(match.group(1) or "").strip()
        if payload in seen:
            return ""
        seen.add(payload)
        return match.group(0)

    return _MCP_CALL_DEDUP_RE.sub(_filter, str(text or ""))


def _promote_thought_tool_calls(reasoning: str):
    """把思考文本里的完整私有工具块抽出（改写为 ``<mcp-call>``），从推理中剥离。

    返回 ``(cleaned_reasoning, promoted_content)``。用于非流式路径：grok 常在思考里
    决定动作而不在正文复述，而服务端只从 content 解析工具调用。
    """
    blocks: List[str] = []

    def _take(match: "re.Match[str]") -> str:
        blocks.append(_fc_block_to_mcp_call(match))
        return ""

    cleaned = _FC_BLOCK_RE.sub(_take, str(reasoning or ""))
    return cleaned, "\n".join(blocks)


_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpeg|jpg|webp|gif));base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)


class Config:
    command = os.environ.get("GROK_CLI_COMMAND", "grok")
    host = os.environ.get("GROK_CLI_HOST", "127.0.0.1")
    port = int(os.environ.get("GROK_CLI_PORT", "8100") or 8100)
    timeout = int(os.environ.get("GROK_CLI_TIMEOUT", "600") or 600)
    api_key = os.environ.get("GROK_CLI_API_KEY", "").strip()
    models = [
        m.strip()
        for m in os.environ.get("GROK_CLI_MODELS", "grok-4.5").split(",")
        if m.strip()
    ]
    # ACP 桥接（方案 B）：请求带 tools[] 时走有状态 agent 会话 + 真实工具调用。
    # 置 GROK_CLI_ACP=0 强制回退旧 headless 文本协议。
    acp_enabled = str(os.environ.get("GROK_CLI_ACP", "1")).strip() not in ("0", "false", "no")
    # 首个工具调用到达后，再等这么久收集同批的其它并行调用（秒）。
    tool_grace = float(os.environ.get("GROK_CLI_TOOL_GRACE", "0.5") or 0.5)
    # ACP 会话空闲回收阈值（秒）与并存上限。
    session_ttl = int(os.environ.get("GROK_CLI_SESSION_TTL", "1800") or 1800)
    max_sessions = int(os.environ.get("GROK_CLI_MAX_SESSIONS", "6") or 6)


# ---------------------------------------------------------------------------
# 图片落盘（供 grok read_file 查看）
# ---------------------------------------------------------------------------

def _image_source_from_block(block: Dict[str, Any]) -> str:
    """从 OpenAI/Anthropic/ACP 图片块中取出 data URL、HTTP URL 或本地路径。"""
    btype = str(block.get("type") or "").lower()
    if btype == "image_url":
        value = block.get("image_url")
        if isinstance(value, dict):
            return str(value.get("url") or "").strip()
        return str(value or "").strip()
    if btype != "image":
        return ""

    source = block.get("source")
    if isinstance(source, dict):
        source_type = str(source.get("type") or "").lower()
        data = str(source.get("data") or "").strip()
        media_type = str(source.get("media_type") or source.get("mime_type") or "image/png")
        if source_type == "base64" and data:
            return f"data:{media_type};base64,{data}"
        return str(source.get("url") or source.get("path") or "").strip()

    value = block.get("data") or block.get("url") or block.get("path")
    if value and block.get("mimeType") and not str(value).startswith(("data:", "http://", "https://")):
        return f"data:{block.get('mimeType')};base64,{value}"
    return str(value or "").strip()


def _image_suffix(media_type: str, source: str = "") -> str:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(normalized)
    if suffix:
        return suffix
    path_suffix = os.path.splitext(urllib.parse.urlparse(source).path)[1].lower()
    return path_suffix if path_suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".png"


def _write_image(data: bytes, suffix: str, temporary_paths: List[str]) -> str:
    if not data or len(data) > IMAGE_MAX_BYTES:
        return ""
    image_file = tempfile.NamedTemporaryFile(
        mode="wb", suffix=suffix, prefix="image_", dir=RUNTIME_DIR, delete=False
    )
    try:
        image_file.write(data)
    finally:
        image_file.close()
    image_path = os.path.abspath(image_file.name)
    temporary_paths.append(image_path)
    return image_path


def _materialize_image(block: Dict[str, Any], temporary_paths: List[str]) -> str:
    source = _image_source_from_block(block)
    if not source:
        return ""

    if os.path.isfile(source):
        return os.path.abspath(source)
    if source.lower().startswith("file://"):
        local_path = urllib.request.url2pathname(urllib.parse.urlparse(source).path)
        if os.name == "nt" and local_path.startswith("/") and len(local_path) > 2 and local_path[2] == ":":
            local_path = local_path[1:]
        if os.path.isfile(local_path):
            return os.path.abspath(local_path)

    match = _DATA_IMAGE_RE.match(source)
    if match:
        encoded = re.sub(r"\s+", "", match.group(2))
        if len(encoded) > ((IMAGE_MAX_BYTES * 4) // 3) + 8:
            return ""
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            return ""
        return _write_image(data, _image_suffix(match.group(1)), temporary_paths)

    if source.startswith(("http://", "https://")):
        try:
            request = urllib.request.Request(source, headers={"User-Agent": "HeySure-GrokCLI/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                media_type = str(response.headers.get_content_type() or "").lower()
                if not media_type.startswith("image/"):
                    return ""
                declared_size = int(response.headers.get("Content-Length") or 0)
                if declared_size > IMAGE_MAX_BYTES:
                    return ""
                data = response.read(IMAGE_MAX_BYTES + 1)
            return _write_image(data, _image_suffix(media_type, source), temporary_paths)
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------------------
# 对话序列化
# ---------------------------------------------------------------------------

def _content_to_text(content: Any, temporary_paths: List[str]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
            elif btype in ("image_url", "image"):
                image_path = _materialize_image(block, temporary_paths)
                if image_path:
                    parts.append(
                        "[图片附件]\n"
                        f"图片绝对路径：{image_path}\n"
                        "必须使用 read_file 查看这张图片的像素内容，再基于实际画面继续。"
                    )
                else:
                    parts.append("[图片附件读取失败：适配层未能解析或下载该图片。]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _serialize_convo(messages: List[Dict], temporary_paths: List[str]) -> str:
    """把 OpenAI 格式对话展平为带角色标签的转录文本，system 合并置顶。"""
    system_parts: List[str] = []
    lines: List[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        text = _content_to_text(msg.get("content"), temporary_paths)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        # 原生工具协议的历史（ACP 全量重放路径）里，assistant 消息可能携带
        # tool_calls：把调用文本化，让新会话知道之前做过什么。
        call_lines: List[str] = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            fn_name = str(fn.get("name") or "").strip()
            if fn_name:
                call_lines.append(f"[已调用工具] {fn_name}({fn.get('arguments') or '{}'})")
        if call_lines:
            text = (f"{text}\n" if text else "") + "\n".join(call_lines)
        if not text:
            continue
        label = _ROLE_LABELS.get(role, role or "User")
        lines.append(f"{label}: {text}".rstrip())
    prompt_parts: List[str] = []
    if system_parts:
        prompt_parts.append("[系统设定]\n" + "\n\n".join(system_parts))
    prompt_parts.append("[对话记录]\n" + ("\n\n".join(lines) if lines else "User: （无内容）"))
    return "\n\n".join(prompt_parts)


def _serialize_tail(tail_msgs: List[Dict], temporary_paths: List[str]) -> str:
    """ACP 恢复路径：工具结果之后追加的消息（用户插入/系统通知/截图）文本化。"""
    parts: List[str] = []
    for msg in tail_msgs or []:
        if not isinstance(msg, dict):
            continue
        text = _content_to_text(msg.get("content"), temporary_paths)
        if text:
            role = str(msg.get("role") or "").strip().lower()
            parts.append(f"{_ROLE_LABELS.get(role, 'User')}: {text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI 进程
# ---------------------------------------------------------------------------

def _resolve_cli_argv() -> List[str]:
    command = str(Config.command or "").strip()
    if not command:
        raise RuntimeError("未配置 CLI 命令（GROK_CLI_COMMAND 或 --command）")
    argv = [tok.strip('"') for tok in shlex.split(command, posix=(os.name != "nt"))]
    argv = [tok for tok in argv if tok]
    if not argv:
        raise RuntimeError("未配置 CLI 命令（GROK_CLI_COMMAND 或 --command）")
    exe = argv[0]
    resolved = shutil.which(exe)
    if resolved is None and not os.path.isfile(exe):
        raise RuntimeError(f"CLI 命令未找到：{exe}。请安装该 CLI 或用完整路径配置")
    argv[0] = resolved or exe
    return argv


def _kill_quietly(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _unlink_quietly(paths: List[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _stderr_thread(pipe, sink: List[bytes]) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            sink.append(raw)
            if len(sink) > 50:
                del sink[:-50]
    except Exception:
        pass


def run_cli_turn(model: str, messages: List[Dict]):
    """生成器：跑一轮 CLI 推理，产出 ("thought"|"text", str) 事件。

    CLI 缺失 / 启动失败 / 无输出异常退出时抛 RuntimeError（带用户可读信息）。
    """
    argv = _resolve_cli_argv()

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    temporary_paths: List[str] = []
    prompt_text = _serialize_convo(messages, temporary_paths)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", prefix="prompt_",
        dir=RUNTIME_DIR, delete=False,
    )
    try:
        prompt_file.write(prompt_text)
    finally:
        prompt_file.close()
    temporary_paths.append(prompt_file.name)

    full_argv = argv + [
        "--prompt-file",
        prompt_file.name,
        "--system-prompt-override",
        CLI_SYSTEM_WRAPPER,
        "--cwd",
        RUNTIME_DIR,
    ] + CLI_FIXED_ARGS
    if str(model or "").strip():
        full_argv += ["-m", str(model).strip()]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            full_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=RUNTIME_DIR,
            creationflags=creationflags,
        )
    except OSError as exc:
        _unlink_quietly(temporary_paths)
        raise RuntimeError(f"CLI 启动失败：{exc}") from exc

    stderr_tail: List[bytes] = []
    threading.Thread(target=_stderr_thread, args=(proc.stderr, stderr_tail), daemon=True).start()
    watchdog = threading.Timer(Config.timeout, _kill_quietly, args=(proc,))
    watchdog.daemon = True
    watchdog.start()

    produced_output = False
    timed_out = False
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "thought":
                data = str(event.get("data") or "")
                if data:
                    produced_output = True
                    yield ("thought", data)
            elif etype == "text":
                data = str(event.get("data") or "")
                if data:
                    produced_output = True
                    yield ("text", data)
            # end / 会话簿记等其他事件类型忽略。
    finally:
        timed_out = not watchdog.is_alive() and proc.poll() is not None and not produced_output
        watchdog.cancel()
        _kill_quietly(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        _unlink_quietly(temporary_paths)

    returncode = proc.poll()
    if returncode not in (0, None) and not produced_output:
        stderr_text = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
        if timed_out:
            raise RuntimeError(f"CLI 推理超时（超过 {Config.timeout} 秒），进程已终止")
        detail = stderr_text[-600:] if stderr_text else "（无错误输出）"
        raise RuntimeError(f"CLI 进程异常退出（退出码 {returncode}）：{detail}")


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return max(0, len(text) // 4)


def _usage(prompt_text: str, completion_text: str) -> Dict[str, int]:
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(completion_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "grok-cli-gateway/1.0"

    # -- 基础 ---------------------------------------------------------------

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    def _json_response(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, err_type: str = "invalid_request_error") -> None:
        self._json_response(status, {"error": {"message": message, "type": err_type, "code": None}})

    def _check_auth(self) -> bool:
        if not Config.api_key:
            return True
        auth = str(self.headers.get("Authorization") or "")
        if auth == f"Bearer {Config.api_key}":
            return True
        self._error(401, "Invalid API key", "authentication_error")
        return False

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/")

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):
        path = self._path()
        if path.startswith("/mcp/"):
            # streamable-http 允许服务端不提供 GET 事件流。
            self._error(405, "GET not supported on MCP endpoint")
            return
        if path in ("", "/health"):
            self._json_response(200, {
                "service": "grok-cli-gateway",
                "command": Config.command,
                "models": Config.models,
                "endpoint": "/v1/chat/completions",
                "acp": Config.acp_enabled,
                "acp_sessions": ACP_REGISTRY.count(),
            })
            return
        if path.endswith("/models"):
            if not self._check_auth():
                return
            now = int(time.time())
            self._json_response(200, {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": now, "owned_by": "grok-cli"}
                    for m in Config.models
                ],
            })
            return
        self._error(404, f"Unknown path: {path}")

    def do_POST(self):
        path = self._path()
        mcp_match = re.match(r"^/mcp/([0-9a-f]{8})$", path)
        if mcp_match:
            # grok 的 MCP 客户端不带我们的 Bearer；token 即会话凭据，仅监听回环。
            self._handle_mcp(mcp_match.group(1))
            return
        if not path.endswith("/chat/completions"):
            self._error(404, f"Unknown path: {path}（仅支持 /v1/chat/completions）")
            return
        if not self._check_auth():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._error(400, "请求体缺失或过大")
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._error(400, "请求体不是合法 JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "请求体必须是 JSON 对象")
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error(400, "messages 不能为空")
            return
        model = str(payload.get("model") or "").strip()
        stream = bool(payload.get("stream"))

        tools = payload.get("tools")
        use_acp = Config.acp_enabled and isinstance(tools, list) and bool(tools)

        prompt_preview = json.dumps(messages, ensure_ascii=False)
        try:
            if use_acp:
                self._handle_acp_chat(model, messages, tools, stream, prompt_preview)
            elif stream:
                self._handle_stream(model, messages, prompt_preview)
            else:
                self._handle_blocking(model, messages, prompt_preview)
        except (BrokenPipeError, ConnectionError):
            # 客户端断开：headless 路径由 run_cli_turn 的 finally 清理；
            # ACP 路径在各自循环里已 cancel + drop。
            pass

    # -- MCP server（供 grok ACP 会话回连） ---------------------------------

    def _handle_mcp(self, token: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= MAX_BODY_BYTES else b""
            msg = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._json_response(400, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"},
            })
            return
        if not isinstance(msg, dict):
            msg = {}
        method = str(msg.get("method") or "")
        rpc_id = msg.get("id")

        # 通知（无 id）：initialized 等，202 收下即可。
        if rpc_id is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        def reply(result: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None):
            out: Dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
            if error is not None:
                out["error"] = error
            else:
                out["result"] = result if result is not None else {}
            self._json_response(200, out)

        if method == "initialize":
            params = msg.get("params") or {}
            reply({
                "protocolVersion": params.get("protocolVersion") or "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "heysure-gateway", "version": "1.0"},
            })
            return
        if method == "ping":
            reply({})
            return

        sess = ACP_REGISTRY.get(token)
        if sess is None or sess.closed:
            reply(error={"code": -32000, "message": "unknown or expired session"})
            return
        if method == "tools/list":
            reply({"tools": sess.mcp_tools_list()})
            return
        if method == "tools/call":
            params = msg.get("params") or {}
            name = str(params.get("name") or "").strip()
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                pc = sess.mcp_tools_call(name, arguments, timeout=float(Config.session_ttl))
            except acp_bridge.AcpError as exc:
                reply(error={"code": -32000, "message": str(exc)})
                return
            reply({
                "content": [{"type": "text", "text": pc.result_text}],
                "isError": bool(pc.is_error),
            })
            return
        reply(error={"code": -32601, "message": f"method not found: {method}"})

    # -- 推理 ---------------------------------------------------------------

    def _handle_blocking(self, model: str, messages: List[Dict], prompt_preview: str) -> None:
        reasoning_parts: List[str] = []
        text_parts: List[str] = []
        try:
            for kind, data in run_cli_turn(model, messages):
                (reasoning_parts if kind == "thought" else text_parts).append(data)
        except RuntimeError as exc:
            self._error(500, str(exc), "server_error")
            return
        seen_calls: set = set()
        content = _dedup_mcp_calls(normalize_tool_markup("".join(text_parts)), seen_calls)
        # grok 可能把决定好的工具调用只写在思考里；服务端只解析 content，故把思考
        # 里的完整私有块抽出、改写为 <mcp-call> 并入 content（与正文去重）。
        reasoning, promoted = _promote_thought_tool_calls("".join(reasoning_parts))
        promoted = _dedup_mcp_calls(promoted, seen_calls)
        if promoted:
            content = f"{content}\n{promoted}".strip() if content else promoted
        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        self._json_response(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or (Config.models[0] if Config.models else "grok"),
            "system_fingerprint": GATEWAY_FINGERPRINT,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": _usage(prompt_preview, content + reasoning),
        })

    def _handle_stream(self, model: str, messages: List[Dict], prompt_preview: str) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model_name = model or (Config.models[0] if Config.models else "grok")

        def chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None,
                  usage: Optional[Dict[str, int]] = None) -> bytes:
            obj: Dict[str, Any] = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "system_fingerprint": GATEWAY_FINGERPRINT,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if usage is not None:
                obj["usage"] = usage
            return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        self.wfile.write(chunk({"role": "assistant", "content": ""}))
        self.wfile.flush()

        normalizer = StreamingToolMarkupNormalizer()
        thought_normalizer = StreamingThoughtNormalizer()
        seen_calls: set = set()
        collected = ""
        reasoning_all = ""
        error_text = ""

        def emit_content(text: str) -> None:
            nonlocal collected
            text = _dedup_mcp_calls(text, seen_calls)
            if text:
                collected += text
                self.wfile.write(chunk({"content": text}))
                self.wfile.flush()

        def emit_reasoning(text: str) -> None:
            nonlocal reasoning_all
            if text:
                reasoning_all += text
                self.wfile.write(chunk({"reasoning_content": text}))
                self.wfile.flush()

        try:
            for kind, data in run_cli_turn(model, messages):
                if kind == "thought":
                    # 思考流里落着的私有工具块会被抽出，改走 content（否则服务端
                    # 看不到工具调用，整轮被判为最终回答而中断）。
                    reasoning_out, content_out = thought_normalizer.feed(data)
                    emit_reasoning(reasoning_out)
                    emit_content(content_out)
                    continue
                emit_content(normalizer.feed(data))
        except RuntimeError as exc:
            # SSE 头已发出，无法改状态码；把错误作为正文增量给到调用方。
            error_text = f"\n[grok-cli-gateway 错误] {exc}"

        thought_reasoning, thought_content = thought_normalizer.flush()
        emit_reasoning(thought_reasoning)
        emit_content(thought_content)
        emit_content(normalizer.flush())
        if error_text:
            self.wfile.write(chunk({"content": error_text}))
            self.wfile.flush()

        self.wfile.write(chunk({}, finish_reason="stop", usage=_usage(prompt_preview, collected + reasoning_all)))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # -- ACP 桥接（方案 B）---------------------------------------------------

    def _handle_acp_chat(
        self,
        model: str,
        messages: List[Dict],
        tools: List[Dict],
        stream: bool,
        prompt_preview: str,
    ) -> None:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        tools_registry = acp_bridge.tools_registry_from_payload(tools)
        temp_paths: List[str] = []
        token, raw_results, tail_msgs = acp_bridge.extract_resume_info(messages)

        # 恢复路径：tool_call id 里的会话 token 命中且挂起调用齐全 → 作答续跑。
        sess = None
        if token:
            cand = ACP_REGISTRY.get(token)
            if (
                cand is not None
                and not cand.closed
                and (not model or cand.model == model)
                and cand.busy.acquire(blocking=False)
            ):
                results = {
                    cid: _content_to_text(value, temp_paths)
                    for cid, value in raw_results.items()
                }
                if cand.matches_results(results):
                    cand.update_tools(tools_registry)
                    cand.adopt_temp_paths(temp_paths)
                    cand.answer_calls(results, _serialize_tail(tail_msgs, temp_paths))
                    sess = cand
                else:
                    cand.busy.release()

        # 新会话路径：首轮，或恢复失败（网关重启/会话过期/上下文被重写）→ 全量重放。
        if sess is None:
            temp_paths = []
            try:
                argv = _resolve_cli_argv()
                prompt_text = ACP_SYSTEM_WRAPPER + "\n\n" + _serialize_convo(messages, temp_paths)
                sess = acp_bridge.AcpSession.create(
                    exe=argv[0],
                    model=model,
                    tools=tools_registry,
                    mcp_url_base=f"http://{Config.host}:{Config.port}/mcp",
                    cwd=RUNTIME_DIR,
                    registry=ACP_REGISTRY,
                )
            except (RuntimeError, acp_bridge.AcpError) as exc:
                _unlink_quietly(temp_paths)
                print(f"[acp] 会话创建失败，回退 headless 路径：{exc}")
                if stream:
                    self._handle_stream(model, messages, prompt_preview)
                else:
                    self._handle_blocking(model, messages, prompt_preview)
                return
            sess.adopt_temp_paths(temp_paths)
            sess.start_turn(prompt_text)

        if stream:
            self._acp_stream(sess, model, prompt_preview)
        else:
            self._acp_blocking(sess, model, prompt_preview)

    def _acp_pump(self, sess, on_text, on_thought):
        """消费会话事件直到本请求可以收尾。

        返回 ``("tools", [PendingToolCall])`` / ``("end", stop_reason)`` /
        ``("error", message)``。首个工具调用到达后再等 ``tool_grace`` 收集同批
        的其它调用；期间的 text/thought 照常流出。
        """
        deadline = time.time() + Config.timeout
        while True:
            try:
                kind, data = sess.queue.get(timeout=1.0)
            except Empty:
                if time.time() > deadline:
                    return "error", f"ACP 推理超时（超过 {Config.timeout} 秒）"
                continue
            if kind == "thought":
                on_thought(data)
                continue
            if kind == "text":
                on_text(data)
                continue
            if kind == "mcp_call":
                calls = [data]
                end_at = time.time() + Config.tool_grace
                while True:
                    remaining = end_at - time.time()
                    if remaining <= 0:
                        break
                    try:
                        k2, d2 = sess.queue.get(timeout=remaining)
                    except Empty:
                        break
                    if k2 == "mcp_call":
                        calls.append(d2)
                        end_at = time.time() + Config.tool_grace
                    elif k2 == "thought":
                        on_thought(d2)
                    elif k2 == "text":
                        on_text(d2)
                    else:
                        # end/error 塞回队列，先把已收的调用批次上报。
                        sess.queue.put((k2, d2))
                        break
                return "tools", calls
            if kind == "end":
                return "end", data
            if kind == "error":
                return "error", data

    @staticmethod
    def _tool_calls_payload(calls) -> List[Dict[str, Any]]:
        return [
            {
                "index": index,
                "id": pc.call_id,
                "type": "function",
                "function": {
                    "name": pc.name,
                    "arguments": json.dumps(pc.arguments, ensure_ascii=False),
                },
            }
            for index, pc in enumerate(calls)
        ]

    def _acp_park(self, sess) -> None:
        """工具批次已上报：会话原地等待 HeySure 送回结果。"""
        ACP_REGISTRY.touch(sess)
        try:
            sess.busy.release()
        except RuntimeError:
            pass

    def _acp_stream(self, sess, model: str, prompt_preview: str) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model_name = model or (Config.models[0] if Config.models else "grok")

        def chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None,
                  usage: Optional[Dict[str, int]] = None) -> bytes:
            obj: Dict[str, Any] = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "system_fingerprint": GATEWAY_FINGERPRINT,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if usage is not None:
                obj["usage"] = usage
            return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(chunk({"role": "assistant", "content": ""}))
        self.wfile.flush()

        normalizer = StreamingToolMarkupNormalizer()
        thought_norm = StreamingThoughtNormalizer()
        seen_calls: set = set()
        collected = ""
        reasoning_all = ""

        def emit_content(text: str) -> None:
            nonlocal collected
            text = _dedup_mcp_calls(text, seen_calls)
            if text:
                collected += text
                self.wfile.write(chunk({"content": text}))
                self.wfile.flush()

        def emit_reasoning(text: str) -> None:
            nonlocal reasoning_all
            if text:
                reasoning_all += text
                self.wfile.write(chunk({"reasoning_content": text}))
                self.wfile.flush()

        def on_text(data: str) -> None:
            emit_content(normalizer.feed(data))

        def on_thought(data: str) -> None:
            reasoning_out, content_out = thought_norm.feed(data)
            emit_reasoning(reasoning_out)
            emit_content(content_out)

        try:
            outcome, data = self._acp_pump(sess, on_text, on_thought)
            reasoning_out, content_out = thought_norm.flush()
            emit_reasoning(reasoning_out)
            emit_content(content_out)
            emit_content(normalizer.flush())
            usage = _usage(prompt_preview, collected + reasoning_all)

            if outcome == "tools":
                self.wfile.write(chunk({"tool_calls": self._tool_calls_payload(data)}))
                self.wfile.write(chunk({}, finish_reason="tool_calls", usage=usage))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self._acp_park(sess)
                return

            if outcome == "error":
                emit_content(f"\n[grok-cli-gateway 错误] {data}")
                usage = _usage(prompt_preview, collected + reasoning_all)
            self.wfile.write(chunk({}, finish_reason="stop", usage=usage))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            ACP_REGISTRY.drop(sess)
        except (BrokenPipeError, ConnectionError):
            sess.cancel_turn()
            ACP_REGISTRY.drop(sess)
            raise

    def _acp_blocking(self, sess, model: str, prompt_preview: str) -> None:
        text_parts: List[str] = []
        thought_parts: List[str] = []
        try:
            outcome, data = self._acp_pump(
                sess, text_parts.append, thought_parts.append
            )
        except (BrokenPipeError, ConnectionError):
            sess.cancel_turn()
            ACP_REGISTRY.drop(sess)
            raise

        seen_calls: set = set()
        content = _dedup_mcp_calls(normalize_tool_markup("".join(text_parts)), seen_calls)
        reasoning, promoted = _promote_thought_tool_calls("".join(thought_parts))
        promoted = _dedup_mcp_calls(promoted, seen_calls)
        if promoted:
            content = f"{content}\n{promoted}".strip() if content else promoted

        message: Dict[str, Any] = {"role": "assistant", "content": content or None}
        if reasoning:
            message["reasoning_content"] = reasoning
        finish_reason = "stop"

        if outcome == "tools":
            message["tool_calls"] = self._tool_calls_payload(data)
            finish_reason = "tool_calls"
        elif outcome == "error":
            message["content"] = f"{content}\n[grok-cli-gateway 错误] {data}".strip()

        self._json_response(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or (Config.models[0] if Config.models else "grok"),
            "system_fingerprint": GATEWAY_FINGERPRINT,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": _usage(prompt_preview, (message.get("content") or "") + reasoning),
        })
        if outcome == "tools":
            self._acp_park(sess)
        else:
            ACP_REGISTRY.drop(sess)


def main() -> None:
    # stdout 重定向到日志文件时 Python 默认块缓冲，日志会长期看似为空；强制行缓冲
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="grok CLI → OpenAI 兼容本地 API 网关")
    parser.add_argument("--command", default=None, help="CLI 命令或完整路径（默认取 GROK_CLI_COMMAND / grok）")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8100）")
    parser.add_argument("--timeout", type=int, default=None, help="单次推理超时秒数（默认 600）")
    parser.add_argument("--api-key", default=None, help="可选：要求 Bearer 鉴权的 key")
    parser.add_argument("--models", default=None, help="/v1/models 展示的模型 id（逗号分隔）")
    args = parser.parse_args()

    if args.command:
        Config.command = args.command
    if args.host:
        Config.host = args.host
    if args.port:
        Config.port = args.port
    if args.timeout:
        Config.timeout = args.timeout
    if args.api_key is not None:
        Config.api_key = args.api_key.strip()
    if args.models:
        Config.models = [m.strip() for m in args.models.split(",") if m.strip()]

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    try:
        argv = _resolve_cli_argv()
        print(f"CLI 命令：{argv[0]}")
    except RuntimeError as exc:
        print(f"警告：{exc}（服务照常启动，请求到达时会再次检查）")

    ACP_REGISTRY.configure(float(Config.session_ttl), Config.max_sessions)
    print(
        f"ACP 桥接：{'启用（请求携带 tools[] 时走 agent 会话 + 真实工具调用）' if Config.acp_enabled else '禁用（GROK_CLI_ACP=0）'}"
    )

    server = ThreadingHTTPServer((Config.host, Config.port), Handler)
    server.daemon_threads = True
    print(f"grok-cli-gateway 监听 http://{Config.host}:{Config.port}/v1/chat/completions")
    print(f"模型预设 Base URL 填：http://{Config.host}:{Config.port}/v1/chat/completions，API Key 任意非空值")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
