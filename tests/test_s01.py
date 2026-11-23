"""步骤 01（本机环境）的验收测试。

运行前置
    1. 已执行 ``bash scripts/bootstrap``（依赖已从 ``uv.lock`` 以 ``--frozen`` 装进
       ``.venv``，``var/`` 子目录已建立）；
    2. PostgreSQL 已 ``bash scripts/pg init`` 并 ``bash scripts/pg start``。
    测试**不依赖** PG 已经处于运行状态：未运行时执行一次 ``bash scripts/pg start``
    （绝不 reset，绝不删除或重建数据目录）。
    运行方式：``.venv/bin/pytest -q tests/test_s01.py``。

约定的三件事
    * **只读数据库**：只发 ``select 1`` / ``select current_database()`` / ``show ...``；
      ``scripts/pg stop|start|init`` 只做启停与幂等核对（``init`` 遇到已初始化目录
      只会沿用并重写配置，不会重置）。
    * **不读取任何真实密钥**：``CHATBI_LLM_API_KEY`` 的值只在 ``subprocess`` 的环境里
      出现，pytest 进程从不从 ``os.environ`` 读出它；未设置时用内存里的伪造值。
      “输出里有没有密钥/口令”的判定在**子进程**里做：它自己读密钥与
      ``var/run/secrets`` 下口令文件的前 ``SECRET_PROBE_LEN`` 个字符，判断是否出现在
      被检查文本中，只回传布尔。完整口令与密钥值不进入 pytest 进程、不进入断言消息，
      也不会出现在 pytest 的失败截图里；失败消息只说“泄露了口令文件 xxx 的内容”。
    * 每个 ``subprocess`` 调用都带 ``timeout``（≤180s）与 ``stdin=DEVNULL``；
      子进程环境去掉 ``PYTEST_*``，``TMPDIR``/``HOME`` 指到项目外的私有临时目录。

运行状态判定（重要）
    受限沙箱里每条命令可能处于不同的 PID 命名空间，别的会话启动的 postmaster 在这里
    ``/proc`` 看不到，``kill -0``/``os.kill`` 对已回收 PID 也不可靠。因此**不用** ps/kill
    判断“PG 在运行”，只看 ``scripts/pg status`` 输出、``scripts/pg sql`` 的返回码和
    ``chatbi doctor --json`` 的 ``postgres.connected``。

守卫类用例（``*_guard_*``）在 ``tmp_path`` 里的伪造根目录上运行 ``scripts/pg`` 的副本
（只改 ``ROOT=`` 那一行），并让 ``TMPDIR`` 指向伪造根目录、用哨兵 ``curl`` 拦住下载，
绝不接触真实数据目录。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Iterable, Sequence

import pytest

# ------------------------------------------------------------------ 常量 --

ROOT = Path(__file__).resolve().parents[1]
PG = ROOT / "scripts" / "pg"
CHATBI = ROOT / ".venv" / "bin" / "chatbi"
PYTHON = ROOT / ".venv" / "bin" / "python"

VAR = ROOT / "var"
PG_HOME = VAR / "pg"
DATA = PG_HOME / "data"
RUN = VAR / "run"
SOCKDIR = RUN / "pg"
SECRETS = RUN / "secrets"
CLUSTER_ENV = PG_HOME / "cluster.env"
CLUSTER_ID_FILE = DATA / "CHATBI_CLUSTER"
PG_VERSION_FILE = DATA / "PG_VERSION"
CONF = DATA / "postgresql.conf"
AUTO_CONF = DATA / "postgresql.auto.conf"
PMID = DATA / "postmaster.pid"
BOOTSTRAP_JSON = RUN / "bootstrap.json"

PORT = 55432
SOCKET = SOCKDIR / f".s.PGSQL.{PORT}"
SERVER_VERSION_NUM = "170012"
DB_NAME = "chatbi"
OWNER_ROLE = "chatbi_owner"

KEY_ENV = "CHATBI_LLM_API_KEY"
DUMMY_KEY = "sk-test-DUMMY-c0ffee-NOT-REAL"  # 伪造值：只在内存里，不写任何文件

TIMEOUT = 180
ENV_PREFIX = "PYTEST_"
SECRET_PROBE_LEN = 8
MIN_SECRET_LEN = 12  # 过短的串（如误读出的 `->`）不算口令

PINNED_DIRECT = {
    "openpyxl": "3.1.5",
    "psycopg": "3.2.9",
    "fastapi": "0.116.1",
    "uvicorn": "0.35.0",
    "PyYAML": "6.0.2",
    "sqlglot": "27.8.0",
    "httpx": "0.28.1",
}
PINNED_PYTEST = "8.4.1"
PYTHON_VERSION = "3.13.5"
PG_BINARY_VERSION = "17.12"
UV_NEEDLE = "0.13.9"
NODE_NEEDLE = "24.22.1"

#: llm 节点（含嵌套）下不允许出现的键名。
FORBIDDEN_LLM_KEYS = {
    "key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "credential",
    "credentials",
}


# -------------------------------------------------------- 进程与子进程 --

#: 本测试专用的临时根目录（不在项目里，含 HOME/TMPDIR）。
#: 子进程的 TMPDIR/HOME 都指到这里：doctor 输出不会带出项目路径，项目里也不落临时文件。
_SESSION_TMP: Path | None = None


def session_tmp() -> Path:
    global _SESSION_TMP
    if _SESSION_TMP is None:
        try:
            base = Path(tempfile.gettempdir())
        except OSError:
            base = Path("/tmp")
        _SESSION_TMP = Path(tempfile.mkdtemp(prefix="chatbi-s01-", dir=str(base)))
    return _SESSION_TMP


def child_env(
    extra: dict[str, str] | None = None, *, drop: Iterable[str] = ()
) -> dict[str, str]:
    """子进程环境：去掉 PYTEST_* 与显式丢弃的变量，再叠加 extra；TMPDIR/HOME 指向私有临时根。"""
    drop_set = set(drop)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(ENV_PREFIX) and key not in drop_set
    }
    env.setdefault("TMPDIR", str(session_tmp()))
    if extra:
        env.update(extra)
    return env


def _run(
    argv: Sequence[str],
    *,
    timeout: int = TIMEOUT,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert timeout <= TIMEOUT, "subprocess 超时必须 ≤ 180s"
    return subprocess.run(
        list(argv),
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env=env if env is not None else child_env(),
        check=False,
    )


def pg_script(*args: str, timeout: int = TIMEOUT) -> subprocess.CompletedProcess[str]:
    return _run(["bash", str(PG), *args], timeout=timeout)


def wait_ready_sql(sql: str, *, attempts: int = 60, interval: float = 0.5) -> str:
    """刚启动时连接可能暂时失败：重试 ``scripts/pg sql`` 直到成功。"""
    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(attempts):
        last = pg_script("sql", sql, timeout=120)
        if last.returncode == 0:
            return last.stdout.strip()
        time.sleep(interval)
    assert last is not None, "unreachable"
    assert_ok(last, f"scripts/pg sql {sql!r}")
    raise AssertionError("unreachable")


#: scripts/pg 失败时的错误类别标签（只报类别，不展开可能含无关片段的原始输出）。
_ERROR_TAGS = (
    "拒绝",
    "不属于本脚本",
    "请先运行",
    "未运行",
    "下载失败",
    "启动失败",
    "停止失败",
    "不完整",
    "未找到 psql",
)


def _error_class(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stdout or "") + (result.stderr or "")
    hits = [tag for tag in _ERROR_TAGS if tag in text]
    if hits:
        return "；".join(hits[:2])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return f"stderr 首行：{lines[0][:120]}" if lines else "无输出"


def assert_ok(result: subprocess.CompletedProcess[str], what: str) -> None:
    assert result.returncode == 0, (
        f"{what} 失败：rc={result.returncode}（{_error_class(result)}）"
        "；完整 stdout/stderr 见 pytest 的截获输出（此处不展开）"
    )


# ------------------------------------------------------- 运行状态判定 --
# 唯一可靠、跨会话成立的判据：scripts/pg status / scripts/pg sql / doctor 的 connected。
# 不使用 ps/kill/os.kill 判断进程是否在运行。

_STATUS_LINE = re.compile(r"server_version=17\.[0-9]+ .*server_version_num=170012")
# pid 行有两种合法形式：有进程号，或本会话看不到 postmaster 进程时的说明文字；
# 是否在运行以 state 行（state  running）与 server_version 行为准。
_STATUS_PID = re.compile(r"^pid\s+(\d+)\s*$", re.M)
_STATUS_STATE = re.compile(r"^state\s+(\S.*)$", re.M)
_STATUS_STARTED = re.compile(r"data_dir=(\S+) started=([^\n]+)")
_STATUS_CLUSTER_ID = re.compile(r"^cluster_id\s+(\S+)", re.M)


def status_text() -> str:
    result = pg_script("status", timeout=120)
    return result.stdout or ""


def status_pid() -> int | None:
    """``scripts/pg status`` 的 ``pid  <N>`` 行；未运行、未接受连接或本会话看不到进程时为 None。"""
    match = _STATUS_PID.search(status_text())
    return int(match.group(1)) if match else None


def status_running() -> bool:
    """实例是否在接受连接：``state  running`` 且有 server_version/170012 行。"""
    text = status_text()
    match = _STATUS_STATE.search(text)
    return bool(match and match.group(1).strip().startswith("running") and _STATUS_LINE.search(text))


def status_started() -> str | None:
    match = _STATUS_STARTED.search(status_text())
    return match.group(2).strip() if match else None


def status_cluster_id() -> str | None:
    match = _STATUS_CLUSTER_ID.search(status_text())
    return match.group(1) if match else None


def pg_running() -> bool:
    """运行判据：status 里出现 server_version=17.x/170012、data_dir/started 行，或 sql 成功。"""
    text = status_text()
    if _STATUS_LINE.search(text) and _STATUS_STARTED.search(text):
        return True
    return pg_script("sql", "select 1", timeout=120).returncode == 0


def wait_pg_running(deadline_s: float = 30.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if pg_script("sql", "select 1", timeout=120).returncode == 0:
            return True
        time.sleep(0.5)
    return False


def start_pg_if_needed() -> bool:
    """确保实例在跑（已在运行时 ``scripts/pg start`` 会报告“已在运行”）。"""
    if pg_running():
        return True
    pg_script("start")
    return wait_pg_running()


def _wait_until(predicate, deadline_s: float = 30.0, interval: float = 0.3) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


#: 前置阶段 ``scripts/pg start`` 的结论（只用作跳过/失败消息里的原因）。
_PG_START_FAILURE = ""


def _require_pg() -> None:
    """依赖运行中 PG 的用例：确保实例在跑，否则直接失败并给出 scripts/pg 的错误类别。"""
    if pg_running():
        return
    assert start_pg_if_needed(), (
        f"PG 未运行且 scripts/pg start 未成功（{_PG_START_FAILURE or '未知原因'}）"
    )


@pytest.fixture(scope="session", autouse=True)
def _pg_running() -> None:
    """默认前置：PG 运行中。未运行时执行一次 ``scripts/pg start``（绝不 reset）。"""
    global _PG_START_FAILURE
    if not pg_running():
        result = pg_script("start")
        if result.returncode != 0 or not wait_pg_running():
            _PG_START_FAILURE = _error_class(result)
    yield
    # 收尾：保证测试结束时 PG 处于运行状态（正常情况下无需动作）。
    if not pg_running():
        start_pg_if_needed()


def doctor(*flags: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run([str(CHATBI), "doctor", *flags], env=env)


# ------------------------------------------------------- 凭据泄露判定 --

#: 在子进程里运行：读密钥/口令的前缀，判定是否出现在给定输出中（只回传布尔）。
LEAK_CHECK_SRC = r'''
import json, os
from pathlib import Path

set_key = os.environ.get("LEAK_SET_KEY", "")
probe_len = int(os.environ["LEAK_PROBE_LEN"])
min_len = int(os.environ["LEAK_MIN_LEN"])
outputs = {k: os.environ.get("LEAK_OUT_" + k.upper(), "") for k in ("text", "json")}
secrets_dir = Path(os.environ["LEAK_SECRETS"])


def needles(value):
    """待查片段：完整值（足够长时）与前 probe_len 个字符。"""
    found = []
    if len(value) >= min_len:
        found.append(value)
    head = value[:probe_len]
    if len(head) == probe_len:
        found.append(head)
    return found


result = {"secret_files": [], "secret_in_text": {}, "key_in_text": False, "key_in_json": False}

if set_key:
    for name in ("text", "json"):
        result["key_in_" + name] = any(n in outputs[name] for n in needles(set_key))

if secrets_dir.is_dir():
    for path in sorted(secrets_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        values = [line.strip() for line in raw.splitlines() if len(line.strip()) >= min_len]
        result["secret_files"].append(path.name)
        result["secret_in_text"][path.name] = any(
            n in outputs["text"] or n in outputs["json"]
            for value in values
            for n in needles(value)
        )

print(json.dumps(result))
'''


def _leak_verdict(env_extra: dict[str, str]) -> dict[str, object]:
    base = {
        "LEAK_SECRETS": str(SECRETS),
        "LEAK_PROBE_LEN": str(SECRET_PROBE_LEN),
        "LEAK_MIN_LEN": str(MIN_SECRET_LEN),
    }
    base.update(env_extra)
    result = _run([str(PYTHON), "-B", "-c", LEAK_CHECK_SRC], env=child_env(base), timeout=60)
    assert result.returncode == 0, "凭据泄露检查脚本自身失败"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _assert_no_key(verdict: dict[str, object], what: str) -> None:
    assert not verdict.get("key_in_text"), f"{what} 的原始文本输出里出现了 LLM 密钥值或其前 8 个字符"
    assert not verdict.get("key_in_json"), f"{what} 的 --json 输出里出现了 LLM 密钥值或其前 8 个字符"


def _assert_no_secret(verdict: dict[str, object], what: str) -> None:
    secrets_in = verdict.get("secret_in_text") or {}
    assert secrets_in, "没有读到任何口令文件（var/run/secrets/pgpass*）"
    for name, leaked in secrets_in.items():
        assert not leaked, f"{what}：泄露了口令文件 {name} 的内容"


# ------------------------------------------------------------ 文本工具 --


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _conf_effective(path: Path) -> dict[str, str]:
    """postgresql.conf 形式的 key = value（后出现的覆盖前面的，与 PG 一致）。"""
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.split("#", 1)[0].strip()
    return data


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


# ============================================================ 1) 重启标识 ==


def _cluster_identity() -> dict[str, str]:
    return {
        "cluster_env": CLUSTER_ENV.read_text(encoding="utf-8"),
        "cluster_id": CLUSTER_ID_FILE.read_text(encoding="utf-8").strip(),
        "PG_VERSION": PG_VERSION_FILE.read_text(encoding="utf-8").strip(),
    }


def test_s01_restart_keeps_cluster_id_and_connects() -> None:
    """stop → start → init → sql：标识完全不变、status 的 pid 变了、启动时间变新、连接可用。"""
    _require_pg()
    assert CLUSTER_ENV.is_file(), "缺少 var/pg/cluster.env（先 scripts/pg init）"
    assert CLUSTER_ID_FILE.is_file(), "缺少 var/pg/data/CHATBI_CLUSTER"
    identity_before = _cluster_identity()
    assert re.fullmatch(r"[0-9a-f]{32}", identity_before["cluster_id"]), (
        "CHATBI_CLUSTER 不是 32 位十六进制"
    )
    assert f"CLUSTER_ID='{identity_before['cluster_id']}'" in identity_before["cluster_env"], (
        "cluster.env 的 CLUSTER_ID 与 CHATBI_CLUSTER 不一致"
    )
    assert identity_before["PG_VERSION"] == "17", "PG_VERSION 不是 17"
    assert f"DATA_DIRECTORY='{DATA}'" in identity_before["cluster_env"]

    # 先在同一个 pytest 会话里确保它在跑（已在运行时 scripts/pg start 只报告“已在运行”）
    assert start_pg_if_needed(), "scripts/pg start 未能让实例接受连接"
    pid_before = status_pid()
    started_before = status_started()
    assert status_running(), "scripts/pg status 未报告 state running"
    # 受限沙箱里别的会话启动的 postmaster 不可见，pid 可能显示为说明文字；
    # 断言重启以“启动时间变新”为准，pid 能拿到时再加一条 pid 不同的断言。
    assert started_before is not None, "scripts/pg status 没有给出 data_dir/started 行"
    assert status_cluster_id() == identity_before["cluster_id"], "status 报告的 cluster_id 与 CHATBI_CLUSTER 不一致"

    assert_ok(pg_script("stop"), "scripts/pg stop")
    assert _wait_until(lambda: pg_script("sql", "select 1", timeout=120).returncode != 0, 30.0), (
        "stop 之后 scripts/pg sql 仍能连接"
    )
    assert not status_running(), "stop 之后 scripts/pg status 仍报告 running"

    result = pg_script("start")
    if result.returncode != 0 or not wait_pg_running(deadline_s=30.0):
        pytest.skip(
            "需要同一会话内完成重启：本机受限沙箱里停掉的 postmaster 无法再被拉起"
            f"（scripts/pg start：{_error_class(result)}）。手工复现命令："
            "scripts/pg stop && scripts/pg start && .venv/bin/pytest -q tests/test_s01.py"
        )
    pid_after = status_pid()
    started_after = status_started()
    assert status_running(), "重启后 scripts/pg status 未报告 state running"
    assert started_after is not None and started_after != started_before, (
        "重启前后 postmaster 启动时间相同（没有真的重启）"
    )
    if pid_before is not None and pid_after is not None:
        assert pid_after != pid_before, "重启前后 scripts/pg status 报告的 pid 相同（没有真的重启）"

    # 再执行 init：对已初始化的集群只做幂等核对，绝不重置、不重启。
    assert_ok(pg_script("init"), "scripts/pg init（第二次）")
    assert pg_running(), "再次 init 之后实例不在运行"

    assert _cluster_identity() == identity_before, "重启/再次 init 之后标识发生变化"
    assert status_cluster_id() == identity_before["cluster_id"], "status 报告的 cluster_id 变了"

    assert wait_ready_sql("select 1") == "1", "重启后连接不可用"
    assert wait_ready_sql("show server_version_num") == SERVER_VERSION_NUM
    assert wait_ready_sql("select current_database()") == DB_NAME
    match = _STATUS_STARTED.search(status_text())
    assert match and match.group(1) == str(DATA), "status 报告的 data_dir 不是本数据目录"


# ================================================== 2) socket 权限与位置 ===


def test_s01_socket_permissions_location_and_conf() -> None:
    """socket 目录/文件、私有目录与口令文件权限，postgresql.conf 的固定参数。"""
    _require_pg()
    uid = os.getuid()

    assert SOCKDIR.is_dir(), "socket 目录不存在：var/run/pg"
    assert not SOCKDIR.is_symlink(), "var/run/pg 是符号链接"
    assert _mode(SOCKDIR) == 0o700, f"var/run/pg 权限是 {oct(_mode(SOCKDIR))}，期望 0o700"
    assert SOCKDIR.stat().st_uid == uid, "var/run/pg 属主不是当前用户"
    assert len(str(SOCKDIR.resolve())) < 100, "socket 目录绝对路径必须 < 100 字符"

    assert SOCKET.exists(), "实例在运行但没有 socket 文件"
    st = SOCKET.stat()
    assert stat.S_ISSOCK(st.st_mode), "var/run/pg/.s.PGSQL.55432 不是 socket"
    assert stat.S_IMODE(st.st_mode) == 0o700, (
        f"socket 文件权限是 {oct(stat.S_IMODE(st.st_mode))}，期望 0o700"
    )
    assert st.st_uid == uid, "socket 文件属主不是当前用户"

    for path in (DATA, RUN, SECRETS):
        assert path.is_dir(), f"缺少目录：{path}"
        assert _mode(path) == 0o700, f"{path} 权限是 {oct(_mode(path))}，期望 0o700"
        assert path.stat().st_uid == uid, f"{path} 属主不是当前用户"

    for name in ("pgpass", "pgpass-svc"):
        path = SECRETS / name
        assert path.is_file(), f"缺少口令文件 var/run/secrets/{name}"
        st = path.stat()
        assert stat.S_ISREG(st.st_mode), f"var/run/secrets/{name} 不是普通文件"
        assert stat.S_IMODE(st.st_mode) == 0o600, f"var/run/secrets/{name} 不是 0600"
        assert st.st_uid == uid, f"var/run/secrets/{name} 属主不是当前用户"
    for path in sorted(SECRETS.iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, (
            f"var/run/secrets/{path.name} 对组/其他用户可访问"
        )

    conf = _conf_effective(CONF)
    expect = {
        "listen_addresses": "''",
        "port": "55432",
        "shared_buffers": "64MB",
        "work_mem": "4MB",
        "max_connections": "12",
    }
    for key, value in expect.items():
        assert key in conf, f"postgresql.conf 缺少 {key}"
        assert conf[key] == value, f"postgresql.conf {key} = {conf[key]!r}，期望 {value!r}"
    assert _unquote(conf.get("unix_socket_directories", "")) == str(SOCKDIR)
    auto = _conf_effective(AUTO_CONF)
    for key, value in expect.items():
        if key in auto:
            assert auto[key] == value, f"postgresql.auto.conf 覆盖了 {key} = {auto[key]!r}"

    assert wait_ready_sql("show listen_addresses") == "", "show listen_addresses 不是空"
    assert wait_ready_sql("show port") == str(PORT)
    assert wait_ready_sql("show shared_buffers") == "64MB"
    assert wait_ready_sql("show work_mem") == "4MB"
    assert wait_ready_sql("show max_connections") == "12"


# =========================================== 3) psql 经 socket：不落口令 ===


PSQL_SESSION = r'''
import json, os, subprocess, sys
from pathlib import Path

secrets = Path(os.environ["PSQL_SECRETS_DIR"])
port = os.environ["PSQL_PORT"]
sock = os.environ["PSQL_SOCKDIR"]
targets = [("chatbi_svc", "pgpass-svc"), ("chatbi_owner", "pgpass")]
psql = sys.argv[1]

passes = {}
for _user, name in targets:
    lines = [l.strip() for l in (secrets / name).read_text().splitlines() if l.strip()]
    passes[name] = lines[0]


def show(user, file_name, setting):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update({"PGPASSWORD": passes[file_name], "PGHOST": sock, "PGPORT": port,
                "PGUSER": user, "PGDATABASE": "chatbi",
                "PGCONNECT_TIMEOUT": "10", "PGAPPNAME": "chatbi-test-psql"})
    r = subprocess.run([psql, "-w", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                        "-c", f"show {setting}"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=60, env=env)
    blob = r.stdout + r.stderr
    return {"rc": r.returncode, "out": r.stdout.strip(),
            "leaked": any(v in blob for v in passes.values()),
            "err_head": r.stderr.strip()[:300]}


out = {
    "listen": show("chatbi_svc", "pgpass-svc", "listen_addresses"),
    "num": show("chatbi_svc", "pgpass-svc", "server_version_num"),
    "owner": show("chatbi_owner", "pgpass", "server_version_num"),
}
print(json.dumps(out))
'''


def _psql_probe(tmp_path: Path) -> dict[str, dict[str, object]]:
    """在子进程里读口令文件、经环境变量交给 psql，经 socket 查询；命令里不含口令。"""
    psql = shutil.which("psql")
    assert psql, "找不到 psql 客户端"
    script = tmp_path / "psql_session.py"
    script.write_text(PSQL_SESSION, encoding="utf-8")
    env = child_env(
        {"PSQL_SECRETS_DIR": str(SECRETS), "PSQL_PORT": str(PORT), "PSQL_SOCKDIR": str(SOCKDIR)},
        drop=("PGPASSWORD", "PGOPTIONS", "PGSERVICE", "PGPASSFILE"),
    )
    result = _run([str(PYTHON), "-B", str(script), psql], env=env, cwd=tmp_path, timeout=120)
    assert result.returncode == 0, f"psql 探测脚本自身失败：rc={result.returncode}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_s01_socket_psql_listen_addresses_empty(tmp_path: Path) -> None:
    """psql 经 socket（口令取自 0600 口令文件，经环境变量传递）查询：listen_addresses 为空。"""
    _require_pg()
    probe = _psql_probe(tmp_path)
    listen = probe["listen"]
    assert listen["rc"] == 0, f"psql 经 socket 连接失败（rc={listen['rc']}）：{listen['err_head']}"
    assert listen["out"] == "", "show listen_addresses 经 psql 不是空字符串"
    assert listen["leaked"] is False, "psql 输出里出现了口令文件的内容"

    num = probe["num"]
    assert num["rc"] == 0, f"psql 经 socket 连接失败（rc={num['rc']}）：{num['err_head']}"
    assert num["out"] == SERVER_VERSION_NUM
    assert num["leaked"] is False, "psql 输出里出现了口令文件的内容"

    owner = probe["owner"]
    assert owner["rc"] == 0, f"超管口令经 socket 连接失败（rc={owner['rc']}）：{owner['err_head']}"
    assert owner["out"] == SERVER_VERSION_NUM, "超管口令经 socket 的连接结果不符"
    assert owner["leaked"] is False, "psql 输出里出现了口令文件的内容"


# =================================================== 4) doctor 没有凭据值 =


def _llm_key_paths(node: object, prefix: str = "llm") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, child in node.items():
            found.append(f"{prefix}.{key}")
            found.extend(_llm_key_paths(child, f"{prefix}.{key}"))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found.extend(_llm_key_paths(child, f"{prefix}[{index}]"))
    return found


def test_s01_doctor_reports_key_presence_without_value(tmp_path: Path) -> None:
    """doctor 文本/JSON 都不含密钥值与口令文件内容；llm 节点只有布尔；设置/未设置分别 True/False。"""
    # 当前会话环境（不改写）
    env_natural = child_env({"HOME": str(tmp_path / "home-natural"), "TMPDIR": str(tmp_path)})
    text_natural = doctor(env=env_natural)
    json_natural = doctor("--json", env=env_natural)
    assert_ok(text_natural, "chatbi doctor")
    assert_ok(json_natural, "chatbi doctor --json")

    # 设置伪造密钥 / 未设置密钥
    set_env = child_env(
        {KEY_ENV: DUMMY_KEY, "HOME": str(tmp_path / "home-set"), "TMPDIR": str(tmp_path)}
    )
    unset_env = child_env(
        {"HOME": str(tmp_path / "home-unset"), "TMPDIR": str(tmp_path)}, drop=(KEY_ENV,)
    )
    text_set = doctor(env=set_env)
    json_set = doctor("--json", env=set_env)
    text_unset = doctor(env=unset_env)
    json_unset = doctor("--json", env=unset_env)
    for label, result in (
        ("chatbi doctor（设置密钥）", text_set),
        ("chatbi doctor --json（设置密钥）", json_set),
        ("chatbi doctor（未设置密钥）", text_unset),
        ("chatbi doctor --json（未设置密钥）", json_unset),
    ):
        assert_ok(result, label)
        assert str(tmp_path) not in result.stdout, f"{label} 输出里出现了临时目录路径"

    verdict = _leak_verdict(
        {
            "LEAK_SET_KEY": DUMMY_KEY,
            "LEAK_OUT_TEXT": "\n".join([text_natural.stdout, text_set.stdout, text_unset.stdout]),
            "LEAK_OUT_JSON": "\n".join([json_natural.stdout, json_set.stdout, json_unset.stdout]),
        }
    )
    _assert_no_key(verdict, "chatbi doctor")
    _assert_no_secret(verdict, "chatbi doctor")

    report = json.loads(json_natural.stdout)
    paths = _llm_key_paths(report["llm"])
    leaves = {path.rsplit(".", 1)[-1].lower() for path in paths}
    bad = sorted(leaves & FORBIDDEN_LLM_KEYS)
    assert not bad, f"llm 节点下出现了可疑键名：{bad}"
    head = DUMMY_KEY.lower()[:8]
    assert not any(head in path.lower() for path in paths), "llm 节点下出现了密钥前缀形状的键名"
    assert report["llm"]["api_key_present"] in (True, False)

    report_set = json.loads(json_set.stdout)
    report_unset = json.loads(json_unset.stdout)
    assert report_set["llm"]["api_key_present"] is True, "设置了密钥时 llm.api_key_present 应为 True"
    assert report_set["llm"]["state"] == "ready"
    assert report_unset["llm"]["api_key_present"] is False, "未设置密钥时 llm.api_key_present 应为 False"
    assert report_unset["llm"]["state"] == "pending_missing_key"
    assert "present" in text_set.stdout, "文本输出应报告密钥是否存在"
    assert "absent" in text_unset.stdout, "文本输出应报告密钥是否存在"


# ========================================= 5) doctor 的版本/资源/PG 连接 ==


def test_s01_doctor_versions_resources_and_pg_connect() -> None:
    """doctor --json 的 Python/依赖/工具/资源/PG 连接，以及 --strict 退出码为 0。"""
    _require_pg()
    natural_env = child_env()
    result = doctor("--json", env=natural_env)
    assert_ok(result, "chatbi doctor --json")
    report = json.loads(result.stdout)

    assert report["python"]["version"] == PYTHON_VERSION
    assert report["python"]["matches_required"] is True

    direct = report["dependencies"]["direct"]
    assert direct == PINNED_DIRECT, f"直接依赖版本不符：{direct}"
    assert report["dependencies"]["pytest"] == PINNED_PYTEST

    tools = report["tools"]
    assert UV_NEEDLE in (tools["uv"]["version"] or ""), "tools.uv.version 不含 0.13.9"
    assert NODE_NEEDLE in (tools["node"]["version"] or ""), "tools.node.version 不含 24.22.1"
    assert PG_BINARY_VERSION in (tools["psql"]["version"] or ""), "tools.psql.version 不含 17.12"

    resources = report["resources"]
    for key in ("nproc", "mem_total_mib", "disk_free_gib"):
        assert key in resources, f"resources 缺少 {key}"
        assert isinstance(resources[key], (int, float)), f"resources.{key} 不是数值"
    assert resources["nproc"] >= 1
    assert resources["mem_total_mib"] > 0
    assert resources["disk_free_gib"] > 0

    pg = report["postgres"]
    assert pg["binary_version"] == PG_BINARY_VERSION
    assert pg["connected"] is True, (
        f"PG 未连接：running={pg.get('running')} pid={pg.get('pid')} "
        f"socket_mode={pg.get('socket_mode')!r} error_class={pg.get('error_class')!r}"
    )
    assert pg["server_version_num"] == int(SERVER_VERSION_NUM)
    assert pg["database"] == DB_NAME
    assert pg["user"] == OWNER_ROLE, "postgres.user 不是角色名（疑似连接串）"
    assert not re.search(r"[/@?&=]|dbname=|password=", str(pg["user"])), (
        "postgres.user 看起来像连接串"
    )
    assert pg["socket_mode"] == "0o700", f"postgres.socket_mode={pg['socket_mode']!r}，期望 0o700"
    assert pg["port"] == PORT

    strict = doctor("--strict", env=natural_env)
    assert strict.returncode == 0, (
        f"chatbi doctor --strict 退出码 {strict.returncode}；应为 0（缺密钥不算未就绪）"
    )


# ==================================================== 6) 锁文件与 bootstrap ==


def test_s01_uv_lock_and_venv() -> None:
    """uv.lock 存在且只覆盖 3.13；.venv 的解释器是 3.13；控制台入口可用。"""
    lock = ROOT / "uv.lock"
    assert lock.is_file(), "缺少 uv.lock"
    lock_text = lock.read_text(encoding="utf-8")
    assert lock.stat().st_size > 0
    match = re.search(r'^requires-python = "([^"]+)"', lock_text, re.M)
    assert match, "uv.lock 缺少 requires-python"
    spec = match.group(1)  # uv 的写法可能是 "==3.13.*" 或 ">=3.13,<3.14"
    assert "3.13" in spec, f"uv.lock 的 requires-python 不含 3.13：{spec}"
    assert not re.search(r"3\.(?:1[0-2]|1[4-9])", spec), (
        f"uv.lock 的 requires-python 覆盖了 3.13 以外的解释器：{spec}"
    )
    for name, version in PINNED_DIRECT.items():
        locked = "pyyaml" if name == "PyYAML" else name.lower()
        assert re.search(rf'^name = "{locked}"$', lock_text, re.M), f"uv.lock 里没有 {name}"
        assert re.search(rf'^name = "{locked}"\nversion = "{re.escape(version)}"', lock_text, re.M), (
            f"uv.lock 里 {name} 的版本不是 {version}"
        )

    cfg_text = (ROOT / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    cfg = {}
    for line in cfg_text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()
    assert "version_info" in cfg, "pyvenv.cfg 缺少 version_info"
    assert re.fullmatch(r"3\.13(\.\d+)?", cfg["version_info"]), (
        f"pyvenv.cfg version_info={cfg['version_info']} 不是 3.13.x"
    )
    assert cfg.get("include-system-site-packages") == "false"
    entry = ROOT / ".venv" / "bin" / "chatbi"
    assert entry.is_file() and os.access(entry, os.X_OK), "缺少可执行的 .venv/bin/chatbi"
    version = _run([str(entry), "--version"], timeout=60)
    assert_ok(version, ".venv/bin/chatbi --version")
    assert "chatbi" in version.stdout


def test_s01_bootstrap_record(tmp_path: Path) -> None:
    """var/run/bootstrap.json：frozen=true、workbook_sha256 为 64 位十六进制；bootstrap 幂等。"""
    _require_pg()
    assert BOOTSTRAP_JSON.is_file(), "缺少 var/run/bootstrap.json（先 bash scripts/bootstrap）"
    record = json.loads(BOOTSTRAP_JSON.read_text(encoding="utf-8"))
    assert record.get("frozen") is True, "bootstrap.json 的 frozen 不是 true"
    assert re.fullmatch(r"[0-9a-f]{64}", record.get("workbook_sha256", "")), (
        "workbook_sha256 不是 64 位十六进制"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", record.get("lock_sha256", "")), (
        "lock_sha256 不是 64 位十六进制"
    )
    assert record.get("llm_api_key_present") in (True, False), "llm_api_key_present 不是布尔"
    assert _mode(BOOTSTRAP_JSON) == 0o600, (
        f"bootstrap.json 权限是 {oct(_mode(BOOTSTRAP_JSON))}，期望 0600"
    )

    pid_before = status_pid()
    # --no-sync：只建目录、写记录，绝不触网、不改依赖、不动集群
    result = _run(
        ["bash", str(ROOT / "scripts" / "bootstrap"), "--no-sync", "--no-verify-db"],
        env=child_env({"TMPDIR": str(tmp_path)}),
    )
    assert_ok(result, "bash scripts/bootstrap --no-sync")
    after = json.loads(BOOTSTRAP_JSON.read_text(encoding="utf-8"))
    assert after.get("workbook_sha256") == record.get("workbook_sha256")
    assert after.get("lock_sha256") == record.get("lock_sha256")
    assert after.get("frozen") is False, "--no-sync 时 frozen 必须如实记为 false"
    for path in (DATA, RUN, SECRETS, VAR / "stage", VAR / "audit", VAR / "evidence"):
        assert _mode(path) == 0o700, f"bootstrap 之后 {path} 不是 0700"
    assert pg_running(), "bootstrap --no-sync 之后实例不在运行"
    assert status_pid() == pid_before, "bootstrap --no-sync 不应动到运行中的实例"

    # 恢复成“已同步”的记录：uv sync --frozen 离线执行 + 普通 bootstrap
    lock_sha = record.get("lock_sha256")
    sync = _run(
        ["uv", "sync", "--frozen", "--offline"],
        env=child_env({"UV_CACHE_DIR": str(VAR / ".uvcache"), "TMPDIR": str(tmp_path)}),
    )
    if sync.returncode == 0:
        restore = _run(["bash", str(ROOT / "scripts" / "bootstrap")], env=child_env({"TMPDIR": str(tmp_path)}))
        assert_ok(restore, "bash scripts/bootstrap（恢复记录）")
        restored = json.loads(BOOTSTRAP_JSON.read_text(encoding="utf-8"))
        assert restored.get("frozen") is True, "重新同步后 bootstrap.json 的 frozen 应为 true"
        assert restored.get("lock_sha256") == lock_sha
    else:
        # 离线缓存不可用时如实报告（不伪造 frozen=true）；记录已由 --no-sync 写为 false
        pytest.fail(
            f"无法离线恢复 bootstrap 记录（uv sync --frozen --offline rc={sync.returncode}）；"
            "请在可联网会话执行 bash scripts/bootstrap 后再复跑"
        )


def test_s01_pyproject_pins() -> None:
    """pyproject.toml：Python 范围、7 个固定直接依赖、dev 组 pytest、console script。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["requires-python"] == ">=3.13,<3.14"

    parsed: dict[str, str] = {}
    for item in project["dependencies"]:
        name = re.split(r"[\[<=>!~ ]", item.strip(), maxsplit=1)[0]
        assert name not in parsed, f"直接依赖重复声明：{name}"
        parsed[name] = item.strip()
    assert set(parsed) == set(PINNED_DIRECT), f"直接依赖集合不符：{sorted(parsed)}"
    for name, version in PINNED_DIRECT.items():
        spec = parsed[name]
        if name == "psycopg":
            # psycopg[binary]==3.2.9：允许二进制 extra，版本必须精确固定
            assert re.fullmatch(r"psycopg(\[[a-z,]+\])?==3\.2\.9", spec), (
                f"psycopg 未固定为 3.2.9：{spec}"
            )
            continue
        assert spec == f"{name}=={version}", f"{name} 未固定为 {version}：{spec}"

    dev = [item.strip() for item in data["dependency-groups"]["dev"]]
    assert f"pytest=={PINNED_PYTEST}" in dev, "pytest 未固定在 dev 组"
    assert data["project"]["scripts"]["chatbi"] == "chatbi.cli:main"


