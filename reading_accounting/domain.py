"""领域常量、分类标签与口径版本定义。

本模块只放“名称与含义”的单一事实来源；计算逻辑放在 timeutil / usage /
attribution / credit 等模块。所有标签同时给出中文说明，供审议与界面使用。
"""

# 访问请求的最终业务分类（失败请求不得从统计中消失，因此全部具名保留）。
ACCESS_CATEGORIES = {
    "SUCCESS": "成功获得授权并进入服务",
    "AUTH_DENIED": "授权被拒绝（并发上限或授权范围等服务侧限制）",
    "SCHOOL_CLOSED": "学校未开放：请求落在合同开放时段之外",
    "INSUFFICIENT_EVIDENCE": "需求证据不足：无请求、无会话且无可核验经历",
    "PLATFORM_FAULT": "平台故障：开放时段内被归责于平台的中断影响",
    "PENDING_DISPUTE": "中断归责存在未裁定争议，暂不兑现抵扣",
}

# 归责结论（incident 的最终状态）。
CAUSE_PLATFORM = "platform"
CAUSE_SCHOOL_CLOSED = "school_closed"
CAUSE_UNDECIDED = "undecided"
CAUSE_LABELS = {
    CAUSE_PLATFORM: "平台责任",
    CAUSE_SCHOOL_CLOSED: "学校未开放",
    CAUSE_UNDECIDED: "争议未裁定",
}

# 申请办理状态（正式决定追加，不覆盖）。
CLAIM_STATUS = {
    "DRAFT": "草稿，尚未提交",
    "SUBMITTED": "已提交待办",
    "IN_REVIEW": "审议中（存在争议或等待补证）",
    "SETTLED": "已结算（金额冻结，后续以调整单处理）",
    "SUPERSEDED": "同一损失已在另一申请兑现，本申请不再支付",
}

# 决定台账的动作类型。
LEDGER_ACTIONS = {
    "IMPORT": "原始资料导入",
    "REPROCESS": "按新事实重算（修订使用量或归责）",
    "RULE_PROPOSED": "口径提案",
    "RULE_APPROVED": "口径正式生效",
    "DISPUTE_FILED": "提出争议",
    "DISPUTE_DECIDED": "争议正式裁定",
    "CLAIM_SUBMITTED": "提交抵扣申请",
    "CLAIM_SETTLED": "结算抵扣",
    "CLAIM_ADJUSTMENT": "已结金额的后续调整（不覆盖原结算）",
}

# 变更性质三类：业务测试必须区分这三者，不得混为一个“总数变化”。
CHANGE_KINDS = {
    "USAGE_REVISION": "使用量修订（时钟订正/晚回传改变阅读证据）",
    "ATTRIBUTION_CHANGE": "归责变化（争议改判，不增加实际阅读）",
    "AMOUNT_ADJUSTMENT": "金额调整（抵扣口径重算后的钱变化）",
}

# 抵扣口径上限：合同补充口径明确“最高 1200 元”，与各合同 price 一致；
# 仍以合同字段为准，这里仅作为合同缺省时的兜底与审议提示。
CONTRACT_CREDIT_CAP_FALLBACK = 1200.0


def rule_versions():
    """内置口径版本。新版本只能追加，不得修改或删除旧版本。

    每个版本固化一组参数与文字说明；提案可基于 v1 调整参数，经批准后成为
    新版本，旧版本下产生的指标快照永久保留，可查询受影响范围。
    """
    return [
        {
            "version": "v1.0",
            "status": "approved",
            "proposed_on": "2026-04-01",
            "decided_on": "2026-04-01",
            "params": {
                # 仅前台事件可计入活跃；后台停留不代表阅读。
                "require_foreground": True,
                # 相邻事件超过该秒数视为无活动间隔，间隔本身不计时长。
                "inactivity_gap_seconds": 1800,
                # 单次前台停留超过该秒数的尾段视为无活动，不累计。
                "max_dwell_seconds": 1800,
                # 有效阅读的最低门槛（满足其一）：累计前台秒数 / 净进度。
                "min_active_seconds": 300,
                "min_net_progress": 0.25,
                # 未知时长（缺尾点等）不得补零，单独计数。
                "zero_fill_unknown": False,
            },
            "notes": (
                "音频播放、页面停留与实际进度分别解释："
                "audio/page 的前台时段分别累计为听读秒与页面停留秒；"
                "实际进度只取每个内容位置序列的净新增最大值，回退不扣减、跳读不重复计；"
                "无活动间隔与后台时段不计入时长；同时刻且无法确定先后的记录标记顺序存疑，"
                "存疑时段不重复累计；未知时长单列、不补零。"
            ),
        }
    ]
