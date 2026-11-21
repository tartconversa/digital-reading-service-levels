"""抵扣口径：以项目合同为准，对开放区间与平台责任损失取交集。

合同补充口径：
* 抵扣 = (平台责任且落在开放时段的损失分钟 − 容忍中断分钟) × credit_per_minute；
* 重叠区间只计一次（同一合同周期内多中断、学校与区域申请重叠均取并集）；
* 单合同周期封顶不超过合同金额（契约文字：最高 1200 元；代码以合同 price 为准）。
"""
from __future__ import annotations

from . import timeutil as tu
from .domain import CAUSE_PLATFORM, CAUSE_UNDECIDED, CONTRACT_CREDIT_CAP_FALLBACK


def incident_open_segments(incident, contract):
    """单个中断与该合同开放窗口的交集区间。"""
    s, e = tu.parse_dt(incident["start"]), tu.parse_dt(incident["end"])
    return tu.intersect_with_open([(s, e)], contract)


def platform_loss_minutes(incident_ids, incidents_by_id, adjudications, contract,
                          include_pending=False):
    """汇总若干中断在本合同开放时段内的平台责任损失（重叠只计一次）。

    返回 (平台分钟, 各中断明细, 合并后区间)。未裁定争议默认不计平台损失，
    include_pending=True 时按“若改判平台”的预览口径纳入（仅用于争议影响预览）。
    """
    details, all_segments = {}, []
    for iid in incident_ids:
        inc = incidents_by_id[iid]
        segs = incident_open_segments(inc, contract)
        open_minutes = round(sum((e - s).total_seconds() for s, e in segs) / 60.0, 3)
        verdict = adjudications.get(iid)
        is_platform = bool(verdict and verdict["cause"] == CAUSE_PLATFORM)
        is_pending = bool(verdict and verdict["cause"] == CAUSE_UNDECIDED)
        counted = is_platform or (include_pending and is_pending)
        details[iid] = {
            "open_minutes": open_minutes,
            "cause": verdict["cause"] if verdict else None,
            "counted_platform": counted,
            "segments": [(tu.iso(s), tu.iso(e)) for s, e in segs],
        }
        if counted:
            all_segments.extend(segs)
    union_minutes, merged = tu.union_minutes(all_segments)
    return round(union_minutes, 3), details, merged


def credit_for_minutes(minutes, contract):
    """按合同口径把平台损失分钟换算为抵扣（先扣容忍、再封顶）。"""
    allowable = contract["allowable_outage_minutes"]
    chargeable = max(0.0, minutes - allowable)
    raw = round(chargeable * contract["credit_per_minute"], 2)
    cap = min(float(contract["price"]), CONTRACT_CREDIT_CAP_FALLBACK)
    return min(raw, cap), chargeable, cap


def loss_key(contract_id, incident_id, segment_iso):
    """损失最小单元：合同 × 中断 × 开放交集区间。同一 key 全局只兑现一次。

    segment_iso 接受 (start_iso, end_iso) 元组，输出稳定字符串，避免 tuple repr。
    """
    start_iso, end_iso = segment_iso
    return f"{contract_id}|{incident_id}|{start_iso}/{end_iso}"


def evaluate_claim(claim, contracts_by_id, incidents_by_id, adjudications,
                   paid_loss_keys, include_pending=False):
    """核算一笔申请的损失明细，并按 loss_key 去重。

    容忍分钟与封顶是合同周期级口径，在 contract_credit_pool 中只作用一次；
    本函数只负责：逐中断算开放/平台分钟、标出已在别处兑现的损失 key、
    汇总本申请尚未兑现的平台分钟（重叠只计一次）。
    paid_loss_keys: 已经在其他申请中兑现（结算）的 loss_key 集合，保证同一损失
    不会在学校与区域两份申请中重复兑现。
    """
    contract = contracts_by_id[claim["contract_id"]]
    minutes, details, _merged = platform_loss_minutes(
        claim["incident_ids"], incidents_by_id, adjudications, contract,
        include_pending=include_pending)

    dedup_segments, line_items = [], []
    for iid, d in details.items():
        for seg_iso in d["segments"]:
            key = loss_key(contract["id"], iid, seg_iso)
            already = key in paid_loss_keys
            line_items.append({
                "loss_key": key, "incident_id": iid, "segment": seg_iso,
                "already_paid": already})
            if not already and d["counted_platform"]:
                s, e = tu.parse_dt(seg_iso[0]), tu.parse_dt(seg_iso[1])
                dedup_segments.append((s, e))
    dedup_minutes, _ = tu.union_minutes(dedup_segments)

    return {
        "claim_id": claim["claim_id"],
        "contract_id": contract["id"],
        "level": claim["level"],
        "gross_platform_minutes": minutes,
        "dedup_platform_minutes": round(dedup_minutes, 3),
        "details": details,
        "line_items": line_items,
        "has_pending": any(v["cause"] == CAUSE_UNDECIDED for v in details.values()),
    }


def contract_credit_pool(contract, gross_platform_minutes):
    """合同周期级抵扣池：容忍与封顶只作用一次（以项目合同为准）。"""
    payable, chargeable, cap = credit_for_minutes(gross_platform_minutes, contract)
    return {"gross_minutes": gross_platform_minutes,
            "chargeable_minutes": chargeable,
            "gross_payable": payable, "cap": cap}


def cumulative_increment(contract, prior_minutes, added_minutes):
    """在已认定 prior_minutes 基础上新增 added_minutes 的边际抵扣。

    容忍分钟与封顶在整条累计曲线上只作用一次：先算累计到 prior、再算累计到
    prior+added，两者之差即本笔申请仅就“自己新增损失”应得的抵扣。这样先结算
    的小损失申请不能领走整池额度，后结算申请也不会被二次容忍。
    """
    before = credit_for_minutes(prior_minutes, contract)[0]
    after = credit_for_minutes(prior_minutes + added_minutes, contract)[0]
    return round(after - before, 2)
