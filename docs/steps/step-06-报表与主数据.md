# 06 报表与主数据

## 目标

将报表转成长表，保留报表本身的合计项目；统一跨表业务键与来源。

## 前置条件

04 完成，05 标量与暂存行协议可用。

## 输入

03 清单中除 05 的 15 表之外的 13 表；工作簿字典、已确认映射。

## 详细任务清单

1. pnl、balance、cashflow 生成项目行。键包括 section、parent、line_code（行次缺失时用已确认的完整路径）、period_start/end 或 as_of、scenario(actual/prior/budget)、频度、source_cell。展示名相同的子项须带父项。分别保留月、本季、上季、去年同期、预算；现金流直接/间接法分段。资产负债期初取表头日，现金期初取现金流表说明；报表“净额/总计/小计”可为合法指标。
2. target、budget 按多层列头展开月度目标/实际，季度列及区域、部门汇总单列 level 供勾稽。trend 保留来源标签（快报/决算）、上年与本年。dashboard 只读取指标块，按单位列、目标/实际、指标方向、前期转换；驾驶舱比率和万元披露精度要记录。只将字典的审核后定义纳入语义层；封面登记为 metadata_only。
3. customer 用归一税号、核对名称和业务编码去重，输出 canonical_id 与所有源编码的 alias 映射。税号冲突、空税号且编码冲突送人工；不凭同名合并不同税号。product 编码统一大写，名称 NFKC、trim；supplier、org 按确认的编码唯一。未发现单独的“是否服务”列，用产品名称/系列及单位生成待审分类，人工区分维修、延保与实物配件，记录规则和行号。隐藏字段与税号不入服务字典。重映射销售、库存等事实外键，保留原键供导入者追溯。
4. 输出清洗前后客户记录数、映射冲突和未匹配编码报告。报表按物理行做账、按展开后的单元格另计条数；保存 `source_row/source_col`，如一行含多期不得丢列坐标。

## 交付物

`src/chatbi/ingest/{reports,masters}.py`、`config/profiles/report_items.yaml`、`var/stage/<run>/report_manifest.json`、`var/stage/<run>/aliases.jsonl`、`tests/test_s06.py`。

## 验收条件

1. `chatbi clean reports && chatbi clean audit --group reports` 退出 0；13 表均有角色、去向和行账，披露数值可追到单元格，封面产出分析事实数为 0。
2. `pytest -q tests/test_s06.py -k statements` 全绿；合成报表月和季度分开、同名子项各归其父、现金流两种方法独立、快报与决算标签可查、逆向达成率按目标/实际，零分母为空。
3. `pytest -q tests/test_s06.py -k masters` 全绿；重复税号映到同一客户、两笔销售都保留，规范化 SKU 唯一；实物配件未被归入服务，输出无电话和银行账号。

## 常见坑

季度余额、季度累计、预算和实绩的数值外形相似。把报表项目名当唯一键，会覆盖不同类别的“职工薪酬”。

## 预计工作量与并行

2–3 小时。主数据与报表适配可并行，最后共用行账核验。
