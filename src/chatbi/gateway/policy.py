"""模型网关的出口契约、脱敏与本地审计（步骤 02）。

这个模块**不发网络请求**，只负责“能不能出域、出域时长什么样、本地记什么”：

* :class:`GatewayConfig`：读取 ``config/gateway.yaml`` 并做结构校验。端点 URL、模型、
  system 指令版本、各类上限、预算、调用方用途都只来自这个文件；调用方不能在请求里
  覆盖它们。
* :func:`redact_question`：对问句做字段/域检查与本地实体匹配，把客户名、金额、电话、
  账号、邮箱、身份证、地址和原始 SQL 参数等替换成 ``[客户名]`` / ``[金额]`` 等标记；
  等值参数（列名后紧跟的名称、代码、账号）改写成 ``:pN`` 占位符，原值只放在本地
  :class:`LocalParameterBag` 里。
* :func:`reject_pasted_table` / :func:`assert_clean`：拒绝粘贴表格、成段记录；拒收多余键、
  ``$`` 插值、疑似敏感值。
* :func:`build_payload`：按用途从零重建 ``{model, temperature, max_tokens, messages,
  stream}``，``messages`` 只有 versioned system + 含 ``question`` / ``dictionary`` 的 user。
* :class:`AuditLog`：本地 JSONL 审计，只记 request_id、用途、字典摘要、模型、耗时、状态
  和 token 用量；调用方传入的额外信息也要过滤，密钥形态的值一律拒收。

所有错误都是 :class:`GatewayError` 的子类，异常文本里只有类别、位置和“删除过的片段
长度”，不会带出原始值。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from chatbi import config as app_config

# ------------------------------------------------------------------ 用途 --

MAPPING = "mapping"
QUERY = "query"
PURPOSES = (MAPPING, QUERY)

#: 字典的“形状”：顶层键 → (值类型, 该层允许的子键)。
#: 形状之外的一律拒收（字典里出现原始行、SQL 结果列等都不符合这个形状）。
_MAPPING_SHAPE: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "source_tables": ("list", ("table", "headers")),
    "standard_fields": ("list", ("field", "label", "dtype", "hint")),
    "units": ("list", None),
    "notes": ("str", None),
}
_QUERY_SHAPE: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "views": ("list", ("name", "columns", "note")),
    "fields": ("list", ("view", "name", "type", "unit", "description")),
    "relations": ("list", ("left", "right", "on", "note")),
    "metrics": ("list", ("name", "definition", "unit", "note")),
    "examples": ("list", ("question", "sql", "note")),
}
DICTIONARY_SHAPE: dict[str, dict[str, tuple[str, tuple[str, ...] | None]]] = {
    MAPPING: _MAPPING_SHAPE,
    QUERY: _QUERY_SHAPE,
}

#: 字典与问句里不允许出现的键名（结果列、逐行数据、SQL 结果等都不属于字典）。
FORBIDDEN_KEYS = frozenset(
    {
        "rows",
        "result",
        "results",
        "result_rows",
        "raw_rows",
        "sample_rows",
        "sample_data",
        "data",
        "values",
        "answer",
        "answers",
        "expected",
        "standard_answer",
        "standard_answers",
        "baseline",
        "ground_truth",
        "api_key",
        "apikey",
        "authorization",
        "token",
        "password",
        "secret",
        "credential",
        "credentials",
        "headers",  # 仅作为 HTTP 头出现时拒收；表字典用 source_tables[].headers，见 _check_keys
    }
)
#: 允许在字典任意层级出现的键名（白名单式；“headers”只在 source_tables 表项里合法）。
_ALLOWED_KEYS = frozenset(
    {
        "table",
        "headers",
        "field",
        "label",
        "dtype",
        "hint",
        "unit",
        "units",
        "notes",
        "note",
        "name",
        "columns",
        "view",
        "type",
        "description",
        "left",
        "right",
        "on",
        "definition",
        "question",
        "sql",
        "source_tables",
        "standard_fields",
        "views",
        "fields",
        "relations",
        "metrics",
        "examples",
    }
)

#: 固定映射任务说明（出域时放在 user.question 里，不含任何数据值）。
MAPPING_TASK_QUESTION = (
    "任务：字段映射。请按 system 指令，为 dictionary.standard_fields 中的每个标准字段，"
    "在 dictionary.source_tables 中挑选候选源表头，并按 JSON 结构输出 candidates。"
)

#: 服务端“冒烟自检”用的合成字典与问题（无业务数据）。
PROBE_DICTIONARY: dict[str, Any] = {
    "views": [{"name": "probe_synthetic", "columns": ["k", "v"], "note": "synthetic"}],
    "fields": [
        {"view": "probe_synthetic", "name": "k", "type": "text", "unit": "", "description": "键"},
        {"view": "probe_synthetic", "name": "v", "type": "number", "unit": "1", "description": "值"},
    ],
    "relations": [],
    "metrics": [],
    "examples": [],
}
PROBE_QUESTION = "请输出一条只读 SQL：统计视图 probe_synthetic 的行数。"


# ------------------------------------------------------------------ 错误 --


class GatewayError(Exception):
    """网关错误基类：``kind`` 是稳定的本地枚举，``detail`` 不含原始值。"""

    kind = "gateway_error"

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": str(self), "detail": self.detail}


class ConfigError(GatewayError):
    kind = "config_error"


class ContractError(GatewayError):
    """请求或字典不符合出口契约（拒收，未发请求）。"""

    kind = "contract"

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message, detail={"code": code, **detail})
        self.code = code


class PurposeNotAllowed(GatewayError):
    kind = "purpose_not_allowed"


class CallerNotAllowed(GatewayError):
    kind = "caller_not_allowed"


class ResponseError(GatewayError):
    """响应结构、大小或内容不合格（不执行、不粘贴）。"""

    kind = "response_invalid"

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message, detail={"code": code, **detail})
        self.code = code


class OutboundResponse(GatewayError):
    """远端返回了非 2xx（401/403/429/5xx…），或传输失败。"""

    kind = "outbound"

    def __init__(
        self,
        code: str,
        status: int | None,
        message: str,
        *,
        retry_after: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            message,
            detail={
                "code": code,
                "status": status,
                "retry_after": retry_after,
                "retryable": retryable,
            },
        )
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable


class LiveKeyMissing(GatewayError):
    kind = "live_key_missing"

    def __init__(self) -> None:
        super().__init__("LIVE_KEY_MISSING：环境变量未提供模型密钥（实连待验）")


# ------------------------------------------------------------------ 配置 --


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(data: Any) -> bytes:
    """稳定序列化：紧凑、按键排序、UTF-8；用于字典摘要与哈希。"""
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def dictionary_digest(dictionary: Mapping[str, Any]) -> str:
    return _sha256_bytes(stable_json(dictionary))


def _is_number(value: Any) -> bool:
    # bool 是 int 的子类：数值字段不接受 True/False。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(mapping: Mapping[str, Any], key: str, kind: str, where: str) -> Any:
    """取必需键并做类型检查；``kind`` 取 ``dict`` / ``list`` / ``str`` / ``int`` / ``number`` / ``bool``。"""
    if key not in mapping:
        raise ConfigError(f"{where}.{key} 缺失")
    value = mapping[key]
    checks = {
        "dict": lambda v: isinstance(v, dict),
        "list": lambda v: isinstance(v, list),
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": _is_number,
        "bool": lambda v: isinstance(v, bool),
    }
    if not checks[kind](value):
        raise ConfigError(f"{where}.{key} 类型不符")
    return value


@dataclass(frozen=True)
class PurposePolicy:
    """一种用途的静态参数：system 指令、字典形状与允许的顶层键。"""

    purpose: str
    system: str
    allowed_keys: frozenset[str]
    shape: Mapping[str, tuple[str, tuple[str, ...] | None]]


@dataclass(frozen=True)
class TransportPolicy:
    timeout_seconds: float
    connect_timeout_seconds: float
    follow_redirects: bool
    proxy_from_env: bool
    transient_connect_retries: int
    verify: bool


@dataclass(frozen=True)
class Limits:
    response_bytes: int
    content_bytes: int
    dictionary_bytes: int
    question_bytes: int
    request_bytes: int
    json_depth: int


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: tuple[float, ...]


@dataclass(frozen=True)
class CallerRule:
    purpose: str
    dictionary_keys: frozenset[str]


@dataclass(frozen=True)
class GatewayConfig:
    """``config/gateway.yaml`` 的只读视图。"""

    base_url: str
    path: str
    model: str
    temperature: float
    max_tokens: int
    transport: TransportPolicy
    limits: Limits
    retry: RetryPolicy
    key_env: str
    server_socket: str
    server_max_connections: int
    server_request_timeout: float
    callers: Mapping[str, CallerRule]
    purposes: Mapping[str, PurposePolicy]
    system_version: str
    repair_version: str
    repair_categories: tuple[str, ...]
    source: str
    raw_sha256: str

    # -- 端点 --
    @property
    def endpoint_url(self) -> str:
        return self.base_url.rstrip("/") + self.path

    @property
    def endpoint_origin(self) -> tuple[str, str, int | None]:
        parts = urlsplit(self.base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return (parts.scheme, parts.hostname or "", port)

    def request_url(self) -> str:
        """http 传输层的 request hook 用它判定“去向合法”。"""
        return self.endpoint_url

    def caller(self, caller: str) -> CallerRule:
        rule = self.callers.get(caller)
        if rule is None:
            raise CallerNotAllowed(f"调用方 {caller!r} 不在本地配置的允许清单内")
        return rule

    def purpose(self, purpose: str) -> PurposePolicy:
        pol = self.purposes.get(purpose)
        if pol is None:
            raise PurposeNotAllowed(f"用途 {purpose!r} 不在允许的用途内（仅 mapping / query）")
        return pol

    def as_summary(self) -> dict[str, Any]:
        """可写入证据的摘要：不含凭据，不含 system 全文。"""
        return {
            "source": self.source,
            "raw_sha256": self.raw_sha256,
            "endpoint": {"base_url": self.base_url, "path": self.path, "model": self.model},
            "transport": {
                "timeout_seconds": self.transport.timeout_seconds,
                "follow_redirects": self.transport.follow_redirects,
                "proxy_from_env": self.transport.proxy_from_env,
                "verify": self.transport.verify,
            },
            "limits": {
                "response_bytes": self.limits.response_bytes,
                "content_bytes": self.limits.content_bytes,
                "dictionary_bytes": self.limits.dictionary_bytes,
                "request_bytes": self.limits.request_bytes,
            },
            "retry": {
                "max_attempts": self.retry.max_attempts,
                "backoff_seconds": list(self.retry.backoff_seconds),
            },
            "key_env": self.key_env,
            "system_version": self.system_version,
            "purposes": {p: sorted(pol.allowed_keys) for p, pol in self.purposes.items()},
            "callers": {
                c: {"purpose": r.purpose, "dictionary_keys": sorted(r.dictionary_keys)}
                for c, r in self.callers.items()
            },
        }


CONFIG_REL = "config/gateway.yaml"


def load_config(path: Path | None = None) -> GatewayConfig:
    """读取并校验网关配置；结构不符直接抛 :class:`ConfigError`。"""
    import yaml  # 延迟导入：policy 的纯函数（脱敏、重建请求）不依赖 yaml

    target = Path(path) if path is not None else app_config.LAYOUT.root / CONFIG_REL
    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise ConfigError(f"无法读取网关配置 {target.name}: {type(exc).__name__}") from None
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        raise ConfigError("网关配置不是合法的 YAML/UTF-8") from None
    if not isinstance(data, dict):
        raise ConfigError("网关配置顶层必须是映射")

    ep = _require(data, "endpoint", "dict", "config")
    base_url = _require(ep, "base_url", "str", "endpoint")
    path_s = _require(ep, "path", "str", "endpoint")
    model = _require(ep, "model", "str", "endpoint")
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError("endpoint.base_url 必须是固定 https 地址")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError("endpoint.base_url 不能含查询串、片段或用户信息")
    if path_s != "/chat/completions":
        raise ConfigError("endpoint.path 只能是 /chat/completions")

    tr = _require(data, "transport", "dict", "config")
    transport = TransportPolicy(
        timeout_seconds=float(_require(tr, "timeout_seconds", "number", "transport")),
        connect_timeout_seconds=float(_require(tr, "connect_timeout_seconds", "number", "transport")),
        follow_redirects=bool(_require(tr, "follow_redirects", "bool", "transport")),
        proxy_from_env=bool(_require(tr, "proxy_from_env", "bool", "transport")),
        transient_connect_retries=int(_require(tr, "transient_connect_retries", "int", "transport")),
        verify=bool(_require(tr, "verify", "bool", "transport")),
    )
    if transport.follow_redirects:
        raise ConfigError("transport.follow_redirects 必须为 false（不跟随跳转）")
    if transport.timeout_seconds <= 0 or transport.connect_timeout_seconds <= 0:
        raise ConfigError("transport 超时必须为正数")

    lim = _require(data, "limits", "dict", "config")
    limits = Limits(
        response_bytes=int(_require(lim, "response_bytes", "int", "limits")),
        content_bytes=int(_require(lim, "content_bytes", "int", "limits")),
        dictionary_bytes=int(_require(lim, "dictionary_bytes", "int", "limits")),
        question_bytes=int(_require(lim, "question_bytes", "int", "limits")),
        request_bytes=int(_require(lim, "request_bytes", "int", "limits")),
        json_depth=int(_require(lim, "json_depth", "int", "limits")),
    )
    if limits.content_bytes > 16384:
        raise ConfigError("limits.content_bytes 不得超过 16 KiB")

    rt = _require(data, "retry", "dict", "config")
    retry = RetryPolicy(
        max_attempts=int(_require(rt, "max_attempts", "int", "retry")),
        backoff_seconds=tuple(float(x) for x in _require(rt, "backoff_seconds", "list", "retry")),
    )
    if retry.max_attempts > 3:
        raise ConfigError("retry.max_attempts 不得超过 3（预算封顶）")
    if len(retry.backoff_seconds) < retry.max_attempts - 1:
        raise ConfigError("retry.backoff_seconds 不足以覆盖重试次数")

    key_env = _require(_require(data, "credential", "dict", "config"), "env", "str", "credential")
    if key_env != app_config.LLM_KEY_ENV:
        raise ConfigError(f"credential.env 必须是 {app_config.LLM_KEY_ENV}")

    srv = _require(data, "server", "dict", "config")
    server_socket = _require(srv, "socket", "str", "server")
    if not server_socket.startswith("var/run/") or ".." in server_socket:
        raise ConfigError("server.socket 必须在 var/run/ 下")
    server_max_connections = int(_require(srv, "max_connections", "int", "server"))
    server_request_timeout = float(_require(srv, "request_timeout_seconds", "number", "server"))

    sy = _require(data, "system", "dict", "config")
    system_version = _require(sy, "version", "str", "system")
    purposes: dict[str, PurposePolicy] = {}
    for purpose in PURPOSES:
        raw_text = _require(sy, purpose, "str", "system")
        if "{{version}}" not in raw_text:
            raise ConfigError(f"system.{purpose} 未登记版本占位符")
        text = raw_text.replace("{{version}}", system_version)
        if not text.strip():
            raise ConfigError(f"system.{purpose} 为空")
        purposes[purpose] = PurposePolicy(
            purpose=purpose,
            system=text,
            allowed_keys=frozenset(DICTIONARY_SHAPE[purpose]),
            shape=DICTIONARY_SHAPE[purpose],
        )

    rp = _require(data, "repair", "dict", "config")
    repair_categories = tuple(_require(rp, "categories", "list", "repair"))
    repair_version = _require(rp, "version", "str", "repair")

    callers_raw = _require(data, "callers", "dict", "config")
    callers: dict[str, CallerRule] = {}
    for name, rule in callers_raw.items():
        if not isinstance(rule, dict):
            raise ConfigError(f"callers.{name} 必须是映射")
        purpose = _require(rule, "purpose", "str", f"callers.{name}")
        if purpose not in PURPOSES:
            raise ConfigError(f"callers.{name}.purpose 必须是 mapping 或 query")
        keys = tuple(_require(rule, "dictionary_keys", "list", f"callers.{name}"))
        unknown = set(keys) - set(DICTIONARY_SHAPE[purpose])
        if unknown:
            raise ConfigError(f"callers.{name}.dictionary_keys 含该用途不支持的键")
        callers[str(name)] = CallerRule(purpose=purpose, dictionary_keys=frozenset(keys))
    if not callers:
        raise ConfigError("callers 不能为空")

    raw_sha = _sha256_bytes(raw_bytes)

    return GatewayConfig(
        base_url=base_url.rstrip("/"),
        path=path_s,
        model=model,
        temperature=float(ep.get("temperature", 0)),
        max_tokens=int(ep.get("max_tokens", 1200)),
        transport=transport,
        limits=limits,
        retry=retry,
        key_env=key_env,
        server_socket=server_socket,
        server_max_connections=server_max_connections,
        server_request_timeout=server_request_timeout,
        callers=callers,
        purposes=purposes,
        system_version=system_version,
        repair_version=repair_version,
        repair_categories=repair_categories,
        source=CONFIG_REL,
        raw_sha256=raw_sha,
    )


# ------------------------------------------------------------------ 脱敏 --

#: 带“命名”语义的列：名称/代码/批次等，后面跟值时就改成 :pN 或标记。
_NAME_COLUMNS = frozenset(
    "客户 客户名称 客户简称 客户编码 供应商 供应商名称 供应商编码 工厂 工厂名称 项目 项目名称 "
    "产品 产品名称 产品型号 物料 料号 物料编码 部门 部门名称 科目 科目名称 账户 账号 批次 批次号 "
    "订单号 单据号 合同号 凭证号 工号 员工 联系人 姓名 负责人 region customer supplier plant "
    "project product department account".split()
)
#: 数值列：后面跟值（不足四位）时标成 [金额]。
_NUMBER_COLUMNS = frozenset(
    "金额 销售金额 回款金额 收入 营收 成本 利润 毛利 净利润 预算 实际 数量 单价 余额 库存 工时 "
    "费用 税额 期数 账期".split()
)
_PERIOD_COLUMNS = frozenset("期间 季度 月份 年度".split())
_EN_NAME = "[A-Za-z][A-Za-z0-9_]{1,40}"
#: 列名/参数名/指标名（冒号等值参数、token 判定复用）。
_PARAM_COLUMNS = _NAME_COLUMNS | _NUMBER_COLUMNS | _PERIOD_COLUMNS
#: 等值参数里不当作值的常见问词/代词。
_NOT_A_VALUE = frozenset("多少 哪些 什么 是 为 谁 几个 怎么样 何时 哪个".split())
#: 列名候选（长名优先，避免“客户名称”被“客户”截断）。列名都是汉字/字母，不必转义。
_NAME_COLUMN_ALT = "|".join(sorted(_PARAM_COLUMNS, key=len, reverse=True)) + "|" + _EN_NAME

#: 常见词表：问题里的列名、指标、疑问词、语助词、单位。含这些的片段保留原样。
_COMMON_CN = frozenset(
    (
        "客户 供应商 客户简称 客户名称 名称 简称 编码 代码 表头 值 取值 数值 数量 数字 多少 几个 "
        "金额 销售金额 回款金额 收入 营收 成本 利润 毛利 净利润 预算 实际 差异 环比 同比 增长 下降 "
        "单位 元 千元 百分之 比例 占比 比率 率 百分比 百分点 万元 亿元 "
        "期间 季度 月份 年度 年 月 日 期 起 止 开始 结束 至今 本季度 上季度 "
        "工厂 项目 产品 产品名称 产品型号 物料 料号 物料编码 部门 科目 账户 批次 批次号 订单号 "
        "单据号 合同号 凭证号 工号 员工 联系人 姓名 负责人 "
        "华东 华南 华北 华中 西北 西南 东北 一厂 二厂 三厂 一号 二号 三号 "
        "大于 小于 多于 少于 超过 低于 高于 不低于 不超过 等于 不等 及以上 及以下 以上 以下 "
        "之间 其中 每个 各个 各 按 对于 关于 相关 所有 全部 任一 某些 某 部分 "
        "有哪些 哪些 哪个 哪 何 什么 怎么 怎么样 如何 为何 为什么 是否 有没有 有 些 "
        "是 为 的 个 条 笔 项 位 家 张 份 次 天 小时 工时 分钟 秒 台 件 套 "
        "合计 总计 小计 平均 均值 最大 最小 排名 前 后 排序 列出 计算 统计 显示 查询 给出 输出 "
        "问题 问句 请问 帮我 麻烦 告诉 一下 分别 同时 与 和 或 及 在 于 从 到 比 较 对 中 内 里 "
        "退货 报工 检验 出货 入库 领料 采购 销售 生产 库存 应收 应付 折旧 费用 税金 税额 "
        "报表 明细 汇总 本期 上期 变动 原因 情况 说明 口径 定义 来源 字段 视图 表 时间 "
        "编号 单据 凭证 记录 数据 左右 大约 大概 约 第一 第二 第三"
    ).split()
)
#: 中文数字（日期/期间里常见，不当值）。
_CN_NUM = frozenset("零一二三四五六七八九十百千万两半")
_MAX_WORD = max(len(w) for w in _PARAM_COLUMNS | _COMMON_CN)

#: 冒号形式的等值参数：冒号前是列名时把值换成 :pN（“客户：华东一厂”）。
_PREFIX_PARAM = re.compile(
    rf"(?P<col>{_NAME_COLUMN_ALT})(?P<sep>[:：])\s*"
    rf'(?P<val>"[^"]{{1,200}}"|“[^”]{{1,200}}”|[^\s，。？！,;?！、]+)'
)

#: 结构性模式（顺序有意义）：原文块、报错、SQL、凭据、日期，最后是裸金额/数量。
_STRUCT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("原文", re.compile(r"```[\s\S]*?```")),
    (
        "原始报错",
        re.compile(
            r"(?:ERROR|FATAL|DETAIL|HINT|SQLSTATE|Traceback|Exception|psycopg\.[A-Za-z]+|"
            r"KeyError|ValueError|TypeError|SyntaxError)[^\n。；]{0,200}"
        ),
    ),
    ("密钥", re.compile(r"(?:sk|pk|rk)-[A-Za-z0-9_-]{6,}")),
    ("账号", re.compile(r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.I)),
    ("密钥", re.compile(r"(?:api[_-]?key|token|password|passwd|secret)\s*[:=：]\s*\S+", re.I)),
    (
        "SQL",
        re.compile(
            r"(?:select|insert|update|delete|with)\b[\s\S]{0,200}?\b(?:from|into|set)\b[^\n。；]{0,200}",
            re.I,
        ),
    ),
    ("账号", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("电话", re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")),
    ("电话", re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("账号", re.compile(r"(?<![A-Za-z0-9])\d{12,30}(?![A-Za-z0-9])")),
    ("IP", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    (
        "地址",
        re.compile("[\u4e00-\u9fa5]{2,}(?:省|市|区|县|镇|街道|路|大道)[\u4e00-\u9fa50-9号栋室巷弄-]{0,20}"),
    ),
    (
        "日期",
        re.compile(
            r"(?<!\d)(?:[12]\d{3}\s*[-/.]\s*\d{1,2}(?:\s*[-/.]\s*\d{1,2})?"
            r"|\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
            r"|\d{1,2}\s*月\s*\d{1,2}\s*日)(?!\d)"
        ),
    ),
    ("金额", re.compile(r"(?<![\w:.])[-+]?\d{2,3}[,，]\d{3}(?:[,，]\d{3})*(?:\.\d+)?(?![\w.])")),
    # 四位数以上：年份/季度（2026、2026Q3）不算金额；其余（可能带千分位）标成金额。
    ("金额", re.compile(r"(?<![\w:.])[-+]?\d{4,}(?:[\d,，]*\d)?(?:\.\d+)?(?!\d|年|Q|[A-Za-z])")),
)

#: 替换后的片段里如果还残留这些形态，说明值没清干净，直接拒收。
_RESIDUAL = re.compile(
    r"(sk-[A-Za-z0-9_-]{6,}|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)|"
    r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])|"
    r"[-+]?[¥￥$]\s?\d|"
    r"(?<![\w:.])\d[\d,]{3,}(?:\.\d+)?(?![\w.])|"
    r"(?<![\w:.])\d+(?:\.\d+)?\s*(?:万元|亿元|元|万|亿)|"
    r"(?<![\w:.])\d{2,3}[,，]\d{3}(?![\d]))"
)


def _strip_pasted(text: str) -> str:
    """去掉粘贴的表格/成段记录（这类内容一律删掉，不代替换）。"""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.count("\t") >= 1 or stripped.count("|") >= 2:
            continue
        if re.fullmatch(r"[\s|:\-]+", stripped):
            continue
        if re.fullmatch(r"[\d.,%-]+", stripped):
            continue
        kept.append(line)
    text = "\n".join(kept)
    text = re.sub(
        "(?:[\u4e00-\u9fa5A-Za-z_]{2,12}[：:=][^\\s，,;；]{0,40}[\\s，,;；]+){3,}",
        " ",
        text,
    )
    return text


@dataclass
class Redaction:
    """一次脱敏的结果（摘要可写入本地审计）。"""

    text: str
    count: int
    parameters: list[dict[str, str]] = field(default_factory=list)
    categories: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "redactions": self.count,
            "categories": dict(sorted(self.categories.items())),
            "parameters": [p["name"] for p in self.parameters],
        }


class LocalParameterBag:
    """本地持有等值参数的原值，只在本地绑定/审计时使用，绝不进入请求。"""

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, name: str, value: str) -> None:
        self._values[name] = value

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def names(self) -> list[str]:
        return list(self._values)

    def count(self) -> int:
        return len(self._values)

    def digest(self) -> str:
        return _sha256_bytes(stable_json(self._values))[:16]

    def __repr__(self) -> str:  # 不展开原值
        return f"LocalParameterBag({self.count()} params)"


class _Redactor:
    """内部状态：计数、参数表（占位符只记名字，原值进本地参数袋）。"""

    def __init__(self, bag: LocalParameterBag | None) -> None:
        self.bag = bag
        self.parameters: list[dict[str, str]] = []
        self.categories: dict[str, int] = {}
        self.count = 0

    def count_category(self, category: str, n: int = 1) -> None:
        self.categories[category] = self.categories.get(category, 0) + n
        self.count += n

    def masked(self, label: str):
        def repl(match: re.Match[str]) -> str:
            self.count_category(label)
            return f"[{label}]"

        return repl


#: token 化：引号值、斜杠日期、带单位金额、已替换标记、英文标识符、汉字单字、数字。
#: 汉字单字最后由 :func:`_merge_words` 按词表贪心合并；词表外的连续汉字合成一段。
_TOKEN = re.compile(
    r"(?P<q>" r'"[^"\n]{1,200}"' r"|“[^”\n]{1,200}”|'[^'\n]{1,200}'|「[^」\n]{1,200}」)"
    r"|(?P<slash>(?<![\d:./])[-+]?\d[\d,]*(?:\.\d+)?/\d{1,2}(?:/\d{1,4})?(?![\d.]))"
    r"|(?P<amount>(?<![\w:./])(?:[¥￥$]\s?\d[\d,]*(?:\.\d+)?"
    r"|[-+]?\d[\d,]*(?:\.\d+)?\s*(?:万元|亿元|元整|元|万|亿))(?![A-Za-z]))"
    r"|(?P<mask>\[[^\[\]\n]{1,12}\])"
    r"|(?P<en>(?<![A-Za-z0-9_@.-])[A-Za-z](?:[A-Za-z0-9_@.-]*[A-Za-z0-9])?)"
    r"|(?P<cjk>[\u4e00-\u9fa5])"
    r"|(?P<num>(?<![\w:./])[-+]?\d[\d,]*(?:\.\d+)?(?:\s*(?:万元|亿元|元|万|亿))?(?![\w.]))"
)


class _Span:
    """词级合并后的片段：``kind == "cjk"`` 表示若干汉字，其余沿用 token 类别。"""

    __slots__ = ("start", "end", "kind", "text")

    def __init__(self, start: int, end: int, kind: str | None, text: str) -> None:
        self.start, self.end, self.kind, self.text = start, end, kind, text

    def group(self) -> str:
        return self.text

    @property
    def lastgroup(self) -> str | None:
        return self.kind


def _has_alnum(segment: str) -> bool:
    return any(ch.isascii() and ch.isalnum() for ch in segment)


def _known_word(segment: str) -> bool:
    """字典/常见词，且不含 ASCII 字母数字（防止把“是2026Q3”当成词再切坏）。"""
    return (segment in _PARAM_COLUMNS or segment in _COMMON_CN) and not any(
        not ("\u4e00" <= ch <= "\u9fa5") for ch in segment
    )


def _merge_words(tokens: list[re.Match[str]], text: str) -> list[_Span]:
    """把连续的单字汉字按“最长词表词”贪心合并；词表外的连续汉字合成一段。"""
    out: list[_Span] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.lastgroup != "cjk":
            out.append(_Span(tok.start(), tok.end(), tok.lastgroup, tok.group()))
            i += 1
            continue
        j = i
        while j < n and tokens[j].lastgroup == "cjk":
            j += 1
        start, stop = tokens[i].start(), tokens[j - 1].end()
        pos = start
        while pos < stop:
            best = 0
            for size in range(min(_MAX_WORD, stop - pos), 1, -1):
                if _known_word(text[pos : pos + size]):
                    best = size
                    break
            if best:
                out.append(_Span(pos, pos + best, "cjk", text[pos : pos + best]))
                pos += best
                continue
            end = pos + 1
            while end < stop and not any(
                _known_word(text[end : end + size])
                for size in range(min(_MAX_WORD, stop - end), 1, -1)
            ):
                end += 1
            out.append(_Span(pos, end, "cjk", text[pos:end]))
            pos = end
        i = j
    return out


#: 问题里的连接词/单位提示：词表外但含这些的片段（“100万元的有”“1000元的”）保留原样。
_HINT = re.compile("万元|亿元|千元|的有|元的|都是|都在|之间|以上|以下|左右|多的|少的")
#: 通用问题用语（前 5 个客户、1 到 3 月、2026Q3 至 9 月）：含这些的短片段不当值。
_GENERIC = frozenset("前后至到约大概左右期间区间分别各每个全部所有之间以上以下".split())
#: 片段里含年份/季度/YYYY-MM：保留原文（年份与季度本身不是客户可识别的敏感值）。
#: SQL 关键字：出现在问题里时是原样引用的示例语句用语，不是客户数据。
_SQL_WORDS = frozenset(
    "select from where group order by sum avg count join on and or as desc asc limit having "
    "with case when then else end null not in between distinct left right inner outer".split()
)
_PERIOD_TAIL = re.compile(r"(?:[12]\d{3}|[12]\d{3}-\d{1,2}|[Qq][1-4])")


def _classify_cn(segment: str) -> str:
    """给一段词表外的汉字归类：num（中文数字）/ hint（问题用语）/ value（值）。"""
    if _HINT.search(segment):
        return "hint"
    if any(ch.isascii() and (ch.isalnum()) for ch in segment):
        return "value"  # 词表外但混有 ASCII 字母/数字（如 的2026Q3、ACME）
    if all(ch in _CN_NUM for ch in segment):
        return "num"
    if any(ch in _CN_NUM for ch in segment) and len(segment) > 1 and segment[-1] in "月年日号期":
        return "num"
    if segment.startswith("是") and len(segment) > 1 and all(ch in _CN_NUM for ch in segment[1:]):
        return "num"  # “是二〇二六”这类纯中文数字
    return "value"


def prefix_param(match: re.Match[str], red: _Redactor) -> str:
    """“客户：华东一厂”这类冒号等值参数：名称换成 :pN，金额/期间换成标记。"""
    value = match.group("val").strip().strip('"“”')
    if not value or value.startswith(":p") or value in _NOT_A_VALUE:
        return match.group(0)
    column = match.group("col")
    sep = match.group("sep")
    if column in _PERIOD_COLUMNS or re.fullmatch(
        r"(?i)\d{4}\s*[-/.年]?\s*(?:q[1-4]|\d{1,2}[月日]?)?", value
    ):
        red.count_category("期间")
        return f"{column}{sep}[期间]"
    if column in _NUMBER_COLUMNS or re.fullmatch(
        r"[-+]?\d[\d,]*(?:\.\d+)?\s*(?:万元|亿元|元|万|亿)?", value
    ):
        red.count_category("金额")
        return f"{column}{sep}[金额]"
    placeholder = f":p{len(red.parameters) + 1}"
    red.parameters.append({"name": placeholder[1:], "column": column})
    if red.bag is not None:
        red.bag.put(placeholder[1:], value)
    red.count_category("参数")
    return f"{column}{sep}{placeholder}"


def _redact_tokens(text: str, red: _Redactor) -> str:
    """词级兜底：剩下的汉字/英文/数字片段逐个判定是否为“值”。"""
    parts: list[str] = []
    pending: list[str] = []
    prev_tok: str | None = None
    last_end = 0
    tokens = _merge_words(list(_TOKEN.finditer(text)), text)
    for index, match in enumerate(tokens):
        gap = text[last_end : match.start]
        last_end = match.end
        kind = match.kind
        tok = match.group()
        replacement = tok
        if kind == "cjk":
            if tok in _PARAM_COLUMNS or tok in _COMMON_CN:
                if tok in _NAME_COLUMNS or tok in _NUMBER_COLUMNS or tok in _PERIOD_COLUMNS:
                    pending.append(tok)
                parts.extend((gap, tok))
                prev_tok = "name"
                continue
            if tok in _GENERIC or tok in _CN_NUM:
                parts.extend((gap, tok))
                pending = []
                prev_tok = "name"
                continue
            cls = _classify_cn(tok)
            if (
                cls in ("value", "num")
                and _PERIOD_TAIL.search(tok)
                and not re.search(r"\d{4,}", tok.replace("2026", ""))
            ):
                # “的2026Q3”“是2026Q3”“是2026年3月”：年份/季度是期间，保留原文。
                replacement = tok
                parts.extend((gap, replacement))
                pending = []
                prev_tok = "period"
                continue
            if cls == "value":
                category = "期间" if pending and pending[-1] in _PERIOD_COLUMNS else "参数"
                replacement = f"[{category}]"
                red.count_category(category)
            elif cls == "hint":
                cls = "name"
            pending = []
            parts.extend((gap, replacement))
            prev_tok = cls
            continue
        if kind == "q":
            inner = tok[1:-1]
            category = "参数"
            for pattern, name in (
                (re.compile(r"sk-[A-Za-z0-9_-]{6,}"), "密钥"),
                (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "账号"),
                (re.compile(r"1[3-9]\d{9}"), "电话"),
                (re.compile(r"0\d{2,3}-\d{7,8}"), "电话"),
                (re.compile(r"\d{17}[\dXx]"), "身份证"),
                (re.compile(r"\d{12,}"), "账号"),
                (re.compile(r"[-+]?[¥￥$]?\d[\d,]*(?:\.\d+)?"), "金额"),
                (None, "参数"),
            ):
                if name == "参数" or (pattern is not None and pattern.search(inner)):
                    category = name or "参数"
                    break
            if category == "参数":
                placeholder = f":p{len(red.parameters) + 1}"
                red.parameters.append(
                    {"name": placeholder[1:], "column": pending[-1] if pending else ""}
                )
                if red.bag is not None:
                    red.bag.put(placeholder[1:], inner)
                replacement = placeholder
            else:
                replacement = f"[{category}]"
            red.count_category(category)
            pending = []
            prev_tok = "value"
        elif kind == "mask":
            pending = []
            prev_tok = "mask"
        elif kind == "slash":
            replacement = "[期间]"
            red.count_category("期间")
            pending = []
            prev_tok = "period"
        elif kind == "amount":
            replacement = "[金额]"
            red.count_category("金额")
            pending = []
            prev_tok = "value"
        elif kind == "en":
            if re.fullmatch(r"[Ff][Yy]\d{2,4}", tok):
                replacement = "[期间]"
                red.count_category("期间")
                prev_tok = "period"
                pending = []
            elif tok.lower() in _SQL_WORDS or tok.islower():
                prev_tok = "en"  # SQL 关键字/小写标识符是问题用语
                pending = []
            else:
                # 单字母、大写编码、带连字符合并的代码：按值脱敏。
                replacement = "[参数]"
                red.count_category("参数")
                prev_tok = "value"
                pending = []
        elif kind == "num":
            if pending and pending[-1] in _PERIOD_COLUMNS:
                replacement = "[期间]"
                red.count_category("期间")
                prev_tok = "period"
            elif (
                index > 0
                and tokens[index - 1].kind in ("period", "mask", "cjk")
                and (
                    tokens[index - 1].group() in ("年", "月", "日", "号", "是")
                    or _classify_cn(tokens[index - 1].group()) == "hint"
                    or tokens[index - 1].kind in ("period", "mask")
                )
            ):
                prev_tok = "num"
            elif re.fullmatch(r"\d{1,3}", tok) or (
                index > 0 and tokens[index - 1].group() in _GENERIC
            ):
                prev_tok = "num"
            else:
                replacement = "[金额]"
                red.count_category("金额")
                prev_tok = "value"
            pending = []
        parts.extend((gap, replacement))
    parts.append(text[last_end:])
    return "".join(parts)


def _redact_with(text: str, red: _Redactor) -> str:
    out = _strip_pasted(text)
    for label, pattern in _STRUCT_RULES:
        out = pattern.sub(red.masked(label), out)
    out = _PREFIX_PARAM.sub(lambda m: prefix_param(m, red), out)
    out = _redact_tokens(out, red)
    return re.sub(r"\s+", " ", out).strip()


def redact_question(text: str) -> Redaction:
    """保留语义（列名、指标、运算符、疑问词），只替换值。"""
    red = _Redactor(bag=None)
    out = _redact_with(text, red)
    return Redaction(text=out, count=red.count, parameters=red.parameters, categories=red.categories)


def redact_question_with_bag(text: str, bag: LocalParameterBag) -> Redaction:
    """同 :func:`redact_question`，但等值参数的原值存进本地参数袋（不出域）。"""
    red = _Redactor(bag=bag)
    out = _redact_with(text, red)
    return Redaction(text=out, count=red.count, parameters=red.parameters, categories=red.categories)


# ------------------------------------------------------------------ 校验 --


def assert_clean(text: str, *, where: str = "question") -> None:
    """最后一道出口检查：替换后的文本不得残留敏感形态。"""
    m = _RESIDUAL.search(text)
    if m:
        raise ContractError(
            "residual_sensitive",
            f"{where} 仍含疑似敏感值（已拒收，未发请求）",
            match_len=len(m.group(0)),
        )


#: “列名: 值”单元格：冒号/等号（含全角），值到空白或常见标点为止。
#: 分隔符用 chr() 拼出，避免依赖转移字符在源码里的具体写法。
_WS = chr(32) + "".join(chr(c) for c in (9, 13, 10, 0x3000))
_PUNCT = "".join(chr(c) for c in (0xFF0C, 0x2C, 0x3B, 0xFF1B, 0xFF1F, 0xFF01, 0x3002, 0x3F, 0x21, 0x2E))
_SEP = "".join(chr(c) for c in (0x3A, 0x3D, 0xFF1A, 0xFF1D))
_CELLS = re.compile(
    "[^" + _WS + _PUNCT + _SEP + "]{2,12}" + "[" + _SEP + "]"
    + "[^" + _WS + _PUNCT + "]{0,40}" + "[" + _WS + _PUNCT + "]+"
)


#: 报错文本里的“前缀: 细节”不是粘贴的记录。
_ERROR_PREFIX = re.compile(
    "(?:DETAIL|HINT|ERROR|FATAL|CONTEXT|SQLSTATE|NOTICE|WARNING|LINE|原始报错|报错|错误)\\s*[:：]",
    re.I,
)


def reject_pasted_table(original: str) -> None:
    """原始问句里像粘贴表格/成段记录时直接拒收（不猜，不代替换）。"""
    lines = [ln for ln in original.splitlines() if ln.strip()]
    if any(("\t" in ln) or ln.count("|") >= 2 for ln in lines):
        raise ContractError("pasted_table", "问句含粘贴的表格（已拒收）")
    cells = [c for c in _CELLS.findall(original) if not _ERROR_PREFIX.match(c)]
    if len(cells) >= 3:
        raise ContractError("pasted_rows", "问句含成段记录（已拒收）")
    if len(original) > 400 and len(lines) <= 2:
        raise ContractError("pasted_rows", "问句过长且不像单个问题（已拒收）")
    if re.search(r"(?m)^\s*[\d.,%-]+(?:\s+[\d.,%-]+){2,}\s*$", original):
        raise ContractError("pasted_table", "问句含数字表格行（已拒收）")


def _depth_ok(value: Any, max_depth: int, depth: int = 0) -> bool:
    if depth > max_depth:
        return False
    if isinstance(value, dict):
        return all(_depth_ok(v, max_depth, depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(_depth_ok(v, max_depth, depth + 1) for v in value)
    return True


def _check_keys(value: Any, *, path: str, table_item: bool = False) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            if not isinstance(key, str):
                raise ContractError("bad_key", f"{path} 含非字符串键")
            low = key.lower()
            if low in FORBIDDEN_KEYS and not (low == "headers" and table_item):
                raise ContractError("forbidden_key", f"{path} 含不允许的键（已拒收）", key_len=len(key))
            if low not in _ALLOWED_KEYS:
                raise ContractError("unknown_key", f"{path} 含契约外的键（已拒收）", key_len=len(key))
            if low == "headers" and table_item:
                if not isinstance(sub, list) or not all(isinstance(h, str) for h in sub):
                    raise ContractError("bad_shape", f"{path}.headers 必须是字符串列表")
                continue
            _check_keys(sub, path=f"{path}.?")
    elif isinstance(value, list):
        for item in value:
            _check_keys(item, path=f"{path}[]", table_item=isinstance(item, dict) and set(item) == {"table", "headers"})


def validate_dictionary(
    cfg: GatewayConfig, purpose: str, dictionary: Mapping[str, Any]
) -> dict[str, Any]:
    """按用途校验授权字典的形状，返回重建后的新字典（只保留允许的键）。"""
    pol = cfg.purpose(purpose)
    if not isinstance(dictionary, Mapping):
        raise ContractError("bad_dictionary", "dictionary 必须是对象")
    extra = set(dictionary) - pol.allowed_keys
    if extra:
        raise ContractError(
            "extra_keys", f"{purpose} 字典含不允许的顶层键（已拒收）", count=len(extra)
        )
    if not dictionary:
        raise ContractError("empty_dictionary", "dictionary 不能为空")
    encoded = stable_json(dictionary)
    if len(encoded) > cfg.limits.dictionary_bytes:
        raise ContractError("dictionary_too_large", "dictionary 超出大小上限", size=len(encoded))
    _check_keys(dictionary, path="dictionary")
    if not _depth_ok(dictionary, cfg.limits.json_depth):
        raise ContractError("too_deep", "dictionary 嵌套过深")
    rebuilt: dict[str, Any] = {}
    for key in pol.allowed_keys:
        if key not in dictionary:
            continue
        value = dictionary[key]
        kind, sub_keys = pol.shape[key]
        if kind == "str":
            if not isinstance(value, str):
                raise ContractError("bad_shape", "字典字段类型不符")
            rebuilt[key] = value
        else:
            if not isinstance(value, list):
                raise ContractError("bad_shape", "字典字段类型不符")
            items: list[Any] = []
            for item in value:
                if sub_keys is not None and isinstance(item, dict):
                    items.append({k: item[k] for k in sub_keys if k in item})
                else:
                    if sub_keys is not None and not isinstance(item, str):
                        raise ContractError("bad_shape", "字典列表项类型不符")
                    items.append(item)
            rebuilt[key] = items
    for item in rebuilt.get("source_tables", []):
        if isinstance(item, dict) and "headers" in item:
            item["headers"] = [_scrub_value(h, cfg) for h in item["headers"]]
    for key, value in dictionary.items():
        if isinstance(value, str) and pol.shape[key][0] == "str":
            rebuilt[key] = _scrub_value(value, cfg)
    return rebuilt


def _scrub_value(value: str, cfg: GatewayConfig) -> str:
    """字典里的表头/说明也走一遍敏感值替换（不建参数，不保留原值）。"""
    redacted = redact_question(value)
    assert_clean(redacted.text, where="dictionary")
    return redacted.text


# ------------------------------------------------------------------ 请求 --


@dataclass(frozen=True)
class OutboundRequest:
    """重建后的出站请求。``dictionary`` 是校验后的字典（与 body 完全一致）。"""

    url: str
    purpose: str
    model: str
    body: bytes
    system_version: str
    dictionary_digest: str
    dictionary_keys: tuple[str, ...]
    question: str
    question_digest: str
    redaction: Mapping[str, Any]
    dictionary: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))

    def __repr__(self) -> str:  # 不展开正文与字典，避免误入日志
        return (
            f"OutboundRequest(purpose={self.purpose!r}, url={self.url!r}, "
            f"dictionary_digest={self.dictionary_digest[:12]}…, "
            f"question_digest={self.question_digest[:12]}…, bytes={len(self.body)})"
        )


def build_payload(
    cfg: GatewayConfig, purpose: str, question: str, dictionary: Mapping[str, Any]
) -> dict[str, Any]:
    """从零重建请求体：只含固定参数 + versioned system + {question, dictionary}。"""
    request = build_request(cfg, purpose, question, dictionary, skip_redaction_check=True)
    return json.loads(request.body.decode("utf-8"))


def build_request(
    cfg: GatewayConfig,
    purpose: str,
    question: str,
    dictionary: Mapping[str, Any],
    *,
    skip_redaction_check: bool = False,
) -> OutboundRequest:
    """构造出域请求。任何不合契约的输入在发出前抛 :class:`ContractError`。"""
    pol = cfg.purpose(purpose)
    if not isinstance(question, str) or not question.strip():
        raise ContractError("empty_question", "question 必须是非空字符串")
    if purpose == MAPPING and question != MAPPING_TASK_QUESTION:
        raise ContractError("fixed_task_required", "mapping 用途只接受固定映射任务说明")
    if len(question.encode("utf-8")) > cfg.limits.question_bytes:
        raise ContractError("question_too_large", "question 超出大小上限")
    sanitized = validate_dictionary(cfg, purpose, dictionary)
    if not skip_redaction_check:
        if purpose == QUERY:
            reject_pasted_table(question)
        assert_clean(question)

    user = stable_json({"question": question, "dictionary": sanitized}).decode("utf-8")
    payload: dict[str, Any] = {
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "messages": [
            {"role": "system", "content": pol.system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > cfg.limits.request_bytes:
        raise ContractError("request_too_large", "重建后的请求超出大小上限", size=len(body))
    return OutboundRequest(
        url=cfg.request_url(),
        purpose=purpose,
        model=cfg.model,
        body=body,
        system_version=cfg.system_version,
        dictionary_digest=dictionary_digest(sanitized),
        dictionary_keys=tuple(sorted(sanitized)),
        question=question,
        question_digest=_sha256_bytes(question.encode("utf-8")),
        redaction={},
        dictionary=sanitized,
    )


def prepare(
    cfg: GatewayConfig,
    purpose: str,
    question: str,
    dictionary: Mapping[str, Any],
    *,
    bag: LocalParameterBag | None = None,
) -> tuple[OutboundRequest, Redaction]:
    """query：本地脱敏 → 重建请求；mapping：固定任务说明 + 校验后重建请求。"""
    if purpose == QUERY:
        reject_pasted_table(question)
        redaction = (
            redact_question_with_bag(question, bag) if bag is not None else redact_question(question)
        )
        assert_clean(redaction.text)
        request = replace(
            build_request(cfg, purpose, redaction.text, dictionary),
            redaction=redaction.summary(),
        )
        return request, redaction
    if purpose == MAPPING:
        reject_pasted_table(question)
        return build_request(cfg, purpose, question, dictionary), Redaction(text=question, count=0)
    raise PurposeNotAllowed(f"用途 {purpose!r} 不在允许的用途内")


# ------------------------------------------------------------------ 响应 --


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ResponseError("duplicate_key", "响应 JSON 含重复键")
        out[key] = value
    return out


def parse_model_json(text: str, *, max_bytes: int, max_depth: int) -> dict[str, Any]:
    """严格解析模型给出的 JSON：唯一对象、无重复键、深度受限、大小受限。"""
    if not isinstance(text, str) or not text.strip():
        raise ResponseError("empty_content", "模型内容为空")
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ResponseError("content_too_large", "模型内容超出大小上限", size=len(encoded))
    stripped = text.strip()
    if stripped.startswith("```") or not stripped.startswith("{"):
        raise ResponseError("not_json", "模型内容不是单个 JSON 对象")
    try:
        data = json.loads(stripped, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError:
        raise ResponseError("invalid_json", "模型内容不是合法 JSON") from None
    if not isinstance(data, dict):
        raise ResponseError("not_object", "模型内容不是 JSON 对象")
    if not _depth_ok(data, max_depth):
        raise ResponseError("too_deep", "模型内容嵌套过深")
    return data


def check_mapping_candidates(
    data: Mapping[str, Any],
    dictionary: Mapping[str, Any],
    *,
    max_candidates: int = 64,
    max_per_field: int = 3,
) -> list[dict[str, Any]]:
    """校验映射候选：只能在授权表头里选，单位只能在授权单位里选。"""
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise ResponseError("missing_candidates", "映射响应缺少 candidates 列表")
    known_headers: set[tuple[str, str]] = set()
    for table in dictionary.get("source_tables", []):
        if isinstance(table, dict):
            name = str(table.get("table", ""))
            headers = table.get("headers") if isinstance(table.get("headers"), list) else []
            for header in headers:
                known_headers.add((name, str(header)))
    known_units = {str(u) for u in dictionary.get("units", []) if isinstance(u, str)}
    known_fields = {
        str(f.get("field", "")) for f in dictionary.get("standard_fields", []) if isinstance(f, dict)
    }
    out: list[dict[str, Any]] = []
    per_field: dict[str, int] = {}
    for item in candidates:
        if not isinstance(item, dict):
            raise ResponseError("bad_candidate", "候选项必须是对象")
        field = item.get("standard_field")
        table = item.get("source_table")
        header = item.get("source_header")
        if not all(isinstance(x, str) and x for x in (field, table, header)):
            raise ResponseError("bad_candidate", "候选缺少 standard_field/source_table/source_header")
        if field not in known_fields:
            raise ResponseError("unknown_field", "候选引用了字典外的标准字段")
        if (table, header) not in known_headers:
            raise ResponseError("unknown_header", "候选引用了审核表头之外的来源")
        unit = item.get("unit", "")
        if not isinstance(unit, str):
            raise ResponseError("bad_candidate", "候选单位必须是字符串")
        if unit and unit not in known_units:
            raise ResponseError("unknown_unit", "候选单位不在审核单位内")
        confidence = item.get("confidence", 0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ResponseError("bad_candidate", "候选置信度必须是数字")
        per_field[field] = per_field.get(field, 0) + 1
        if per_field[field] > max_per_field:
            raise ResponseError("too_many_candidates", "同一标准字段的候选过多")
        reason = item.get("reason", "")
        if not isinstance(reason, str):
            raise ResponseError("bad_candidate", "候选说明必须是字符串")
        out.append(
            {
                "standard_field": field,
                "source_table": table,
                "source_header": header,
                "unit": unit,
                "confidence": float(confidence),
                "reason": reason,
            }
        )
    if len(out) > max_candidates:
        raise ResponseError("too_many_candidates", "候选总数过多")
    return out


_SQL_START = re.compile(r"^\s*(?:\(\s*)*(?:select|with)\b", re.I)
_SQL_FORBIDDEN = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|merge|call|do|"
    r"pg_sleep|pg_read_file|pg_terminate_backend|lo_import|dblink|set_config|current_setting)\b",
    re.I,
)
_SQL_MULTI = re.compile(r";\s*\S")


def check_query_output(
    data: Mapping[str, Any], dictionary: Mapping[str, Any]
) -> dict[str, Any]:
    """校验 SQL 生成响应：只读单条语句、列在授权视图内。完整 AST 校验在步骤 10。"""
    status = data.get("status")
    if not isinstance(status, str):
        raise ResponseError("missing_status", "SQL 响应缺少 status")
    if status in {"clarify", "unsupported"}:
        reason = data.get("reason", "")
        if not isinstance(reason, str):
            raise ResponseError("bad_reason", "reason 必须是字符串")
        return {"status": status, "missing": list(data.get("missing") or []), "reason": reason}
    if status != "ok":
        raise ResponseError("bad_status", "status 只能是 ok / clarify / unsupported")
    sql = data.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ResponseError("missing_sql", "响应缺少 sql")
    if not _SQL_START.match(sql):
        raise ResponseError("not_select", "sql 必须是单条只读 SELECT")
    if _SQL_FORBIDDEN.search(sql):
        raise ResponseError("forbidden_sql", "sql 含写操作或禁止的函数")
    if _SQL_MULTI.search(sql):
        raise ResponseError("multi_statement", "sql 含多条语句")
    if "--" in sql or "/*" in sql:
        raise ResponseError("sql_comment", "sql 不允许注释")
    authorized_views = {
        str(v.get("name", "")) for v in dictionary.get("views", []) if isinstance(v, dict)
    }
    authorized_columns = {
        str(f.get("name", "")) for f in dictionary.get("fields", []) if isinstance(f, dict)
    }
    import sqlglot

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError:
        raise ResponseError("sql_syntax", "sql 语法不合法") from None
    if len(statements) != 1 or statements[0] is None:
        raise ResponseError("multi_statement", "sql 必须是单条语句")
    tree = statements[0]
    if not isinstance(tree, (sqlglot.exp.Select, sqlglot.exp.Union)):
        raise ResponseError("not_select", "sql 必须是 SELECT")
    tables = {t.name for t in tree.find_all(sqlglot.exp.Table) if t.name}
    unknown_tables = tables - authorized_views
    if unknown_tables:
        raise ResponseError(
            "unknown_view", "sql 引用了授权视图之外的视图", count=len(unknown_tables)
        )
    columns = {c.name for c in tree.find_all(sqlglot.exp.Column) if c.name}
    unknown_columns = columns - authorized_columns
    if unknown_columns:
        raise ResponseError(
            "unknown_column", "sql 引用了授权字段之外的列", count=len(unknown_columns)
        )
    params = data.get("params", [])
    if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
        raise ResponseError("bad_params", "params 必须是字符串列表")
    placeholders = set(re.findall(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)", sql))
    if not placeholders <= set(params):
        raise ResponseError("undeclared_param", "sql 使用了未声明的参数")
    out_columns = data.get("output_columns", [])
    if not isinstance(out_columns, list):
        raise ResponseError("bad_output_columns", "output_columns 必须是列表")
    return {
        "status": "ok",
        "sql": sql,
        "params": list(params),
        "output_columns": [
            c for c in out_columns if isinstance(c, dict) and isinstance(c.get("name"), str)
        ],
        "reason": str(data.get("reason", "")),
        "tables": sorted(tables),
    }


def check_content(
    cfg: GatewayConfig, purpose: str, content: str, dictionary: Mapping[str, Any]
) -> dict[str, Any]:
    data = parse_model_json(
        content, max_bytes=cfg.limits.content_bytes, max_depth=cfg.limits.json_depth
    )
    if purpose == MAPPING:
        return {"kind": MAPPING, "candidates": check_mapping_candidates(data, dictionary)}
    if purpose == QUERY:
        return {"kind": QUERY, **check_query_output(data, dictionary)}
    raise PurposeNotAllowed(f"用途 {purpose!r} 不在允许的用途内")


# ------------------------------------------------------------------ 审计 --

#: 只认“密钥形态”：sk-/pk-/rk- 前缀、Bearer 头、调用方显式密钥值。哈希摘要不是密钥，
#: 不能因为“长得像随机串”就被替换掉（否则审计无法核对是哪次请求）。
_KEYLIKE = re.compile(r"(\bsk-[A-Za-z0-9_-]{6,})|(\bBearer\s+\S+)")
_SECRET_FIELD_NAMES = frozenset(
    {"authorization", "api_key", "apikey", "key", "token", "password", "secret", "credential"}
)
_AUDIT_FIELDS = (
    "request_id",
    "ts",
    "caller",
    "purpose",
    "dictionary_digest",
    "dictionary_keys",
    "question_digest",
    "redactions",
    "redaction_categories",
    "system_version",
    "model",
    "transport",
    "attempts",
    "duration_ms",
    "elapsed_ms",
    "status",
    "error_kind",
    "error_code",
    "http_status",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "request_bytes",
    "response_bytes",
    "content_bytes",
    "fingerprint",
)


def scrub_audit_value(value: Any, *, secret: str | None = None, depth: int = 0) -> Any:
    """过滤审计的额外信息：白名单键、深度受限、密钥形态的值一律丢弃。"""
    if depth > 3:
        return "[dropped]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if not isinstance(key, str) or key.lower() in _SECRET_FIELD_NAMES:
                continue
            out[key] = scrub_audit_value(sub, secret=secret, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [scrub_audit_value(v, secret=secret, depth=depth + 1) for v in value[:32]]
    if isinstance(value, str):
        if secret and len(secret) >= 6 and secret in value:
            return "[redacted]"
        return _KEYLIKE.sub("[redacted]", value)[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "[dropped]"


class AuditLog:
    """本地 JSONL 审计。只追加；每行一个 JSON；不含问题原文、字典原文或密钥。"""

    def __init__(self, path: Path, *, secret: str | None = None) -> None:
        self.path = Path(path)
        self.secret = secret
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, Any]) -> dict[str, Any]:
        # 未显式传入密钥时从环境读取（只用于“值是否出现”的比对，绝不写入）。
        secret = self.secret
        if secret is None:
            secret = os.environ.get(app_config.LLM_KEY_ENV)
        entry: dict[str, Any] = {}
        for key in _AUDIT_FIELDS:
            if key not in record:
                continue
            entry[key] = scrub_audit_value(record[key], secret=secret)
        entry.setdefault("ts", _utc_now())
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        # 落盘前再查一次：任何长随机短语/密钥前缀都不应出现。
        if _KEYLIKE.search(line) or (secret and len(secret) >= 6 and secret in line):
            for key in list(entry):
                if isinstance(entry[key], str):
                    entry[key] = "[redacted]" if _KEYLIKE.search(entry[key]) else entry[key]
            line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            created = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if created:
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
        return entry

    def read(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"unparsed": True})
        return rows


# ------------------------------------------------------------------ 工具 --


def new_request_id() -> str:
    import secrets

    return secrets.token_hex(8)


def now() -> float:
    return time.monotonic()


def iso_now() -> str:
    return _utc_now()


def iter_key_paths(value: Any, path: str = "") -> Iterable[str]:
    """调试用：列出 JSON 的键路径（不含值）。"""
    if isinstance(value, dict):
        for key, sub in value.items():
            yield f"{path}.{key}" if path else key
            yield from iter_key_paths(sub, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for item in value:
            yield from iter_key_paths(item, f"{path}[]")
