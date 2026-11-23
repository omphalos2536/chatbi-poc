"""``chatbi gateway ...`` 子命令（步骤 02）。

    chatbi gateway probe --mock         走完整请求构建 + 假传输，打印 MOCK_OK（不联网）
    chatbi gateway probe --live         用合成字典问题真实调用；缺密钥时打印 LIVE_KEY_MISSING
                                        并退出 2（“实连待验”，离线步骤可继续）
    chatbi gateway probe --healthcheck  本地服务冒烟自检（不发送问题与字典）
    chatbi gateway serve                在本机 Unix socket 上启动网关（前台阻塞）
    chatbi gateway call                 通过 Unix socket 发一题（本机调试）

所有输出都经过“密钥出现在输出里”的兜底过滤；任何参数、审计与输出都不写密钥值。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from chatbi import config as app_config
from chatbi.gateway import client as gw_client
from chatbi.gateway import policy
from chatbi.gateway import server as gw_server
from chatbi.gateway.policy import AuditLog, GatewayConfig

#: probe 的合成字典与问题（无业务数据）：与 policy 里的 PROBE_* 同源。
PROBE_QUESTION = policy.PROBE_QUESTION
PROBE_DICTIONARY = policy.PROBE_DICTIONARY

LIVE_KEY_MISSING = "LIVE_KEY_MISSING"
MOCK_OK = "MOCK_OK"
LIVE_OK = "LIVE_OK"


def _mask(text: str) -> str:
    secret = os.environ.get(app_config.LLM_KEY_ENV)
    return gw_client.redact_secret(text, secret)


def _print(*parts: Any) -> None:
    print(*[_mask(str(p)) for p in parts])


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[attr-defined]
    parser = sub.add_parser("gateway", help="模型网关：probe / serve / call（步骤 02）")
    gw = parser.add_subparsers(dest="gateway_command", metavar="<command>")

    probe = gw.add_parser("probe", help="网关探针：--mock / --live / --healthcheck")
    mode = probe.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="假传输完整路径，打印 MOCK_OK")
    mode.add_argument("--live", action="store_true", help="真实调用合成字典问题，打印 LIVE_OK")
    mode.add_argument(
        "--healthcheck", action="store_true", help="本地冒烟自检（默认；不发问题与字典）"
    )
    probe.add_argument("--json", action="store_true", help="额外输出一行 JSON 摘要")
    probe.add_argument(
        "--audit", default=None, help="审计文件路径（默认 var/audit/gateway.jsonl）"
    )

    serve = gw.add_parser("serve", help="在 Unix socket 上启动网关（前台）")
    serve.add_argument("--ready-file", default=None, help="就绪后写入 socket 路径（测试用）")

    call = gw.add_parser("call", help="通过 Unix socket 发一题（本机调试）")
    call.add_argument("--caller", default="live-probe", help="本地配置里的调用方标识")
    call.add_argument("--purpose", default=None, choices=["mapping", "query"])
    call.add_argument("--question", required=True)
    call.add_argument("--dictionary", required=True, help="字典 JSON 文件（只读本地）")
    call.add_argument("--json", action="store_true")

    return parser


def _audit_for(args: argparse.Namespace) -> AuditLog | None:
    path = getattr(args, "audit", None)
    if path is None:
        return None
    return AuditLog(Path(path))


def run(args: argparse.Namespace) -> int:
    if args.gateway_command == "probe":
        cfg = policy.load_config()
        audit = _audit_for(args)
        if args.mock:
            return probe_mock(cfg, audit, as_json=args.json)
        if args.live:
            return probe_live(cfg, audit, as_json=args.json)
        return probe_healthcheck(cfg, audit, as_json=args.json)
    if args.gateway_command == "serve":
        return serve(args)
    if args.gateway_command == "call":
        return call(args)
    _print("用法：chatbi gateway {probe|serve|call} ...")
    return 2


def probe_mock(cfg: GatewayConfig, audit: AuditLog | None, *, as_json: bool) -> int:
    outcome = gw_client.probe_mock(config=cfg, audit=audit)
    ok = bool(outcome["ok"]) and outcome["status"] == 200
    _print(f"{MOCK_OK if ok else 'MOCK_FAIL'} transport=mock model={outcome['model']} "
           f"status={outcome['status']} request_id={outcome['request_id']}")
    if as_json:
        _print(json.dumps({k: v for k, v in outcome.items()}, ensure_ascii=False))
    return 0 if ok else 1


def probe_healthcheck(cfg: GatewayConfig, audit: AuditLog | None, *, as_json: bool) -> int:
    outcome = gw_client.probe_healthcheck(config=cfg, audit=audit)
    query = outcome["query"]
    mapping = outcome["mapping"]
    # 本地探针会把请求改写为 HEAD：远端回 2xx 但响应体不是模型信封，所以这里以
    # “请求确实构造并发出、审计已写、错误类别可识别”为健康标志。
    healthy = query["outbound"] >= 1 and mapping["outbound"] >= 1
    _print(f"{'HEALTHCHECK_OK' if healthy else 'HEALTHCHECK_FAIL'} "
           f"query_status={query.get('http_status')} query_error={query.get('error', {}).get('kind')} "
           f"mapping_error={mapping.get('error', {}).get('kind')} outbound={outcome['outbound']}")
    if as_json:
        _print(json.dumps(outcome, ensure_ascii=False))
    return 0 if healthy else 1


def probe_live(cfg: GatewayConfig, audit: AuditLog | None, *, as_json: bool) -> int:
    started = time.monotonic()
    outcome = gw_client.live_probe(config=cfg, audit=audit)
    if outcome["state"] == "missing_key":
        _print(f"{LIVE_KEY_MISSING} 实连待验：环境变量 {cfg.key_env} 未设置；"
               f"离线步骤可继续（可先运行 chatbi gateway probe --mock）")
        if as_json:
            _print(json.dumps(outcome, ensure_ascii=False))
        return 2
    if outcome["state"] != "ok":
        error = outcome.get("error") or {}
        _print(f"LIVE_FAIL http_status={outcome.get('http_status')} "
               f"error={error.get('kind')}/{error.get('code')} request_id={outcome.get('request_id')} "
               f"elapsed_ms={outcome.get('elapsed_ms')}")
        if as_json:
            _print(json.dumps(outcome, ensure_ascii=False))
        return 1
    requested_at = policy.iso_now()
    _print(f"{LIVE_OK} model={outcome['model']} configured_model={cfg.model} "
           f"http_status={outcome.get('http_status')} content_parsed={outcome['content_is_object']} "
           f"request_id={outcome['request_id']} requested_at={requested_at} "
           f"elapsed_ms={outcome.get('elapsed_ms')} usage={json.dumps(outcome.get('usage'))}")
    if as_json:
        _print(json.dumps({**outcome, "requested_at": requested_at}, ensure_ascii=False))
    return 0


def serve(args: argparse.Namespace) -> int:
    cfg = policy.load_config()
    # 启动器清空其它服务相关的凭据变量（密钥只留 CHATBI_LLM_API_KEY）。
    clean = gw_server.sanitized_env()
    os.environ.clear()
    os.environ.update(clean)
    srv = gw_server.GatewayServer(cfg)
    ready = Path(args.ready_file) if args.ready_file else None
    try:
        if ready is not None:
            ready.write_text(str(srv.path), encoding="utf-8")
        _print(f"gateway listening on {srv.path}")
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


def call(args: argparse.Namespace) -> int:
    cfg = policy.load_config()
    dictionary = json.loads(Path(args.dictionary).read_text(encoding="utf-8"))
    response = gw_server.call(
        cfg, caller=args.caller, question=args.question, dictionary=dictionary,
        purpose=args.purpose,
    )
    if args.json:
        _print(json.dumps(response, ensure_ascii=False))
    else:
        _print(f"ok={response.get('ok')} status={response.get('http_status')} "
               f"parsed={response.get('parsed_kind')} request_id={response.get('request_id')}")
        if not response.get("ok"):
            _print(json.dumps(response.get("error"), ensure_ascii=False))
    return 0 if response.get("ok") else 1
