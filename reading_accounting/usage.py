"""使用口径引擎：把原始事件还原为可审议的阅读经历。

设计原则（对应需求逐条落地）：
* 登录次数/页面时长不代表阅读：只以前台事件构造停留，且分别报告音频听读秒、
  页面停留秒与净进度，不混成一个数。
* 设备时钟先校正再判断；晚回传与迟到校正支持点在时间复现（as_of）。
* 同一学生跨设备接续：按人合并时间线，重叠时段取并集，重复进度不膨胀时长。
* 无活动间隔（超过 inactivity_gap_seconds）切会话且不计时长；回退不扣减净
  进度、跳读不重复计；无法确定先后（同刻多记录）标顺序存疑，存疑段不重复累计。
* 未知时长单列 unknown_seconds，绝不补零；成熟期未到的经历/指标标 provisional。
"""
from __future__ import annotations

from datetime import timedelta

from . import timeutil as tu


def correct_events(events, devices_by_id, corrections_by_device, as_of=None):
    """返回带真实时间与校正来源的事件；as_of 过滤晚回传事件与迟到校正。"""
    devices = {d["id"]: d for d in devices_by_id}
    corrections = {}
    for c in tu.as_of_filter(
            [c for cs in corrections_by_device.values() for c in cs], as_of):
        corrections.setdefault(c["device_id"], []).append(c)
    out = []
    for e in tu.as_of_filter(events, as_of):
        dev = devices[e["device_id"]]
        device_at = tu.parse_dt(e["device_at"])
        true_at, corr_id = tu.apply_clock_correction(
            device_at, dev["clock_offset_seconds"],
            corrections.get(e["device_id"], []))
        out.append({
            **e,
            "true_at": true_at,
            "correction_id": corr_id,
            "person_id": dev["person_id"],
        })
    return out


def _kind_intervals(kind_events, max_dwell, gap_seconds):
    """单形态前台事件 → 停留半开区间；无活动间隔切分、停留封顶、存疑不计时长。"""
    intervals = []
    fg = [e for e in kind_events if e["foreground"]]
    for a, b in zip(fg, fg[1:]):
        gap = (b["true_at"] - a["true_at"]).total_seconds()
        if gap <= 0:  # 同刻或倒挂：顺序无法确定，存疑，不计时长
            continue
        if gap > gap_seconds:  # 无活动间隔：不认作阅读
            continue
        dwell = min(gap, max_dwell)
        intervals.append((a["true_at"],
                          a["true_at"] + timedelta(seconds=dwell), a["kind"]))
    return intervals


def _tail_unknown_points(timeline):
    """无尾点的前台停留时刻：该形态最后一个前台点之后，再无更晚的同形态前台点。

    后台事件不能界定前台停留的结束，因此只在同形态前台序列内判断；返回
    (设备本地阅读日, 真实时刻)，避免大时钟偏移下真实日与阅读日错位。
    """
    unknown = []
    for kind in ("audio", "page"):
        fg = sorted([e for e in timeline if e["foreground"] and e["kind"] == kind],
                    key=lambda e: (e["true_at"], e["id"]))
        if not fg:
            continue
        last_fg = fg[-1]
        later_fg = [e for e in fg if e["true_at"] > last_fg["true_at"]]
        if not later_fg:  # 之后没有同形态前台点，停留时长无法界定，不补零
            local_day = tu.parse_dt(last_fg["device_at"]).astimezone(
                tu.UTC8).date().isoformat()
            unknown.append((local_day, last_fg["true_at"]))
    return unknown


def _merge_kind_intervals(intervals):
    """同类区间先按设备/来源合并取并集（去重），返回秒数与并集区间。"""
    merged = []
    for s, e, _kind in sorted(intervals, key=lambda x: x[0]):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged, sum((e - s).total_seconds() for s, e in merged)


def _progress_stats(events, initial_high_water=0.0):
    """净进度、回退次数、跳读次数（按真实时间排序后的位置序列）。

    高水位可由跨日的同一 person×content 时间线传入，避免每天从 0 重置导致重复
    读取同一进度被重复累计。同刻多事件的先后不可知（已标 uncertain），不计
    回退/跳读噪声，但仍参与高水位（学生确实到达过该位置）。
    """
    ordered = sorted(events, key=lambda e: (e["true_at"], e["id"]))
    high_water = initial_high_water
    net = 0.0
    rewinds = skips = 0
    # 同一真实时间的多条记录先后不可知，折叠为一个时刻：取该刻位置的最大、
    # 最小值；高水位仍推进（学生确实到达过这些位置），但不产生回退/跳读噪声。
    moments = []
    for e in ordered:
        if moments and moments[-1][0] == e["true_at"]:
            moments[-1][1].append(e["position"])
        else:
            moments.append([e["true_at"], [e["position"]]])
    prev_max_pos = None
    for _moment, positions in moments:
        lo, hi = min(positions), max(positions)
        if prev_max_pos is not None:
            # 用相邻时刻的极值比较，同刻内部顺序不参与回退/跳读。
            if lo < prev_max_pos:
                rewinds += 1
            elif lo - prev_max_pos > 0.3:
                skips += 1
        if hi > high_water:
            net += hi - high_water
            high_water = hi
        prev_max_pos = hi
    return round(net, 6), rewinds, skips, high_water


