"""编排引擎：导入 → 重算 → 审议/争议 → 结算/调整 的本地办理流程。

每次事实变化（晚回传、时钟订正、争议改判）都开一个新 run，旧快照保留；
run_changes 明确区分 USAGE_REVISION / ATTRIBUTION_CHANGE / AMOUNT_ADJUSTMENT。
已结申请金额冻结，新事实只让在办申请继续办理、让已结申请产生可追查调整单。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import attribution, credit, metrics as metrics_mod, usage
from . import timeutil as tu
from .domain import LEDGER_ACTIONS, CLAIM_STATUS
from .store import Store


class Engine:
    def __init__(self, db_path):
        self.store = Store(db_path)
        self._ensure_rules()

    def close(self):
        self.store.close()

    # ---------------- 基础资料 ----------------
    def _ensure_rules(self):
        from .domain import rule_versions
        for rv in rule_versions():
            if not self.store.get_rule(rv["version"]):
                self.store.upsert_rule(rv, rv.get("proposed_on"))

    def _tables(self):
        data = {}
        for name in ("schools", "people", "contracts", "devices", "events",
                     "clock_corrections", "access_requests", "incidents",
                     "claims", "disputes"):
            data[name] = self.store.get_raw(name)
        return data

    def import_materials(self, materials_dir):
        materials_dir = Path(materials_dir)
        raw = (materials_dir / "data.json").read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        existing = self.store.fingerprint_imported(sha)
        if existing:
            return {"batch_id": existing, "dedup": True}
        data = json.loads(raw)
        counts = {n: len(r) for n, r in data.items()}
        batch_id = self.store.import_raw(data, sha, materials_dir / "data.json", counts)
        self.store.append_ledger(
            LEDGER_ACTIONS["IMPORT"], "采购员",
            {"source": str(materials_dir / "data.json"), "sha256": sha,
             "counts": counts})
        self.reprocess(note="首次导入全量重算")
        return {"batch_id": batch_id, "dedup": False, "counts": counts}

    # ---------------- 重算 ----------------
    def reprocess(self, as_of=None, note="", rule_version=None):
        data = self._tables()
        rule = self.store.get_rule(rule_version or "v1.0")
        run_id = self.store.start_run("reprocess", rule["version"], as_of, note)

        devices = data["devices"]
        corrections_by_device = {}
        for c in data["clock_corrections"]:
            corrections_by_device.setdefault(c["device_id"], []).append(c)

        corrected = usage.correct_events(
            data["events"], devices, corrections_by_device, as_of)
        maturity = self._maturity_day(data)
        contracts_by_school = {c["school_id"]: c for c in data["contracts"]}
        experiences = usage.build_experiences(
            corrected, data["people"], rule, as_of=as_of,
            maturity_day=maturity, contracts_by_school=contracts_by_school)
        self.store.replace_experiences(run_id, experiences)
        coverage = usage.summarize_person_coverage(experiences, data["people"])

        adjudications = attribution.adjudicate_incidents(
            data["incidents"], data["disputes"])
        for iid, v in adjudications.items():
            self.store.put_adjudication(
                iid, v["cause"], v["dispute_id"], v["decided_on"],
                v["statement"], run_id)

        schools_by_id = {s["id"]: dict(s) for s in data["schools"]}
        for p in data["people"]:
            schools_by_id[p["school_id"]].setdefault("_people", []).append(p["id"])
        contracts_by_id = {c["id"]: c for c in data["contracts"]}

        attempts = attribution.classify_requests(
            data["access_requests"], contracts_by_id, schools_by_id,
            data["incidents"], adjudications, rule["version"], as_of)
        self.store.replace_attempts(run_id, attempts)

        incidents_by_id = {i["id"]: i for i in data["incidents"]}
        metric_rows = []
        for cid, contract in contracts_by_id.items():
            school = schools_by_id[contract["school_id"]]
            m = metrics_mod.period_metric(
                contract, school, attempts, coverage, incidents_by_id,
                adjudications, rule["version"], as_of)
            metric_rows.append(m)
        self.store.replace_metrics(run_id, metric_rows)

        prev_runs = [r for r in self.store.list_runs() if r["id"] != run_id]
        rule_changed = bool(prev_runs and prev_runs[-1]["rule_version"]
                            != rule["version"])
        if rule_changed:
            affected = sorted({(m["school_id"], m["contract_id"])
                               for m in metric_rows})
            self.store.add_change(
                run_id, "USAGE_REVISION",
                {"rule_version_before": prev_runs[-1]["rule_version"],
                 "rule_version_after": rule["version"],
                 "affected_schools_contracts": affected},
                {"kind": "RULE_VERSION_CHANGE",
                 "note": "口径版本切换导致的使用量重算；旧版本快照保留可查"})
        self._diff_against_previous(run_id, as_of)
        self._reconcile_claims(run_id)
        self.store.append_ledger(
            LEDGER_ACTIONS["REPROCESS"], "系统",
            {"run_id": run_id, "as_of": as_of, "note": note,
             "experiences": len(experiences), "attempts": len(attempts)}, run_id)
        return run_id

    def _maturity_day(self, data):
        # 周期成熟日 = 合同止日；之后到达的证据会让指标保持 provisional。
        contracts = data["contracts"]
        if not contracts:
            return None
        return max(c["to_on"] for c in contracts)

    def _diff_against_previous(self, run_id, as_of):
        """登记三类变更及其影响学校/周期，区分“事实变化”与“口径/时点变化”。

        provisional（成熟度）翻转本身不是使用量修订；只有活跃秒、净进度等实质
        证据变化才登记 USAGE_REVISION。基线选择：
        * 同 as_of 口径的最近 run——捕捉常规重算；
        * 最近的异口径 run——捕捉晚回传/时钟订正（历史时点 → 当前事实）。
        """
        runs = [r for r in self.store.list_runs() if r["id"] != run_id]
        if not runs:
            return
        same_basis = [r for r in runs if r.get("as_of") == as_of]
        cross_basis = [r for r in runs if r.get("as_of") != as_of]
        baseline_ids = []
        if same_basis:
            baseline_ids.append(same_basis[-1]["id"])
        if cross_basis:
            baseline_ids.append(cross_basis[-1]["id"])
        if not baseline_ids:
            baseline_ids = [runs[-1]["id"]]
        new_metrics = {m["contract_id"]: m for m in self.store.metrics_at_run(run_id)}
        new_exp = self.store.experiences_at_run(run_id)

        def exp_index(rows):
            return {(r["person_id"], r["content_id"], r["day"]): r for r in rows}

        nx = exp_index(new_exp)

        def substance(r):
            return (round(r["active_seconds"], 2), round(r["net_progress"], 4),
                    r["uncertain"], r["unknown_seconds"], r["audio_seconds"],
                    r["page_seconds"])

        # 合并对各基线的实质经历差异（去重到人×学校）。
        usage_scopes = set()
        for prev_id in baseline_ids:
            ox = exp_index(self.store.experiences_at_run(prev_id))
            for k, r in nx.items():
                if k not in ox or substance(ox[k]) != substance(r):
                    usage_scopes.add((r["school_id"], r["person_id"]))
            for k, r in ox.items():
                if k not in nx:
                    usage_scopes.add((r["school_id"], r["person_id"]))
        if usage_scopes:
            self.store.add_change(run_id, "USAGE_REVISION",
                                  sorted(usage_scopes),
                                  {"affected_schools": sorted({s for s, _ in usage_scopes}),
                                   "people": sorted({p for _, p in usage_scopes})})

        # 归责变化 + 金额调整：与同口径基线（或唯一基线）在指标层面比较。
        prev_id = baseline_ids[0]
        old_metrics = {m["contract_id"]: m
                       for m in self.store.metrics_at_run(prev_id)}
        for cid, nm in new_metrics.items():
            om = old_metrics.get(cid)
            if not om:
                continue
            if om["platform_loss_minutes"] != nm["platform_loss_minutes"] or \
                    om["attempt_breakdown"] != nm["attempt_breakdown"]:
                self.store.add_change(run_id, "ATTRIBUTION_CHANGE",
                                      [nm["school_id"], cid],
                                      {"minutes_before": om["platform_loss_minutes"],
                                       "minutes_after": nm["platform_loss_minutes"],
                                       "breakdown_before": om["attempt_breakdown"],
                                       "breakdown_after": nm["attempt_breakdown"]})
            if om["credit_payable_now"] != nm["credit_payable_now"]:
                self.store.add_change(run_id, "AMOUNT_ADJUSTMENT",
                                      [nm["school_id"], cid],
                                      {"credit_before": om["credit_payable_now"],
                                       "credit_after": nm["credit_payable_now"]})

    # ---------------- 争议 ----------------
    def file_dispute(self, dispute_id, incident_id, statement, submitted_on):
        data = self._tables()
        # 争议作为新的输入事实写入 raw（追加），便于留痕。
        record = {"id": dispute_id, "incident_id": incident_id,
                  "submitted_on": submitted_on, "decided_on": None,
                  "decision": None, "statement": statement}
        self._append_raw("disputes", record)
        self.store.append_ledger(
            LEDGER_ACTIONS["DISPUTE_FILED"], "服务商/采购员", record)
        return self.reprocess(note=f"提出争议 {dispute_id}（归责暂挂起）")

    def reopen_dispute(self, dispute_id, note):
        """把已裁定争议撤回到未决挂起状态（原裁定批次仍留痕，不删除）。

        用于在本地复现“争议未决时不提前付抵扣、只给影响预览”的办理过程。
        """
        rows = self.store.get_raw("disputes")
        target = next((r for r in rows if r["id"] == dispute_id), None)
        if not target:
            raise ValueError("争议不存在：" + dispute_id)
        reopened = {**target, "decision": None, "decided_on": None,
                    "statement": (target.get("statement") or "") + "｜" + note}
        self._append_raw("disputes", reopened)
        self.store.append_ledger(
            LEDGER_ACTIONS["DISPUTE_FILED"], "采购员",
            {"dispute_id": dispute_id, "reopened": True, "note": note})
        return self.reprocess(note=f"争议 {dispute_id} 撤回裁定、重新挂起")

    def decide_dispute(self, dispute_id, decision, decided_on):
        rows = self.store.get_raw("disputes")
        target = next((r for r in rows if r["id"] == dispute_id), None)
        if not target:
            raise ValueError("争议不存在：" + dispute_id)
        updated = {**target, "decision": decision, "decided_on": decided_on}
        self._append_raw("disputes", updated)
        self.store.append_ledger(
            LEDGER_ACTIONS["DISPUTE_DECIDED"], "正式裁定",
            {"dispute_id": dispute_id, "decision": decision,
             "decided_on": decided_on,
             "note": "改判只改变归责，不增加任何学生的实际阅读经历"})
        run_id = self.reprocess(note=f"争议 {dispute_id} 正式裁定")
        return run_id

    def _append_raw(self, table, record):
        # 以新批次写同 id 记录；get_raw 取最新批次，旧批次仍可追溯。
        cur = self.store.con.execute(
            "INSERT INTO import_batches(created_at,source_path,sha256,table_counts)"
            " VALUES(?,?,?,?)",
            (json_default_now(), "engine:" + table, None,
             json.dumps({table: 1}, ensure_ascii=False)))
        batch_id = cur.lastrowid
        self.store.con.execute(
            "INSERT INTO raw_records(table_name,record_id,batch_id,payload)"
            " VALUES(?,?,?,?)",
            (table, record["id"], batch_id, json.dumps(record, ensure_ascii=False)))
        self.store.con.commit()

    # ---------------- 申请与结算 ----------------
    def _reconcile_claims(self, run_id):
        """把原始 claims 同步为办理状态，并按当前事实核算每笔申请。

        分配模型（修复小申请套池、重复兑现、重复容忍三类问题）：
        * 每个合同维护一条累计平台分钟曲线，容忍/封顶在曲线上只作用一次；
        * 申请按“已结优先、提交日优先”排序，每笔只就自己新增且未兑现的损失，
          取累计曲线的边际抵扣（credit.cumulative_increment）；
        * loss_key 全局只兑现一次，已在更早申请兑现的损失标记 PAID_ELSEWHERE；
        * 已结金额冻结，不被重算覆盖；当前应得与冻结额不符时另开调整单。
        """
        data = self._tables()
        contracts_by_id = {c["id"]: c for c in data["contracts"]}
        incidents_by_id = {i["id"]: i for i in data["incidents"]}
        adjudications = attribution.adjudicate_incidents(
            data["incidents"], data["disputes"])
        persisted = {c["claim_id"]: c for c in self.store.all_claims()}
        existing_adj = {a["claim_id"] for a in self.store.adjustments()}
        prior_runs = [r for r in self.store.list_runs() if r["id"] != run_id]

        # 每个合同当前事实下的平台损失总池（容忍/封顶只作用一次）。
        pools = {}
        for cid, contract in contracts_by_id.items():
            incident_ids = [iid for iid, inc in incidents_by_id.items()
                            if contract["school_id"] in inc["school_ids"]]
            minutes, _, _ = credit.platform_loss_minutes(
                incident_ids, incidents_by_id, adjudications, contract)
            pools[cid] = credit.contract_credit_pool(contract, minutes)

        # 按合同分组，组内已结优先、提交日优先。
        by_contract = {}
        for raw_claim in data["claims"]:
            by_contract.setdefault(raw_claim["contract_id"], []).append(raw_claim)

        losses_out, settlement_view = [], {}
        for contract_id, claims in by_contract.items():
            contract = contracts_by_id[contract_id]
            locked_keys = set()        # 本合同已兑现的 loss_key（整段标识）
            locked_segments = []       # 本合同已兑现的真实时间区间（用于部分重叠去重）
            cumulative_minutes = 0.0   # 已认定平台分钟（驱动累计抵扣曲线）
            ordered = sorted(claims, key=lambda c: (
                c["settled_on"] is None, c["submitted_on"]))

            for raw_claim in ordered:
                cid = raw_claim["id"]
                prior = persisted.get(cid)
                ev = credit.evaluate_claim(
                    {"claim_id": cid, "contract_id": contract_id,
                     "level": raw_claim["level"],
                     "incident_ids": raw_claim["incident_ids"]},
                    contracts_by_id, incidents_by_id, adjudications, locked_keys)

                # 本申请的平台损失段，减去已兑现时间区间后，才是真正净增损失。
                claim_platform_segments = []
                for item in ev["line_items"]:
                    if ev["details"][item["incident_id"]]["counted_platform"]:
                        claim_platform_segments.append(
                            (tu.parse_dt(item["segment"][0]),
                             tu.parse_dt(item["segment"][1])))
                claim_union, _ = tu.union_minutes(claim_platform_segments)
                fresh_segments = tu.subtract_union(
                    claim_platform_segments, locked_segments)
                new_minutes, _ = tu.union_minutes(fresh_segments)
                new_minutes = round(new_minutes, 3)
                legitimate_due = credit.cumulative_increment(
                    contract, cumulative_minutes, new_minutes)

                was_settled = bool(raw_claim["settled_on"]) or (
                    prior and prior["status"] == CLAIM_STATUS["SETTLED"])
                duplicate_settled = was_settled and new_minutes == 0.0 and \
                    any(li["already_paid"] for li in ev["line_items"])

                if was_settled:
                    status = CLAIM_STATUS["SETTLED"]
                    frozen_paid = float(raw_claim["paid_credit"]) \
                        if raw_claim["settled_on"] else float(prior["paid_credit"])
                    settled_on = raw_claim["settled_on"] or \
                        (prior.get("settled_on") if prior else None)
                    # 冻结金额保留展示；重算不覆盖，差额走调整单。
                    due = frozen_paid
                    # 已结申请覆盖到的平台损失全部锁定，累计分钟按当前事实推进。
                    locked_segments.extend(claim_platform_segments)
                    for item in ev["line_items"]:
                        if ev["details"][item["incident_id"]]["counted_platform"]:
                            locked_keys.add(item["loss_key"])
                    cumulative_minutes = round(
                        cumulative_minutes + new_minutes, 3)
                elif ev["has_pending"]:
                    status, due = CLAIM_STATUS["IN_REVIEW"], 0.0
                elif ev["line_items"] and all(
                        li["already_paid"] or not
                        ev["details"][li["incident_id"]]["counted_platform"]
                        for li in ev["line_items"]) and any(
                        li["already_paid"] for li in ev["line_items"]):
                    status, due = CLAIM_STATUS["SUPERSEDED"], 0.0
                else:
                    status = CLAIM_STATUS["SUBMITTED"]
                    due = legitimate_due  # 只就本申请新增损失取边际抵扣

                settlement_view[cid] = {
                    "status": status, "frozen_paid": frozen_paid if was_settled else 0.0,
                    "legitimate_due": legitimate_due, "was_settled": was_settled,
                    "new_minutes": new_minutes,
                    "duplicate_settled": duplicate_settled,
                    "contract_id": contract_id}

                self.store.upsert_claim({
                    "claim_id": cid, "level": raw_claim["level"],
                    "contract_id": contract_id,
                    "incident_ids": raw_claim["incident_ids"], "status": status,
                    "submitted_on": raw_claim["submitted_on"],
                    "settled_on": settled_on if was_settled
                    else raw_claim["settled_on"],
                    "paid_credit": due if was_settled else raw_claim["paid_credit"]},
                    run_id)
                for item in ev["line_items"]:
                    d = ev["details"][item["incident_id"]]
                    seg_minutes = round(
                        (tu.parse_dt(item["segment"][1])
                         - tu.parse_dt(item["segment"][0])).total_seconds() / 60.0, 3)
                    line_status = "PAID_ELSEWHERE" if item["already_paid"] else status
                    losses_out.append({
                        "claim_id": cid, "contract_id": contract_id,
                        "incident_id": item["incident_id"],
                        "loss_key": item["loss_key"], "open_minutes": seg_minutes,
                        "platform_minutes": seg_minutes if d["counted_platform"] else 0.0,
                        "payable_credit": due, "status": line_status})

        self.store.replace_losses(run_id, losses_out)
        if prior_runs:  # 首次运行建立基线，不生成“后续调整单”
            self._settlement_adjustments(run_id, settlement_view)

    def _settlement_adjustments(self, run_id, settlement_view):
        """已结抵扣遇新事实：冻结金额不覆盖，按当前边际口径开可追查调整单。

        统一适用于原始资料已结与系统内结算（系统结算同样进入 settlement_view）。
        容忍/封顶已在累计边际里只作用一次，这里不再对单笔重新扣费。
        """
        existing_adj = {a["claim_id"] for a in self.store.adjustments()}
        for cid, view in settlement_view.items():
            if not view["was_settled"] or cid in existing_adj:
                continue
            paid, due_now = view["frozen_paid"], view["legitimate_due"]
            delta = round(due_now - paid, 2)
            if delta == 0:
                continue
            self.store.add_adjustment({
                "claim_id": cid, "contract_id": view["contract_id"],
                "reason": "争议改判/新事实导致合同口径重算，原结算金额冻结",
                "change_kind": "AMOUNT_ADJUSTMENT",
                "delta": delta, "prior_paid": paid,
                "new_total": round(paid + delta, 2),
                "related_decision_id": cid + "-ADJ1"}, run_id)
            self.store.add_change(run_id, "AMOUNT_ADJUSTMENT",
                                  [view["contract_id"], cid],
                                  {"frozen_paid": paid, "delta": delta,
                                   "new_total": round(paid + delta, 2)})

    def settle_claim(self, claim_id, settled_on):
        """正式结算在办申请：按累计边际口径支付，并锁定损失 key。"""
        claim = self.store.get_claim(claim_id)
        if not claim:
            raise ValueError("申请不存在：" + claim_id)
        if claim["status"] == CLAIM_STATUS["SETTLED"]:
            raise ValueError("申请已结算，金额不可覆盖；请走调整流程")
        if claim["status"] == CLAIM_STATUS["SUPERSEDED"]:
            raise ValueError("申请损失已在别处兑现，不能重复领取")
        if claim["status"] == CLAIM_STATUS["IN_REVIEW"]:
            raise ValueError("争议尚未裁定，不能在归责未定时结算；请先完成裁定")

        # 金额只取最新一次 reconcile 的损失行，避免历史 run 的高值污染。
        latest_run_id = self.store.latest_run("reprocess")["id"]
        lines = [l for l in self.store.losses_for_contract(claim["contract_id"])
                 if l["claim_id"] == claim_id
                 and l["run_id"] == latest_run_id
                 and l["status"] != "PAID_ELSEWHERE"]
        amount = round(max((l["payable_credit"] for l in lines), default=0.0), 2)
        self.store.upsert_claim({**claim, "status": CLAIM_STATUS["SETTLED"],
                                 "settled_on": settled_on, "paid_credit": amount},
                                latest_run_id)
        self.store.append_ledger(
            LEDGER_ACTIONS["CLAIM_SETTLED"], "采购员",
            {"claim_id": claim_id, "amount": amount,
             "note": "累计边际抵扣内支付；loss_key 已去重，不与学校/区域申请重复"})
        self.reprocess(note=f"结算申请 {claim_id}")
        return amount


def json_default_now():
    from datetime import datetime
    return datetime.now().astimezone().isoformat()
