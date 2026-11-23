# 05 明细清洗

## 目标

把明细还原成带行号的记录，同时保留足以重算导入前后差异的证据。

## 前置条件

04 已确认，先与 06 冻结标量转换和输出协议。

## 输入

原工作簿；映射版本；03 中 sales、expense、purchase、production、overhead、oee、capacity、fg、material、wip、ar、ap、labor、complaint、backlog 的配置。

## 详细任务清单

1. 实现 `parse_number(cell, spec)`、`parse_date(cell, epoch, year)`。数值从字符串用 Decimal 转换，统一半角、去千分位/币符/空格、处理括号及全角负号。按列区分横杠表示零、N/A/待定表示缺测；金额缺失不能充零。百分数文本除 100，Excel 百分比格式保留底层小数，普通数按确认的列规则转换，达成率可超过 1。日期覆盖原生日期、Excel 序号、年月日、点/斜杠、8 位日期及无年份月日；无年份依报表期，不能限定所有发票、签约、交期在 Q3。
2. 逐行先记原始坐标、空值掩码和标签，识别表头、注、空、周/月/季汇总，再在**已声明维度列的真实合并范围**从左上角补齐，跨小计、跨订单不得继承；明细识别用源序号/工单/发票号或产品编码等组合键。汇总标签可能含月份前缀。允许未合并的业务空值，必填失败单独隔离。退货、冲销保留负号。采购/在手金额乘 10000，单价维持元；存货取月末，月末余额跨月不可相加。
3. 按 `var/stage/<run>/<logical>.jsonl` 写受限本地暂存。字段含 batch_candidate、sheet、source_row、原字段到坐标的映射版本、源单位、标准字段。标准合同至少包括 sales(order_date,order_no,customer_id,region,channel,salesperson,product_id,category,qty,list_price,gross_discount,tax_rate,revenue_yuan,cost_yuan)、production(work_date,factory,product_id,actual_qty,good_qty,defect_qty,reason)。金额比例输出十进制字符串，日期 ISO。其余表按 04 的 ID。重复单号多行按源行保留。
4. 生成诊断事实（按表/列/期间/行类型/授权域）：行数、单位转换前后和、合并前维度缺失的明细金额、文本归一前后类别数、主键重复数、原始数量×含税价与记账净额。汇总行的解析数仅入诊断。原始与清洗值必须按同一数值解析器比较。保存 `physical=detail+summary+header+blank+note+quarantine` 行账及行号索引；按实际日历过滤与报表快照的范围分别记录。

## 交付物

`src/chatbi/ingest/{scalars,details,quality}.py`、`var/stage/<run>/`（含 `detail_report.json` 和 `diagnostics.jsonl`）、`tests/test_s05.py`。

## 验收条件

1. `pytest -q tests/test_s05.py -k scalars` 全绿；用合成格验证“（￥1,234.50）”为 -1234.50、万元和元列共存、未知单价为空、125% 为 1.25、多种日期还原同一天。
2. `pytest -q tests/test_s05.py -k rows` 全绿；样本含跨订单同名、同单两行、小计、下组空维度、退货。断言各行互斥归类、符号、补齐边界与手算的诊断差额。
3. `chatbi clean details && chatbi clean audit --group details` 退出 0；上述 15 表逐表有行账，未解释行数、未处理的必填异常均为 0。随机种子 42 每表抽 3 条可按源行回读（不足取全）。

## 常见坑

openpyxl 不计算公式。缓存缺失又无已审核的替代公式时应记异常。未收发票和客诉台账可能含季前日期；快照问题和按发生日的问题要分别建模。

## 预计工作量与并行

2.5–3 小时。标量和行账格式先合并，明细按销售采购、运营库存两组分工。
