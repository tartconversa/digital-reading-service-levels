"""周期评估指标：可用率、有效覆盖、授权瓶颈与争议影响预览。"""
from __future__ import annotations

from collections import Counter

from .credit import credit_for_minutes, platform_loss_minutes


def period_metric(contract, school, attempts, coverage_by_person,
                  incidents_by_id, adjudications, rule_version, as_of=None,
                  include_pending=False):
    """组装一个合同周期的可审议指标包（每个数字都能追到 attempts/经历）。"""
    mine = [a for a in attempts if a["contract_id"] == contract["id"]]
    counts = Counter(a["category"] for a in mine)

    requested = [a for a in mine if a["request_id"] is not None]
    n_req = len(requested)
    # 平台 SLA 可用率：只在开放时段内、且责任已确定的请求上考核平台。
    # 未裁定争议（PENDING_DISPUTE）责任未定，不提前计入平台故障，从分母剔除并
    # 单独披露；该周期同时标 provisional，提示可用率仍可能变化。
    open_requests = [a for a in requested if a["in_open"]]
    pending_open = [a for a in open_requests
                    if a["category"] == "PENDING_DISPUTE"]
    sla_scoped = [a for a in open_requests
                  if a["category"] != "PENDING_DISPUTE"]
    open_fault = [a for a in sla_scoped if a["category"] == "PLATFORM_FAULT"]
    sla_availability = (
        1.0 if not sla_scoped
        else round(1 - len(open_fault) / len(sla_scoped), 4))
    sla_pending_requests = len(pending_open)
    # 总体请求成功率（含授权拒绝、未开放等所有结果），失败请求不消失。
    success_rate = (
        None if not n_req
        else round(counts.get("SUCCESS", 0) / n_req, 4))

    # 有效覆盖：名册学生中达到口径有效阅读门槛的人数；无证据者单列。
    roster = school.get("_people", [])
    covered, provisional_people, unknown_people = [], [], []
    for pid in roster:
        p = coverage_by_person.get(pid)
        if p is None:
            continue
        if p["valid_days"] > 0:
            covered.append(pid)
        if p["provisional"]:
            provisional_people.append(pid)
        if p["unknown_seconds"] > 0:
            unknown_people.append(pid)
    effective_coverage = (round(len(covered) / len(roster), 4) if roster else None)

    # 平台损失分钟与抵扣（当前裁定口径 + 争议预览口径并存，不互相覆盖）。
    incident_ids = [iid for iid in incidents_by_id
                    if contract["school_id"] in incidents_by_id[iid]["school_ids"]]
    minutes, details, _ = platform_loss_minutes(
        incident_ids, incidents_by_id, adjudications, contract,
        include_pending=False)
    preview_minutes, preview_details, _ = platform_loss_minutes(
        incident_ids, incidents_by_id, adjudications, contract,
        include_pending=True)
    payable, chargeable, cap = credit_for_minutes(minutes, contract)
    preview_payable, preview_chargeable, _ = credit_for_minutes(
        preview_minutes, contract)

    provisional = any(a["provisional"] for a in mine) or bool(provisional_people) \
        or as_of is not None or sla_pending_requests > 0

    return {
        "contract_id": contract["id"],
        "school_id": contract["school_id"],
        "rule_version": rule_version,
        "as_of": as_of,
        "provisional": provisional,
        "roster_size": len(roster),
        "attempt_breakdown": dict(counts),
        "requests_total": n_req,
        "sla_availability": sla_availability,
        "sla_pending_dispute_requests": sla_pending_requests,
        "request_success_rate": success_rate,
        "effective_coverage": effective_coverage,
        "effective_people": sorted(covered),
        "insufficient_evidence_people": sorted(
            {a["person_id"] for a in mine
             if a["category"] == "INSUFFICIENT_EVIDENCE"}),
        "insufficient_evidence_count": counts.get("INSUFFICIENT_EVIDENCE", 0),
        "auth_denied_people": sorted({a["person_id"] for a in mine
                                      if a["category"] == "AUTH_DENIED"}),
        "provisional_people": sorted(provisional_people),
        "unknown_duration_people": sorted(set(unknown_people)),
        "platform_loss_minutes": minutes,
        "chargeable_minutes": chargeable,
        "credit_payable_now": payable,
        "credit_cap": cap,
        "dispute_preview": {
            "platform_minutes_if_pending_platform": preview_minutes,
            "credit_if_pending_platform": preview_payable,
            "credit_delta": round(preview_payable - payable, 2),
            "minutes_delta": round(preview_minutes - minutes, 3),
        },
        "incident_detail": details,
    }


