# 数字阅读使用口径与授权服务核算

面向采购周期的本地核算应用：把数字阅读的**有效使用、服务责任、续购判断**放到同一套可互相核对的口径里。
只处理本地合成的终端、学校与合同资料（`materials/`），不依赖任何网络服务或第三方库，使用 Python 3.11 标准库。

## 要解决的问题

- 服务商与学校对一次中断责任有分歧时，采购员既不能立刻按平台故障付抵扣，也不能把没访问成功的学生记作“无需求”。
- 登录次数和页面停留不足以代表阅读；设备时钟会偏、离线上传会晚到、同一学生会跨设备接续。
- 同一损失可能同时出现在学校与区域两份申请里，不能重复兑现；抵扣口径以项目合同为准。
- 换一个统计规则后，不能只留下一个新总数——旧口径结果与受影响范围必须可查回。

## 运行方式

所有命令通过根目录启动器执行，兼容隔离模式 `python3 -I`（避免在资料目录内解释执行时的同名模块注入）。
状态默认写入 `state/reading.db`（SQLite，已在 `.gitignore`），原始资料目录始终只读。

```sh
# 运行全部测试（资料结构测试 + 业务/口径回归测试，共 40 项）
python3 -I run_tests.py

# 导入资料（按 sha256 指纹去重，重复导入不产生新批次）
python3 -I run_app.py import materials

# 周期查询：可用率、有效覆盖、平台损失、当前应付抵扣与争议改判预览
python3 -I run_app.py periods
python3 -I run_app.py --json periods --contract K2

# 从结论追到原始经历（含音频/页面/进度三口径、校正来源、证据事件 id）
python3 -I run_app.py --json experience --person P019

# 访问请求分类明细：失败请求具名保留，不从统计中消失
python3 -I run_app.py --json attempts --contract K2

# 历史时点复现：只采用该日期前收到的资料（晚回传、迟到校正被排除）
python3 -I run_app.py reprocess --as-of 2026-04-30 --note "4月末时点"

# 争议：提出补证 / 撤回裁定回到挂起 / 正式裁定
python3 -I run_app.py dispute --file DP2 I2 /path/to/statement.txt
python3 -I run_app.py dispute --reopen DP1 "复现争议未决办理过程"
python3 -I run_app.py dispute --decide DP1 platform 2026-05-12

# 申请办理、结算与审计
python3 -I run_app.py claims
python3 -I run_app.py settle CL2 2026-05-15
python3 -I run_app.py audit

# 续购/调整授权建议（含并发放开后的反事实覆盖）
python3 -I run_app.py renewal

# 口径审议：提案不改变现有总数，批准后重算并登记受影响范围
python3 -I run_app.py rules --propose /path/to/proposal.json
python3 -I run_app.py rules --approve v1.1-rc1

# 最近一次运行的三类变更
python3 -I run_app.py changes
```

`--json` 是全局参数，须放在子命令前（如 `run_app.py --json periods`）。

## 口径设计（可审议，版本化）

### 1. 使用口径（`reading_accounting/usage.py`，规则 v1.0）

- **三口径分别报告**：音频听读秒（`audio_seconds`）、页面停留秒（`page_seconds`）、实际净进度（`net_progress`），不混成一个数。
- **前台才算停留**：后台事件不产生时长；相邻前台事件超过 30 分钟视为无活动间隔，间隔不计时长，单次停留封顶 30 分钟。
- **进度只认净新增**：高水位从 0 起算并沿整条“人 × 内容”时间线**跨日延续**，回退（position 变小）计数但不扣减已取得进度，跳读（单步 >0.3）留痕审议，重复到达（含跨日重读同一进度）不重复计；同一真实时间的多条记录折叠为一个不可知时刻，不产生回退/跳读噪声。
- **跨设备接续**：先按“人 × 内容”组织完整时间线，再按设备本地阅读日产出经历；同一时段跨设备/跨形态取并集，重复进度不膨胀时长。
- **未知时长不补零**：某形态最后一个前台点之后若再无更晚的同形态前台事件（后台事件不能界定前台停留结束），才计 `unknown_seconds`，绝不填 0。
- **顺序存疑**：同一真实时间存在多条记录时标 `uncertain`，存疑时段不重复累计。
- **未成熟可见**：证据在合同周期成熟日之后才到达（晚回传）时，相关经历与周期指标标 `provisional`，明确告知使用率还会变化。
- **时钟校正**：真实时间 = 设备时间 − 适用偏移（取 `effective_from` 不晚于事件时间的最新校正，否则用设备初始偏移）。校正改变经历是否落在开放窗（`in_open_active_seconds`），但不改变“读了多久”本身。