def build_experiences(events, people_by_id, rule, as_of=None, maturity_day=None,
                      contracts_by_school=None):
    """生成阅读经历列表（含有效/无效、provisional、证据链）。"""
    params = rule["params"]
    gap = params["inactivity_gap_seconds"]
    max_dwell = params["max_dwell_seconds"]
    people = {p["id"]: p for p in people_by_id}
    contracts_by_school = contracts_by_school or {}

    # 先按“人 × 内容”组织完整时间线（跨设备、跨日），这样无活动切分与
    # 未知尾点判断才能看到后续事件；随后再按设备本地阅读日产出经历。
    timelines = {}
    for e in events:
        timelines.setdefault((e["person_id"], e["content_id"]), []).append(e)

    experiences = []
    for (person_id, content_id), timeline in sorted(timelines.items()):
        timeline = sorted(timeline, key=lambda e: (e["true_at"], e["id"]))
        person_school = people[person_id]["school_id"]
        contract = contracts_by_school.get(person_school)

        # 整条时间线上，每个形态最后一个前台点之后若再无“同形态前台”事件，
        # 其停留时长未知；后台事件不能充当尾点，故按形态前台序列判断。
        unknown_points = _tail_unknown_points(timeline)

        # 按设备本地日切分（时钟校正不拆阅读日），逐日构造经历。
        by_day = {}
        for e in timeline:
            local_day = tu.parse_dt(e["device_at"]).astimezone(tu.UTC8).date().isoformat()
            by_day.setdefault(local_day, []).append(e)

        timeline_high_water = 0.0  # 跨日延续：人×内容的净进度高水位
        for day, evs in sorted(by_day.items()):
            evs = sorted(evs, key=lambda e: (e["true_at"], e["id"]))
            stamps = [e["true_at"] for e in evs]
            uncertain = len(set(stamps)) != len(stamps)

            audio_in = _kind_intervals(
                [e for e in evs if e["kind"] == "audio"], max_dwell, gap)
            page_in = _kind_intervals(
                [e for e in evs if e["kind"] == "page"], max_dwell, gap)
            unknown = [p for (local_day, p) in unknown_points
                       if local_day == day]

            audio_union, audio_seconds = _merge_kind_intervals(audio_in)
            page_union, page_seconds = _merge_kind_intervals(page_in)
            # 跨设备/跨形态的活跃总时长：两类并集再取并，重叠不重复。
            active_minutes, active_union = tu.union_minutes(
                [(s, e) for s, e in audio_union + page_union])
            active_seconds = round(active_minutes * 60.0, 3)

            in_open_seconds = active_seconds
            if contract is not None:
                open_parts = tu.intersect_with_open(active_union, contract)
                in_open_seconds = round(
                    sum((e - s).total_seconds() for s, e in open_parts), 3)

            # 净进度沿整条人×内容时间线累计，跨日不重置，重复读取不重复计。
            net_progress, rewinds, skips, timeline_high_water = _progress_stats(
                evs, timeline_high_water)
            evidence_ids = sorted(e["id"] for e in evs)
            corrected_by = sorted({e["correction_id"] for e in evs
                                   if e["correction_id"]})
            last_received = max(tu.parse_dt(e["received_at"]) for e in evs)

            provisional = False
            if maturity_day is not None:
                cutoff = tu.parse_dt(maturity_day + "T23:59:59+08:00")
                if last_received > cutoff:
                    provisional = True  # 证据在成熟时点之后才到，使用率还会变化
            if as_of is not None:
                provisional = True      # 历史点在时间复现，天然未成熟

            is_valid = (active_seconds >= params["min_active_seconds"]
                        or net_progress >= params["min_net_progress"])
            experiences.append({
                "person_id": person_id,
                "school_id": person_school,
                "content_id": content_id,
                "day": day,
                "start_iso": tu.iso(evs[0]["true_at"]),
                "end_iso": tu.iso(evs[-1]["true_at"]),
                "devices": sorted({e["device_id"] for e in evs}),
                "audio_seconds": round(audio_seconds, 3),
                "page_seconds": round(page_seconds, 3),
                "active_seconds": active_seconds,
                "in_open_active_seconds": in_open_seconds,
                "net_progress": net_progress,
                "rewinds": rewinds,
                "skips": skips,
                "unknown_seconds": float(len(unknown)),  # 未知尾点个数（不补零）
                "uncertain": uncertain,
                "is_valid": is_valid,
                "provisional": provisional,
                "correction_ids": corrected_by,
                "evidence_ids": evidence_ids,
                "rule_version": rule["version"],
            })
    return experiences


def summarize_person_coverage(experiences, people):
    """按人汇总：有效覆盖人数、听读/页面/进度口径、provisional 人数。"""
    by_person = {}
    for x in experiences:
        p = by_person.setdefault(x["person_id"], {
            "active_seconds": 0.0, "audio_seconds": 0.0,
            "page_seconds": 0.0, "net_progress": 0.0,
            "valid_days": 0, "provisional": False, "unknown_seconds": 0.0,
        })
        p["active_seconds"] += x["active_seconds"]
        p["audio_seconds"] += x["audio_seconds"]
        p["page_seconds"] += x["page_seconds"]
        p["net_progress"] += x["net_progress"]
        p["unknown_seconds"] += x["unknown_seconds"]
        if x["is_valid"]:
            p["valid_days"] += 1
        if x["provisional"]:
            p["provisional"] = True
    return by_person