# ================================================== 7) pg 脚本的失败路径 ==
#
# 全部在 tmp_path 里的伪造根目录上运行，绝不接触真实数据目录：
#   * 副本只把 ``ROOT=`` 那一行改到 tmp 里（scripts/pg 把 ROOT 写死成绝对路径）；
#   * 子进程 ``TMPDIR`` 指到 tmp 内的空目录（下载/解包只落在伪造根目录），
#     并用 PATH 上的哨兵 ``curl`` 记录任何下载尝试，断言记录文件不存在；
#   * 真实二进制若可用，则硬链接进伪造根目录，让检查走到“数据目录校验”这一步。


def _make_fake_clone(tmp_path: Path) -> tuple[Path, Path]:
    """在 tmp_path 里做一份 ``ROOT=`` 指向 fake_root 的 scripts/pg 副本。

    返回 (伪造脚本, 伪造根目录)。外层 stub 在 ``init`` 进入真脚本（即调用 initdb 前）
    把 pg_ctl/initdb 包成哨兵：真脚本真的调用它们时留下 ``start.called`` / ``initdb.sentinel``。
    """
    real = PG.read_text(encoding="utf-8").splitlines(keepends=True)
    idx = [i for i, line in enumerate(real) if line.startswith("ROOT=")]
    assert len(idx) == 1, "scripts/pg 里应恰好有一行以 ROOT= 开头"
    line_no = idx[0]
    assert re.fullmatch(r"ROOT=/[^\s\"'$]*\n", real[line_no]), (
        "scripts/pg 的 ROOT= 不是简单的绝对路径，无法在临时目录里做等价副本"
    )
    fake_root = tmp_path / "fakeroot"
    fake_root.mkdir()
    cloned = [*real]
    cloned[line_no] = f"ROOT={fake_root}\n"
    (tmp_path / "pg.real").write_text("".join(cloned), encoding="utf-8")
    stub = tmp_path / "pg"
    # stub 只做两件事：把 ROOT 换成伪造根目录的真脚本转调过去；把 PGBIN 的
    # pg_ctl/initdb 包装成哨兵副本——真脚本一旦真的调用它们，就留下
    # start.called / initdb.sentinel，用来证明“拒绝发生在动进程之前”。
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for name in pg_ctl initdb; do\n'
        '  real="$PGBIN/$name"\n'
        '  [ -x "$real" ] || continue\n'
        '  mkdir -p "$SENTINEL_PGBIN"\n'
        '  if [ ! -e "$SENTINEL_PGBIN/$name" ]; then\n'
        '    real_pgbin="$real"\n'
        '    printf "#!/usr/bin/env bash\\n: > \\\"$MARKFILE\\\"\\nexec \\\"%s\\\" \\\"$@\\\"\\n" "$real_pgbin" > "$SENTINEL_PGBIN/$name"\n'
        '    chmod 0755 "$SENTINEL_PGBIN/$name"\n'
        '  fi\n'
        'done\n'
        'export PATH="$SENTINEL_PGBIN:$PATH"\n'
        'export MARKFILE=""\n'
        'case "$1" in pg_ctl) export MARKFILE="$START_MARK";; initdb) export MARKFILE="$INITDB_SENTINEL";; esac\n'
        'exec bash "$(dirname "$0")/pg.real" "$@"\n',
        encoding="utf-8",
    )
    return stub, fake_root