### 2. 访问归责（`reading_accounting/attribution.py`）

每个 `access_request` 都得到一个具名分类，失败请求一条不丢：

| 分类 | 含义 |
| --- | --- |
| `SUCCESS` | 成功授权 |
| `AUTH_DENIED` | 授权被拒绝（数据中为并发上限 `concurrency`） |
| `SCHOOL_CLOSED` | 落在合同开放时段之外，或中断归责学校未开放 |
| `PLATFORM_FAULT` | 开放时段内、命中平台责任中断的失败 |
| `PENDING_DISPUTE` | 中断归责争议未裁定，暂不付抵扣 |
| `INSUFFICIENT_EVIDENCE` | 名册学生无任何请求/经历，证据不足而非“无需求” |

中断归责：已裁定争议优先，其次原始声明；争议已提出未裁定则为 `undecided` 挂起。
`SLA 可用率`只在开放时段内实际发起的请求上考核平台；`请求成功率`覆盖全部结果，两者分开报告。

### 3. 抵扣口径（`reading_accounting/credit.py`，以合同为准）

- 对中断区间与**合同开放窗口取交集**，再按当前归责筛平台责任。
- 重叠区间先取并集，**只计一次**（多中断重叠、学校与区域申请重叠都去重）；后一笔申请的中断与已兑现区间部分重叠时，按**时间区间差集**只保留净增的未兑现分钟，而不是只比对整段标识。
- 容忍分钟与封顶按**合同周期只作用一次**：
  `抵扣 = max(0, 平台损失分钟 − allowable_outage_minutes) × credit_per_minute`，封顶不超过合同金额（契约文字最高 1200 元）。
- **累计边际分配**：申请按“已结优先、提交日优先”排序，每笔只就自己净增的损失，取累计抵扣曲线的边际值
  `cumulative_increment(合同, 已认定分钟, 本笔净增分钟)`。这样先结算的小损失申请不能领走整个合同池，后结算申请也不会被二次容忍，全合同累计支付天然不超过封顶。
- 损失最小单元 `loss_key = 合同|中断|开放交集区间`，全局只兑现一次；两笔外部已结申请若覆盖同一损失，后一笔标记 `PAID_ELSEWHERE`，冻结金额留痕但不重复计入。
- 已结申请金额**冻结不覆盖**；新事实只让在办申请继续办理，让已结申请（含系统内结算）产生可追查的调整单（`adjustments`），不再次领取同一权利。审议中（争议未裁定）的申请禁止结算。

### 4. 续购判断（`reading_accounting/metrics.py`）

先识别瓶颈再谈额度：并发拒绝（`AUTH_DENIED`）说明瓶颈是并发上限而非座位数，建议提并发；
平台故障/未开放条件下加额度无法扩大覆盖；富余座位在覆盖已达全员时才建议优化，不把未用额度一概说成浪费。
同时给出“若放开并发，被拒学生可转化的反事实覆盖率”。

## 三类变更的区分（业务测试的核心断言）

每次事实变化开启一个新 run，旧快照保留，`run_changes` 明确登记：

- `USAGE_REVISION` 使用量修订：时钟订正/晚回传/口径切换改变阅读证据，精确到受影响学生与学校。
- `ATTRIBUTION_CHANGE` 归责变化：争议改判改变中断责任与分类，**不增加任何阅读经历**。
- `AMOUNT_ADJUSTMENT` 金额调整：合同口径重算后的抵扣变化；已结金额走调整单而非覆盖。

## 数据与状态分层

- `raw_records`：导入的原始事实，只追加；更正以新批次写入，旧批次仍可追溯。
- `experiences / attempts / metrics`：某次 run 在某口径版本下的派生快照，重算产生新 run、旧快照保留。
- `rule_versions`：口径参数与文字说明，版本只增不改。
- `claims_state / claim_losses / adjustments`：申请办理状态、损失去重明细、已结后续调整。
- `ledger`：正式决定追加台账（导入、重算、争议、结算、口径批准），可从结论追到原始经历与正式决定。

## 资料

合成资料契约见 [资料契约.md](资料契约.md)，机读字段在 `materials/contract.json`，原始记录在 `materials/data.json`。
资料结构检查工具 `inspect_materials.py` 保持不变；应用功能不修改原始资料。
