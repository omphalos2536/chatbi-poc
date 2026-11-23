# 01 本机环境

## 目标

让第二个开发者用普通账号从锁文件建立同版本环境，独立启动、查询和停止 PostgreSQL。

## 前置条件

Debian 13 amd64，工作区和临时目录可写，网络能访问 Debian 和 PyPI。

## 输入

总体设计的版本表、工作簿、系统 apt 索引；先记录 nproc、free -m、df -h .、python3 -V、psql -V、node -v、uv --version。

## 详细任务清单

1. 新建 `pyproject.toml`，以 `src/chatbi` 为包，声明 Python >=3.13,<3.14，注册 `chatbi` 控制台入口。直接依赖固定至总体设计版本，生成 `uv.lock`；`scripts/bootstrap` 重跑执行 `uv sync --frozen`。开发阶段将 pytest 放 dev 组。引导时创建 var 子目录，私有目录权限 0700。
2. `scripts/pg` 实现 init/start/status/sql/stop；首选已存在的完整 17.12 服务端。此机只有客户端，采用下方普通用户路径，下载后先验哈希。软件存 `var/pg/dist`，集群单独存 `var/pg/data`。`initdb -L` 指向解包的共享目录，编码 UTF8、locale C。socket 用短的绝对路径，目录属主 0700，端口 55432，`listen_addresses=''`。本地用 SCRAM 与仅属主可读的 pgpass，initdb 超管口令随机生成，查询服务仅分发各自角色口令。任何日志都跳过口令。
3. 从当前 Debian 索引可复现的命令如下。版本下架时由管理员提供同哈希缓存或兼容的本地 17.x 服务，先记录偏差、跑全套 PG 测试。禁止默认使用系统集群或重置现存数据目录。
```bash
mkdir -p var/pg/dist
curl -fLsS -o var/pg/server.deb https://deb.debian.org/debian/pool/main/p/postgresql-17/postgresql-17_17.12-0+deb13u1_amd64.deb
echo '72443dcedfa0bdfa79dafdf414c18498b89929eeee1555e987c87154f3082fea  var/pg/server.deb' | sha256sum -c -
dpkg-deb -x var/pg/server.deb var/pg/dist
var/pg/dist/usr/lib/postgresql/17/bin/postgres --version
```
4. `scripts/pg sql 'SQL'` 经 psql -XAt、ON_ERROR_STOP 执行，创建专用库 chatbi；脚本校验 data_directory 和自己的 pid，再启动/停止。`chatbi doctor` 打印版本、资源、PG 连接和密钥是否存在（布尔）；缺密钥仅标记 live 未就绪。

## 交付物

`pyproject.toml`、`uv.lock`、`scripts/bootstrap`、`scripts/pg`、`src/chatbi/cli.py`、`tests/test_s01.py`、`var/evidence/env.json`。

## 验收条件

1. `bash scripts/bootstrap && uv sync --frozen && .venv/bin/chatbi doctor --json` 成功；报告的 Python、PG 二进制、直接依赖版本与总体设计一致。
2. `scripts/pg init && scripts/pg start && scripts/pg sql 'show server_version_num'` 输出 170012；再运行 init/start 后库标识不变。`scripts/pg sql 'show listen_addresses'` 返回空。
3. `scripts/pg stop && scripts/pg start && .venv/bin/pytest -q tests/test_s01.py` 全绿；测试核对重启标识、socket 权限以及 doctor 中没有凭据值。

## 常见坑

psql 可用不能证明有 initdb。系统 Python 受 PEP 668 管理，避免全局 pip。离线缓存优先复用；安装资源与服务运行目录分开。

## 预计工作量与并行

1.5–2 小时。为其余步骤提供基础，先合并。
