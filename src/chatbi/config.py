"""本机布局与常量。

所有路径都相对项目根目录解析；项目根可用环境变量 ``CHATBI_ROOT`` 覆盖（仅
测试或验收时使用，正常从根目录运行即可自动定位到 pyproject.toml 所在目录）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def running_in_sandbox() -> bool:
    """当前解释器是否运行在 bwrap 沙箱里（每个会话独立 PID 命名空间）。

    判断依据是根进程命令行里出现 ``--unshare-pid``。沙箱会话之间互相看不到
    postmaster 进程，后台进程也会随会话结束被回收；此时“在运行”只能以 socket
    能否连上为准，且不能断言“进程没在运行”。
    """
    try:
        cmdline = Path("/proc/1/cmdline").read_bytes()
    except OSError:
        return False
    return b"--unshare-pid" in cmdline

#: 总体设计技术栈表里固定的直接依赖版本，供 doctor 与测试对照。
PINNED_DIRECT_DEPS: dict[str, str] = {
    "openpyxl": "3.1.5",
    "psycopg": "3.2.9",
    "fastapi": "0.116.1",
    "uvicorn": "0.35.0",
    "PyYAML": "6.0.2",
    "sqlglot": "27.8.0",
    "httpx": "0.28.1",
}
PINNED_PYTEST = "8.4.1"

#: 工具链期望版本（用于 doctor 的 checks；版本不符时对应项为 False）。
EXPECTED_UV_VERSION = "0.13.9"
EXPECTED_NODE_VERSION = "24.22.1"

#: 解释器与数据库预期版本（步骤 01 验收条件）。
REQUIRED_PYTHON = "3.13.5"
REQUIRED_PG_VERSION_NUM = 170012
REQUIRED_PG_BINARY_VERSION = "17.12"
REQUIRED_PYTHON_RANGE = ">=3.13,<3.14"

#: 大模型密钥只从环境读取，任何文件、日志和输出都不得包含其值。
LLM_KEY_ENV = "CHATBI_LLM_API_KEY"
LLM_CONFIGURED: dict[str, str] = {
    "base_url": "https://api3.kkyy.site/v1",
    "model": "deepseek-v4.1-flash",
}

DB_NAME = "chatbi"
DB_SUPERUSER = "chatbi_owner"
DEFAULT_PORT = 55432

#: POC 固定内存参数（总体设计“技术栈与运行约定”）。
PG_SHARED_BUFFERS = "64MB"
PG_WORK_MEM = "4MB"
PG_MAX_CONNECTIONS = 12


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def var(self) -> Path:
        return self.root / "var"

    @property
    def pg(self) -> Path:
        return self.var / "pg"

    @property
    def pg_dist(self) -> Path:
        return self.pg / "dist"

    @property
    def pg_data(self) -> Path:
        return self.pg / "data"

    @property
    def run(self) -> Path:
        return self.var / "run"

    @property
    def audit(self) -> Path:
        return self.var / "audit"

    @property
    def approvals(self) -> Path:
        return self.var / "approvals"

    @property
    def evidence(self) -> Path:
        return self.var / "evidence"

    @property
    def stage(self) -> Path:
        return self.var / "stage"

    @property
    def secrets(self) -> Path:
        """仅属主可读的运行时口令目录（pgpass/pgpass-svc）。"""
        return self.run / "secrets"

    @property
    def pg_socket_dir(self) -> Path:
        """私有实例的 Unix socket 目录（短绝对路径，0700），与 scripts/pg 一致。"""
        return self.run / "pg"

    @property
    def pgpass(self) -> Path:
        return self.secrets / "pgpass"

    @property
    def pgpass_svc(self) -> Path:
        return self.secrets / "pgpass-svc"

    @property
    def bootstrap_json(self) -> Path:
        return self.var / "run" / "bootstrap.json"

    def pg_bindir(self) -> Path:
        return self.pg_dist / "usr/lib/postgresql/17/bin"

    def pg_binary(self, name: str) -> Path:
        return self.pg_bindir() / name


def detect_root() -> Path:
    env = os.environ.get("CHATBI_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


LAYOUT = Layout(detect_root())
