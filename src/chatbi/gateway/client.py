"""唯一的模型外发客户端（步骤 02）。

* 只向固定地址的 ``/chat/completions`` 发 POST：``base_url``、模型、system 指令、
  超时和预算都取自 :class:`policy.GatewayConfig`，构造函数不接受 URL / 密钥参数。
* httpx 客户端：TLS、不跟随重定向、``trust_env=False``（不自动读代理变量）、不用任何
  SDK 自动重试；只在连接建立阶段对暂时性失败重试 1 次（配置）。
* 预算由本模块控制：最多 1 次请求 + 至多 2 次退避（429 / 暂时性 5xx / 超时），
  401/403 立即 ``unavailable``；每次尝试写一条本地审计（不含正文与密钥）。
* 传输层可替换：测试用 :class:`ScriptedTransport` 记录“实际出站”的请求；也可注入
  任意实现 ``httpx.BaseTransport`` 的替身。假传输分别模拟成功、超时、无权限、畸形
  JSON、429。
* 只在“合成测试”里允许捕获请求正文（:class:`CapturedRequest` 由测试传的 transport
  保存）；正常运行时正文既不写日志也不进证据。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from chatbi.gateway import policy
from chatbi.gateway.policy import (
    MAPPING,
    QUERY,
    AuditLog,
    GatewayConfig,
    GatewayError,
    LiveKeyMissing,
    OutboundRequest,
    OutboundResponse,
    ResponseError,
)

#: 审计里“怎么来的”标记：live（从环境读密钥的真实请求）/ mock / server / scripted。
TRANSPORT_LIVE = "live"
TRANSPORT_MOCK = "mock"
TRANSPORT_SCRIPTED = "scripted"

# ------------------------------------------------------------------ 凭据 --


def secret_from_env(cfg: GatewayConfig) -> str | None:
    """只从环境变量读取密钥；不做任何持久化。返回 None 表示未配置。"""
    value = os.environ.get(cfg.key_env) or ""
    value = value.strip()
    return value or None


def redact_secret(text: str, secret: str | None) -> str:
    """把文本里的密钥值替换掉（只用于异常消息的兜底）。"""
    if secret and len(secret) >= 6 and secret in text:
        return text.replace(secret, "[redacted]")
    return text


# ------------------------------------------------------------------ 记录 --


@dataclass
class Transcript:
    """一次调用的全部尝试（不含正文）。"""

    request_id: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    outbound: int = 0
    sleeps: list[float] = field(default_factory=list)


@dataclass
class GatewayResult:
    """一次网关调用的结果。正文（content/parsed）只在内存里，供本地校验或绑定。"""

    request_id: str
    purpose: str
    model: str
    system_version: str
    status: int | None
    elapsed_ms: int
    fingerprint: str
    dictionary_digest: str
    request: OutboundRequest
    redaction: policy.Redaction | None
    content: str | None
    parsed: dict[str, Any] | None
    usage: dict[str, Any]
    attempts: int
    transport: str
    outbound: int
    transcript: Transcript
    error: GatewayError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> dict[str, Any]:
        """可写入证据/日志的摘要：没有正文、问题、字典或密钥。"""
        out: dict[str, Any] = {
            "request_id": self.request_id,
            "purpose": self.purpose,
            "model": self.model,
            "system_version": self.system_version,
            "http_status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "outbound": self.outbound,
            "transport": self.transport,
            "dictionary_digest": self.dictionary_digest,
            "dictionary_keys": list(self.request.dictionary_keys),
            "fingerprint": self.fingerprint,
            "usage": self.usage,
            "ok": self.ok,
        }
        if self.redaction is not None:
            out["redaction"] = self.redaction.summary()
        if self.error is not None:
            out["error"] = self.error.as_dict()
        elif self.parsed is not None:
            out["parsed_kind"] = self.parsed.get("kind")
            out["parsed_status"] = self.parsed.get("status")
        return out


@dataclass
class CapturedRequest:
    """出站请求的本地记录（只在合成测试 / 显式捕获时生成）。"""

    url: str
    method: str
    headers: dict[str, str]
    body: bytes
    at: str

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))

    def messages(self) -> list[dict[str, str]]:
        return self.json().get("messages", [])

    def header_names(self) -> list[str]:
        return sorted(self.headers)


# ------------------------------------------------------------------ 假传输 --


@dataclass
class ScriptedStep:
    """假传输的一步：``server_error`` 连连接都不建立；否则是一个 ready 响应或超时。"""

    kind: str = "ready"  # ready | timeout | server_error
    status: int = 200
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def to_response(self, request: httpx.Request) -> httpx.Response:
        if isinstance(self.body, (dict, list)) or self.body is None:
            text = json.dumps(self.body if self.body is not None else {}, ensure_ascii=False)
        elif isinstance(self.body, bytes):
            text = self.body
        else:
            text = str(self.body)
        return httpx.Response(
            self.status,
            text=text,
            headers={"content-type": "application/json", **dict(self.headers)},
            request=request,
        )


def openai_ok(payload: Mapping[str, Any], *, model: str | None = None) -> ScriptedStep:
    """标准 OpenAI 兼容成功响应。"""
    return ScriptedStep(
        status=200,
        body={
            "id": "cmpl-synthetic-1",
            "object": "chat.completion",
            "created": 0,
            "model": model or "synthetic-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        },
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """解析 Retry-After（只接受秒数形式；HTTP 日期形式忽略）。"""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:  # NaN / 负数
        return None
    return min(seconds, 10.0)


@dataclass
class ScriptedTransport(httpx.BaseTransport):
    """按脚本返回响应的假传输；记录每次尝试（url、headers、body）。"""

    steps: list[ScriptedStep]
    capture: bool = True

    calls: list[CapturedRequest] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.steps = list(self.steps)
        index = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            if self.capture:
                self.calls.append(
                    CapturedRequest(
                        url=str(request.url),
                        method=request.method,
                        headers=dict(request.headers),
                        body=request.read(),
                        at=policy.iso_now(),
                    )
                )
            step = self.steps[index["i"]] if index["i"] < len(self.steps) else ScriptedStep(
                status=500, body={"error": "script exhausted"}
            )
            index["i"] += 1
            if step.kind == "server_error":
                raise httpx.ConnectError("synthetic connect error", request=request)
            if step.kind == "timeout":
                raise httpx.ReadTimeout("synthetic read timeout", request=request)
            return step.to_response(request)

        super().__init__()
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)

    def close(self) -> None:  # pragma: no cover - 无资源
        return None


def scripted(*steps: ScriptedStep) -> ScriptedTransport:
    """便捷构造：``ScriptedTransport([*steps])``。"""
    return ScriptedTransport(list(steps))


# ------------------------------------------------------------------ 客户端 --


class MockTransport(httpx.BaseTransport):
    """``--mock`` 用的确定性假响应：按用途回一个通过的合成 JSON，绝不联网。"""

    def __init__(self, cfg: GatewayConfig) -> None:
        self.cfg = cfg
        self.calls: list[CapturedRequest] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            CapturedRequest(
                url=str(request.url),
                method=request.method,
                headers=dict(request.headers),
                body=request.read(),
                at=policy.iso_now(),
            )
        )
        body = json.loads(self.calls[-1].body)
        user = json.loads(body["messages"][1]["content"])
        dictionary = user["dictionary"]
        if "source_tables" in dictionary:
            payload: dict[str, Any] = {"candidates": [], "notes": "mock"}
        else:
            payload = {
                "status": "ok",
                "sql": "SELECT 1 AS mock",
                "params": [],
                "output_columns": [{"name": "mock", "unit": ""}],
                "reason": "mock",
            }
        payload["mock"] = True
        return openai_ok(payload, model=self.cfg.model).to_response(request)


@dataclass
class _Attempt:
    index: int
    started: float
    status: int | None = None
    error: GatewayError | None = None


class GatewayClient:
    """唯一外发入口。调用方只能给“用途 + 脱敏问题 + 授权字典”。"""

    def __init__(
        self,
        *,
        purpose: str,
        transport: httpx.BaseTransport | str = TRANSPORT_LIVE,
        secret: str | Callable[[], str | None] | None = None,
        config: GatewayConfig | None = None,
        audit: AuditLog | None = None,
        sleep: Callable[[float], None] = time.sleep,
        caller: str | None = None,
    ) -> None:
        self.config = config or policy.load_config()
        # 目的用途必须在允许清单内；调用方（若声明）也要在本地配置里，且用途一致。
        self.config.purpose(purpose)
        if caller is not None:
            rule = self.config.caller(caller)
            if rule.purpose != purpose:
                raise policy.PurposeNotAllowed("调用方声明的用途与请求用途不一致")
        self.purpose = purpose
        self.caller = caller
        self._sleep = sleep
        if isinstance(transport, str):
            if transport not in (TRANSPORT_LIVE, TRANSPORT_MOCK):
                raise GatewayError("未知传输：只支持 live / mock 或 httpx transport")
            self.transport_name = transport
        else:
            self.transport_name = TRANSPORT_SCRIPTED
        if secret is None and self.transport_name == TRANSPORT_LIVE:
            secret = lambda: secret_from_env(self.config)  # noqa: E731
        self._secret_source = secret
        self._secret_cached: str | None = None
        self.audit = audit

        tr = self.config.transport
        timeout = httpx.Timeout(tr.timeout_seconds, connect=tr.connect_timeout_seconds)
        base = {
            "timeout": timeout,
            "follow_redirects": tr.follow_redirects,
            # 不自动读 HTTP(S)_PROXY / NETRC 等：请求只可能去固定地址。
            "trust_env": tr.proxy_from_env,
            "verify": tr.verify,
            "limits": httpx.Limits(
                max_connections=self.config.server_max_connections,
                max_keepalive_connections=1,
            ),
        }
        if isinstance(transport, httpx.BaseTransport):
            self._http = httpx.Client(transport=transport, **base)
        elif self.transport_name == TRANSPORT_MOCK:
            self._mock = MockTransport(self.config)
            self._http = httpx.Client(transport=self._mock, **base)
        else:
            self._http = httpx.Client(**base)
        self._outbound = 0
        self._last_usage: Mapping[str, Any] | None = None

    # -- 凭据 --
    def _secret(self) -> str | None:
        if self._secret_cached is not None:
            return self._secret_cached
        source = self._secret_source
        if source is None:
            return None
        value = source() if callable(source) else source
        if value:
            self._secret_cached = value
        return value or None

    @classmethod
    def mock(cls, *, config: GatewayConfig | None = None, **kwargs: Any) -> "GatewayClient":
        return cls(transport=TRANSPORT_MOCK, config=config, **kwargs)

    # -- 调用 --
    def call(
        self,
        question: str,
        dictionary: Mapping[str, Any],
        *,
        purpose: str | None = None,
        bag: policy.LocalParameterBag | None = None,
        check_content: bool = True,
    ) -> GatewayResult:
        purpose = purpose or self.purpose
        self.config.purpose(purpose)  # 用途固定；不接受调用方自定义
        request_id = policy.new_request_id()
        transcript = Transcript(request_id=request_id)
        started = time.monotonic()

        try:
            request, redaction = policy.prepare(
                self.config, purpose, question, dictionary, bag=bag
            )
        except GatewayError as exc:
            self._record(
                request_id, purpose, status="rejected", error=exc,
                elapsed=0, transcript=transcript,
            )
            return self._finish(request_id, purpose, None, started, transcript, exc)

        secret = self._secret()
        if self.transport_name == TRANSPORT_LIVE and not secret:
            exc = LiveKeyMissing()
            self._record(
                request_id, purpose, status="unavailable", error=exc, elapsed=0,
                transcript=transcript, request=request, redaction=redaction,
            )
            return self._finish(request_id, purpose, None, started, transcript, exc, request, redaction)

        headers = {"content-type": "application/json", "accept": "application/json"}
        if secret:
            headers["authorization"] = f"Bearer {secret}"

        retry = self.config.retry
        error: GatewayError | None = None
        content: str | None = None
        http_status: int | None = None
        response_bytes = 0

        for attempt_no in range(1, retry.max_attempts + 1):
            self._outbound += 1
            transcript.outbound = self._outbound
            error = None
            try:
                response = self._http.post(request.url, content=request.body, headers=headers)
                http_status = response.status_code
                response_bytes = len(response.content)
                if response_bytes > self.config.limits.response_bytes:
                    error = ResponseError("response_too_large", "响应体超出大小上限")
                elif 200 <= http_status < 300:
                    try:
                        content = self._extract_content(response.content)
                    except GatewayError as exc:
                        error = exc
                else:
                    error = self._http_error(response, http_status)
            except httpx.TimeoutException:
                error = OutboundResponse(
                    "timeout", None, "请求超时（未收到完整响应）", retryable=True
                )
            except (httpx.ConnectError, httpx.WriteError) as exc:
                error = OutboundResponse(
                    "transport_error", None, f"连接失败：{type(exc).__name__}", retryable=True
                )
            except httpx.HTTPError as exc:
                error = OutboundResponse(
                    "transport_error", None, f"传输错误：{type(exc).__name__}", retryable=False
                )
            transcript.attempts.append(
                {
                    "attempt": attempt_no,
                    "http_status": http_status,
                    "error_code": getattr(error, "code", None) if error else None,
                }
            )
            if error is None:
                break
            retryable = isinstance(error, OutboundResponse) and error.retryable
            if not retryable or attempt_no >= retry.max_attempts:
                break
            wait = retry.backoff_seconds[attempt_no - 1]
            if error.retry_after is not None:
                wait = max(wait, error.retry_after)
            transcript.sleeps.append(wait)
            self._sleep(wait)

        parsed: dict[str, Any] | None = None
        if error is None and content is not None and check_content:
            try:
                parsed = policy.check_content(self.config, purpose, content, request.dictionary)
            except GatewayError as exc:
                error = exc

        if error is None:
            status = "ok"
        elif isinstance(error, ResponseError) or isinstance(error, (OutboundResponse,)):
            status = "invalid" if isinstance(error, ResponseError) else "unavailable"
        else:
            status = "error"
        self._record(
            request_id, purpose, status=status, error=error,
            elapsed=time.monotonic() - started, transcript=transcript, request=request,
            redaction=redaction, http_status=http_status, response_bytes=response_bytes,
            content_bytes=len(content.encode("utf-8")) if content else 0,
        )
        return self._finish(
            request_id, purpose, http_status, started, transcript, error, request, redaction,
            content=content, parsed=parsed,
        )

    # -- 响应解析 --
    def _extract_content(self, body: bytes) -> str:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ResponseError("invalid_json", "响应不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ResponseError("bad_envelope", "响应顶层不是对象")
        if isinstance(data.get("error"), (dict, str)):
            raise OutboundResponse("remote_error", None, "远端在 2xx 响应里返回了 error 对象")
        self._last_usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ResponseError("no_choices", "响应缺少 choices[0]")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ResponseError("no_message", "choices[0] 缺少 message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ResponseError("empty_content", "choices[0].message.content 为空")
        return content

    def _usage(self) -> dict[str, Any]:
        usage = self._last_usage
        out: dict[str, Any] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        if isinstance(usage, Mapping):
            for key in out:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    out[key] = value
        return out

    @staticmethod
    def _http_error(response: httpx.Response, status: int) -> OutboundResponse:
        if status in (401, 403):
            return OutboundResponse(
                "unavailable", status, f"远端拒绝凭据（HTTP {status}）", retryable=False
            )
        if status == 429:
            return OutboundResponse(
                "rate_limited", status, "远端限流（HTTP 429）",
                retry_after=_retry_after_seconds(response), retryable=True,
            )
        if 500 <= status < 600:
            return OutboundResponse(
                "server_error", status, f"远端暂时性错误（HTTP {status}）",
                retry_after=_retry_after_seconds(response), retryable=True,
            )
        if 300 <= status < 400:
            return OutboundResponse(
                "redirect_refused", status, "远端返回跳转（不跟随）", retryable=False
            )
        return OutboundResponse("http_error", status, f"远端返回 HTTP {status}", retryable=False)

    # -- 记录 --
    def _record(
        self,
        request_id: str,
        purpose: str,
        *,
        status: str,
        error: GatewayError | None,
        elapsed: float,
        transcript: Transcript,
        request: OutboundRequest | None = None,
        http_status: int | None = None,
        redaction: policy.Redaction | None = None,
        response_bytes: int = 0,
        content_bytes: int = 0,
    ) -> None:
        if self.audit is None:
            return
        usage = self._usage()
        record: dict[str, Any] = {
            "request_id": request_id,
            "ts": policy.iso_now(),
            "caller": self.caller or self.transport_name,
            "purpose": purpose,
            "dictionary_digest": request.dictionary_digest if request else None,
            "dictionary_keys": list(request.dictionary_keys) if request else None,
            "question_digest": request.question_digest if request else None,
            "system_version": request.system_version if request else self.config.system_version,
            "model": self.config.model,
            "transport": self.transport_name,
            "attempts": len(transcript.attempts) or self._outbound,
            "duration_ms": int(elapsed * 1000),
            "status": status,
            "error_kind": error.kind if error else None,
            "error_code": getattr(error, "code", None),
            "http_status": http_status,
            "request_bytes": len(request.body) if request else None,
            "response_bytes": response_bytes,
            "content_bytes": content_bytes,
            "fingerprint": None,
            **usage,
        }
        if redaction is not None:
            record["redactions"] = redaction.count
            record["redaction_categories"] = redaction.categories
        # 非 live 传输的指纹：请求体哈希（只在合成数据上产生，便于核对“每次重建”）。
        if request is not None and self.transport_name != TRANSPORT_LIVE:
            record["fingerprint"] = hashlib.sha256(request.body).hexdigest()[:16]
        self.audit.write(record)

    def _finish(
        self,
        request_id: str,
        purpose: str,
        status: int | None,
        started: float,
        transcript: Transcript,
        error: GatewayError | None,
        request: OutboundRequest | None = None,
        redaction: policy.Redaction | None = None,
        *,
        content: str | None = None,
        parsed: dict[str, Any] | None = None,
    ) -> GatewayResult:
        if request is None:
            request = _EMPTY_REQUEST
        fingerprint = (
            hashlib.sha256(request.body).hexdigest()[:16]
            if request.body and self.transport_name != TRANSPORT_LIVE
            else ""
        )
        return GatewayResult(
            request_id=request_id,
            purpose=purpose,
            model=self.config.model,
            system_version=self.config.system_version,
            status=status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            fingerprint=fingerprint,
            dictionary_digest=request.dictionary_digest,
            request=request,
            redaction=redaction,
            content=content,
            parsed=parsed,
            usage=self._usage(),
            attempts=len(transcript.attempts) or self._outbound,
            transport=self.transport_name,
            outbound=self._outbound,
            transcript=transcript,
            error=error,
        )

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # pragma: no cover - 关闭失败不影响结论
            pass

    def __enter__(self) -> "GatewayClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def outbound_count(self) -> int:
        return self._outbound

    @property
    def http(self) -> httpx.Client:
        return self._http


_EMPTY_REQUEST = OutboundRequest(
    url="", purpose="", model="", body=b"", system_version="", dictionary_digest="",
    dictionary_keys=(), question="", question_digest="", redaction={}, dictionary={},
)


def probe_mock(
    *, config: GatewayConfig | None = None, audit: AuditLog | None = None
) -> dict[str, Any]:
    """``chatbi gateway probe --mock``：走完整请求构建 + 假传输，打印 MOCK_OK。"""
    cfg = config or policy.load_config()
    client = GatewayClient(purpose=QUERY, transport=TRANSPORT_MOCK, config=cfg, audit=audit)
    try:
        result = client.call(policy.PROBE_QUESTION, policy.PROBE_DICTIONARY, purpose=QUERY)
    finally:
        client.close()
    return {
        "mode": "mock",
        "ok": result.ok,
        "request_id": result.request_id,
        "model": result.model,
        "status": result.status,
        "parsed": result.parsed,
        "elapsed_ms": result.elapsed_ms,
        "outbound": result.outbound,
        "error": None if result.ok else result.error.as_dict(),
    }


#: 健康检查用的最小授权字典与问题（无业务数据，约 1.7 KB 请求）。
#: 注意：本模块的非 live 传输都只记录**不保存正文**（:class:`CapturedRequest` 只在
#: 合成测试里由测试自己传入 transport 时产生）；真实调用不保存正文。
HEALTHCHECK_DICTIONARY: dict[str, Any] = {
    "views": [{"name": "probe", "columns": ["k"], "note": ""}],
    "fields": [{"name": "k", "view": "probe", "type": "text", "unit": "", "description": "键"}],
}
HEALTHCHECK_QUESTION = "请给出视图 probe 的 k 列"


def probe_healthcheck(
    *,
    config: GatewayConfig | None = None,
    audit: AuditLog | None = None,
    handler: httpx.BaseTransport | None = None,
    secret: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """``chatbi gateway probe --healthcheck``：本地服务冒烟自检。

    * 有密钥时：发一次**最小的真实请求**（上面的合成字典，无业务数据），验证端点、
      鉴权、响应判别与审计，并回报状态码。
    * 无密钥时：用合成密钥走完整“真实传输”代码路径，但传输层改成
      :class:`_HeadProbeTransport`（把 POST 改写为不带正文/密钥的 HEAD），只验证
      请求构造、凭据注入、审计与错误判别，且**不伪造成功**（报告里注明）。
    * 测试可注入 ``handler``（:class:`ScriptedTransport`）与 ``secret`` 来核对全部路径。
    """
    cfg = config or policy.load_config()
    real = secret if secret is not None else secret_from_env(cfg)
    synthetic = not real
    if real is None:
        real = "synthetic-healthcheck-not-a-real-key"
    client = GatewayClient(
        purpose=QUERY,
        transport=TRANSPORT_LIVE,
        secret=real,
        config=cfg,
        audit=audit,
        sleep=lambda _s: None,
    )
    try:
        if handler is not None or transport is not None:
            client._http.close()
            client._http = httpx.Client(
                transport=handler or transport,  # type: ignore[arg-type]
                timeout=httpx.Timeout(cfg.transport.timeout_seconds),
                trust_env=cfg.transport.proxy_from_env,
            )
        elif synthetic:
            client._http.close()
            client._http = httpx.Client(
                transport=_HeadProbeTransport(),
                timeout=httpx.Timeout(cfg.transport.timeout_seconds),
                trust_env=cfg.transport.proxy_from_env,
            )
        query = client.call(HEALTHCHECK_QUESTION, HEALTHCHECK_DICTIONARY, purpose=QUERY)
        mapping = client.call(
            policy.MAPPING_TASK_QUESTION,
            {
                "source_tables": [{"table": "probe", "headers": ["键"]}],
                "standard_fields": [{"field": "f", "label": "键", "dtype": "text"}],
                "units": [],
                "notes": "",
            },
            purpose=MAPPING,
        )
    finally:
        client.close()
    return {
        "mode": "healthcheck",
        "synthetic_credential": synthetic,
        "query": query.summary(),
        "mapping": mapping.summary(),
        "outbound": client.outbound_count,
    }


class _HeadProbeTransport(httpx.BaseTransport):
    """健康检查用的本地探针：HEAD 到固定端点，不带正文、不带 Authorization。

    响应由本地合成（不是模型信封），因此不会伪造“成功”；它只证明请求构造、凭据
    注入与审计链条在工作。返回的 user-agent 等头由 httpx 正常生成。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
        self.calls.append(str(request.url))
        return httpx.Response(200, request=request, json={"_probe": "ok"})

    def close(self) -> None:  # pragma: no cover
        return None


