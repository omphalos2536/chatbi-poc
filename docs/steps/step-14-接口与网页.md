# 14 接口与网页

## 目标

让验收者从同一网页或 HTTP 入口提问，看到金额、单位、实际 SQL、口径和原表位置。

## 前置条件

13 完成；本地 PG、网关和当前发布批次可用。

## 输入

统一结果块、授权策略、签认记录、当前批次、审计与来源索引。

## 详细任务清单

1. FastAPI 提供 GET /health、POST /api/session、POST /api/ask、GET /api/sources/{id}、DELETE /api/session。演示身份由服务器核验本地随机令牌，POST /api/session 换 HttpOnly、SameSite=Strict、短时 cookie，状态请求校验 Origin；脚本可用受限 bearer。令牌文件 0600，`chatbi auth issue --user boss --curl-config var/run/boss.curl` 生成本机 curl 配置，勿把令牌写页面。ask 入参仅 question（1–1000 字符）、可选 session_id；拒绝注入角色、题号或 expected。授权从服务器确定。
2. 统一响应契约（下列字段名供评分器使用）。顶层 `request_id,status,route,batch_id,session_id,blocks[],warnings[]`。每块 `metric_id,title,columns[{id,label,type,unit}],rows,sql,params,definition,period,sources[],check_ids[],notes[],template_version,attempts`；rows 为按 columns.id 的对象，Decimal 用字符串，日期 ISO，NULL 用 JSON null。sources 含 source_id、文件哈希、sheet、行号/行段或坐标、映射版本；聚合保留可复算的筛选、贡献行数和本地行集引用。实际 SQL 与占位符参数分开，带当次 scope，允许同角色复算。空表也返回列、口径；部分子问失败返回 partial 并说明缺项。
3. 200 携 ok/empty/partial/clarify/unsupported，401 未认证、403 拒绝、422 参数错误、503 模型或数据未就绪、504 整问超时。读来源和会话时按当前权限及批次复核；只展示授权原表坐标或脱敏预览。错误页给 request_id。前端同源服务，一页包含问题、提交、表格、单位、SQL 展开、口径和可点击来源；表格语义标签、键盘可提交，空态/加载/超时可见。以文本节点渲染值。静态路由只挂 web/。
4. `scripts/serve start|stop|status` 先验库/版本，提供私有 BFF socket 和 127.0.0.1:8080 同源转发。用 bwrap 建最小文件树，仅挂依赖、已审核 runtime 资产、所需 socket、查询角色凭据和专属可写审计目录；BFF/查询无外网（独立网络命名空间），前置转发器固定上游 socket；网关只挂模型配置/策略/私有 socket，持外发凭据，目的地由 02 限定。导入者只挂所需工作簿和映射。数据库不挂评测目录。运行环境不挂项目根、宿主 /tmp、refs、private-eval、evaluator、历史日志、设计文档。保留 `scripts/serve probe-isolation` 以相同启动配置检查路径不可读，报告每个进程的挂载和出口。

## 交付物

`src/chatbi/api/{app,auth,sources}.py`、`web/{index.html,app.js,style.css}`、`scripts/serve`、`tests/test_s14.py`、`var/evidence/ui/`。

## 验收条件

1. `scripts/serve start && curl -fsS http://127.0.0.1:8080/health` 返回 ready 与当前批次；`pytest -q tests/test_s14.py` 全绿，覆盖响应 schema、401/403/422、空态、来源撤权和模板/长尾结果。
2. `chatbi auth issue --user boss --curl-config var/run/boss.curl && curl -fsS -K var/run/boss.curl -H 'Content-Type: application/json' -d '{"question":"本季按月列出合格产量"}' http://127.0.0.1:8080/api/ask` 返回非空 blocks、月份、单位、执行 SQL、口径、可访问来源及批次。
3. `node --check web/app.js && scripts/serve probe-isolation` 成功，所有被测进程读取基准/私有报告失败，BFF 直接访问外网失败。用浏览器打开 `http://127.0.0.1:8080/`，实际提交上题，展开 SQL 并点一个源行；验收者比对同参数 API 的字段和值相同，保存结果、来源两张截图及浏览器版本到 `var/evidence/ui/`。

## 常见坑

低权限访问某张原始 Excel 会看到隐藏列，来源接口只能提供受权投影。网页可通过本机浏览器或 SSH 本地端口转发演示。

## 预计工作量与并行

2–3 小时。接口与原生网页可按契约并行。
