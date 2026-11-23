VERDICT: PASS

# Step 01 本机环境验收报告（Attempt 1）

## 验收上下文

- 执行人：`box`
- 执行时间：2026-11-24 05:47-05:49 CST（UTC+8）
- 机器：Debian 13 amd64，Linux 6.12.113+，8 CPU，16002 MiB 内存，工作区可用 115 GiB
- Git HEAD：`6b8a928dfbf2effef0e83e0cac645ca81853fa0d`
- 构建摘要：`de0dcf108dea494b0654467561afd36d7efe9ba49890ab0f517dd253ca434039`
- `uv.lock` SHA-256：`e4ef844facb8af5698272a0d78b1ef392b2c929ebfdd04346755cc91d412b0de`
- PostgreSQL：17.12（Debian 17.12-0+deb13u1），私有集群 `be37e7ec1973e46080e66cf8eae632b1`
- 环境证据：`var/evidence/env.json`（最终状态 PASS，SHA-256 `30787570f15d34e45a7bb4fc8e103fdd947c3388834b13f4d02f72ad314863ff`）
- 汇总证据：`var/evidence/step-01.json`

## 前置环境记录

执行命令：`nproc; free -m; df -h .; python3 -V; psql -V; node -v; uv --version`

实际结果：退出码 0。`nproc=8`；内存 16002 MiB；磁盘可用 115 GiB；Python 3.13.5；psql 17.12；Node v24.22.1；uv 0.13.9。

是否通过：通过。

## 验收条件 1

验收要求：bootstrap、冻结同步及 doctor 成功；Python、PG 二进制和直接依赖版本与总体设计一致。

执行命令：

```bash
timeout 300 bash -c 'bash scripts/bootstrap && uv sync --frozen && .venv/bin/chatbi doctor --json'
```

实际结果：退出码 0。`uv sync --frozen` 检查 27 个包成功；Python 3.13.5、PG 二进制 17.12、uv 0.13.9、Node 24.22.1、pytest 8.4.1。直接依赖为 openpyxl 3.1.5、psycopg 3.2.9、FastAPI 0.116.1、Uvicorn 0.35.0、PyYAML 6.0.2、sqlglot 27.8.0、httpx 0.28.1，均与版本表一致。doctor 未输出任何密钥值。

是否通过：通过。

## 验收条件 2

验收要求：初始化并启动 PG，服务端版本号为 170012；重复 init/start 后库标识不变；监听地址为空。

执行命令：

```bash
timeout 300 bash -c 'scripts/pg init; scripts/pg start; scripts/pg sql "show server_version_num"; sed -n "1p" var/pg/data/CHATBI_CLUSTER; scripts/pg init; scripts/pg start; sed -n "1p" var/pg/data/CHATBI_CLUSTER; scripts/pg sql "show listen_addresses"; scripts/pg status'
```

实际结果：退出码 0。`server_version_num=170012`；两轮集群标识均为 `be37e7ec1973e46080e66cf8eae632b1`；`listen_addresses` 返回空行；状态显示数据库 `chatbi` 正在通过 `/workspace/chatbi-poc/var/run/pg` 的 55432 端口 Unix socket 接受连接。服务器包 SHA-256 为文档指定的 `72443dcedfa0bdfa79dafdf414c18498b89929eeee1555e987c87154f3082fea`。

是否通过：通过。

## 验收条件 3

验收要求：停止并重启 PG 后，`tests/test_s01.py` 全绿；覆盖重启标识、socket 权限和 doctor 不泄露凭据。

执行命令：

```bash
timeout 300 bash -c 'scripts/pg stop && scripts/pg start && .venv/bin/pytest -q tests/test_s01.py'
```

实际结果：退出码 0；`13 passed in 11.93s`，无 skip、xfail。启动状态仍使用上述集群标识并返回 170012。目录 `var/pg`、`var/run`、`var/run/pg`、`var/run/secrets` 均为 0700；`pgpass` 与 `pgpass-svc` 均为 0600。

是否通过：通过。

## 独立正负例复验

正例命令：`timeout 180 .venv/bin/pytest -q tests/test_s01.py -k restart_keeps_cluster_id_and_connects`

实际结果：退出码 0；`1 passed, 12 deselected in 4.58s`。

负例命令：`timeout 180 .venv/bin/pytest -q tests/test_s01.py -k pg_guard_rejects_foreign_data_dir`

实际结果：退出码 0；`1 passed, 12 deselected in 1.64s`，确认脚本拒绝外部数据目录。

是否通过：通过。

## 结论与问题清单

3 条必验条件全部通过，独立正例与负例均通过，未发现 FAIL/PENDING 项。问题清单：无。