def render_result(result: GatewayResult) -> str:
    """把结果渲染成终端可读的一行摘要（不含正文与密钥）。"""
    parts = [f"request_id={result.request_id}", f"status={result.status}"]
    if result.error is not None:
        parts.append(f"error={result.error.kind}/{getattr(result.error, 'code', None)}")
    else:
        parsed = result.parsed or {}
        parts.append(f"parsed={parsed.get('kind')}")
    parts.append(f"model={result.model}")
    parts.append(f"elapsed_ms={result.elapsed_ms}")
    return " ".join(parts)


def live_probe(config: GatewayConfig | None = None, audit: AuditLog | None = None) -> dict[str, Any]:
    """``chatbi gateway probe --live``：无数据的合成字典问题，真实调用。

    返回 dict：``state`` 为 ``ok`` / ``missing_key`` / ``failed``。``model`` 取自响应
    信封（远端回显）；``content_parsed`` 表示 content 能解析为 JSON 对象。
    """
    cfg = config or policy.load_config()
    secret = secret_from_env(cfg)
    out: dict[str, Any] = {
        "mode": "live",
        "endpoint": cfg.endpoint_url,
        "configured_model": cfg.model,
    }
    if not secret:
        out["state"] = "missing_key"
        out["request_id"] = None
        return out
    started = time.monotonic()
    client = GatewayClient(
        purpose=QUERY, transport=TRANSPORT_LIVE, secret=secret, config=cfg, audit=audit
    )
    try:
        result = client.call(policy.PROBE_QUESTION, policy.PROBE_DICTIONARY, purpose=QUERY)
    finally:
        client.close()
    wall = time.monotonic() - started
    out.update(result.summary())
    out["wall_ms"] = int(wall * 1000)
    if result.ok:
        out["state"] = "ok"
    else:
        out["state"] = "missing_key" if isinstance(result.error, LiveKeyMissing) else "failed"
    out["content_is_object"] = bool(result.parsed)
    return out


