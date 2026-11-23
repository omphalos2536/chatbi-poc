# 04 字段映射

## 目标

把规则命中、模型候选与最终人工决定连到同一个可重放的版本。

## 前置条件

02 的假/真接口可用，03 清单生成。

## 输入

inventory.json、工作簿字典、目标逻辑表、字段类型与敏感标签。

## 详细任务清单

1. 按每张逻辑表建立目标 schema，至少有字段 ID、PG 类型、原单位、标准单位、比例、必填性、键、指标方向、敏感级、数据域。先精确匹配多级路径，再应用作用域内的同义词和单位规则，例如采购单价（元）与金额（万元）按列分别处理。未知列与单位冲突保持 pending。
2. 实现 `chatbi map suggest`，规则产生候选及理由；对未定项经 02 发送获准表头和候选字典，记录建议、来源、模型版本和分数。无密钥时标注 unavailable，仍可人工录入。模型输出目标 ID 必须在候选集中；置信度不触发自动批准。
3. `chatbi map review` 在本机逐表呈现源坐标、候选、单位及排除项，接受 accept/edit/exclude/reject，必填项不可排除。允许逐表查看后整组确认。复核者输入署名和理由；自动化测试仅用夹具身份。保存不可覆盖的决策事件，记录原建议、新值、时间、审核人、源结构指纹、文件哈希和映射版本。
4. `chatbi map verify` 检查每列有去向或排除原因、必填唯一、转换有维度、敏感列仅限获准目的。冻结内容哈希供加载器复验；同结构重导引用历史决定并绑定新批次，新增或变更字段、单位、审核策略使受影响决定失效。

## 交付物

`config/mapping/targets.yaml`、`config/mapping/aliases.yaml`、`src/chatbi/ingest/mapping.py`、`var/approvals/mapping/`、`tests/test_s04.py`。

## 验收条件

1. `chatbi map suggest && chatbi map review && chatbi map verify`；人工确认后退出 0，输出 `pending=0`、映射版本、哈希及署名；每个已盘点列都有使用或排除记录。
2. `pytest -q tests/test_s04.py -k lifecycle` 全绿；未经确认、必填缺失、换单位、篡改已签文件均阻止使用，重导同结构复用有出处。
3. `pytest -q tests/test_s04.py -k suggestion` 全绿；模拟含歧义表头确实调用网关，记录 model→edit→approved 全链；模拟离线时可确认、无虚假模型来源。

## 常见坑

同名“实际”可能来自不同月份。更改列序时按指纹和路径重检，切勿沿用旧 Excel 列字母自动批准。

## 预计工作量与并行

2–3 小时。审核需一名实际操作员；与后续模板版式设计可并行。
