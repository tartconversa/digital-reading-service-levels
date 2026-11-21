"""口径规则单元测试：时间校正、阅读经历、区间去重与合同抵扣的纯函数不变量。"""
import unittest

from reading_accounting import timeutil as tu
from reading_accounting import usage, credit
from reading_accounting.domain import rule_versions

RULE = next(r for r in rule_versions() if r["version"] == "v1.0")
CONTRACT = {
    "id": "KT", "school_id": "S1", "from_on": "2026-04-01", "to_on": "2026-05-01",
    "seats": 20, "concurrent": 8, "price": 1200, "open_start": "14:00",
    "open_end": "17:00", "allowable_outage_minutes": 60, "credit_per_minute": 2,
}


def ev(eid, device, person, at, received, kind, pos, fg, device_at=None):
    return {"id": eid, "device_id": device, "content_id": "BK",
            "device_at": device_at or at, "received_at": received, "kind": kind,
            "position": pos, "foreground": fg, "person_id": person}


class ClockTests(unittest.TestCase):
    def test_correction_uses_latest_applicable_offset(self):
        dev = {"id": "D", "clock_offset_seconds": 0}
        corrections = [
            {"id": "C1", "effective_from": "2026-04-01T00:00:00+08:00",
             "offset_seconds": 3600},
        ]
        device_at = tu.parse_dt("2026-04-10T14:00:00+08:00")
        true_at, cid = tu.apply_clock_correction(device_at, 0, corrections)
        self.assertEqual(true_at, tu.parse_dt("2026-04-10T13:00:00+08:00"))
        self.assertEqual(cid, "C1")

    def test_falls_back_to_initial_offset(self):
        device_at = tu.parse_dt("2026-03-01T14:00:00+08:00")
        corrections = [
            {"id": "C1", "effective_from": "2026-04-01T00:00:00+08:00",
             "offset_seconds": 3600}]
        true_at, cid = tu.apply_clock_correction(device_at, 120, corrections)
        self.assertEqual(true_at, tu.parse_dt("2026-03-01T13:58:00+08:00"))
        self.assertIsNone(cid)


class IntervalTests(unittest.TestCase):
    def test_union_merges_overlap_once(self):
        s = tu.parse_dt("2026-04-15T14:00:00+08:00")
        intervals = [
            (s, tu.parse_dt("2026-04-15T16:30:00+08:00")),
            (tu.parse_dt("2026-04-15T15:00:00+08:00"),
             tu.parse_dt("2026-04-15T17:00:00+08:00")),
        ]
        minutes, merged = tu.union_minutes(intervals)
        self.assertEqual(minutes, 180.0)  # 14:00-17:00，重叠只计一次
        self.assertEqual(len(merged), 1)

    def test_intersect_with_open_window(self):
        seg = [(tu.parse_dt("2026-04-15T13:00:00+08:00"),
                tu.parse_dt("2026-04-15T15:00:00+08:00"))]
        parts = tu.intersect_with_open(seg, CONTRACT)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][0], tu.parse_dt("2026-04-15T14:00:00+08:00"))