def renewal_recommendation(metric, contract, school):
    """续购/调整授权建议：先识别瓶颈，再回答加额度能否扩大覆盖。

    未用额度不一律视为浪费：若瓶颈是并发（AUTH_DENIED）或开放条件
    （SCHOOL_CLOSED / 平台故障），增加座位数本身不会扩大有效覆盖。
    """
    bd = metric["attempt_breakdown"]
    denied = bd.get("AUTH_DENIED", 0)
    fault = bd.get("PLATFORM_FAULT", 0) + bd.get("PENDING_DISPUTE", 0)
    closed = bd.get("SCHOOL_CLOSED", 0)
    coverage = metric["effective_coverage"] or 0.0
    roster = metric["roster_size"]
    seats = contract["seats"]
    unused_seats = max(0, seats - roster)

    reasons, actions = [], []
    if denied > 0:
        reasons.append(
            f"开放时段内 {denied} 次并发授权拒绝，瓶颈是并发上限 {contract['concurrent']}，"
            "而非座位不足")
        actions.append(f"优先把并发上限从 {contract['concurrent']} 上调，"
                       "再评估座位数")
    if fault > 0 or closed > 0:
        reasons.append(
            f"存在平台/开放条件受限（故障或争议 {fault} 次、未开放 {closed} 次），"
            "增加授权额度不能修复可用性")
        actions.append("先完成争议裁定与服务整改，再谈扩额")
    if coverage < 1.0 and denied == 0 and fault == 0:
        reasons.append("覆盖不足且无授权拒绝证据，属于需求证据不足，不应据失败访问推断浪费")
        actions.append("补采集使用证据后再评估，暂不扩额")
    if unused_seats > 0 and coverage >= 1.0 and denied == 0:
        reasons.append(
            f"有效覆盖已达全员，座位数 {seats} 高于名册 {roster} 人，"
            f"{unused_seats} 个富余座位不产生阅读价值（未用额度不等于浪费，但可优化）")
        actions.append("续购可按名册规模下调座位数，把预算转向并发或内容")
    if not reasons:
        reasons.append("授权与开放条件均未构成瓶颈，覆盖与可用率稳定")
        actions.append("可按现额度续购")

    # 反事实：若并发上限放开，被拒且尚未有效覆盖的学生可转化为新增覆盖。
    denied_people = set(metric["auth_denied_people"])
    already = set(metric["effective_people"])
    convertible = len(denied_people - already)
    projected_coverage = round(
        min(1.0, (len(already) + convertible) / roster), 4) if roster else coverage

    return {
        "contract_id": contract["id"],
        "recommendation": "；".join(actions),
        "reasons": reasons,
        "unused_seats": unused_seats,
        "concurrency_denied": denied,
        "denied_people": sorted(denied_people),
        "convertible_if_concurrency_relaxed": convertible,
        "coverage_now": coverage,
        "projected_coverage_if_concurrency_relaxed": projected_coverage,
        # 加座位只有在“无并发/开放瓶颈、覆盖未满、且无富余座位”时才可能扩大覆盖。
        "seat_increase_would_help": denied == 0 and unused_seats == 0 and coverage < 1.0,
    }
