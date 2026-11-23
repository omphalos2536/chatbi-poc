# 02 模型网关

## 目标

把映射建议和 SQL 生成收敛到一个受控的外发入口，能用记录传输体的替身独立验收。

## 前置条件

01 完成，实连探针另需操作员在进程环境中设置密钥。

## 输入

模型、地址和变量名见总体设计；映射字典和查询字典先用虚构字段夹具。

## 详细任务清单

1. `gateway/client.py` 只接受两类结构。mapping 的问题为固定映射任务，字典含已审核的源表头、单位、候选标准字段；query 的问题为脱敏问句，字典含获准的视图、字段、关系、口径和无值示例。审核前的陌生表头先本地人工标记可出域。每次按用途重建请求，拒收多余键。先对问句做字段/域检查、本地实体匹配，用参数标记代替客户名等值，拒绝粘贴表格、账号、电话和成段记录，原始参数仅在本地持有。
2. httpx 只允许向固定地址的 `/chat/completions` POST，TLS、无重定向，无自动代理和未计数 SDK 重试。model 固定；system 为版本化静态指令，user 中只有 question 和 dictionary。映射返回候选 JSON，SQL 返回含候选 SQL 的 JSON。仅提取 choices[0].message.content，本地校验结构、大小（建议 16 KiB）；响应不执行。纠错类别选预定义 system 指令，详见 12。
3. 用私有 Unix socket 提供网关服务，调用者及用途受本地配置限制，禁止客户端指定 URL/密钥/任意消息。启动器清空其他服务中的密钥。记录 request_id、用途、授权字典摘要、模型、耗时、状态和 token 用量（若有）；只在合成测试捕获请求正文，无外发日志平台。
4. 假传输分别模拟成功、超时、无权限、畸形 JSON、429。启用独立 `chatbi gateway probe --live`，发无数据的合成字典问题，打印模型和状态码；未配置打印 `LIVE_KEY_MISSING`，不伪造成功。

## 交付物

`src/chatbi/gateway/{client,server,policy}.py`、`config/gateway.yaml`、`tests/test_s02.py`、`var/evidence/gateway.json`。

## 验收条件

1. `pytest -q tests/test_s02.py -k payload` 通过；用合成哨兵替代金额、客户名、原始错误、额外字段和密钥，在解码后的 messages 中出现次数为 0；两种用途各有成功例。
2. `pytest -q tests/test_s02.py -k transport` 通过；拒绝更换主机、跳转及未授权字段，拒绝前的出站次数为 0，所有日志无密钥。`chatbi gateway probe --mock` 输出 MOCK_OK。
3. `chatbi gateway probe --live` 有凭据时退出 0，返回 LIVE_OK、指定模型、可解析内容及请求时间；缺凭据时退出非零，记“实连待验”，可继续离线步骤。

## 常见坑

把样例行当字段说明，或在重试中带上 PG 完整错误，都绕过了出口契约。模型接口支持情况须用真实请求验证。

## 预计工作量与并行

1.5–2 小时。与 03 并行。
