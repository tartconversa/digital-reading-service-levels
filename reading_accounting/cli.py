"""数字阅读使用与授权服务核算 —— 本地命令行。

所有操作只读写本地 SQLite（默认 state/reading.db），原始资料目录保持只读。
输出默认中文表格；--json 输出机读结果，便于复核与留档。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine
from .metrics import renewal_recommendation

DEFAULT_DB = Path(__file__).resolve().parents[1] / "state" / "reading.db"


def _print(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(obj, str):
        print(obj)
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = list(obj[0].keys())
        print("\t".join(keys))
        for row in obj:
            print("\t".join(str(row.get(k, "")) for k in keys))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def build_parser():
    p = argparse.ArgumentParser(
        prog="reading", description="数字阅读使用与授权服务核算（本地）")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 状态库路径")
    p.add_argument("--json", action="store_true", help="机读 JSON 输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="导入合成资料（重复指纹自动去重）")
    s.add_argument("materials_dir")

    s = sub.add_parser("rules", help="查询口径版本与受影响运行范围")
    s.add_argument("--propose", help="按 JSON 文件提出新口径参数（不立即生效）")
    s.add_argument("--approve", help="批准指定提案版本使其生效")

    s = sub.add_parser("reprocess", help="按新事实重算（可给 as_of 复现历史时点）")
    s.add_argument("--as-of", dest="as_of", help="YYYY-MM-DD，只采用该日前收到的资料")
    s.add_argument("--note", default="")

    s = sub.add_parser("periods", help="周期查询：可用率/有效覆盖/抵扣")
    s.add_argument("--contract", help="限定合同编号")
    s.add_argument("--as-of", dest="as_of")

    s = sub.add_parser("experience", help="从结论追到原始经历")
    s.add_argument("--person", required=True)
    s.add_argument("--run", type=int, help="指定运行号，默认最新")

    s = sub.add_parser("attempts", help="访问请求分类明细（失败请求不消失）")
    s.add_argument("--contract")
    s.add_argument("--run", type=int)

    s = sub.add_parser("dispute", help="争议补证与裁定")
    s.add_argument("--file", nargs=3, metavar=("ID", "INCIDENT_ID", "STATEMENT_FILE"))
    s.add_argument("--decide", nargs=3, metavar=("ID", "DECISION", "DECIDED_ON"))
    s.add_argument("--reopen", nargs=2, metavar=("ID", "NOTE"),
                   help="撤回已裁定争议，回到未决挂起")

    s = sub.add_parser("claims", help="申请/抵扣办理状态与损失去重明细")

    s = sub.add_parser("settle", help="结算一笔在办申请")
    s.add_argument("claim_id")
    s.add_argument("settled_on")

    s = sub.add_parser("renewal", help="续购/调整授权建议（含反事实覆盖）")
    s.add_argument("--contract")

    s = sub.add_parser("changes", help="最近运行的三类变更与影响范围")
    s.add_argument("--run", type=int)

    s = sub.add_parser("audit", help="正式决定台账与调整单")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    eng = Engine(args.db)
    try:
        return _dispatch(eng, args)
    finally:
        eng.close()


def _dispatch(eng, args):
    store = eng.store
    if args.cmd == "import":
        _print(eng.import_materials(args.materials_dir), args.json)

    elif args.cmd == "rules":
        if args.propose:
            payload = json.loads(Path(args.propose).read_text(encoding="utf-8"))
            _print(_propose_rule(eng, payload), args.json)
        elif args.approve:
            rv = store.get_rule(args.approve)
            if not rv:
                raise SystemExit("口径版本不存在：" + args.approve)
            rv["status"] = "approved"
            store.upsert_rule(rv, rv.get("proposed_on"))
            run_id = eng.reprocess(rule_version=args.approve,
                                   note=f"口径 {args.approve} 生效重算")
            store.append_ledger("RULE_APPROVED", "采购员",
                                {"version": args.approve, "run_id": run_id})
            _print({"approved": args.approve, "run_id": run_id}, args.json)
        else:
            _print(store.list_rules(), args.json)

    elif args.cmd == "reprocess":
        run_id = eng.reprocess(as_of=args.as_of, note=args.note)
        _print({"run_id": run_id, "as_of": args.as_of}, args.json)

    elif args.cmd == "periods":
        run = store.latest_run("reprocess")
        if args.as_of:
            run_id = eng.reprocess(as_of=args.as_of, note="周期查询时点复现")
            run = store.latest_run("reprocess")
        rows = store.metrics_at_run(run["id"])
        if args.contract:
            rows = [r for r in rows if r["contract_id"] == args.contract]
        if not args.json:
            rows = [_compact_metric(r) for r in rows]
        _print(rows, args.json)

    elif args.cmd == "experience":
        run_id = args.run or store.latest_run("reprocess")["id"]
        rows = store.experiences_at_run(run_id, args.person)
        _print(rows or ["无经历记录"], args.json)

    elif args.cmd == "attempts":
        run_id = args.run or store.latest_run("reprocess")["id"]
        _print(store.attempts_at_run(run_id, args.contract), args.json)

    elif args.cmd == "dispute":
        if args.file:
            did, iid, stmt_file = args.file
            statement = Path(stmt_file).read_text(encoding="utf-8")
            run_id = eng.file_dispute(did, iid, statement, _today())
            _print({"run_id": run_id, "dispute": did, "state": "pending"}, args.json)
        elif args.decide:
            did, decision, on = args.decide
            run_id = eng.decide_dispute(did, decision, on)
            _print({"run_id": run_id, "dispute": did, "decision": decision}, args.json)
        elif args.reopen:
            did, note = args.reopen
            run_id = eng.reopen_dispute(did, note)
            _print({"run_id": run_id, "dispute": did, "state": "pending"}, args.json)
        else:
            _print(store.adjudications(), args.json)

    elif args.cmd == "claims":
        out = []
        for c in store.all_claims():
            losses = [l for l in store.losses_for_contract(c["contract_id"])
                      if l["claim_id"] == c["claim_id"]]
            out.append({**c, "loss_lines": len(losses),
                        "paid_elsewhere_keys": sum(
                            1 for l in losses if l["status"] == "PAID_ELSEWHERE")})
        _print(out, args.json)

    elif args.cmd == "settle":
        amount = eng.settle_claim(args.claim_id, args.settled_on)
        _print({"claim_id": args.claim_id, "paid_credit": amount}, args.json)

    elif args.cmd == "renewal":
        data = eng._tables()
        contracts = {c["id"]: c for c in data["contracts"]}
        schools = {s["id"]: dict(s) for s in data["schools"]}
        for p in data["people"]:
            schools[p["school_id"]].setdefault("_people", []).append(p["id"])
        run = store.latest_run("reprocess")
        recs = []
        for m in store.metrics_at_run(run["id"]):
            if args.contract and m["contract_id"] != args.contract:
                continue
            recs.append(renewal_recommendation(
                m, contracts[m["contract_id"]], schools[m["school_id"]]))
        _print(recs, args.json)

    elif args.cmd == "changes":
        run_id = args.run or store.latest_run("reprocess")["id"]
        _print(store.changes(run_id), args.json)

    elif args.cmd == "audit":
        _print({"ledger": store.ledger(),
                "adjustments": store.adjustments()}, args.json)
    return 0


def _compact_metric(m):
    return {
        "合同": m["contract_id"], "学校": m["school_id"],
        "未成熟": "是" if m["provisional"] else "否",
        "SLA可用率": m["sla_availability"],
        "争议挂起请求": m.get("sla_pending_dispute_requests", 0),
        "请求成功率": m["request_success_rate"],
        "有效覆盖": m["effective_coverage"],
        "分类计数": json.dumps(m["attempt_breakdown"], ensure_ascii=False),
        "平台损失分钟": m["platform_loss_minutes"],
        "当前应付抵扣": m["credit_payable_now"],
        "争议改判预览抵扣": m["dispute_preview"]["credit_if_pending_platform"],
        "预览差额": m["dispute_preview"]["credit_delta"],
    }


def _propose_rule(eng, payload):
    base = eng.store.get_rule("v1.0")
    version = payload["version"]
    if eng.store.get_rule(version):
        raise SystemExit("版本已存在：" + version)
    rv = {"version": version, "status": "proposed",
          "proposed_on": payload.get("proposed_on", _today()),
          "decided_on": None,
          "params": {**base["params"], **payload.get("params", {})},
          "notes": payload.get("notes", "提案：基于 v1.0 调整参数，尚未生效")}
    eng.store.upsert_rule(rv, rv["proposed_on"])
    eng.store.append_ledger("RULE_PROPOSED", "采购员", rv)
    return {"proposed": version, "status": "proposed",
            "affected": "提案不改变任何现有总数；批准后重算并登记受影响学校/周期"}


def _today():
    from datetime import date
    return date.today().isoformat()


if __name__ == "__main__":
    sys.exit(main())