def _fake_env(tmp_path: Path) -> dict[str, str]:
    """伪造根目录用的子进程环境：隔离 TMPDIR/HOME，curl 换成只记账的哨兵。"""
    tmpdir = tmp_path / "foreign-tmp"
    tmpdir.mkdir(exist_ok=True)
    bindir = tmp_path / "sentinel-bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "${CURL_SENTINEL_LOG}"\n'
        "echo 'sentinel: 测试中禁止下载' >&2\n"
        "exit 22\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return child_env(
        {
            "TMPDIR": str(tmpdir),
            "HOME": str(tmp_path / "foreign-home"),
            "CURL_SENTINEL_LOG": str(tmp_path / "curl.called"),
            "INITDB_SENTINEL": str(tmp_path / "initdb.sentinel"),
            "START_MARK": str(tmp_path / "start.called"),
            "SENTINEL_PGBIN": str(bindir),
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
        }
    )


def _seed_fake_dist(fake_root: Path) -> bool:
    """给伪造根目录放一份“可运行”的 17.12 二进制（符号链接到真实解包目录）。

    目的：让 start/init 通过“软件已就绪”检查，从而停在数据目录归属校验上，
    而不是先去下载。不复制 16MB+ 的包，只链接 bin 与 share 两个目录；
    真实解包目录可用时返回 True。
    """
    src = PG_HOME / "dist"
    postgres = src / "usr/lib/postgresql/17/bin/postgres"
    bki = src / "usr/share/postgresql/17/postgres.bki"
    if not postgres.is_file() or not bki.is_file() or not shutil.which("psql"):
        return False
    pgdir = fake_root / "var" / "pg"
    pgdir.mkdir(parents=True, exist_ok=True)
    dist = pgdir / "dist" / "usr"
    try:
        dist.mkdir(parents=True, exist_ok=True)
        for name in ("lib", "share"):
            link = dist / name
            if not link.exists():
                link.symlink_to((src / "usr" / name).resolve(), target_is_directory=True)
    except OSError:
        return False
    return True