class UsageRuleTests(unittest.TestCase):
    def _people(self):
        return [{"id": "P1", "school_id": "S1"}]

    def test_foreground_required_and_three_lenses(self):
        events = [
            ev("E1", "D1", "P1", "2026-04-01T14:00:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.1, True),
            ev("E2", "D1", "P1", "2026-04-01T14:05:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.5, True),
            ev("E3", "D1", "P1", "2026-04-01T14:10:00+08:00",
               "2026-04-01T18:00:00+08:00", "audio", 0.9, False),  # 后台音频不计
        ]
        corrected = usage.correct_events(events, [{"id": "D1", "person_id": "P1",
                                                   "clock_offset_seconds": 0}], {})
        exps = usage.build_experiences(corrected, self._people(), RULE)
        self.assertEqual(len(exps), 1)
        x = exps[0]
        self.assertEqual(x["page_seconds"], 300.0)   # 两个前台 page 点间隔
        self.assertEqual(x["audio_seconds"], 0.0)    # 后台音频不产生停留
        self.assertGreater(x["net_progress"], 0.0)

    def test_rewind_does_not_reduce_progress(self):
        events = [
            ev("E1", "D1", "P1", "2026-04-01T14:00:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.8, True),
            ev("E2", "D1", "P1", "2026-04-01T14:05:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.3, True),  # 回退
            ev("E3", "D1", "P1", "2026-04-01T14:10:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.9, True),
        ]
        corrected = usage.correct_events(events, [{"id": "D1", "person_id": "P1",
                                                   "clock_offset_seconds": 0}], {})
        x = usage.build_experiences(corrected, self._people(), RULE)[0]
        self.assertEqual(x["rewinds"], 1)
        # 高水位 0.8：回退到 0.3 不扣减已取得进度；之后到 0.9 只净增 0.1。
        self.assertAlmostEqual(x["net_progress"], 0.9, places=4)

    def test_inactivity_gap_splits_and_not_counted(self):
        events = [
            ev("E1", "D1", "P1", "2026-04-01T14:00:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.1, True),
            ev("E2", "D1", "P1", "2026-04-01T15:00:00+08:00",
               "2026-04-01T18:00:00+08:00", "page", 0.4, True),  # 间隔 60 分钟
        ]
        corrected = usage.correct_events(events, [{"id": "D1", "person_id": "P1",
                                                   "clock_offset_seconds": 0}], {})
        x = usage.build_experiences(corrected, self._people(), RULE)[0]
        self.assertEqual(x["active_seconds"], 0.0)  # 超 30 分钟无活动，不计时长

    def test_cross_device_overlap_not_double_counted(self):
        events = []
        for i, dev in enumerate(["D1", "D2"]):
            events += [
                ev(f"{dev}a", dev, "P1", "2026-04-01T14:00:00+08:00",
                   "2026-04-01T18:00:00+08:00", "page", 0.1, True),
                ev(f"{dev}b", dev, "P1", "2026-04-01T14:10:00+08:00",
                   "2026-04-01T18:00:00+08:00", "page", 0.9, True),
            ]
        devices = [{"id": "D1", "person_id": "P1", "clock_offset_seconds": 0},
                   {"id": "D2", "person_id": "P1", "clock_offset_seconds": 0}]
        corrected = usage.correct_events(events, devices, {})
        x = usage.build_experiences(corrected, self._people(), RULE)[0]
        self.assertEqual(set(x["devices"]), {"D1", "D2"})
        self.assertEqual(x["active_seconds"], 600.0)  # 重叠时段只计一次


class CreditTests(unittest.TestCase):
    def test_allowance_then_cap(self):
        # 200 分钟：减 60 容忍 = 140 × 2 = 280，未触顶。
        payable, chargeable, cap = credit.credit_for_minutes(200, CONTRACT)
        self.assertEqual(chargeable, 140.0)
        self.assertEqual(payable, 280.0)
        self.assertEqual(cap, 1200.0)

    def test_cap_ceils_at_contract_price(self):
        payable, _, _ = credit.credit_for_minutes(100000, CONTRACT)
        self.assertEqual(payable, 1200.0)

    def test_loss_key_stable_and_unique(self):
        seg = ("2026-04-15T14:00:00+08:00", "2026-04-15T16:30:00+08:00")
        k1 = credit.loss_key("K2", "I1", seg)
        k2 = credit.loss_key("K2", "I1", seg)
        self.assertEqual(k1, k2)
        self.assertIn("K2|I1|", k1)


class SlaPendingTests(unittest.TestCase):
    def test_pending_dispute_excluded_from_sla_fault(self):
        from reading_accounting.metrics import period_metric
        attempts = [
            {"contract_id": "KT", "person_id": "P1", "request_id": "R1",
             "category": "SUCCESS", "in_open": True, "provisional": False},
            {"contract_id": "KT", "person_id": "P2", "request_id": "R2",
             "category": "PLATFORM_FAULT", "in_open": True, "provisional": False},
            {"contract_id": "KT", "person_id": "P3", "request_id": "R3",
             "category": "PENDING_DISPUTE", "in_open": True, "provisional": True},
        ]
        school = {"id": "S1", "_people": ["P1", "P2", "P3"]}
        m = period_metric(CONTRACT, school, attempts, {}, {}, {},
                          "v1.0")
        # SLA 分母剔除挂起请求后为 2 条，1 条平台故障 -> 0.5，而非被挂起拉到 1/3。
        self.assertEqual(m["sla_availability"], 0.5)
        self.assertEqual(m["sla_pending_dispute_requests"], 1)
        self.assertTrue(m["provisional"])
        # 总体成功率仍把全部请求计入，失败不消失。
        self.assertEqual(m["request_success_rate"], round(1 / 3, 4))


if __name__ == "__main__":
    unittest.main()
