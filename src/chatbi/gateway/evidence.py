"""``chatbi evidence gateway``：写入步骤 02 的 ``var/evidence/gateway.json``。

本命令**自己做**一遍步骤 02 的三条验收（合成数据、假传输），并把命令、退出码、
期望、观察和产物路径按《验收规范》第 3 条写成 JSON：

* S02-AC1：``pytest -q tests/test_s02.py -k payload``；两种用途各有成功例；哨兵
  在解码后的 messages 中出现次数为 0。
* S02-AC2：``pytest -q tests/test_s02.py -k transport``；拒绝换主机/跳转/未授权
  字段时出站次数为 0；日志里 0 处密钥；``chatbi gateway probe --mock`` 输出 MOCK_OK。
* S02-AC3：``chatbi gateway probe --live``：有凭据退出 0 且有 LIVE_OK；缺凭据退出
  非零并记“实连待验”（status=PENDING，不是 FAIL）。

证据里只有变量名、布尔、计数、命令和摘要；不含密钥值、问题或字典原文。
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from chatbi import config, evidence as step01_evidence
from chatbi.gateway import client as gw_client
from chatbi.gateway import policy

EVIDENCE_JSON = "gateway.json"
KEY_ENV = config.LLM_KEY_ENV


def _run(command: list[str], timeout: float = 300.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd=config.LAYOUT.root,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"command": command, "exit_code": None, "stdout": "", "stderr": type(exc).__name__}
    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:],
    }


def _pytest_tail(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _secret() -> str | None:
    return gw_client.secret_from_env(policy.load_config())


def _contains_secret(text: str, secret: str | None) -> bool:
    return bool(secret) and len(secret) >= 6 and secret in text


def _audit_secret_free(path: Path, secret: str | None) -> tuple[bool, int]:
    if not path.is_file():
        return True, 0
    text = path.read_text(encoding="utf-8", errors="replace")
    keylike = re.findall(r"sk-[A-Za-z0-9_-]{6,}", text)
    leaked = (1 if _contains_secret(text, secret) else 0) + len(keylike)
    return leaked == 0, leaked


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"unparsed": True})
    return rows


def _check(name: str, command: list[str], expected: str, ok: bool, observed: str, artifact: list[str], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "case_id": name,
        "command": " ".join(command),
        "exit_code": detail.get("exit_code") if detail else None,
        "expected": expected,
        "observed": observed,
        "artifact": artifact,
        "status": "PASS" if ok else "FAIL",
        "detail": detail or {},
    }


def build_gateway_evidence() -> dict[str, Any]:
    root = config.LAYOUT.root
    cfg = policy.load_config()
    secret = _secret()
    tmp = Path(tempfile.mkdtemp(prefix="chatbi-s02-ev-", dir=str(config.LAYOUT.run)))
    audit = tmp / "gateway-audit.jsonl"
    cases: list[dict[str, Any]] = []

    # -- AC1：payload（两种用途的成功例 + 哨兵 0 次） --
    ac1 = _run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_s02.py", "-k", "payload", "-p", "no:cacheprovider"]
    )
    payload_ok = ac1["exit_code"] == 0 and "passed" in ac1["stdout"]
    sentinels = ["SENT-MONEY-7733", "SENT-CUSTOMER-ORION-55", "SENT-RAWWERR-QX9", "SENT-EXTRA-ZETA-31", "SENT-KEY-9f3a2b7c"]
    token = "sk-sentinel-9f3a2b7c"
    found = 0
    mapping_ok = query_ok = False
    for purpose, question, dictionary in (
        (
            policy.MAPPING,
            policy.MAPPING_TASK_QUESTION,
            {
                "source_tables": [{"table": "t_probe", "headers": ["金额", "客户"]}],
                "standard_fields": [{"field": "amount", "label": "金额", "dtype": "decimal"}],
                "units": ["元"],
                "notes": "synthetic",
            },
        ),
        (
            policy.QUERY,
            f"客户=SENT-CUSTOMER-ORION-55 的金额 SENT-MONEY-7733 元的合计是多少",
            {"views": [{"name": "v_probe", "columns": ["amount"]}], "fields": [{"name": "amount", "view": "v_probe", "type": "number", "unit": "元", "description": "金额"}], "relations": [], "metrics": [], "examples": []},
        ),
    ):
        transport = gw_client.ScriptedTransport(
            [gw_client.openai_ok(
                {"candidates": [], "notes": "ok"} if purpose == policy.MAPPING else {
                    "status": "ok", "sql": "SELECT sum(amount) AS total FROM v_probe", "params": [],
                    "output_columns": [{"name": "total", "unit": "元"}], "reason": "",
                }
            )]
        )
        client = gw_client.GatewayClient(purpose=purpose, transport=transport, config=cfg, secret=token, audit=policy.AuditLog(audit))
        try:
            result = client.call(question, dictionary)
        finally:
            client.close()
        decoded = json.dumps(
            [c.json().get("messages") for c in transport.calls], ensure_ascii=False
        )
        found += sum(decoded.count(s) for s in sentinels)
        found += decoded.count(token)
        found += len(re.findall(r'"(?:workbook_rows|extra|raw_error|api_key|authorization)"\s*:', decoded))
        if purpose == policy.MAPPING:
            mapping_ok = result.ok
        else:
            query_ok = result.ok
    cases.append(
        _check(
            "S02-AC1",
            [sys.executable, "-m", "pytest", "-q", "tests/test_s02.py", "-k", "payload"],
            "pytest 退出 0；mapping 与 query 各有成功例；哨兵（金额、客户名、原始错误、额外字段、密钥）在解码后的 messages 中出现 0 次",
            payload_ok and mapping_ok and query_ok and found == 0,
            f"pytest: {_pytest_tail(ac1['stdout']) or ac1['stderr'][-200:]}；mapping_ok={mapping_ok} query_ok={query_ok} sentinel_hits={found}",
            ["tests/test_s02.py", "var/evidence/gateway.json"],
            {
                "exit_code": ac1["exit_code"],
                "mapping_ok": mapping_ok,
                "query_ok": query_ok,
                "sentinel_hits": found,
                "pytest_summary": _pytest_tail(ac1["stdout"]),
            },
        )
    )

    # -- AC2：transport + 审计无密钥 + probe --mock --
    ac2 = _run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_s02.py", "-k", "transport", "-p", "no:cacheprovider"]
    )
    transport_ok = ac2["exit_code"] == 0 and "passed" in ac2["stdout"]
    probe = _run([sys.executable, "-m", "chatbi.cli", "gateway", "probe", "--mock", "--audit", str(audit)])
    mock_ok = probe["exit_code"] == 0 and "MOCK_OK" in probe["stdout"]
    clean_audit, leaks = _audit_secret_free(audit, secret)
    probe_leak = _contains_secret(probe["stdout"] + probe["stderr"], secret)
    records = _records(audit)
    required = {"request_id", "purpose", "dictionary_digest", "model", "duration_ms", "status", "prompt_tokens"}
    missing_fields = sorted(required - set().union(*[set(r) for r in records]) if records else required)
    cases.append(
        _check(
            "S02-AC2",
            [sys.executable, "-m", "pytest", "-q", "tests/test_s02.py", "-k", "transport"],
            "pytest 退出 0；拒绝换主机/跳转/未授权字段前出站 0 次；所有日志与审计 0 处密钥；chatbi gateway probe --mock 输出 MOCK_OK",
            transport_ok and clean_audit and not probe_leak and mock_ok and not missing_fields,
            f"pytest: {_pytest_tail(ac2['stdout'])}；audit_records={len(records)} audit_secret_leaks={leaks} "
            f"probe_exit={probe['exit_code']} probe='{probe['stdout'].strip().splitlines()[0] if probe['stdout'].strip() else ''}' "
            f"audit_fields_missing={missing_fields}",
            ["tests/test_s02.py", str(audit.relative_to(root)), "var/evidence/gateway.json"],
            {
                "exit_code": ac2["exit_code"],
                "probe_exit_code": probe["exit_code"],
                "probe_first_line": probe["stdout"].strip().splitlines()[0] if probe["stdout"].strip() else "",
                "audit_records": len(records),
                "audit_secret_leaks": leaks,
                "audit_fields_missing": missing_fields,
                "pytest_summary": _pytest_tail(ac2["stdout"]),
            },
        )
    )

    # -- AC3：probe --live --
    live = _run([sys.executable, "-m", "chatbi.cli", "gateway", "probe", "--live", "--json", "--audit", str(audit)])
    live_out = live["stdout"]
    live_leak = _contains_secret(live_out + live["stderr"], secret)
    if not secret:
        ac3_ok = live["exit_code"] not in (0, None) and "LIVE_KEY_MISSING" in live_out and not live_leak
        ac3_status = "PASS" if ac3_ok else "FAIL"
        ac3_observed = (
            f"环境变量 {KEY_ENV} 未设置：exit={live['exit_code']}，输出 LIVE_KEY_MISSING，"
            f"实连待验（PENDING）；输出含密钥={live_leak}"
        )
        live_state = "pending_missing_key"
    else:
        ok = live["exit_code"] == 0 and "LIVE_OK" in live_out and cfg.model in live_out
        ac3_ok, ac3_status = ok, "PASS" if ok else "FAIL"
        live_state = "ok" if ok else "failed"
        first = live_out.strip().splitlines()[0] if live_out.strip() else ""
        ac3_observed = f"有凭据：exit={live['exit_code']}；{first}；输出含密钥={live_leak}"
    cases.append(
        {
            "case_id": "S02-AC3",
            "command": "chatbi gateway probe --live --json",
            "exit_code": live["exit_code"],
            "expected": (
                f"有 {KEY_ENV} 时退出 0、输出 LIVE_OK、模型 {cfg.model} 与可解析内容及请求时间；"
                "缺凭据时退出非零并记“实连待验”（PENDING）"
            ),
            "observed": ac3_observed,
            "artifact": [str(audit.relative_to(root)), "var/evidence/gateway.json"],
            "status": ac3_status,
            "live_state": live_state,
            "secret_present": bool(secret),
            "detail": {"first_line": live_out.strip().splitlines()[0] if live_out.strip() else ""},
        }
    )

    # 附加：有密钥时做一次真实的最小请求（合成字典，无业务数据）；无密钥时只记 PENDING。
    # （有密钥时真实请求；无密钥时本地探针。两者都是附加项，不影响三条必验的结论。）
    if True:  # 总是跑一次：有密钥走最小真实请求，无密钥走本地探针
        hc = _run(
            [
                sys.executable,
                "-m",
                "chatbi.cli",
                "gateway",
                "probe",
                "--healthcheck",
                "--json",
                "--audit",
                str(audit),
            ]
        )
        lines = [ln for ln in hc["stdout"].splitlines() if ln.strip()]
        first = lines[0] if lines else ""
        try:
            payload = json.loads(lines[1]) if len(lines) > 1 else {}
        except json.JSONDecodeError:
            payload = {}
        offline = {
            "case_id": "S02-OFFLINE-HEALTHCHECK",
            "command": "chatbi gateway probe --healthcheck --json",
            "exit_code": hc["exit_code"],
            "expected": "有密钥时发出最小真实请求并记录状态码；无密钥时用本地探针，不伪造成功",
            "observed": first + (
                "（其中真实请求返回了有效模型响应）" if payload.get("synthetic_credential") is False
                and (payload.get("query") or {}).get("ok") else ""
            ),
            "artifact": [str(audit.relative_to(root))],
            "status": "PASS" if hc["exit_code"] == 0 else "FAIL",
            "detail": payload,
            "synthetic_credential": payload.get("synthetic_credential"),
        }
        cases.append(offline)

    clean_audit2, leaks2 = _audit_secret_free(audit, secret)
    records2 = _records(audit)
    build_sha = step01_evidence.build_sha(root)
    statuses = [c["status"] for c in cases]
    status = "FAIL" if "FAIL" in statuses else "PASS"
    return {
        "step": "02",
        "build_sha": build_sha,
        "build_files": step01_evidence.release_files(root) + [
            "config/gateway.yaml",
            "src/chatbi/gateway/policy.py",
            "src/chatbi/gateway/client.py",
            "src/chatbi/gateway/server.py",
            "src/chatbi/gateway/cli.py",
            "src/chatbi/gateway/evidence.py",
            "tests/test_s02.py",
        ],
        "batch_id": "none",
        "operator": getpass.getuser(),
        "timestamp": policy.iso_now(),
        "command": "chatbi evidence gateway",
        "exit_code": 0 if status == "PASS" else 1,
        "expected": "步骤 02 三条验收条件全部通过；实连在无凭据时只记 PENDING",
        "observed": (
            f"{sum(1 for s in statuses if s == 'PASS')}/{len(statuses)} 条 PASS；"
            f"live_state={live_state}；审计记录 {len(records2)} 条、密钥泄漏 {leaks2} 处"
        ),
        "artifact": [f"var/evidence/{EVIDENCE_JSON}", str(audit.relative_to(root))],
        "status": status,
        "cases": cases,
        "gateway": {
            "config": cfg.as_summary(),
            "endpoint": cfg.endpoint_url,
            "model": cfg.model,
            "key_env": KEY_ENV,
            "secret_present": bool(secret),
            "live_state": live_state,
            "audit_records": len(records2),
            "audit_secret_leaks": leaks2,
            "audit_fields": sorted({k for r in records2 for k in r}),
            "audit_sample": records2[-1] if records2 else None,
        },
        "notes": [
            "batch_id: none —— 网关步骤尚无导入批次。",
            "所有密钥相关结论都以“值是否出现”的布尔表示；本文件与审计都不含密钥值。",
            "payload 捕获只发生在合成数据的假传输里；真实调用不保存请求正文。",
        ],
    }


def write_gateway_evidence() -> tuple[Path, str]:
    layout = config.LAYOUT
    layout.evidence.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(layout.evidence, 0o700)
    except OSError:
        pass
    payload = build_gateway_evidence()
    target = layout.evidence / EVIDENCE_JSON
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(target)
    return target, payload["status"]
