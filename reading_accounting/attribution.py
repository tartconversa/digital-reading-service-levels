"""归责引擎：访问分类、中断裁定、周期可用率与有效覆盖。

失败访问请求不得从统计中消失：每个 access_request 都会得到一个具名分类，
且合同名册中的学生即使没有任何请求，也以 INSUFFICIENT_EVIDENCE 单独入册
（而不是被记作“无需求”而消失）。
"""
from __future__ import annotations

from . import timeutil as tu
from .domain import (
    CAUSE_PLATFORM, CAUSE_SCHOOL_CLOSED, CAUSE_UNDECIDED,
)


def adjudicate_incidents(incidents, disputes):
    """形成中断归责表：已裁定争议优先；无裁定则保留原声明；争议提出未裁定为未决。"""
    dispute_by_incident = {dp["incident_id"]: dp for dp in disputes}
    out = {}
    for inc in incidents:
        dp = dispute_by_incident.get(inc["id"])
        if dp and dp.get("decided_on"):
            out[inc["id"]] = {
                "cause": dp["decision"], "dispute_id": dp["id"],
                "decided_on": dp["decided_on"], "statement": dp["statement"],
                "provisional": False}
        elif dp:  # 已提争议但未裁定
            out[inc["id"]] = {
                "cause": CAUSE_UNDECIDED, "dispute_id": dp["id"],
                "decided_on": None, "statement": dp["statement"],
                "provisional": True}
        else:
            out[inc["id"]] = {
                "cause": inc["claimed_cause"], "dispute_id": None,
                "decided_on": None, "statement": None,
                "provisional": False}
    return out


def _incident_at(incidents_by_id, adjudications, moment, school_id):
    """返回该时刻该学校命中的中断（含归责）；多个时取开始最晚者。"""
    hit = None
    for inc in incidents_by_id.values():
        if school_id not in inc["school_ids"]:
            continue
        s, e = tu.parse_dt(inc["start"]), tu.parse_dt(inc["end"])
        if s <= moment < e:
            if hit is None or s > tu.parse_dt(hit["start"]):
                hit = inc
    if not hit:
        return None
    verdict = adjudications[hit["id"]]
    return hit, verdict


def classify_requests(access_requests, contracts_by_id, schools_by_id,
                      incidents, adjudications, rule_version, as_of=None):
    """逐条分类访问请求；另为无请求学生生成 INSUFFICIENT_EVIDENCE 行。"""
    incidents_by_id = {i["id"]: i for i in incidents}
    contracts = contracts_by_id
    attempts = []
    requests = tu.as_of_filter(access_requests, as_of)

    covered_people = set()
    for r in requests:
        contract = contracts[r["contract_id"]]
        school_id = contract["school_id"]
        covered_people.add((r["contract_id"], r["person_id"]))
        moment = tu.parse_dt(r["at"])
        in_open = tu.in_open_window(moment, contract)
        category, cause, incident_id = None, None, None
        provisional = False

        hit = _incident_at(incidents_by_id, adjudications, moment, school_id)
        if r["result"] == "granted":
            category = "SUCCESS"
        elif r["reason"] == "concurrency":
            # 并发授权瓶颈；若同时命中平台责任中断则归平台故障。
            if hit and hit[1]["cause"] == CAUSE_PLATFORM and in_open:
                category = "PLATFORM_FAULT"
                cause = CAUSE_PLATFORM
                incident_id = hit[0]["id"]
                provisional = hit[1]["provisional"]
            elif hit and hit[1]["cause"] == CAUSE_UNDECIDED and in_open:
                # 争议未裁定：失败请求保留为独立类别，不提前按平台故障付抵扣。
                category = "PENDING_DISPUTE"
                cause = CAUSE_UNDECIDED
                incident_id = hit[0]["id"]
                provisional = True
            else:
                category = "AUTH_DENIED"
                cause = "concurrency"
        else:  # 其他拒绝原因同样是授权侧拒绝，保留原始 reason
            category = "AUTH_DENIED"
            cause = r.get("reason") or "unknown_denial"

        # 开放时段之外的失败不算平台 SLA 损失：学校未开放。
        if category == "AUTH_DENIED" and not in_open:
            category = "SCHOOL_CLOSED"
            cause = CAUSE_SCHOOL_CLOSED
        if hit and category == "AUTH_DENIED" and \
                hit[1]["cause"] == CAUSE_SCHOOL_CLOSED:
            category = "SCHOOL_CLOSED"
            cause = CAUSE_SCHOOL_CLOSED
            incident_id = hit[0]["id"]

        attempts.append({
            "request_id": r["id"], "person_id": r["person_id"],
            "contract_id": r["contract_id"], "at_iso": tu.iso(moment),
            "category": category, "cause": cause, "incident_id": incident_id,
            "in_open": in_open, "reason": r.get("reason"),
            "provisional": provisional, "rule_version": rule_version})

    # 名册学生无任何请求：需求证据不足，而不是“无需求”。
    people_seen = {p for cid, p in covered_people}
    for cid, contract in contracts.items():
        school = schools_by_id[contract["school_id"]]
        # people 由引擎按合同学校名册注入（通过 school_people 映射）。
        for person_id in school.get("_people", []):
            if person_id in people_seen:
                continue
            attempts.append({
                "request_id": None, "person_id": person_id,
                "contract_id": cid, "at_iso": None,
                "category": "INSUFFICIENT_EVIDENCE", "cause": None,
                "incident_id": None, "in_open": False, "reason": None,
                "provisional": as_of is not None, "rule_version": rule_version})
    attempts.sort(key=lambda a: (a["contract_id"], a["at_iso"] or "",
                                 a["person_id"]))
    return attempts
