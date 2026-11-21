"""用最小合成资料驱动 Engine，覆盖真实材料未包含的边界（多申请共享池等）。

所有数据在临时目录生成，与 materials/ 完全隔离；Engine 仍走完整导入-重算流程。
"""
import json
from pathlib import Path

from reading_accounting.engine import Engine


def _contract(cid="KX", allowance=60, rate=2, price=1200):
    return {"id": cid, "school_id": "SX", "from_on": "2026-04-01",
            "to_on": "2026-05-01", "seats": 20, "concurrent": 8, "price": price,
            "open_start": "14:00", "open_end": "17:00",
            "allowable_outage_minutes": allowance, "credit_per_minute": rate}


def _incident(iid, start, end, cause="platform", schools=("SX",)):
    return {"id": iid, "school_ids": list(schools), "start": start, "end": end,
            "claimed_cause": cause}


def build_engine(tmpdir, contracts, incidents, claims, disputes=None):
    """生成一份只含必要表的合成资料并导入，返回 Engine（已完成首次重算）。"""
    base = Path(tmpdir)
    data = {
        "schools": [{"id": "SX", "area": "测试", "devices": 4}],
        "people": [{"id": "P1", "school_id": "SX", "household": "H", "grade": 4,
                    "access_need": "text", "sample_weight": 1.0}],
        "contracts": contracts,
        "devices": [], "events": [], "clock_corrections": [],
        "access_requests": [], "incidents": incidents, "claims": claims,
        "disputes": disputes or [],
    }
    contract_meta = {
        "id": "合同", "school_id": "学校", "from_on": "起日", "to_on": "止日",
        "seats": "座位", "concurrent": "并发", "price": "金额",
        "open_start": "起时", "open_end": "止时",
        "allowable_outage_minutes": "容忍", "credit_per_minute": "单价"}
    tables = {}
    for name, rows in data.items():
        tables[name] = {"fields": list(rows[0].keys()) if rows else [],
                        "references": {}, "description": name}
    (base / "data.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (base / "contract.json").write_text(
        json.dumps({"synthetic": True, "tables": tables, "notes": []},
                   ensure_ascii=False), encoding="utf-8")
    eng = Engine(base / "case.db")
    eng.import_materials(base)
    return eng
