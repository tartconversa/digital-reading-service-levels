"""时间口径：ISO8601 解析、设备时钟校正、开放区间与区间交并集。

真实时间统一用带时区的 aware datetime；设备时间必须先校正为真实时间后，
才能判断是否落在合同开放时段或故障区间。所有区间一律半开 [start, end)。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC8 = timezone(timedelta(hours=8))


def parse_dt(value):
    """解析带时区 ISO8601；缺省时按 +08:00 处理并明确标注来源。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC8)
    return dt


def parse_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(value[:10])


def parse_clock(value):
    h, m = [int(x) for x in value.split(":")]
    return time(h, m)


def iso(dt):
    return dt.isoformat()


def apply_clock_correction(device_at, initial_offset_seconds, corrections):
    """把设备时间校正为真实时间。

    校正规则（可审议、写入口径）：取 effective_from <= 设备时间 的最新一条
    clock_corrections 记录的 offset_seconds；没有适用记录时用设备初始
    clock_offset_seconds。offset 为“终端比真实时间快的秒数”，因此
    真实时间 = 设备时间 - offset。

    返回 (真实时间, 适用校正id或None)。
    """
    applicable = None
    for c in corrections:
        eff = parse_dt(c["effective_from"])
        if eff <= device_at:
            if applicable is None or parse_dt(applicable["effective_from"]) < eff:
                applicable = c
    offset = applicable["offset_seconds"] if applicable else initial_offset_seconds
    return device_at - timedelta(seconds=offset), (applicable["id"] if applicable else None)


def open_window_for_day(contract, day):
    """某一自然日的合同开放半开区间；非周期内的日子返回 None。"""
    start_day = parse_day(contract["from_on"])
    end_day = parse_day(contract["to_on"])  # 不含
    if not (start_day <= day < end_day):
        return None
    start = datetime.combine(day, parse_clock(contract["open_start"]), tzinfo=UTC8)
    end = datetime.combine(day, parse_clock(contract["open_end"]), tzinfo=UTC8)
    return start, end


def in_open_window(moment, contract):
    win = open_window_for_day(contract, moment.astimezone(UTC8).date())
    if win is None:
        return False
    start, end = win
    return start <= moment < end


def overlap(a_start, a_end, b_start, b_end):
    """两个半开区间的交集（秒数与区间），无交集返回 0 与 None。"""
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    if s < e:
        return (e - s).total_seconds(), (s, e)
    return 0, None


def union_minutes(intervals):
    """把若干半开区间合并，返回总分钟（float）与合并后的区间列表。

    重叠损失只计一次：同一合同周期内多个中断/申请的重叠部分由这里去重。
    """
    if not intervals:
        return 0.0, []
    ordered = sorted(intervals, key=lambda x: x[0])
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    total = sum((e - s).total_seconds() for s, e in merged) / 60.0
    return total, [(s, e) for s, e in merged]


def subtract_union(intervals, occupied):
    """从 intervals 中减去 occupied（两者都先各自合并），返回剩余半开区间。

    用于按时间占用去重：新中断与已兑现中断部分重叠时，只保留尚未兑现的时间段，
    而不是简单按整段 loss_key 是否相等来判断。
    """
    _, occ_merged = union_minutes(occupied)
    remaining = []
    for s, e in intervals:
        cur_s = s
        for os_, oe in occ_merged:
            if oe <= cur_s:
                continue
            if os_ >= e:
                break
            if os_ > cur_s:
                remaining.append((cur_s, min(os_, e)))
            cur_s = max(cur_s, oe)
            if cur_s >= e:
                break
        if cur_s < e:
            remaining.append((cur_s, e))
    _, out = union_minutes(remaining)
    return out


def intersect_with_open(intervals, contract):
    """把若干真实时间区间逐段与合同开放窗口取交集。"""
    out = []
    days = set()
    for s, e in intervals:
        days.update(_days_between(s, e))
    for day in sorted(days):
        win = open_window_for_day(contract, day)
        if win is None:
            continue
        for s, e in intervals:
            secs, part = overlap(s, e, win[0], win[1])
            if part:
                out.append(part)
    return out


def _days_between(start, end):
    d0 = start.astimezone(UTC8).date()
    d1 = end.astimezone(UTC8).date()
    cur = d0
    while cur <= d1:
        yield cur
        cur += timedelta(days=1)


def contract_period(contract):
    return parse_day(contract["from_on"]), parse_day(contract["to_on"])


def within_period(moment, contract):
    start, end = contract_period(contract)
    d = moment.astimezone(UTC8).date()
    return start <= d < end


def as_of_filter(records, as_of):
    """点在时间（as_of，含当日 23:59:59）之前已接收的记录。

    晚回传与迟到校正据此被排除在历史时点之外，使“当时看到的数”可复现；
    None 表示取当前全部资料。
    """
    if as_of is None:
        return list(records)
    if isinstance(as_of, str):
        as_of = parse_day(as_of)
    cutoff = datetime.combine(as_of, time(23, 59, 59), tzinfo=UTC8)
    kept = []
    for r in records:
        key = r.get("received_at") or r.get("received_on")
        if key is None:
            kept.append(r)
            continue
        stamp = parse_dt(key) if "T" in key else datetime.combine(
            parse_day(key), time(23, 59, 59), tzinfo=UTC8)
        if stamp <= cutoff:
            kept.append(r)
    return kept
