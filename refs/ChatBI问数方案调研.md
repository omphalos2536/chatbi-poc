# ChatBI / 自然语言问数（Text-to-SQL）市面方案调研

- 调研日期：2026-11-24（Asia/Shanghai）
- 服务对象：shengjie sun 的 OPC 公司，面向一家 MLCC 检测治具小厂（约 5–6 名用户，一期预算约 10 万元）
- 项目约束：企业知识库 + 问数；数据库必须本地部署，只有大模型推理可以走云端；需要统一自建界面，不希望把第三方产品 UI 直接交给老板
- 触发问题：做 ChatBI 是不是必须让客户提供固定字段的财务表、我们建库、再为每个可能问出的问题写死 SQL？表格一变就导不进去，问题也要尽量固定，会不会太麻烦？有没有更通用的办法少做这些事？
- 写作规则：每条事实附来源链接；查不到写「未核实」；工作量与费用凡非官方标价均标注「估算」
- GitHub Star、最近推送时间以 2026-11-24 凌晨通过 [GitHub API](https://docs.github.com/en/rest/repos/repos) 读取的 `stargazers_count` / `pushed_at` 为准（API 时间为 UTC，下文已换算为 Asia/Shanghai）

---

## 1. 先直接回答那个疑问

**不必把问题和表格都写死，但必须先把「字段含义」和「指标口径」约定清楚。** 市面上没有一种通用产品，能在既不对口径、又不做字段对齐的情况下，保证财务数字每次都和账对得上。

更准确的分工是：

1. **写死的是怎么算，不是怎么问。** 例如「月营收」要先约定含不含税、退货扣不扣、按开票日还是发货日。这个口径可以写成一条参数化 SQL，也可以写进语义层里的一个指标。老板仍可随便问「上个月营收多少」「A 客户今年各月卖了多少」——变的是时间、客户、分组这些参数，算法始终是同一套。
2. **客户不必改成一张全新的标准表，但导出格式要相对稳定。** 通用做法是：客户继续从 ERP 或 Excel 导出原来的报表，导入时做一层字段映射（列名、日期格式、金额单位对齐到标准库）。同一来源下次导出列名若大改，需要重新映射，不能指望系统无限自动猜对。
3. **模板覆盖不住的问题也能问，只是要标明可信度。** 长尾问题交给模型现场生成 SQL，在本地做只读校验后再执行，答案上标注「非签字口径，仅供参考」。常问的再升级成指标或模板。

这一点和 Codex 在独立评审里建议的「一期六个签字指标 + 受限长尾问数」是同一条路，只是把「手写 6 条 SQL」理解成「最小语义层」，而不是「一个问题写一条 SQL」。评审原文见 [企业知识库-Codex独立评审报告.md](./企业知识库-Codex独立评审报告.md)。

行业里把这条路线叫 **Text2Metrics / 指标语义层**：先识别问题要的是哪个已发布指标，再由引擎生成受控查询，而不是让模型对着原始表自由写 SQL。衡石在 2026-09-16 的公开文章里把这一点写得很直白：SQL 能执行，答案仍可能不可用，因为「本月新增客户」涉及去重规则、时间口径等业务约定，模型无法凭空获得。来源：[从 Text2SQL 到 Text2Metrics：企业问数为什么需要指标语义层](https://www.hengshi.com/blog/text2sql-to-text2metrics-semantic-layer.html)。数势科技也把自家路径称为 NL2Semantics，并明确区分于直接 NL2SQL。来源：[SwiftAgent 产品页](https://digitforce.com/product/data-agent)。

---

## 2. 开源方案对比（截至 2026-11-24）

下表 Star 为约数档，精确值见各节。许可结论只针对公开 LICENSE，不能代替律师意见；若二次封装后交付给客户，务必再做一次许可复核。

| 项目 | Star | 许可证与商用要点 | 核心机制 | Excel/CSV 直接问 | 本地库 + 云端 LLM | 活跃度 | 对本项目 |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| WrenAI 新版 `wren-v0.18.0` | 18,375 | 核心/SDK/CLI 为 Apache-2.0；文档 CC BY 4.0；AGPL 文本为未来模块预留 | 语义层 MDL，受控 Text-to-SQL | 商业云有 CSV 上传；OSS 可经 DuckDB 读文件。Excel 工作簿一键上传未作为新版官方能力核实 | 可以，官方强调数据原地查询、自带模型 | 2026-11-10 发 0.18.0，仓库 2026-11-23 仍有推送 | **二期语义层首选候选**，一期不必整套引入 |
| Wren GenBI Classic `0.29.x` | 同上仓库 | 根 LICENSE 为 AGPLv3 | 聊天式 GenBI + 语义层 | Classic 文档支持 DuckDB 文件 | 可以，但已停功能与安全修复 | 官方称无新功能或安全修复 | **一期不要采用** |
| Vanna | 23,787 | MIT，商用和二开限制少 | RAG：schema、文档、问答 SQL 示例 | 本身是连数据库的框架，不是财务表上传台 | 可以自托管并用云端模型 | **已于 2026-03-29 归档**，最后推送 2026-02-02 | **不建议作为新交付依赖** |
| DB-GPT | 20,326 | MIT | Agent 写 SQL/Python，可微调 Text2SQL | 官方介绍支持 CSV/Excel、仓库和知识库 | 可以，模型可本地或 API | 很活跃，v0.8.3 于 2026-11-17 发布 | 能力全但平台重，一期不宜再叠一套 |
| SQLBot（飞致云） | 7,195 | 修改版 GPLv3：不可去 Logo/版权；衍生作品须开源；细粒度权限等在闭源 X-Pack | RAG：表字段说明、表关系、术语库、SQL 示例，专业版还有自定义提示词 | 官方安装脚本挂载 Excel 目录，发行说明含 Excel 上传接口修复 | 可以 Docker 自托管并自配模型 | 很活跃，v1.10.3 于 2026-10-29 发布 | 中文问数产品最完整，但白标进统一 UI 有许可风险 |
| DataEase | 24,730 | 与 SQLBot 同类：修改版 GPLv3 + Logo 限制 | 开源 BI，问数主要通过嵌入 SQLBot | 自身支持 Excel/CSV 数据源 | 可本地部署 | 很活跃 | 适合当 BI 底座，不替代本项目统一界面 |
| SuperSonic（腾讯音乐） | 5,189 | Apache-2.0 + 附加条件：不改源码和 Logo 可商用；开发并分发衍生作品需向作者取得商业许可 | Chat BI + Headless BI 语义层 | 以连接数据源和语义模型为主，Excel 一键问数未核实 | 可自托管并接 LLM | 仍在维护，最近推送 2026-11-06 | 语义层思路对，但 Java 全家桶偏重，分发衍生作品受限 |
| Chat2DB | 28,369 | 5.3.0 起附加限制：允许个人/非营利/组织内部使用；向外部客户提供产品、托管交付或嵌入均需书面商用授权 | 开发者/DBA 客户端，AI 生成和解释 SQL | 是数据库客户端，不是业务问数台 | 可本地，可接自有模型 | 活跃，最近推送 2026-11-11 | **不能作为交付给客户的问数产品组件**（除非另行授权） |
| RAGFlow 自带 Text2SQL | 94,595 | Apache-2.0（主仓库；依赖需单独清点） | 知识库（Schema、库表说明、问答 SQL 示例）+ 生成 SQL + Execute SQL 组件 | 知识库可解析文档/表格；不是专门的财务导入台 | 可以，已是本项目知识库选型 | v1.0.0 于 2026-11-06 发布，仓库持续更新 | **适合做长尾问数补充**，不能单独当财务签字口径层 |
| Dify | 161,753 | 修改版 Apache-2.0：可作企业应用或后端；多租户运营需商用许可；使用官方前端不可去 Logo | 工作流/Agent + 插件，本身不是 ChatBI 产品 | 需自建或装数据库插件 | 可自托管并接云端模型 | 非常活跃 | 可搭演示，不宜在已有 RAGFlow 上再叠一个平台 |

### 2.1 WrenAI（Canner）

- 仓库：[https://github.com/Canner/WrenAI](https://github.com/Canner/WrenAI)（Star 18,375；`pushed_at` 2026-11-23 11:00 Asia/Shanghai）
- 官网开源边界：[https://www.getwren.ai/en/open-core](https://www.getwren.ai/en/open-core)
- 新版发行：[https://github.com/Canner/WrenAI/releases/tag/wren-v0.18.0](https://github.com/Canner/WrenAI/releases/tag/wren-v0.18.0)（2026-11-10）
- 新版 LICENSE：[https://github.com/Canner/WrenAI/blob/wren-v0.18.0/LICENSE](https://github.com/Canner/WrenAI/blob/wren-v0.18.0/LICENSE)
- Classic 安装与旧接口：[https://docs.getwren.ai/oss/installation](https://docs.getwren.ai/oss/installation)
- Classic `0.29.2` LICENSE：[https://github.com/Canner/WrenAI/blob/0.29.2/LICENSE](https://github.com/Canner/WrenAI/blob/0.29.2/LICENSE)

必须把两条产品线分开：

- **新版（推荐关注的那条）**：2026-05-07 起，仓库改为以 `core/` 语义引擎、CLI、SDK、MCP 为主。官方写明开源范围是 MDL 语义层、受控 Text-to-SQL、MCP、CLI 和 20+ 连接器，**免费、可自托管**；**GenBI 界面、看板、嵌入 API、行列级权限、用户组、审计**等在 Wren AI Cloud 或 Enterprise Plus。见 [Open Core 说明](https://www.getwren.ai/en/open-core) 与 [根 LICENSE 路径表](https://github.com/Canner/WrenAI/blob/main/LICENSE)。PyPI 包 `wrenai 0.18.0` 标注 Apache-2.0，定位为「semantic SQL layer」。来源：[https://pypi.org/project/wrenai/](https://pypi.org/project/wrenai/)。
- **Classic（不要再用）**：旧聊天式 GenBI 应用保存在 `legacy/v1`（tag `v1-final`）。官方明确：**不再有新功能或安全修复**。`0.29.2` 标签根许可证为 **AGPLv3**，且原方案用过的 `POST /api/v1/generate_sql`、`/api/v1/run_sql` 属于这条旧线。来源：[仓库 README / Open Core FAQ](https://www.getwren.ai/en/open-core)、[0.29.2 LICENSE](https://github.com/Canner/WrenAI/blob/0.29.2/LICENSE)、Codex 评审报告。

文件接入：

- 商业云文档提供 **CSV 上传**（单文件小于 100MB，需表头，上传后预览模型再进入 modeling）。来源：[Connect CSV Upload](https://docs.getwren.ai/cp/guide/connect/csv)。这是 `/cp/` 文档，对应云/商业产品线。
- OSS 历史文档可用 DuckDB 读取 CSV / JSON / Parquet。来源：[DuckDB (files)](https://docs.getwren.ai/oss/guide/connect/duckdb)。注意该页仍出现「Wren AI interface」，更像 Classic 界面文档；新版官方定位是 CLI/MCP、无 UI。新版 CLI 是否提供与云端同等的上传向导，**未核实**。
- 另有 Excel 加载项文档，侧重点是把 Wren 查询结果接到 Excel，不是「上传财务工作簿直接问」。来源：[Connect from Excel](https://docs.getwren.ai/oss/guide/integrations/excel-add-in)。

对本项目：一期只有约 6 个签字指标，为 Classic 再背一套 AGPL 应用和权限映射不划算。二期指标涨到几十个、数据源变多时，用**新版 Apache 语义层（MDL）挂在自建 BFF 后面**是合理升级，不必把商业 UI 买进来。

### 2.2 Vanna

- 仓库：[https://github.com/vanna-ai/vanna](https://github.com/vanna-ai/vanna)（Star 23,787，**archived: true**；最后推送 2026-02-02 22:14 Asia/Shanghai）
- LICENSE：[https://github.com/vanna-ai/vanna/blob/main/LICENSE](https://github.com/vanna-ai/vanna/blob/main/LICENSE)（MIT）
- 文档：[https://vanna.ai/docs/](https://vanna.ai/docs/)

机制是典型的「示例训练 / RAG」：把 DDL、业务说明、成功的「问题–SQL」对写入向量记忆，生成时检索相似例子再让模型写 SQL。2.0 还加了用户权限、审计、FastAPI 与 Web 组件。许可宽松，思路也清晰，但仓库已于 **2026-03-29 由所有者归档为只读**，公开修复停止。公司云/企业版是否仍在销售，需看官网，不能当作可维护的开源底座。来源：[GitHub 仓库状态](https://github.com/vanna-ai/vanna)。

### 2.3 DB-GPT

- 仓库：[https://github.com/eosphoros-ai/DB-GPT](https://github.com/eosphoros-ai/DB-GPT)（Star 20,326；最近推送 2026-11-23 23:39 Asia/Shanghai）
- 文档：[http://docs.dbgpt.cn](http://docs.dbgpt.cn)
- LICENSE：MIT，见仓库说明
- 最新发行：v0.8.3（2026-11-17）。来源：[RepositoryStats 对发行日期的摘录](https://repositorystats.com/eosphoros-ai/db-gpt)，发行页请以 [GitHub Releases](https://github.com/eosphoros-ai/DB-GPT/releases) 为准
- Text2SQL 微调子项目：[https://github.com/eosphoros-ai/DB-GPT-Hub](https://github.com/eosphoros-ai/DB-GPT-Hub)（MIT，Star 约 2,017，以打开页面为准）

官方定位是「agentic AI data assistant」：连接数据库、CSV/Excel、数仓和知识库，用自然语言写 SQL 和 Python，再出图表与报告。它比 Wren / Vanna 更像一个分析平台，而不是薄薄一层语义 SQL。对本项目，MIT 许可友好，Excel 接入也符合「先上传再问」的直觉，但体积大、组件多，和已选定的 RAGFlow 功能重叠；在 10 万预算和一台中等配置服务器上再部署一套，运维风险高。可作为对照，不作为一期必选组件。

### 2.4 SQLBot（DataEase / 飞致云）

- 仓库：[https://github.com/dataease/SQLBot](https://github.com/dataease/SQLBot)（Star 7,195；最近推送 2026-11-23 20:39 Asia/Shanghai）
- 官网：[https://sqlbot.org/](https://sqlbot.org/)
- 文档中心：[https://docs.fit2cloud.com/sqlbot/](https://docs.fit2cloud.com/sqlbot/)
- LICENSE：[https://github.com/dataease/SQLBot/blob/main/LICENSE](https://github.com/dataease/SQLBot/blob/main/LICENSE)
- 最新发行：v1.10.3（2026-10-29）。来源：[Releases](https://github.com/dataease/SQLBot/releases)

这是目前中文开源圈里最接近「开箱即用 ChatBI」的产品。官方机制不是为每个问题手写 SQL，而是五层配置：

1. 表和字段描述  
2. 表关联关系  
3. 术语库（例如「大客户 = customer_level='A'」）  
4. SQL 示例库（给可举一反三的标准问法，而不是写死某一个月份）  
5. 自定义提示词（专业版，写全局口径，如销售额必须用 `actual_amount`）

来源：[Text2SQL 最佳实践：五个场景掌握 SQLBot 配置方法](https://blog.fit2cloud.com/?p=019d4881-6e9d-75e9-be9f-7d984cb1524a)（2026-04-01）。

许可与架构边界：

- 许可证本质上是 **GPLv3 + 附加条款**：使用前端时不得删除或修改 Logo 与版权信息；二次开发的衍生作品须履行 GPL 开源义务；商用授权联系 `support@fit2cloud.com`。来源：[LICENSE](https://github.com/dataease/SQLBot/blob/main/LICENSE)、[英文 README](https://github.com/dataease/SQLBot/blob/main/docs/README.en.md)。
- 第三方测评指出，细粒度数据权限等能力主要在名为 `sqlbot_xpack` 的闭源组件里。该文同时写到专业版报价约人民币 3 万元（含 X-Pack 与一年基础支持）。来源：[TechMoon 实测评测，更新于 2026-08-25](https://techmoon.xyz/sqlbot/)。**3 万元不是飞致云官方价目页数字，官方公开文档未列出标准价，实施前应向 400-052-0755 / support@fit2cloud.com 询价。**
- 产品文档将「自定义提示词、登录认证、平台对接、外观设置、参数配置、操作日志」列为**企业版/专业版功能**。来源：[文档中心目录](https://docs.fit2cloud.com/sqlbot/)。
- 发行说明多次提到 Excel 上传相关安全修复，例如 v1.10.2「修复 Excel 上传接口的路径遍历漏洞」，官方 Docker 示例也挂载 `./data/sqlbot/excel`。来源：[v1.10.2 Release](https://github.com/dataease/SQLBot/releases)、[仓库 README 安装命令](https://github.com/dataease/SQLBot)。结论：产品支持 Excel 作为数据进入方式；字段仍需在数据源里维护注释、类型和术语，不是「任意乱表自动变成可信财务口径」。

对本项目：如果只是内部试用「通用问数」，SQLBot 很合适。若要去掉 Logo、改成我们的统一界面并作为商品交付给客户，社区许可通常不够，需要商业授权，且 GPL 衍生作品义务要由律师复核。一期不建议把它作为统一 UI 的内核。

### 2.5 DataEase 问数

- 仓库：[https://github.com/dataease/dataease](https://github.com/dataease/dataease)（Star 24,730；最近推送 2026-11-24 00:27 Asia/Shanghai）
- 官网：[https://dataease.cn/](https://dataease.cn/)
- V3 LICENSE：[https://raw.githubusercontent.com/dataease/dataease/dev-v3/LICENSE](https://raw.githubusercontent.com/dataease/dataease/dev-v3/LICENSE)（修改版 GPLv3，Logo/版权限制与贡献条款同系列产品）

DataEase 是开源 BI（做仪表板、可视化），不是单独的 Text-to-SQL 引擎。官方教程说明：将 DataEase 升到 v2.10.13 以上、SQLBot 升到 v1.1.2 以上后，可把 SQLBot 嵌入 DataEase，在 BI 里使用智能问数。来源：[在 DataEase 中嵌入 SQLBot](https://dataease.cn/blog/post-837dab0a-d140-48ea-a57c-38ac0b6095b9.html)（2025-10-15）。

对本项目：客户要的是「知识库 + 问数」的统一界面，而不是再交付一套 DataEase 控制台。可以把 DataEase + SQLBot 当作竞品或备选「纯 BI 方案」，不作为一期主架构。

### 2.6 SuperSonic（腾讯音乐）

- 仓库：[https://github.com/tencentmusic/supersonic](https://github.com/tencentmusic/supersonic)（Star 5,189；最近推送 2026-11-06 22:19 Asia/Shanghai）
- LICENSE 原文：[https://raw.githubusercontent.com/tencentmusic/supersonic/master/LICENSE](https://raw.githubusercontent.com/tencentmusic/supersonic/master/LICENSE)

官方定位：下一代 AI+BI，把 **LLM Chat BI** 和 **语义层 Headless BI** 合在一起。许可是 Apache-2.0 加附加条件：

- 不修改源码和 Logo，可作为前后端服务商用；
- **若基于它开发并分发衍生作品，须向作者取得商业许可**，联系 `supersonicbi@qq.com`。

对本项目：语义层思路与「指标先于 SQL」一致，适合作为架构参考。若二次开发后作为我们的产品交付给客户，很可能会碰到「分发衍生作品」这条，需先询证。Java 单体也偏重，和现有 Python/RAGFlow 栈叠加不划算。

### 2.7 Chat2DB

- 仓库：[https://github.com/OtterMind/Chat2DB](https://github.com/OtterMind/Chat2DB)（Star 28,369；最近推送 2026-11-11 14:35 Asia/Shanghai）
- LICENSE：[https://raw.githubusercontent.com/OtterMind/Chat2DB/main/LICENSE](https://raw.githubusercontent.com/OtterMind/Chat2DB/main/LICENSE)
- 官网：[https://chat2db.ai](https://chat2db.ai)

这是本地优先的数据库客户端（桌面/Web/Docker/CLI），可用自有模型生成、解释、优化 SQL。Star 很高，但 **5.3.0 及之后** 明确：允许个人、非营利以及**本组织内部使用**；向独立外部方提供外部产品/服务、托管交付、嵌入式产品、白标或对象形式分发，都需要 Chat2DB 书面商用授权。来源：[LICENSE 第 1–3 节](https://raw.githubusercontent.com/OtterMind/Chat2DB/main/LICENSE)。

对本项目：实施人员拿它连库、写和检查 SQL 没问题；**不能把它嵌进交付给治具厂的问数系统**，除非拿到书面授权。它也不是给老板用的 ChatBI。

### 2.8 RAGFlow 的 Text-to-SQL 能力

- 仓库：[https://github.com/infiniflow/ragflow](https://github.com/infiniflow/ragflow)（Star 94,595；最近推送 2026-11-23 21:10 Asia/Shanghai）
- 主许可证：Apache-2.0，见 [v1.0.0 LICENSE](https://github.com/infiniflow/ragflow/blob/v1.0.0/LICENSE)
- 官方教程（中文站）：[构建 SQL 助手工作流](https://ragflow.com.cn/blog/tutorial-building-a-sql-assistant-workflow)、[在 RAGFlow 中实现 Text2SQL](https://ragflow.com.cn/blog/implementing-text2sql-with-ragflow)、[执行 SQL 工具](https://ragflow.com.cn/docs/execute_sql)

RAGFlow 不是 ChatBI 产品，但提供 Text-to-SQL 智能体模板和 Execute SQL 组件。公开资料的用法是：准备三类知识库（库表 Schema/DDL、库表业务说明、Question-to-SQL 示例），生成 SQL 后再用组件连接 MySQL / PostgreSQL / MariaDB / SQL Server 执行。2026-11-24 访问部分中文文档时出现 429，上文机制摘自公开检索摘要，细节以实现时打开的官方页面为准。

对本项目：知识库已经计划用 RAGFlow，长尾问数应优先复用这条能力，避免再引入一套问数平台。但它**没有独立的指标版本、财务签字发布和口径治理**，关键经营数字仍应走参数化模板或未来的语义层。

### 2.9 Dify 的 Text-to-SQL 能力

- 仓库：[https://github.com/langgenius/dify](https://github.com/langgenius/dify)（Star 161,753；最近推送 2026-11-24 01:11 Asia/Shanghai）
- LICENSE：[https://raw.githubusercontent.com/langgenius/dify/main/LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)（修改版 Apache-2.0：可作企业应用或后端；用源码运营多租户需商用许可；使用官方前端时不可去除 Logo）

Dify 本身是 Agent / 工作流平台。阿里云有官方实践：在已部署的 Dify 上安装 AliyunDMS 与通义千问插件，套用 ChatBI 模板，把自然语言转为 SQL 并可视化。来源：[基于 Dify+NL2SQL 搭建 ChatBI](https://help.aliyun.com/zh/aidbs/user-guide/hands-on-dify-nl2sql-chatbi)。社区也有数据库插件可读 schema、执行只读 SQL。

对本项目：用 Dify 做内部原型可以，但会多一套平台，和「统一自建 UI + RAGFlow」重复。AliyunDMS 企业版算法还可能把数据库纳入阿里云 DMS 托管，和「库必须在客户本地」需要单独评估，不能默认采用。

### 2.10 其他相关项目（补充）

| 项目 | 说明 | 来源 |
| --- | --- | --- |
| [alibaba/XiYan-SQL](https://github.com/alibaba/XiYan-SQL) | 阿里 NL2SQL 算法/模型向仓库，Apache-2.0，Star 70，最近推送 2026-08-31。是模型与论文资产，不是可交付的 ChatBI 产品。DMS 插件默认模型名含 `XiYan-SQL-32B`。 | [GitHub API](https://api.github.com/repos/alibaba/XiYan-SQL)、[Dify+NL2SQL 文档](https://help.aliyun.com/zh/aidbs/user-guide/hands-on-dify-nl2sql-chatbi) |
| [Dataherald/dataherald](https://github.com/Dataherald/dataherald) | NL2SQL 引擎，Apache-2.0，Star 3,652，但最近推送停留在 2024-07-24，维护偏静默，不建议新项目依赖。 | [GitHub API](https://api.github.com/repos/Dataherald/dataherald) |
| [Cube](https://cube.dev/use-cases/semantic-layer) / [dbt MetricFlow](https://docs.getdbt.com/docs/build/about-metricflow) | 国际主流指标语义层，走 Text-to-Metrics 而非裸 SQL。对理解「口径层」有用，但对 5–6 人小厂、中文财务口径和 10 万预算过重。 | 产品页如上 |

---

## 3. 商用方案（能查到的定价或部署形态）

商用 ChatBI 几乎都要求先建数据集 / 主题 / 指标，再开放自然语言提问。私有化加上实施，通常会明显超出本项目 10 万软件预算，且很难塞进「自建统一 UI」。以下仅列出已核实的公开信息。

### 3.1 帆软 FineChatBI

- 产品介绍：[FineChatBI 简介](https://help.fanruan.com/finebi6.X/doc-view-2572.html)（文档最近更新显示为 2025-11-27）
- 更新日志：[FineChatBI 更新日志](https://help.fanruan.com/finebi/doc-view-2594.html)，公开最晚版本 **V4.4**，发布日期 **2026-03-10**，适配 FineBI V7.0.7
- 部署说明需 FineBI 底座，另配 AI 服务器，见 [手动部署文档](https://help.fanruan.com/finebi/doc-view-2584.html)
- 公开报价：无标准价，文档写正式部署请拨打 400-811-8890

机制要点：基于 FineBI **分析主题 / 主题模型** 限定数据范围，再做意图解析、字段联想、多轮问答和归因。也就是先由数据产品经理准备主题和口径，业务人员再提问。来源：[简介](https://help.fanruan.com/finebi6.X/doc-view-2572.html)。

需注意：2026 年 10 月前后有媒体称帆软调整 ChatBI 方向、后续能力由 Data Agent 承接。例如 [腾讯新闻转载报道](https://news.qq.com/rain/a/20261012A034DP00)。这与官方帮助文档仍在线、日志停在 2026-03 并不自动等同「产品已下架」。**选型前必须向帆软确认当前可售状态**，在此之前不作为可依赖方案。

### 3.2 观远 ChatBI / 问数 Agent

- 私有化说明：[国企或敏感数据场景能否私有化部署问数 Agent](https://www.guandata.com/faq/chatbi-private-deployment)
- 官方确认：可对接企业自建或内网大模型，将问数 Agent、数据处理与模型推理放在本地或私有云。
- 官方也写明：**准确率依赖数据与知识配置，不能承诺 100%**；建议按场景拆分主题，首个主题先基于单表验证，单个主题建议字段总数不超过 300、表不超过 5 张；复杂指标要维护业务知识和样例集。来源：同上 FAQ。
- 公开标准价：**未核实**。咨询电话 400-880-0750。网上出现的「约 1.5 万元/年」来自非官方评测站，本文不采用。

### 3.3 阿里云 Quick BI 智能小 Q

- 计费说明：[Quick BI 各版本和增值模块的计费方式](https://help.aliyun.com/zh/quick-bi/product-overview/billing-methods-of-quick-bi)
- 产品概述：[Quick BI 智能小 Q 概述](https://help.aliyun.com/zh/quick-bi/user-guide/smartq)

已核实的中国站公开价格（云产品，按用户数和时长购买）：

- 高级版年费 1,000 元起，专业版年费 120,000 元起，AIPro 版年费 40,000 元起（AIPro 最低 10 用户起购）
- 小 Q 搭建 Agent：200 元/个
- 小 Q 问数 Agent：200 元/个（高级版/专业版可增购；购买小 Q 问数后可使用问数与解读能力）
- 小 Q 报告 Agent：600 元/个
- 小 Q 洞察 Agent：15 万元/年
- Token 增值包：1,000 元/个，含 1 亿 Token

这是**公共云 SaaS** 价。虽然可连接自建数据库，查询链路通常仍经过阿里云侧，**与「数据库必须只留在客户本地，只有 LLM 出网」不完全同构**。专有云 / 独立部署价格 **未核实**，需向 4008013260 询问。即便只算云上许可，专业版起步已超过一期预算；AIPro 4 万元起看似能进预算，但部署形态和数据出境要先确认，不能当作已满足约束。

### 3.4 网易有数 ChatBI

- 订阅价（有数 BI，本地部署宣传页）：创作者 4,999 元/用户/年（2 人起售），阅览者 499 元/用户/年（10 人起售）。来源：[有数 BI 订阅页](https://youdata.163.com/index/subscription)
- 私有化环境可绑定第三方大模型。来源：[有数知识库：ChatBI 绑定第三方大模型](https://study.sf.163.com/documents/read/privatization/chbi13)
- **ChatBI 模块本身没有单独公开标价**，需向 0571-89926329 / bigdata@service.netease.com 询价。即便只买 BI 订阅，最低创作者 + 阅览者组合也只是软件订阅，不含实施和指标治理。

### 3.5 数势科技 SwiftAgent

- 产品页：[智能问数 SwiftAgent / Data Agent](https://digitforce.com/product/data-agent)
- 公开机制：强调 NL2Semantics，先走指标语义层（SwiftMetrics）和权限体系，再取数、归因、出报告；支持文本、语音、Excel 等输入。
- 标准价目：**未公开**。可参考的中标结果：安徽省农村信用社联合社「智能问数 ChatBI」项目，中标人北京数势云创科技有限公司，中标价 **人民币 1,300,000 元**，内容为软件 1 套 + 定制开发与实施 + 1 年维保。公告发布时间 2026-01-28。来源：[中标结果公告](https://www.ahrcu.com/eportal/ui?articleKey=156b24c0585543b88ae4bf320d9d533a&columnId=8ce881735bf348dfa5768746aa2bb302&pageId=822bbeebb5e642ae9b1f20c4d53791c6)。该案例是金融机构整包，不能直接当小厂报价，但说明成熟商用 ChatBI 实施往往在百万量级。

---

## 4. 行业共识：口径怎么做准，表格不固定怎么办

### 4.1 三类主流技术，前期工作其实差不多

| 路线 | 代表 | 实施方要做的 | 客户要做的 | 问法是否固定 |
| --- | --- | --- | --- | --- |
| 语义层 / 指标平台（推荐的生产级路线） | WrenAI MDL、SuperSonic、Cube/MetricFlow、衡石 HQL、数势 SwiftMetrics、观远/帆软的主题与指标 | 建模型：表关系、指标公式、时间口径、权限、版本 | 确认「营收、良率」等业务定义，指定数据负责人 | **不固定**。系统把问题解析成指标 + 维度 + 筛选 |
| RAG / 示例问答对 | Vanna、SQLBot、RAGFlow Text2SQL、Dataherald | 灌入表结构说明、术语、高质量「问题–SQL」示例，并持续从真实提问迭代 | 提供正确示例、纠正错答案 | **不固定**，但示例要有代表性，不能只写死某一个月份 |
| 直接生成 SQL | 裸模型或仅给 DDL 的 Agent | 表面工作最少 | 看起来最省事 | 问法自由，但口径最容易漂，不适合财务签字数字 |

SQLBot 官方实践把配置顺序写得很清楚：先做表和字段描述，再做表关系，再做术语，再放可泛化的 SQL 示例，最后用全局提示词约束口径。来源：[SQLBot 最佳实践](https://blog.fit2cloud.com/?p=019d4881-6e9d-75e9-be9f-7d984cb1524a)。观远则建议先单表、限制字段和表的数量，再扩主题。来源：[观远 FAQ](https://www.guandata.com/faq/chatbi-private-deployment)。衡石建议从经营会议和核心看板里的高频指标起步，合并同义名称，找出口径冲突，再发布指标并准备真实评测题。来源：[Text2Metrics 文章](https://www.hengshi.com/blog/text2sql-to-text2metrics-semantic-layer.html)。

**共同结论：省不下的是「和财务对口径」这件事，能省下的是「为每一种问法各写一条 SQL」。**

### 4.2 「表格格式不固定」能自动处理到什么程度

可以自动或半自动的部分：

- **识别表头、类型、样例行**，给出「这一列可能是客户名称 / 开票日期 / 金额」的建议；
- **字段映射表**：把客户的 `客户名`、`cust_name`、`客户` 都对到标准字段 `customer_name`，下次同一模板自动套用；
- **同义词和术语库**：问「大客户」「重点客户」时指向同一规则；
- **覆盖导入**：同一张已映射的表再上传一版新数据（列结构不变时）。

不能指望完全放开的部分：

- 每次导出少一列、改名、合并单元格、把两个月拼在一张乱七八糟的表里，还自动进账并保证数字正确；
- 客户根本没有成本或收款字段，系统却能算出毛利和回款；
- 不经人确认就把模型猜的列映射写进签字指标。

因此推荐的工程做法是：**标准库表固定 + 导入映射可配置 + 大模型只负责建议，人确认后才落库。** 对客户的要求可以放宽为：「继续用现在的导出，但同一种报表请尽量保持列结构；大改时告诉我们，我们改一版映射。」这比要求他们改用一张全新的标准财务表更容易接受。

---

## 5. 对本项目的推荐路线

### 5.1 建议采用的架构（一期 + 二期）

**一期（守住 10 万、先让关键数字可信）**

1. 知识库继续用 RAGFlow，问数界面继续自建，不把第三方 ChatBI 控制台交给客户。
2. 问数做成**最小语义层**：与财务共同选定约 6 个签字指标（例如月营收、销量、投入数、合格数、不良数、加权良率；若要毛利/回款，必须另有成本表和收款表），每个指标一条**参数化 SQL 模板**（时间、客户、产品线、分组方式为参数）。
3. 自然语言只负责两件事：选出模板、填好参数。问法不固定。
4. 导入做 **字段映射 + 大模型辅助识别列名 + 人工确认**。客户不必改原表，但同一来源的列结构应相对稳定。
5. 模板覆盖不住的问题，走 RAGFlow Text2SQL 或受校验的模型生成 SQL，本地只读执行，答案标注「非签字口径」。
6. **不要**在一期引入 Wren GenBI Classic（AGPL 且停更），**不要**把 SQLBot / DataEase / Chat2DB 白标进交付界面，**不要**采购观远/帆软/数势等整包商用 ChatBI。

**二期（指标变多再升级，不必推倒重来）**

- 当签字指标从 6 个涨到二三十个、或要接更多数据源时，把一期的指标定义迁移到 **WrenAI 新版 MDL（Apache-2.0 CLI/SDK/MCP）**，仍由自建 BFF 对外。
- 若客户更接受独立问数控制台、也愿意买商业授权，再评估 SQLBot 专业版或商用 ChatBI，作为可选分支而不是现在的前置条件。

### 5.2 实施方必须做的工作清单（仅问数数据路径，估算）

人天按一人全职估算，单价若沿用现有方案的 2,000 元/人天，仅供内部排期，不是向客户的正式报价。

| 序号 | 工作 | 估算人天 | 说明 |
| ---: | --- | ---: | --- |
| 1 | 与老板/财务确认 6 个指标及口径 | 2 | 含不含税、退货、日期口径、良率是加权还是批均；这是最不能省的会 |
| 2 | 设计本地标准表（销售、质检，预留成本/收款） | 1.5 | 库结构固定，映射层可变 |
| 3 | Excel/CSV 导入、字段映射、LLM 列名建议、人工确认、校验报告 | 4 | 直接回应「表不能写死」；格式大变时重做映射而非推倒数据库 |
| 4 | 编写并测试 6 条参数化 SQL 模板 | 4 | 含几种常见问法、对比期、空结果、权限过滤 |
| 5 | 自然语言选模板和填参，保留手动切换 | 2.5 | 可用关键词规则自动走问数/问资料，再留手动按钮 |
| 6 | 本地 SQL 护栏（只读账号、表白名单、禁止多语句、LIMIT） | 1.5 | 云端模型只看到问题、字段说明和模板参数，看不到明细行 |
| 7 | 长尾生成 SQL +「非签字口径」标注 | 2 | 优先复用 RAGFlow Text2SQL 模板 |
| 8 | 验收题集与回归（标准问法、越权、列名变更、口径冲突） | 1.5 | 建议至少覆盖 12 个标准问法 |
|  | **合计** | **约 19 人天** | 若计入会议等待和返工，按 **16–24 人天** 准备 |

其中第 4、6、7 项与 Codex 评审里「模板 + 长尾约 7 人天」大体同范围；本调研把「对口径」和「导入映射」单独算进问数路径，所以总数更高。这些工作应纳入原「窄范围 10 周 / 约 46 人天」计划中统筹，而不是在 10 万之外再加一轮。

### 5.3 和「只手写 6 个参数化 SQL 模板」比，多做了什么、没多做的是什么

| 对比项 | 只手写 6 条模板 | 本调研推荐（模板 = 最小语义层 + 映射） | 上一套完整 ChatBI 平台 |
| --- | --- | --- | --- |
| 问法 | 已经可以很灵活（参数化） | 同样灵活 | 同样灵活，但要先配置主题/指标 |
| 客户表格 | 若不做映射，容易变成「必须按我们的列名导」 | 保留原导出，映射 + LLM 建议 + 人确认 | 各产品都有导入或连库，仍然要建模 |
| 口径可信度 | 高（签字指标） | 同样高，并明确长尾降级标注 | 取决于实施质量，不是买了就准 |
| 格式偶尔变化 | 容易导失败 | 改映射即可，不必改库和模板 | 同样要维护数据源 |
| 新增第 7 个指标 | 再写 1 条模板（估算 0.5–1 人天） | 同样 | 在语义层加一个指标，前期平台成本已付出 |
| 许可与运维 | 最低 | 低，不新增平台 | SQLBot/Wren Classic/商用 BI 都有许可或费用坑 |
| 估算增量 | 基准 | 多约 **6–10 人天**（主要在映射和对口径） | 往往再加数周集成，或数万到百万元级采购 |

所以：**推荐路线不是推翻「6 条模板」，而是承认这 6 条就是一期语义层，再补上「导入映射」以免把麻烦转嫁给客户。** 它已经是市面通用方案在小预算下的缩小版，而不是一条土办法。

### 5.4 不推荐在一期做的事

- 用 Wren Classic 0.29.x 充当问数引擎（AGPL + 官方停更）。
- 把 SQLBot / DataEase 去 Logo 后当成自有产品交付（许可证不允许，且专业能力可能在闭源包里）。
- 把 Chat2DB 嵌进客户系统（5.3.0 后对外交付需书面授权）。
- 依赖已归档的 Vanna 开源核心做生产。
- 为了「看起来更智能」再部署 DB-GPT 或 Dify，和 RAGFlow 叠床架屋。
- 承诺「任意格式的财务表丢进去就能准确回答所有问题」。

---

## 6. 给客户沟通时可用的三句话

1. 我们不会把您能问的每一句话都写成一条 SQL，只把您每周最关心的几个数字的**算法**固定下来，问法可以很口语。
2. 您不必按我们的模板重做财务报表，沿用现在的导出即可；我们会做字段对应。同一种报表请尽量保持列的顺序和名称稳定，大改时一起调一次对应关系。
3. 这几个签字指标的答案会和财务口径一致；其他探索性问题也能问，但会标明仅供参考。等以后指标多了，再升级到更完整的语义层，不用推倒重来。

---

## 7. 主要来源（按首次引用归并）

- WrenAI 仓库、Open Core、LICENSE、0.18.0 / 0.29.2、CSV 与 DuckDB 文档、PyPI：见第 2.1 节链接
- Vanna、DB-GPT、SQLBot、DataEase、SuperSonic、Chat2DB、RAGFlow、Dify、XiYan-SQL、Dataherald：见第 2 节各仓库与 LICENSE
- SQLBot 实践与文档：[最佳实践](https://blog.fit2cloud.com/?p=019d4881-6e9d-75e9-be9f-7d984cb1524a)、[文档中心](https://docs.fit2cloud.com/sqlbot/)、[TechMoon 测评](https://techmoon.xyz/sqlbot/)
- DataEase 嵌入 SQLBot：[官方教程](https://dataease.cn/blog/post-837dab0a-d140-48ea-a57c-38ac0b6095b9.html)
- 衡石 Text2Metrics：[文章](https://www.hengshi.com/blog/text2sql-to-text2metrics-semantic-layer.html)
- 观远私有化 FAQ：[页面](https://www.guandata.com/faq/chatbi-private-deployment)
- 帆软 FineChatBI：[简介](https://help.fanruan.com/finebi6.X/doc-view-2572.html)、[更新日志](https://help.fanruan.com/finebi/doc-view-2594.html)
- Quick BI：[计费](https://help.aliyun.com/zh/quick-bi/product-overview/billing-methods-of-quick-bi)、[智能小 Q](https://help.aliyun.com/zh/quick-bi/user-guide/smartq)
- 网易有数：[订阅页](https://youdata.163.com/index/subscription)
- 数势：[产品页](https://digitforce.com/product/data-agent)、[安徽农信中标公告](https://www.ahrcu.com/eportal/ui?articleKey=156b24c0585543b88ae4bf320d9d533a&columnId=8ce881735bf348dfa5768746aa2bb302&pageId=822bbeebb5e642ae9b1f20c4d53791c6)
- 阿里云 Dify ChatBI 实践：[手册](https://help.aliyun.com/zh/aidbs/user-guide/hands-on-dify-nl2sql-chatbi)
- 本项目前序结论：[企业知识库-Codex独立评审报告.md](./企业知识库-Codex独立评审报告.md)、[企业知识库-10万初版与升级方案.md](./企业知识库-10万初版与升级方案.md)