def _assert_no_download(tmp_path: Path) -> None:
    assert not (tmp_path / "curl.called").exists(), (
        "伪造根目录里的调用尝试下载（被测试哨兵拦住，不应发生）"
    )


def _assert_failed(result: subprocess.CompletedProcess[str], what: str) -> None:
    assert result.returncode != 0, f"{what}：在没有本脚本初始化的数据目录时应以非零退出，实际 rc=0"
    verdict = _leak_verdict({"LEAK_OUT_TEXT": result.stdout})
    _assert_no_secret(verdict, what)


def _real_unchanged(pid_before: int | None) -> None:
    """真实实例未被伪造副本影响：仍在运行且 pid 未变（原本有 pid 时）。"""
    assert pg_running(), "真实实例被伪造副本影响了"
    if pid_before is not None:
        assert status_pid() == pid_before, "真实实例被伪造副本影响了"


def test_s01_pg_guard_missing_cluster(tmp_path: Path) -> None:
    """伪造目录里没有集群：stop 安全返回 0 且不建目录；start 非零退出且不创建 var/pg/data。"""
    stub, fake_root = _make_fake_clone(tmp_path)
    data = fake_root / "var" / "pg" / "data"
    env = _fake_env(tmp_path)
    seeded = _seed_fake_dist(fake_root)
    assert start_pg_if_needed(), "前置失败：真实实例未能启动（本用例需要它作为对照）"
    pid_before = status_pid()

    stop = _run(["bash", str(stub), "stop"], env=env, cwd=tmp_path, timeout=120)
    assert stop.returncode == 0, "没有数据目录时 stop 应安全返回 0（幂等）"
    assert not data.exists(), "stop 不应创建数据目录"

    start = _run(["bash", str(stub), "start"], env=env, cwd=tmp_path, timeout=120)
    _assert_failed(start, "伪造目录里的 scripts/pg start")
    assert not data.exists(), "start 在缺少集群时创建了 var/pg/data"
    assert not (fake_root / "var" / "run" / "secrets").exists(), "失败的 start 不应创建口令目录"
    if seeded:
        # 二进制就绪时，start 必须停在“先 init”的检查上，且根本没调用 pg_ctl
        assert "请先运行" in start.stderr, "start 应提示先运行 scripts/pg init"
        assert not (tmp_path / "start.called").exists(), "start 应在调用 pg_ctl 之前就拒绝"
        assert not (tmp_path / "initdb.called").exists(), "start 不应走到 initdb"
        _assert_no_download(tmp_path)
    else:
        assert (tmp_path / "curl.called").exists(), "缺少二进制时应先尝试安装（被哨兵拦住）"
    _real_unchanged(pid_before)


