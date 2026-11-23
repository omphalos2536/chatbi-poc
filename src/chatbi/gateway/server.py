"""私有 Unix socket 上的本地模型网关服务（步骤 02）。

* 只监听 ``var/run/gateway.sock``（目录/文件权限 0700/0600，仅属主可连）。
* 调用方必须在 ``config/gateway.yaml`` 的 ``callers`` 里登记，且只能使用登记时的用途；
  请求里的 ``base_url`` / ``model`` / ``headers`` / ``authorization`` / ``key`` /
  ``messages`` / ``prompt`` 等一律拒收（``ContractError``），不会传给客户端。
* 密钥不经过 socket：服务进程只从自己的环境变量 ``CHATBI_LLM_API_KEY`` 读取；启动器
  （CLI）在拉起服务前清空其它服务相关的密钥变量，并在结束时恢复调用方环境。
* 一次连接一问一答（请求：一行 JSON；响应：一行 JSON），然后关闭连接；并发上限由配置
  固定。审计写入 ``var/audit/gateway.jsonl``（不含正文与密钥）。

本模块只做“转发 + 授权 + 审计”，请求重建（system/字典形状/大小上限）仍由
:mod:`chatbi.gateway.client` 与 :mod:`chatbi.gateway.policy` 完成。
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path
from typing import Any, Mapping

from chatbi import config as app_config
from chatbi.gateway import client as gw_client
from chatbi.gateway import policy
from chatbi.gateway.policy import (
    AuditLog,
    CallerNotAllowed,
    ContractError,
    GatewayConfig,
    GatewayError,
)

#: 协议版本：请求/响应里的 ``v`` 字段。
PROTOCOL = 1
#: 单行请求/响应的上限（请求正文最多 64 KiB，响应摘要很小）。
MAX_LINE = 65536

#: 请求里绝不允许出现的顶层键（调用方不得指定端点、凭据或任意消息）。
FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "base_url",
        "url",
        "endpoint",
        "model",
        "headers",
        "authorization",
        "api_key",
        "apikey",
        "key",
        "token",
        "password",
        "secret",
        "messages",
        "prompt",
        "system",
        "temperature",
        "max_tokens",
        "stream",
        "proxy",
        "verify",
    }
)
ALLOWED_REQUEST_KEYS = frozenset({"v", "caller", "purpose", "question", "dictionary"})


def socket_path(cfg: GatewayConfig, root: Path | None = None) -> Path:
    base = root if root is not None else app_config.LAYOUT.root
    return base / cfg.server_socket


def audit_path(root: Path | None = None) -> Path:
    base = root if root is not None else app_config.LAYOUT.root
    return base / "var" / "audit" / "gateway.jsonl"


# ------------------------------------------------------------------ 处理 --


class _ProtocolError(GatewayError):
    kind = "protocol"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, detail={"code": code})
        self.code = code


def validate_request(cfg: GatewayConfig, request: Any) -> tuple[str, str, str, Mapping[str, Any]]:
    """校验本地请求并返回 ``(caller, purpose, question, dictionary)``。"""
    if not isinstance(request, dict):
        raise _ProtocolError("bad_request", "请求必须是 JSON 对象")
    forbidden = set(request) & FORBIDDEN_REQUEST_KEYS
    if forbidden:
        raise ContractError(
            "forbidden_request_key",
            "调用方不得指定端点、凭据或任意消息（已拒收）",
            count=len(forbidden),
        )
    unknown = set(request) - ALLOWED_REQUEST_KEYS
    if unknown:
        raise ContractError("unknown_request_key", "请求含协议外的键（已拒收）", count=len(unknown))
    version = request.get("v", PROTOCOL)
    if version != PROTOCOL:
        raise _ProtocolError("bad_version", "协议版本不符")
    caller = request.get("caller")
    if not isinstance(caller, str) or not caller:
        raise _ProtocolError("missing_caller", "请求缺少 caller")
    rule = cfg.caller(caller)
    purpose = request.get("purpose", rule.purpose)
    if purpose != rule.purpose:
        raise CallerNotAllowed("调用方不得使用登记之外的用途")
    question = request.get("question")
    if not isinstance(question, str):
        raise _ProtocolError("missing_question", "请求缺少 question")
    dictionary = request.get("dictionary")
    if not isinstance(dictionary, dict):
        raise _ProtocolError("missing_dictionary", "请求缺少 dictionary")
    extra_keys = set(dictionary) - rule.dictionary_keys
    if extra_keys:
        raise ContractError(
            "dictionary_key_not_allowed",
            "调用方使用了本地配置未授权的字典键（已拒收）",
            count=len(extra_keys),
        )
    return caller, purpose, question, dictionary


def handle_request(cfg: GatewayConfig, request: Any, *, audit: AuditLog | None = None) -> dict[str, Any]:
    """处理一个请求并返回响应 dict（不抛异常：错误也写成 ``{"ok": false, ...}``）。"""
    try:
        caller, purpose, question, dictionary = validate_request(cfg, request)
        with gw_client.GatewayClient(
            purpose=purpose, config=cfg, audit=audit, caller=caller
        ) as client:
            result = client.call(question, dictionary)
        response = {"ok": result.ok, "v": PROTOCOL, **result.summary()}
        if result.ok and result.parsed is not None:
            response["parsed"] = result.parsed
        return response
    except GatewayError as exc:
        return {"ok": False, "v": PROTOCOL, "error": exc.as_dict()}
    except Exception as exc:  # pragma: no cover - 兜底：只报类别，不报原文
        return {"ok": False, "v": PROTOCOL, "error": {"kind": "internal", "message": type(exc).__name__}}


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # pragma: no cover - 由测试覆盖（真实 socket）
        server: "GatewayServer" = self.server  # type: ignore[assignment]
        sock = self.request
        sock.settimeout(server.request_timeout)
        buffer = b""
        try:
            while b"\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_LINE:
                    server.write_line(sock, {"ok": False, "v": PROTOCOL, "error": {"kind": "protocol", "code": "too_large", "message": "请求过大"}})
                    return
            line = buffer.split(b"\n", 1)[0].strip()
            if not line:
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                server.write_line(sock, {"ok": False, "v": PROTOCOL, "error": {"kind": "protocol", "code": "invalid_json", "message": "请求不是合法 JSON"}})
                return
            response = handle_request(server.config, request, audit=server.audit)
            server.write_line(sock, response)
        except (OSError, socket.timeout):
            return


class GatewayServer(socketserver.ThreadingUnixStreamServer):
    """单实例的 Unix socket 网关。"""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        cfg: GatewayConfig,
        *,
        path: Path | None = None,
        audit: AuditLog | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self.config = cfg
        self.path = Path(path) if path is not None else socket_path(cfg)
        self.audit = audit if audit is not None else AuditLog(audit_path())
        self.request_timeout = request_timeout or cfg.server_request_timeout
        self._sem = threading.BoundedSemaphore(cfg.server_max_connections)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        if self.path.exists() or self.path.is_symlink():
            # 只清理“上次遗留的 socket 文件”，绝不删除普通文件或目录。
            st = self.path.lstat()
            if not stat.S_ISSOCK(st.st_mode):
                raise GatewayError("socket 路径已被非 socket 文件占用，拒绝覆盖")
            self.path.unlink()
        super().__init__(str(self.path), _Handler)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # -- 并发上限 --
    def process_request(self, request: Any, client_address: Any) -> None:  # noqa: D401
        if not self._sem.acquire(blocking=False):
            try:
                self.write_line(
                    request,
                    {"ok": False, "v": PROTOCOL, "error": {"kind": "busy", "message": "并发已满"}},
                )
            finally:
                return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._sem.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()

    @staticmethod
    def write_line(sock: Any, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            sock.sendall(data)
        except OSError:
            pass

    # -- 生命周期 --
    def serve_in_thread(self) -> "GatewayServer":
        self._thread = threading.Thread(target=self.serve_forever, name="chatbi-gateway", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=5)
        self.server_close()
        try:
            if self.path.exists() and stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ 客户端 --


def call(
    cfg: GatewayConfig,
    *,
    caller: str,
    question: str,
    dictionary: Mapping[str, Any],
    purpose: str | None = None,
    path: Path | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """通过 Unix socket 调用本地网关（调用方只能给 caller/question/dictionary）。"""
    target = Path(path) if path is not None else socket_path(cfg)
    rule = cfg.caller(caller)
    payload: dict[str, Any] = {
        "v": PROTOCOL,
        "caller": caller,
        "purpose": purpose or rule.purpose,
        "question": question,
        "dictionary": dict(dictionary),
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(target))
        sock.sendall(data)
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
    line = buffer.split(b"\n", 1)[0]
    if not line:
        raise GatewayError("本地网关没有返回响应")
    return json.loads(line.decode("utf-8"))


def sanitized_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """服务进程环境：保留真正的密钥变量，清掉其它服务相关的凭据类变量。"""
    env = dict(base if base is not None else os.environ)
    keep = {app_config.LLM_KEY_ENV}
    for key in list(env):
        upper = key.upper()
        if key in keep:
            continue
        if (
            "API_KEY" in upper
            or "APIKEY" in upper
            or upper.endswith("_TOKEN")
            or upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            or "PASSWORD" in upper
            or upper.endswith("_SECRET")
        ):
            env.pop(key, None)
    return env
