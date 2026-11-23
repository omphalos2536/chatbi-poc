"""``chatbi doctor``：本机环境自检。

输出只包含版本、资源、连接标识和“密钥是否存在”的布尔值。任何口令、连接串
或密钥值都不会出现在 JSON 或文本里（测试逐字核对）。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from chatbi import config, pglocal


def _resource_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "nproc": os.cpu_count(),
        "machine": platform.machine(),
        "sandboxed_session": config.running_in_sandbox(),
    }
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        info["mem_total_mib"] = round(os.sysconf("SC_PHYS_PAGES") * page / 2**20)
        info["mem_available_mib"] = round(
            os.sysconf("SC_AVPHYS_PAGES") * page / 2**20
        )
    except (ValueError, OSError, AttributeError):
        pass
    try:
        st = os.statvfs(config.LAYOUT.root)
        info["disk_free_gib"] = round(st.f_bavail * st.f_frsize / 2**30, 1)
        info["disk_total_gib"] = round(st.f_blocks * st.f_frsize / 2**30, 1)
    except OSError:
        pass
    try:
        info["loadavg_1m"] = os.getloadavg()[0]
    except OSError:
        pass
    return info


def _dependency_versions() -> dict[str, Any]:
    direct: dict[str, str | None] = {}
    for name in config.PINNED_DIRECT_DEPS:
        try:
            direct[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            direct[name] = None
    try:
        pytest_version: str | None = metadata.version("pytest")
    except metadata.PackageNotFoundError:
        pytest_version = None
    return {"direct": direct, "pytest": pytest_version}


def _tool_versions() -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for name in ("python3", "uv", "node", "psql", "dpkg-deb"):
        path = shutil.which(name)
        version = None
        if path:
            # uv 与 node 认 --version；psql 认 -V；dpkg-deb 认 --version。
            flag = "-V" if name == "psql" else "--version"
            try:
                out = subprocess.run(
                    [path, flag],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    stdin=subprocess.DEVNULL,
                )
                version = (out.stdout or out.stderr).strip().splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                version = None
        tools[name] = {"path": path, "version": version}
    return tools


def _python_info() -> dict[str, Any]:
    ver = platform.python_version()
    return {
        "version": ver,
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "matches_required": ver == config.REQUIRED_PYTHON,
        "required": config.REQUIRED_PYTHON,
    }


def _pg_info() -> dict[str, Any]:
    status = pglocal.probe(config.LAYOUT)
    pg = status.as_dict()
    pg["expected_server_version_num"] = config.REQUIRED_PG_VERSION_NUM
    pg["version_matches"] = status.server_version_num == config.REQUIRED_PG_VERSION_NUM
    pg["binary_matches"] = status.binary_version == config.REQUIRED_PG_BINARY_VERSION
    pg["pgpass_mode"] = _mode(config.LAYOUT.pgpass)
    pg["pgpass_svc_mode"] = _mode(config.LAYOUT.pgpass_svc)
    return pg


def _mode(path: Path) -> str | None:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return None


def _llm_info() -> dict[str, Any]:
    present = bool(os.environ.get(config.LLM_KEY_ENV))
    return {
        "configured": config.LLM_CONFIGURED,
        # 只报告“用什么变量名注入”；值的长度、前缀、哈希都不输出。
        "credential_source": f"env:{config.LLM_KEY_ENV}",
        # 只有布尔值；绝不输出长度、前缀或哈希。步骤 01 的验收只看布尔。
        "api_key_present": present,
        "live_ready": present,
        "state": "ready" if present else "pending_missing_key",
    }


def _bootstrap_info() -> dict[str, Any]:
    path = config.LAYOUT.bootstrap_json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"record": str(path), "present": False}
    return {"record": str(path), "present": True, **data}


def _checks(report: dict[str, Any]) -> dict[str, Any]:
    deps = report["dependencies"]["direct"]
    dep_mismatch = {
        k: {"have": v, "want": w}
        for k, w in config.PINNED_DIRECT_DEPS.items()
        for v in [deps.get(k)]
        if v != w
    }
    pg = report["postgres"]
    tools = report["tools"]

    def tool_has(name: str, needle: str) -> bool:
        version = (tools.get(name) or {}).get("version") or ""
        return needle in version

    return {
        "python_ok": report["python"]["matches_required"],
        "uv_lock_ok": report["dependencies"]["lock_is_frozen_source"],
        "direct_deps_ok": not dep_mismatch,
        "pytest_ok": report["dependencies"]["pytest"] == config.PINNED_PYTEST,
        "uv_ok": tool_has("uv", config.EXPECTED_UV_VERSION),
        "node_ok": tool_has("node", config.EXPECTED_NODE_VERSION),
        "psql_ok": tool_has("psql", config.REQUIRED_PG_BINARY_VERSION),
        "pg_binary_ok": bool(pg["binary_matches"]),
        "pg_running": bool(pg["running"]),
        "pg_connected": bool(pg["connected"]),
        "pg_version_ok": bool(pg["version_matches"]),
        "llm_key_present": report["llm"]["api_key_present"],
    }


def collect() -> dict[str, Any]:
    boot = _bootstrap_info()
    report: dict[str, Any] = {
        "tool": "chatbi doctor",
        "root": str(config.LAYOUT.root),
        "python": _python_info(),
        "resources": _resource_info(),
        "dependencies": {
            "direct": _dependency_versions()["direct"],
            "pytest": _dependency_versions()["pytest"],
            "schemas": {
                "direct": dict(config.PINNED_DIRECT_DEPS),
                "pytest": config.PINNED_PYTEST,
            },
            "lock": str(config.LAYOUT.root / "uv.lock"),
            "lock_present": (config.LAYOUT.root / "uv.lock").is_file(),
            # bootstrap 记录 .venv 由 uv 以 --frozen 建立，说明运行时版本来自锁文件。
            "lock_is_frozen_source": bool(boot.get("frozen")),
        },
        "bootstrap": boot,
        "tools": _tool_versions(),
        "postgres": _pg_info(),
        "postgres_settings": {
            "shared_buffers": config.PG_SHARED_BUFFERS,
            "work_mem": config.PG_WORK_MEM,
            "max_connections": config.PG_MAX_CONNECTIONS,
        },
        "llm": _llm_info(),
        "workbook": {
            "path": "data/江海智造_2026Q3_经营财务模拟数据.xlsx",
            "present": (
                config.LAYOUT.root
                / "data/江海智造_2026Q3_经营财务模拟数据.xlsx"
            ).is_file(),
        },
    }
    report["checks"] = _checks(report)
    # 环境就绪（不含模型密钥）：与 --strict 的退出码判定同一组条件。
    report["environment_ready"] = all(report["checks"][k] for k in _REQUIRED_CHECKS)
    report["ready_for_live_llm"] = report["llm"]["live_ready"]
    return report


#: 环境就绪所需的检查项；模型密钥不在其中（缺密钥只让 live 标为 PENDING）。
_REQUIRED_CHECKS = (
    "python_ok",
    "uv_lock_ok",
    "direct_deps_ok",
    "pytest_ok",
    "uv_ok",
    "node_ok",
    "psql_ok",
    "pg_binary_ok",
    "pg_running",
    "pg_connected",
    "pg_version_ok",
)


def render_text(report: dict[str, Any]) -> str:
    py = report["python"]
    res = report["resources"]
    pg = report["postgres"]
    llm = report["llm"]
    deps = report["dependencies"]["direct"]
    lines = [
        f"chatbi doctor @ {report['root']}",
        f"Python: {py['version']} ({py['implementation']}) "
        f"{'OK' if py['matches_required'] else 'MISMATCH, want ' + py['required']}",
        "Resources: nproc={nproc} mem_total={mem_total_mib}MiB "
        "mem_available={mem_available_mib}MiB disk_free={disk_free_gib}GiB".format(**res),
        "Dependencies: " + ", ".join(f"{k}=={v}" for k, v in deps.items()),
        f"PostgreSQL: binary={pg['binary_version']} running={pg['running']} "
        f"connected={pg['connected']} server_version={pg['server_version']} "
        f"listen_addresses={pg.get('listen_addresses_actual')!r} num={pg['server_version_num']}",
        f"PostgreSQL socket: {pg['socket_dir']} port={pg['port']} mode={pg['socket_mode']} "
        f"file={'present' if pg['socket_mode'] else 'missing'} "
        f"err={pg['error_class']}",
        "LLM: {model} @ {base_url}（接入在步骤 02） key(env:{env})={key} live={live}".format(
            model=llm["configured"]["model"],
            base_url=llm["configured"]["base_url"],
            env=llm["credential_source"].split(":", 1)[1],
            key="present" if llm["api_key_present"] else "absent",
            live="ready" if llm["live_ready"] else "PENDING (missing key)",
        ),
        "Checks: " + ", ".join(f"{k}={v}" for k, v in report["checks"].items()),
    ]
    return "\n".join(lines)


def run(*, as_json: bool, strict: bool) -> int:
    report = collect()
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    if not strict:
        return 0
    return 0 if report["environment_ready"] else 1
