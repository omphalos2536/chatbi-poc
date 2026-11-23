# 11 签字模板

## 目标

使六个有源指标通过版本化 SQL、实际签认和本地绑定，得到可复算的结果。

## 前置条件

08 已发布演示批次，09 和 10 已通过。

## 输入

sales、production 视图与来源，角色权限，工作簿中的公式。POC 签字人为模拟财务/生产负责人，身份和日期据实记录。

## 详细任务清单

| ID / 中文指标 | 分子或金额 | 时间与合法拆分 |
|---|---|---|
| revenue / 营业收入 | SUM(revenue_yuan)，不含税、含冲红 | 订单日期；月、区域、产品大类、客户、渠道、销售员、产品 |
| cost / 营业成本 | SUM(cost_yuan)，含服务和冲红 | 同销售 |
| gross_profit / 毛利额 | SUM(revenue_yuan)−SUM(cost_yuan) | 同销售 |
| gross_margin / 毛利率 | 毛利额/NULLIF(SUM(revenue_yuan),0) | 同销售；按总额加权 |
| good_qty / 合格产量 | SUM(good_qty)，台套 | 报工日；月、工厂、产品 |
| first_pass / 一次合格率 | SUM(good_qty)/NULLIF(SUM(actual_qty),0) | 同生产；以本簿实际报工数量为分母 |

1. 每个指标一份 SQL 和签字元数据，列出固定来源、域、适用问法、可选维度与过滤、期间、税、单位、版本、SQL SHA、公式/维度变更记录。签字命令展示 SQL/公式/夹具复算，再由人确认。签后改 SQL、维度映射或公式，旧签名须失效。
2. where 用 psycopg 参数绑定 date_from（含）/date_to（不含）及可选枚举、编码；group/order、TopN、合计占比从服务端预定义片段构造。维度和排序组合进入版本哈希，禁止字符串插入用户词。支持总计、分组、TopN 及以同筛选全量作分母的占比；同步请求总计与按月拆分时返回两个块。先按请求身份验证来源和字段。单模板不跨域拼其他业务事实。
3. 本地封装执行 SQL、绑定值（依权限脱敏）、结果、单位、模板版本、口径、时间范围和来源。明确缺数据与零值；不给空数据计算达成率。适用模板的任何失败都回报相应原因。

## 交付物

`config/templates/{revenue,cost,gross_profit,gross_margin,good_qty,first_pass}.sql`、`config/templates/registry.yaml`、`var/approvals/metrics/`、`src/chatbi/query/templates.py`、`tests/test_s11.py`。

## 验收条件

1. `chatbi templates review && chatbi templates verify` 确认后显示 signed=6、hash_errors=0，每份能查署名和生效版本。
2. `pytest -q tests/test_s11.py -k signed` 自编每指标两种问法共 12 次全部命中对应模板、无外呼，受控夹具与手算一致；两行毛利率差异样本的总比率按总额算。
3. `pytest -q tests/test_s11.py -k parameters` 全绿；含冲红、边界日、零分母、含引号客户值、未允许维度和签后改动，非法请求 0 次执行。

## 常见坑

“合格产量”与“实际产量”不同。用发票已收金额推算季度回款流水缺少收款日期和已结清发票，必须提示来源缺口。

## 预计工作量与并行

1.5–2.5 小时。可与 12 并行。