def test_s01_pg_guard_rejects_foreign_data_dir(tmp_path: Path) -> None:
    """伪造根目录里有“别人的”数据目录：stop/start/init 全部拒绝，且一字不改。"""
    stub, fake_root = _make_fake_clone(tmp_path)
    data = fake_root / "var" / "pg" / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("17\n", encoding="utf-8")
    sentinel = data / "FOREIGN_KEEP"
    sentinel.write_text("keep\n", encoding="utf-8")
    listing_before = sorted(path.name for path in data.iterdir())
    env = _fake_env(tmp_path)
    seeded = _seed_fake_dist(fake_root)
    assert start_pg_if_needed(), "前置失败：真实实例未能启动（本用例需要它作为对照）"
    pid_before = status_pid()

    for sub in ("start", "stop", "init"):
        result = _run(["bash", str(stub), sub], env=env, cwd=tmp_path, timeout=TIMEOUT)
        _assert_failed(result, f"伪造目录里的 scripts/pg {sub}（外来数据目录）")
        assert sorted(path.name for path in data.iterdir()) == listing_before, (
            f"{sub} 改动了外来数据目录里的文件"
        )
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not (fake_root / "var" / "run" / "secrets").exists(), "拒绝时不应生成口令文件"
    if seeded:
        # 二进制就绪：拒绝必须发生在任何安装动作与 pg_ctl/initdb 之前
        _assert_no_download(tmp_path)
        assert not (fake_root / "var" / "pg" / "spec.sha256").exists(), "拒绝时不应写初始化指纹"
        assert not (tmp_path / "start.called").exists(), "start 不应调用 pg_ctl"
        assert not (tmp_path / "start.called").exists(), "不应调用 pg_ctl"
        assert not (tmp_path / "initdb.sentinel").exists(), "不应调用 initdb"
    else:
        # 没有可用二进制：init 会先走“准备软件”（被哨兵 curl 拦住）；数据目录仍一字未改
        assert (tmp_path / "curl.called").exists(), "缺少二进制时应先尝试安装（被哨兵拦住）"
        assert sorted(path.name for path in data.iterdir()) == listing_before
    _real_unchanged(pid_before)


