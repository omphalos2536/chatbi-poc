"""私有 PostgreSQL 实例的只读探测。

本模块只读取 ``var/pg`` 下的文件并做短连接查询，不启动、不停止、不初始化
实例；启停逻辑在 ``scripts/pg``（由测试套件覆盖）。口令只从 ``var/run/secrets``
下仅属主可读的文件读取，绝不接受命令行/环境变量里的口令，也不会把口令打进
异常文本或日志。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from chatbi import config

SECRET_FILE_MODE = 0o600


def _read_kv(path: Path) -> dict[str, str]:
    """读取 postgresql.conf 形式的 ``key = value``（后出现的覆盖前面的，与 PG 一致）。"""
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("#", 1)[0].strip()
        data[key.strip()] = value
    return data


def _binary_version(binary: Path) -> str | None:
    """读取 ``postgres --version`` 的版本段，如 ``17.12``；缺失时返回 None。"""
    import subprocess

    if not binary.is_file():
        return None
    try:
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # 形如 "postgres (PostgreSQL) 17.12 (Debian 17.12-0+deb13u1)"，取第一个版本号。
    if out.returncode != 0:
        return None
    import re

    m = re.search(r"(\d+\.\d+)", out.stdout or "")
    return m.group(1) if m else None


def _socket_layout(layout: config.Layout) -> tuple[Path, int]:
    """socket 用短的绝对路径；端口默认 55432，可在 var/pg/port 里覆盖。"""
    port = config.DEFAULT_PORT
    try:
        port = int((layout.pg / "port").read_text().strip())
    except (OSError, ValueError):
        pass
    # 与 scripts/pg 一致：socket 在 var/run/pg（短路径），不在 var/run 本身。
    return layout.pg_socket_dir, port


@dataclass
class PgStatus:
    binary_version: str | None = None
    data_directory: str | None = None
    socket_dir: str | None = None
    port: int | None = None
    pid: int | None = None
    pid_alive: bool = False
    running: bool = False
    connected: bool = False
    database: str | None = None
    user: str | None = None
    server_version: str | None = None
    server_version_num: int | None = None
    listen_addresses_configured: str | None = None
    socket_mode: str | None = None
    socket_dir_owner_ok: bool | None = None
    error_class: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "binary_version": self.binary_version,
            "data_directory": self.data_directory,
            "socket_dir": self.socket_dir,
            "port": self.port,
            "pid": self.pid,
            "pid_alive": self.pid_alive,
            "running": self.running,
            "connected": self.connected,
            "database": self.database,
            "user": self.user,
            "server_version": self.server_version,
            "server_version_num": self.server_version_num,
            "listen_addresses_configured": self.listen_addresses_configured,
            "socket_mode": self.socket_mode,
            "socket_dir_owner_ok": self.socket_dir_owner_ok,
            "error_class": self.error_class,
            **self.extra,
        }


def _pid_alive(pid: int) -> bool:
    """进程是否“活着”的**辅助**判断：只看 ``/proc/<pid>``。

    ``kill(pid, 0)`` 会把同一竞态窗口里刚被回收、PID 又被复用的无关进程判为
    存活，因此不使用。另外，受限会话里 ``/proc`` 可能只暴露本会话的进程，
    别的 shell 里启动的 postmaster 看不到——所以这只是辅助信号，判断“在运行”
    以 socket 能否连上为准（见 :func:`probe`）。
    """
    try:
        return Path(f"/proc/{pid}").exists()
    except OSError:
        return False


def _running_pid_is_ours(layout: config.Layout, pid: int) -> bool:
    """pid 文件里的进程是否确实是本数据目录的 postmaster（要求看到命令行）。

    要求 ``/proc/<pid>/cmdline`` 里同时出现 ``-D`` 与本数据目录（argv 逐项
    精确比较，避免 ``-old`` 之类的同前缀路径被误认）；看不到命令行时返回
    False，此时不能凭 pid 断言任何事。
    """
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    want = [b"-D", str(layout.pg_data).encode()]
    return any(argv[i : i + 2] == want for i in range(len(argv) - 1))


def _read_postmaster_pid(layout: config.Layout) -> int | None:
    try:
        return int((layout.pg_data / "postmaster.pid").read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def _connect(sock_dir: Path, port: int, password: str, timeout: float):
    if hasattr(psycopg, "Connection"):
        psycopg.Connection.connect  # noqa: B018 - 仅确认是 psycopg3 模块对象
    return psycopg.connect(
        host=str(sock_dir),
        port=port,
        dbname=config.DB_NAME,
        user=config.DB_SUPERUSER,
        password=password,
        connect_timeout=max(1, int(timeout)),
        options="-c statement_timeout=5000",
    )


def probe(layout: config.Layout = config.LAYOUT, *, timeout: float = 3.0) -> PgStatus:
    status = PgStatus()
    status.binary_version = _binary_version(layout.pg_binary("postgres"))
    status.data_directory = str(layout.pg_data)

    conf = _read_kv(layout.pg_data / "postgresql.conf")
    if "listen_addresses" in conf:
        status.listen_addresses_configured = conf["listen_addresses"].strip("'\"")

    sock_dir, port = _socket_layout(layout)
    if conf.get("unix_socket_directories"):
        sock_dir = Path(conf["unix_socket_directories"].strip("'\"") or sock_dir)
    if "port" in conf:
        try:
            port = int(conf["port"])
        except ValueError:
            pass
    status.socket_dir = str(sock_dir)
    status.port = port

    try:
        st = sock_dir.stat()
        status.socket_dir_owner_ok = (st.st_uid == os.getuid()) and (
            stat.S_IMODE(st.st_mode) & 0o077 == 0
        )
    except OSError:
        status.socket_dir_owner_ok = None

    sock = sock_dir / f".s.PGSQL.{port}"
    if sock.exists():
        status.socket_mode = oct(stat.S_IMODE(sock.stat().st_mode))
    else:
        status.extra["socket_missing"] = True

    # 是否在运行：以“socket 能否连上”为准（跨会话时 /proc 可能看不到）。
    status.pid = _read_postmaster_pid(layout)
    status.pid_alive = bool(status.pid is not None and _pid_alive(status.pid))

    user = config.DB_SUPERUSER
    try:
        status.extra["server_pid"] = int(
            (layout.pg_data / "postmaster.pid").read_text().splitlines()[0]
        )
    except (OSError, ValueError, IndexError):
        pass
    try:
        password = layout.pgpass.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        password = ""
    if not password:
        status.error_class = "no_local_credential"
        return status

    import psycopg

    # 每次都新建连接（不缓存连接、不写连接串文件）；只走本实例的 Unix socket。
    # 口令只在本进程内存里使用，不写文件、不进日志。
    try:
        conn = psycopg.connect(
            host=str(sock_dir),
            port=port,
            dbname=config.DB_NAME,
            user=user,
            password=password,
            connect_timeout=max(1, int(timeout)),
            options="-c statement_timeout=5000",
        )
    except Exception as exc:  # noqa: BLE001 - 只是分类，不输出异常原文
        detail = str(exc).lower()
        name = type(exc).__name__
        if name == "OperationalError" and (
            "does not exist" in detail or "no such file" in detail
        ):
            status.error_class = "not_listening"
        else:
            status.error_class = f"connect_failed:{name}"
        return status

    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "select current_database(), current_user,"
                " current_setting('server_version'),"
                " current_setting('server_version_num')::int,"
                " current_setting('listen_addresses', true),"
                " current_setting('data_directory')"
            )
            row = cur.fetchone()
            if row:
                (
                    status.database,
                    status.user,
                    status.server_version,
                    status.server_version_num,
                    listen_actual,
                    data_dir_actual,
                ) = row
                status.extra["listen_addresses_actual"] = listen_actual
                if data_dir_actual != status.data_directory:
                    status.extra["data_directory_mismatch"] = data_dir_actual
            cur.execute("select rolsuper from pg_roles where rolname = current_user")
            r = cur.fetchone()
            status.extra["current_user_is_superuser"] = bool(r and r[0])
        status.connected = True
        # 连上本实例即视为“在运行”（跨会话时 /proc 可能看不到 postmaster，
        # pid 文件也可能由别的会话写入，不能作为判断在运行的依据）。
        status.running = True
    except Exception as exc:  # noqa: BLE001 - 只是分类，不输出异常原文（可能含连接串）
        name = type(exc).__name__
        # socket 不存在 / 被拒绝：说明实例没在这个目录监听；认证或参数错误则是
        # 实例在跑但连接被拒，两者在 doctor 里要区分开。
        detail = str(exc).lower()
        if name == "OperationalError" and (
            "does not exist" in detail or "no such file" in detail
        ):
            status.error_class = "not_listening"
        else:
            status.error_class = f"connect_failed:{name}"
    return status
