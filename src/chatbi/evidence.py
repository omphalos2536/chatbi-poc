"""``chatbi evidence env``：写入步骤 01 的环境证据 ``var/evidence/env.json``。

证据只包含版本、资源、路径、权限、哈希与运行命令的退出码；任何口令、密钥值
都不会写入（密钥只有“是否存在”的布尔值）。格式遵循《验收规范》第 3 条：

    build_sha、batch_id、operator、timestamp、case_id、command、exit_code、
    expected、observed、artifact、status

安装步骤没有批次号，``batch_id`` 记为 ``none``，并以 ``environment`` 摘要代替。
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from chatbi import config

#: 环境文件（步骤 01 交付物之一）。
ENV_JSON = "env.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_files(root: Path) -> list[str]:
    """发布文件清单：实现契约与环境相关文件的相对路径（排序后）。"""
    fixed = [
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "scripts/bootstrap",
        "scripts/pg",
    ]
    files = [p for p in fixed if (root / p).is_file()]
    for pattern in ("src/chatbi/*.py", "tests/test_*.py"):
        files.extend(
            str(p.relative_to(root)) for p in sorted(root.glob(pattern)) if p.is_file()
        )
    return sorted(set(files))


def build_sha(root: Path) -> str:
    """无 Git 时的发布摘要：对每行 ``<sha256>  <相对路径>`` 再取一次 SHA-256。"""
    lines = []
    for rel in release_files(root):
        digest = _file_sha256(root / rel)
        if digest:
            lines.append(f"{digest}  {rel}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _run(command: list[str], *, timeout: float = 60.0) -> dict[str, Any]:
    """执行一条只读命令并记录退出码与首行输出（凭据类命令不会经过这里）。"""
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
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _probes() -> dict[str, dict[str, Any]]:
    return {
        "nproc": _run(["nproc"]),
        "free_m": _run(["free", "-m"]),
        "df_h_workspace": _run(["df", "-h", "."]),
        "os_release": _run(["cat", "/etc/os-release"]),
        "uname": _run(["uname", "-smr"]),
        "python3": _run(["python3", "-V"]),
        "psql": _run(["psql", "-V"]),
        "node": _run(["node", "-v"]),
        "uv": _run(["uv", "--version"]),
        "pg_binary": _run([str(config.LAYOUT.pg_binary("postgres")), "--version"]),
        "pg_sql_version": _run(["bash", "scripts/pg", "sql", "show server_version_num"]),
        "pg_sql_listen": _run(["bash", "scripts/pg", "sql", "show listen_addresses"]),
    }


def _modes(root: Path) -> dict[str, str | None]:
    paths = [
        "var",
        "var/pg",
        "var/pg/data",
        "var/run",
        "var/run/pg",
        "var/run/secrets",
        "var/run/secrets/pgpass",
        "var/run/secrets/pgpass-svc",
        "var/evidence",
    ]
    modes: dict[str, str | None] = {}
    for rel in paths:
        try:
            modes[rel] = oct((root / rel).stat().st_mode & 0o777)
        except OSError:
            modes[rel] = None
    return modes


def _doctor_report() -> dict[str, Any]:
    """复用 doctor.collect()：不含凭据值，只有版本与布尔。"""
    from chatbi import doctor

    return doctor.collect()


def build_env_evidence() -> dict[str, Any]:
    root = config.LAYOUT.root
    doctor = _doctor_report()
    probe = _probes()
    lock = root / "uv.lock"
    deps = doctor["dependencies"]["direct"]

    expected = {
        "python": config.REQUIRED_PYTHON,
        "postgresql_binary": config.REQUIRED_PG_BINARY_VERSION,
        "server_version_num": config.REQUIRED_PG_VERSION_NUM,
        "direct_dependencies": dict(config.PINNED_DIRECT_DEPS),
        "pytest": config.PINNED_PYTEST,
        "uv": config.EXPECTED_UV_VERSION,
        "node": config.EXPECTED_NODE_VERSION,
        "listen_addresses": "",
        "socket_dir_mode": "0o700",
        "secret_files_mode": "0o600",
    }
    observed = {
        "python": doctor["python"]["version"],
        "postgresql_binary": doctor["postgres"]["binary_version"],
        "server_version_num": doctor["postgres"]["server_version_num"],
        "postgres_connected": doctor["postgres"]["connected"],
        "listen_addresses": doctor["postgres"].get("listen_addresses_actual"),
        "socket_dir": doctor["postgres"]["socket_dir"],
        "socket_mode": doctor["postgres"]["socket_mode"],
        "direct_dependencies": deps,
        "pytest": doctor["dependencies"]["pytest"],
        "uv": (doctor["tools"].get("uv") or {}).get("version"),
        "node": (doctor["tools"].get("node") or {}).get("version"),
        "psql": (doctor["tools"].get("psql") or {}).get("version"),
        "uv_lock_sha256": _file_sha256(lock),
        "var_modes": _modes(root),
        "environment_ready": doctor["environment_ready"],
        "llm_api_key_present": doctor["llm"]["api_key_present"],
        "llm_live_state": doctor["llm"]["state"],
        "resources": doctor["resources"],
        "sandboxed_session": doctor["resources"].get("sandboxed_session"),
    }
    checks = {
        "python": observed["python"] == expected["python"],
        "postgresql_binary": observed["postgresql_binary"] == expected["postgresql_binary"],
        "server_version_num": observed["server_version_num"] == expected["server_version_num"],
        "direct_dependencies": observed["direct_dependencies"] == expected["direct_dependencies"],
        "pytest": observed["pytest"] == expected["pytest"],
        "uv": expected["uv"] in (observed["uv"] or ""),
        "node": expected["node"] in (observed["node"] or ""),
        "listen_addresses": observed["listen_addresses"] == expected["listen_addresses"],
        "socket_dir_mode": observed["socket_mode"] == expected["socket_dir_mode"],
        "secret_files": all(
            observed["var_modes"].get(p) == "0o600"
            for p in ("var/run/secrets/pgpass", "var/run/secrets/pgpass-svc")
        ),
        "private_dirs_0700": all(
            observed["var_modes"].get(p) == "0o700"
            for p in ("var/run", "var/run/pg", "var/pg", "var/pg/data", "var/evidence")
        ),
    }

    command = "chatbi doctor --json"
    return {
        "step": "01",
        "case_id": "01-ENV",
        "build_sha": build_sha(root),
        "build_files": release_files(root),
        "batch_id": "none",
        "operator": getpass.getuser(),
        "timestamp": _utc_now(),
        "command": command,
        "exit_code": 0 if doctor["environment_ready"] else 1,
        "expected": expected,
        "observed": observed,
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "artifact": [(config.LAYOUT.evidence / ENV_JSON).relative_to(root).as_posix()]
        + [(config.LAYOUT.run / "bootstrap.json").relative_to(root).as_posix()],
        "workbook": {
            "path": doctor["workbook"]["path"],
            "sha256": (doctor["bootstrap"].get("workbook_sha256") or None),
            "present": doctor["workbook"]["present"],
        },
        "doctor": {
            "python": doctor["python"],
            "dependencies": doctor["dependencies"],
            "tools": doctor["tools"],
            "postgres": doctor["postgres"],
            "postgres_settings": doctor["postgres_settings"],
            "resources": doctor["resources"],
            "checks": doctor["checks"],
            "environment_ready": doctor["environment_ready"],
            "llm": doctor["llm"],
        },
        "probes": {
            name: {
                "command": shlex.join(r["command"]),
                "exit_code": r["exit_code"],
                "stdout": r["stdout"],
            }
            for name, r in probe.items()
        },
        "environment": {
            "host": platform.node(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python_executable": sys.executable,
            "uid": os.getuid(),
        },
        "notes": [
            "batch_id: none —— 安装步骤尚无导入批次，以 build_sha 与环境摘要代替。",
            "本文件与任何日志都不包含口令或密钥值；llm_api_key_present 只是布尔值。",
            "受限沙箱会话每次命令使用独立 PID 命名空间；判断 PG 在运行以 socket 连接为准。",
        ],
    }


def write_env_evidence() -> Path:
    layout = config.LAYOUT
    layout.evidence.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.evidence, 0o700)
    payload = build_env_evidence()
    target = layout.evidence / ENV_JSON
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    return target