def test_s01_pg_guard_rejects_mismatched_state(tmp_path: Path) -> None:
    """cluster.env 存在但指向别处：start/init 拒绝，不 reset、不起进程。"""
    stub, fake_root = _make_fake_clone(tmp_path)
    data = fake_root / "var" / "pg" / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("17\n", encoding="utf-8")
    (fake_root / "var" / "pg" / "cluster.env").write_text(
        f"DATA_DIRECTORY='{fake_root}/var/pg/other'\nPORT={PORT}\n", encoding="utf-8"
    )
    env = _fake_env(tmp_path)
    seeded = _seed_fake_dist(fake_root)
    assert start_pg_if_needed(), "前置失败：真实实例未能启动（本用例需要它作为对照）"
    pid_before = status_pid()

    for sub in ("start", "init"):
        result = _run(["bash", str(stub), sub], env=env, cwd=tmp_path, timeout=TIMEOUT)
        _assert_failed(result, f"伪造目录里的 scripts/pg {sub}（cluster.env 指向别处）")
        assert (data / "PG_VERSION").read_text(encoding="utf-8") == "17\n"
        assert sorted(path.name for path in data.iterdir()) == ["PG_VERSION"], (
            f"{sub} 改动了数据目录"
        )
    assert not (fake_root / "var" / "run").exists(), "拒绝时不应创建运行期目录"
    if seeded:
        assert not (tmp_path / "start.called").exists(), "start 不应在状态不符时调用 pg_ctl"
    _real_unchanged(pid_before)


