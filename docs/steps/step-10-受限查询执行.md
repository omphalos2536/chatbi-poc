# 10 受限查询执行

## 目标

让所有模型候选在 PostgreSQL 方言下经过语法树、授权对象和实际只读账号的三层检查。

## 前置条件

07 完成，测试夹具在独立 PG 库发布；初始白名单可用人工定义的小字典。

## 输入

候选 SQL、角色许可、已检索字典、绑定参数与执行预算。

## 详细任务清单

1. 用 sqlglot.parse(read="postgres") 校验恰一条完整查询，拒绝未识别节点。定义允许的 SELECT、非递归 CTE、子查询、分组/窗口、排序、聚合、受控算术和比较节点；遍历所有层。按需开放 sum/count/avg/min/max/round/coalesce/nullif/date_trunc 等已审函数，限定 CAST 类型。拒绝写入、锁、select into、管理语句、数据变更 CTE、系统目录、未批准函数、自定义操作符、表函数、递归、笛卡尔积及未知命名空间。仅 COUNT(*) 可用星号。
2. 通过 sqlglot 的作用域遍历和带 schema 的 qualify 解析别名、CTE 列、相关子查询、窗口、谓词和排序列。实体只可来自明确限定的 bi 视图；同时核对角色和此次字典的列集合及许可关系，歧义或无法解析直接拒绝。限制最多 4 张视图和 3 个连接；只允许已声明的连接键/基数。字面过滤值或占位符转为 psycopg 绑定参数，模型不可自定本地参数值；排行和日期粒度等结构参数用受控枚举。
3. 在 AST 外层设置行数上限 200，原有更小 LIMIT 保留；拒绝异常 OFFSET/LIMIT 表达式。只执行校验后重新生成的 SQL，投影列、参数和解析结果同审计关联。模板使用相同只读执行器并校验注册 SQL（11）。任何查询必须有获准来源，拒绝仅返回模型常量代替事实计算。
4. 按当前用户选独立 reader，事务设 REPEATABLE READ READ ONLY、search_path 固定 pg_catalog 与受控 bi、statement_timeout=3000ms、lock_timeout=500ms。失败回滚再归还连接；执行成功记录视图/列、耗时、行数、限制是否生效。表名、列名从已审 AST 得出，不能信候选声明。

## 交付物

`src/chatbi/query/{guard,execute}.py`、`config/semantic/functions.yaml`、`tests/test_s10.py`。

## 验收条件

1. `pytest -q tests/test_s10.py -k allowed` 全绿；用实际 PG 执行授权聚合、别名、CTE、窗口排名和绑定值；读取不超过 200 行，原 LIMIT 3 仍至多 3 行。
2. `pytest -q tests/test_s10.py -k denied` 全绿；为多语句、写操作、未授权列（含谓词/排序/CTE）、越域视图、危险函数、系统目录和未知节点各设负例，所有负例执行器调用计数为 0。
3. `pytest -q tests/test_s10.py -k transaction` 检查实际会话只读和超时参数，受控耗时测试超时并回滚，下一次合法查询成功。所有拒绝和超时有 trace_id，错误响应中无库凭据或原始 PG 错误。

## 常见坑

检查 SQL 首词无法覆盖嵌套查询。把模型原文交给数据库会丢失 LIMIT 重写及参数化的效果。

## 预计工作量与并行

2–3 小时。AST 与 PG 事务执行可并行。
