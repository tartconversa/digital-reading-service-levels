"""端到端业务测试：晚回传、时钟订正、重叠损失申请在同一过程中办理。

三类变更必须可区分：
  USAGE_REVISION   使用量修订（时钟订正/晚回传改变阅读证据）
  ATTRIBUTION_CHANGE 归责变化（争议改判，不增加实际阅读）
  AMOUNT_ADJUSTMENT 金额调整（合同口径重算后的钱变化）
"""
import unittest
from collections import Counter

from tests._helpers import FreshEngine

LATE_PEOPLE = {"P018", "P019", "P038", "P057", "P076", "P095"}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FreshEngine()
        self.eng = self.ctx.__enter__()
        self.store = self.eng.store

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def _changes(self, run_id):
        return Counter(c["change_kind"] for c in self.store.changes(run_id))

    def test_failed_requests_never_disappear(self):
        """240 条访问请求全部具名分类，48 条失败一条都不能少。"""
        run = self.store.latest_run("reprocess")
        attempts = self.store.attempts_at_run(run["id"])
        self.assertEqual(len(attempts), 240)
        cats = Counter(a["category"] for a in attempts)
        self.assertEqual(sum(v for k, v in cats.items() if k != "SUCCESS"), 48)
        # 每条都有合同与人，可回溯到原始 request_id。
        self.assertTrue(all(a["contract_id"] and a["person_id"] for a in attempts))

    def test_three_usage_lenses_are_separate(self):
        """音频听读、页面停留、实际进度分别报告，登录/停留不冒充阅读。"""
        run = self.store.latest_run("reprocess")
        exps = self.store.experiences_at_run(run["id"])
        sample = next(x for x in exps if x["person_id"] == "P001")
        self.assertIn("audio_seconds", sample)
        self.assertIn("page_seconds", sample)
        self.assertIn("net_progress", sample)
        # position 是进度不是时长：净进度被限制在 [0,1]，不随停留膨胀。
        self.assertTrue(all(0.0 <= x["net_progress"] <= 1.0 for x in exps))

    def test_unknown_duration_not_zero_filled(self):
        """未知时长单列、不补零：只在整条时间线末尾出现，且数量等于人数。"""
        run = self.store.latest_run("reprocess")
        exps = self.store.experiences_at_run(run["id"])
        unknown = [x for x in exps if x["unknown_seconds"] > 0]
        self.assertEqual(len(unknown), 96)  # 每人仅末日 1 个未知尾点
        self.assertTrue(all(x["day"] == "2026-04-28" for x in unknown))

    def test_late_backfill_keeps_rates_provisional(self):
        """晚回传证据在周期成熟后到达，受影响使用率必须标未成熟。"""
        run = self.store.latest_run("reprocess")
        exps = self.store.experiences_at_run(run["id"])
        prov_people = {x["person_id"] for x in exps if x["provisional"]}
        self.assertEqual(prov_people, LATE_PEOPLE)
        metric = next(m for m in self.store.latest_metrics()
                      if m["contract_id"] == "K1")
        self.assertTrue(metric["provisional"])

    def test_clock_correction_moves_experience_out_of_open_window(self):
        """D19 的 +3600s 订正把 P019 部分经历移出开放窗，但不抹掉阅读事实。"""
        run = self.store.latest_run("reprocess")
        # 当前事实（含 5/10 才收到的校正）。
        cur = [x for x in self.store.experiences_at_run(run["id"], "P019")]
        corrected_day = next(x for x in cur if x["correction_ids"])
        self.assertLess(corrected_day["in_open_active_seconds"],
                        corrected_day["active_seconds"])

        # 历史时点（4/30）校正尚未收到：同样经历全部落在开放窗内。
        hist_run = self.eng.reprocess(as_of="2026-04-30", note="历史时点")
        hist = self.store.experiences_at_run(hist_run, "P019")
        self.assertTrue(all(x["in_open_active_seconds"] == x["active_seconds"]
                            for x in hist))
        self.assertTrue(all(not x["correction_ids"] for x in hist))

    def test_usage_revision_scoped_to_affected_people(self):
        """时钟订正/晚回传只登记受影响 6 人的使用量修订，不波及全校。"""
        self.eng.reprocess(as_of="2026-04-30", note="历史时点")
        run_id = self.eng.reprocess(note="恢复当前事实")
        changes = self.store.changes(run_id)  # 只看目标 run
        usage_changes = [c for c in changes if c["change_kind"] == "USAGE_REVISION"]
        self.assertEqual(len(usage_changes), 1)
        detail = usage_changes[0]["detail"]
        self.assertEqual(set(detail["people"]), LATE_PEOPLE)
        kinds = {c["change_kind"] for c in changes}
        self.assertIn("USAGE_REVISION", kinds)

    def test_pending_dispute_blocks_credit_but_shows_preview(self):
        """争议未决：不提前按平台故障付抵扣，只给影响预览。"""
        self.eng.reopen_dispute("DP1", "复现未决办理")
        m = next(x for x in self.store.latest_metrics() if x["contract_id"] == "K2")
        # I2 暂不计平台损失：只剩 I1 的 150 分钟。
        self.assertEqual(m["platform_loss_minutes"], 150.0)
        self.assertEqual(m["credit_payable_now"], 180.0)
        # 预览：若 I2 改判平台，新增 30 分钟、60 元。
        self.assertEqual(m["dispute_preview"]["minutes_delta"], 30.0)
        self.assertEqual(m["dispute_preview"]["credit_delta"], 60.0)
        # SLA 字段始终显式披露挂起请求数（本数据的请求均早于 I2 起点，故为 0）；
        # 周期因损失争议未决而标未成熟，不提前按平台故障兑现。
        self.assertIn("sla_pending_dispute_requests", m)
        self.assertEqual(m["sla_pending_dispute_requests"], 0)
        self.assertTrue(m["provisional"])
        claim = self.store.get_claim("CL2")
        self.assertIn("审议", claim["status"])

    def test_dispute_decision_changes_attribution_not_reading(self):
        """争议改判只改变归责与金额，不增加任何阅读经历。"""
        self.eng.reopen_dispute("DP1", "先挂起")
        before = len(self.store.experiences_at_run(
            self.store.latest_run("reprocess")["id"]))
        run_id = self.eng.decide_dispute("DP1", "platform", "2026-05-12")
        after = len(self.store.experiences_at_run(
            self.store.latest_run("reprocess")["id"]))
        self.assertEqual(before, after)  # 阅读证据零变化

        kinds = self._changes(run_id)
        self.assertIn("ATTRIBUTION_CHANGE", kinds)
        self.assertIn("AMOUNT_ADJUSTMENT", kinds)
        self.assertNotIn("USAGE_REVISION", kinds)  # 改判不等于学生读得更多

        m = next(x for x in self.store.latest_metrics() if x["contract_id"] == "K2")
        self.assertEqual(m["platform_loss_minutes"], 180.0)
        self.assertEqual(m["credit_payable_now"], 240.0)

    def test_overlapping_claims_not_double_paid(self):
        """同一 I1 损失在学校 CL1 与区域 CL2 之间只能兑现一次（时间占用去重）。"""
        # CL1 已结、锁定 I1（14:00-16:30，150 分钟）。
        # CL2 的 I2（15:00-17:00）与 I1 重叠 90 分钟，净增仅 30 分钟，
        # 边际抵扣 = credit(180)-credit(150) = 240-180 = 60 元。
        amount = self.eng.settle_claim("CL2", "2026-05-15")
        self.assertEqual(amount, 60.0)  # 只为净增的 30 分钟付费，不重复领 I1
        latest_run = self.store.latest_run("reprocess")["id"]
        lines = [l for l in self.store.losses_for_contract("K2")
                 if l["claim_id"] == "CL2" and l["run_id"] == latest_run]
        i1 = [l for l in lines if l["incident_id"] == "I1"]
        self.assertTrue(all(l["status"] == "PAID_ELSEWHERE" for l in i1))
        # 合同累计支付 = CL1 冻结 120 + CL2 边际 60 = 180，不超合同池 240。
        total = sum(c["paid_credit"] for c in self.store.all_claims())
        self.assertEqual(total, 180.0)

    def test_in_review_claim_cannot_be_settled_before_decision(self):
        """争议未决的审议中申请不得结算，避免在归责未定时付款。"""
        self.eng.reopen_dispute("DP1", "挂起")
        claim = self.store.get_claim("CL2")
        self.assertIn("审议", claim["status"])
        with self.assertRaises(ValueError):
            self.eng.settle_claim("CL2", "2026-05-15")

    def test_settled_amount_frozen_then_adjustment_traceable(self):
        """已结金额不被覆盖；新事实只产生可追查调整单。"""
        self.eng.reopen_dispute("DP1", "挂起")
        self.eng.decide_dispute("DP1", "platform", "2026-05-12")
        cl1 = self.store.get_claim("CL1")
        self.assertEqual(cl1["paid_credit"], 120.0)  # 原结算冻结
        adjustments = self.store.adjustments("CL1")
        self.assertEqual(len(adjustments), 1)
        adj = adjustments[0]
        self.assertEqual(adj["prior_paid"], 120.0)
        self.assertEqual(adj["delta"], 60.0)
        self.assertEqual(adj["new_total"], 180.0)  # 不覆盖，只追加差额

    def test_cannot_settle_twice(self):
        """已结算申请再次结算必须被拒绝，防止同一权利重复领取。"""
        with self.assertRaises(ValueError):
            self.eng.settle_claim("CL1", "2026-05-16")

    def test_contract_cap_and_allowance_apply_once(self):
        """容忍分钟与封顶按合同周期只作用一次，不在每笔申请上重复扣。"""
        m = next(x for x in self.store.latest_metrics() if x["contract_id"] == "K2")
        # 180 平台分钟 - 60 容忍 = 120 可计费 × 2 元 = 240，封顶 1200 不触发。
        self.assertEqual(m["chargeable_minutes"], 120.0)
        self.assertEqual(m["credit_payable_now"], 240.0)
        self.assertEqual(m["credit_cap"], 1200.0)

    def test_renewal_targets_concurrency_not_seats(self):
        """并发瓶颈下应建议提并发而非加座位，未用额度不一律说成浪费。"""
        from reading_accounting.metrics import renewal_recommendation
        data = self.eng._tables()
        contracts = {c["id"]: c for c in data["contracts"]}
        schools = {s["id"]: dict(s) for s in data["schools"]}
        for p in data["people"]:
            schools[p["school_id"]].setdefault("_people", []).append(p["id"])
        run = self.store.latest_run("reprocess")
        m = next(x for x in self.store.metrics_at_run(run["id"])
                 if x["contract_id"] == "K3")
        rec = renewal_recommendation(m, contracts["K3"], schools["C3"])
        self.assertGreater(rec["concurrency_denied"], 0)
        self.assertFalse(rec["seat_increase_would_help"])
        self.assertIn("并发", rec["recommendation"])

    def test_audit_trail_links_decisions(self):
        """正式决定全部追加进台账，可从结论追到决定与运行。"""
        self.eng.decide_dispute("DP1", "platform", "2026-05-12")
        actions = {e["action"] for e in self.store.ledger()}
        self.assertIn("争议正式裁定", actions)
        self.assertIn("原始资料导入", actions)
        self.assertIn("按新事实重算（修订使用量或归责）", actions)

    def test_rule_version_keeps_old_totals_queryable(self):
        """换口径只产生新版本快照，旧版本总数仍可查回，且登记受影响范围。"""
        base = self.store.get_rule("v1.0")
        new_version = "v1.1-rc1"
        self.store.upsert_rule({
            "version": new_version, "status": "approved",
            "params": {**base["params"], "min_active_seconds": 600},
            "notes": "测试提案", "proposed_on": "2026-05-20",
            "decided_on": "2026-05-20"}, "2026-05-20")
        run_id = self.eng.reprocess(rule_version=new_version, note="口径切换")
        # 新旧 run 各自带版本，旧 run 快照未被覆盖。
        runs = {r["id"]: r["rule_version"] for r in self.store.list_runs()}
        self.assertEqual(runs[run_id], new_version)
        old_run = min(runs)
        self.assertEqual(runs[old_run], "v1.0")
        self.assertEqual(len(self.store.metrics_at_run(old_run)), 6)
        self.assertEqual(len(self.store.metrics_at_run(run_id)), 6)
        rule_changes = [c for c in self.store.changes(run_id)
                        if c["detail"].get("kind") == "RULE_VERSION_CHANGE"]
        self.assertEqual(len(rule_changes), 1)


if __name__ == "__main__":
    unittest.main()