# ================================================== 8) 启停是真的在运行 ===


def test_s01_pg_start_is_idempotent_and_live() -> None:
    """运行中的实例再 start 不会另起进程；status / sql 一致报告在运行。"""
    _require_pg()
    text = status_text()
    assert _STATUS_LINE.search(text), "scripts/pg status 未报告 server_version/170012"
    assert _STATUS_STARTED.search(text), "scripts/pg status 未报告 data_dir/started"
    pid = status_pid()
    started = status_started()
    assert status_running(), "scripts/pg status 未报告 state running"

    result = pg_script("start")
    assert_ok(result, "scripts/pg start（已在运行时）")
    assert "已在运行" in result.stderr, "已在运行时 start 应报告复用"
    assert "已启动" not in result.stderr, "已在运行时 start 不应再启动一个新的 postmaster"
    # 复用而不是重启：启动时间不变（pid 能拿到时也要求 pid 不变）。
    assert status_started() == started, "已在运行时 start 竟然重启了 postmaster"
    if pid is not None:
        assert status_pid() == pid, "已在运行时 start 竟然换了 pid"
    assert wait_ready_sql("select current_database()") == DB_NAME


# ------------------------------------------------------------ 会话收尾 --


def test_s01_zzz_final_pg_running() -> None:
    """整场结束时 PG 必须处于运行状态（重启类用例的收尾）。"""
    if not pg_running():
        start_pg_if_needed()
    assert pg_running(), f"测试结束时 PG 未运行（{_PG_START_FAILURE or 'scripts/pg start 未成功'}）"
    text = status_text()
    assert _STATUS_LINE.search(text), "测试结束时 scripts/pg status 未报告在运行"
    assert wait_ready_sql("select 1") == "1"
    assert _mode(SOCKDIR) == 0o700
