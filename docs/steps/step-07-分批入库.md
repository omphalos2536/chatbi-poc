# 07 分批入库

## 目标

建立可重导、可回退且能隔离权限的 PostgreSQL 数据结构。

## 前置条件

05、06 有完整批次暂存；01 私有 PG 正常。

## 输入

暂存 manifest、签认映射、角色域配置，测试使用独立测试库。

## 详细任务清单

1. 划分 ingest（导入及诊断）、core（物理事实）、meta（版本、检查、当前批次）、bi（授权视图）。迁移版本入表。最小结构如下，物理事实字段使用 04 的类型合同：
   - meta.batch(id, dataset_id, file_sha, mapping_sha, cleaner_version, state, created_at)，四个业务标识合成唯一键。meta.current(dataset_id PK,batch_id FK) 单一发布指针。
   - 同一批内事实键为 (batch_id,sheet,source_row,subkey)，subkey 表示展开的列/期间；维表另设 (batch_id,canonical_id) 唯一。来源表保存源文件名、SHA、列映射、表、行号、坐标。金额 numeric(22,4)，比例及单价至少 8 位小数，数量允许小数。
   - meta.check_result(batch_id,rule_version,check_id,scope,lhs,rhs,delta,tolerance,status,source_refs)，ingest 的行账和诊断与批次绑定；建 batch_id、期间、常用维度索引。
2. `chatbi db migrate` 和 `chatbi load` 实现分表 COPY、类型检查、键和计数校验。每批使用唯一键和事务级 advisory lock，构建失败回滚或标记 failed；同哈希+同版本重复加载返回同一批。新版本建新批，失败可重试。状态 staged→checked→published，旧发布版仍可追溯。此步 `load` 只到 staged。
3. 建 bi.v_<logical>（仅获准的业务字段、行来源 ID，连接 meta.current）、bi.v_diagnostics（可重算清洗影响）、bi.v_checks（按域的已发布勾稽证据）。跨域诊断/勾稽携带所涉全部域和最高密级。按角色拆分列投影或单独视图，固定视图 owner/search_path；库内授权与运行字典取交集。
4. 建 migration_owner、loader、publisher 和少数 reader（boss 全域、sales 销售普通、ops 生产普通），不共用超管。角色用独立 PG 登录，禁止角色继承到较高权限；收回 PUBLIC 的库 TEMP/CREATE、public schema CREATE、非业务函数和基表访问。reader 只连专库，只能 SELECT 所获准视图，默认只读；BFF 不持写库口令。

## 交付物

`sql/migrations/001_batch.sql`、`sql/views/`、`config/roles.yaml`、`src/chatbi/db/load.py`、`tests/test_s07.py`。

## 验收条件

1. `chatbi db migrate && chatbi load && chatbi load` 两次返回同一 batch_id；`chatbi db inspect-batch` 显示 staged、同版物理行计数稳定，bi 中该批可见行数为 0。
2. `pytest -q tests/test_s07.py -k atomic` 在真实测试 PG 中并发加载同文件、制造 COPY 中途失败再重试；唯一批次、无半批可见、发布指针不动，重试行数匹配清单。
3. `pytest -q tests/test_s07.py -k grants` 用实际 reader 连接检查 has_table_privilege 和查询；获准视图可读，基表、其他域、写操作、创建临时表均被拒绝。

## 常见坑

销售单号、客户名、同月部门名均可能重复。查询角色若是视图/表所有者或可切换成 loader，字典白名单无法补救。

## 预计工作量与并行

2–3 小时。DDL 与 COPY 可按字段合同并行，迁移统一提交。
