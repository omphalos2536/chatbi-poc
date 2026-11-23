VERDICT: PASS

# Step 02 模型网关验收报告（Attempt 1）

## 验收上下文

- 验收时间：2026-11-24T07:35:41+08:00（Asia/Shanghai）
- 执行人：Codex acceptance agent
- 机器：`grok-bot-vm-193309795`，Linux 6.12.113+ x86_64
- Git 基线：`ec1f5ef88d44892d56a8f73601e4ecb858267d18`
- 构建标识：`932b486148791b2c5a8357fef87bb586b04f26fe62098c118b601359f5abf00a`（当前 Step 02 交付未提交；该值为相关交付文件逐项 SHA-256 排序后的清单哈希）
- 运行环境：Python 3.13.5、pytest 8.4.1、httpx 0.28.1
- PostgreSQL：17.12，验收时未启动；Step 02 不依赖 PostgreSQL
- 配置摘要：固定 HTTPS 端点 `/chat/completions`，模型 `deepseek-v4.1-flash`，不跟随跳转，不读取代理环境，系统指令版本 `s02.1`
- 凭据：`CHATBI_LLM_API_KEY` 存在；仅检查存在性并用于真实探针，未读取、打印或写入其值
- 交付物：`src/chatbi/gateway/{client,server,policy}.py`、`config/gateway.yaml`、`tests/test_s02.py`、`var/evidence/gateway.json` 均存在

## AC1：外发载荷约束与两种用途成功例

执行命令：

```bash
timeout 300 .venv/bin/pytest -q tests/test_s02.py -k payload
```

实际结果：退出码 0；`4 passed, 13 deselected in 0.10s`。测试断言实际解码出站 `messages`，验证 mapping/query 各有一个成功例，并验证合成金额、客户名、原始错误、额外字段、合成密钥及禁止键出现次数均为 0。

结论：**PASS**。证据：`var/evidence/step-02-attempt-1/ac1-payload.txt`。

## AC2：固定传输、拒绝越权与 mock 探针

执行命令：

```bash
timeout 300 .venv/bin/pytest -q tests/test_s02.py -k transport
timeout 120 .venv/bin/chatbi gateway probe --mock
```

实际结果：两条命令退出码均为 0。transport 测试为 `7 passed, 10 deselected in 0.28s`；覆盖固定 HTTPS 主机和路径、禁跳转、未授权字段本地拒绝、拒绝前零出站、失败与重试预算及审计无密钥。独立 CLI 输出：`MOCK_OK transport=mock model=deepseek-v4.1-flash status=200`。

结论：**PASS**。证据：`var/evidence/step-02-attempt-1/ac2-transport.txt`、`var/evidence/step-02-attempt-1/ac2-probe-mock.txt`。

## AC3：真实模型探针

执行命令：

```bash
timeout 120 .venv/bin/chatbi gateway probe --live
```

实际结果：退出码 0；输出 `LIVE_OK`，`model=deepseek-v4.1-flash` 与 `configured_model=deepseek-v4.1-flash` 一致，`http_status=200`，`content_parsed=True`，含 `requested_at`，`elapsed_ms=27052`，并返回 token usage。

结论：**PASS**。这是真实网络请求，不是脚本化替身。证据：`var/evidence/step-02-attempt-1/ac3-probe-live.txt`。

## 独立正负复验与完整回归

正例命令为独立运行的 `timeout 120 .venv/bin/chatbi gateway probe --mock`，退出 0 并输出 `MOCK_OK`。

负例命令：

```bash
timeout 120 env -u CHATBI_LLM_API_KEY .venv/bin/chatbi gateway probe --live
```

实际结果：退出码 2，输出 `LIVE_KEY_MISSING`，未输出 `LIVE_OK`，符合缺凭据不得伪造成功的要求。证据：`var/evidence/step-02-attempt-1/independent-negative-missing-key.txt`。

完整回归命令：

```bash
timeout 300 .venv/bin/pytest -q tests/test_s02.py
```

实际结果：退出码 0；`17 passed in 9.95s`，无 skip、xfail。证据：`var/evidence/step-02-attempt-1/full-suite.txt`。

另以环境中的真实凭据值对本轮 6 个文本证据做字节扫描（不输出凭据），命中数为 0。

## 问题清单

无。三个验收条件均通过，无 FAIL/PENDING。

结构化验收证据：`var/evidence/step-02.json`。
