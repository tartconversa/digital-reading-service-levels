"""本地状态存储（SQLite，仅标准库）。

分层原则：
* raw_records 保存导入的原始事实，只追加、不就地改写；重复导入按指纹去重。
* experiences / attempts / metrics 是某一次运行（run）在某口径版本下的派生
  快照，重算产生新 run，旧快照保留，因此换规则后仍可查回旧总数与受影响范围。
* ledger / adjustments 是正式决定：追加式，已结金额冻结，后续以调整单列账。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches(
  id INTEGER PRIMARY KEY, created_at TEXT, source_path TEXT,
  sha256 TEXT, table_counts TEXT
);
CREATE TABLE IF NOT EXISTS raw_records(
  table_name TEXT NOT NULL, record_id TEXT NOT NULL, batch_id INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(table_name, record_id, batch_id)
);
CREATE TABLE IF NOT EXISTS rule_versions(
  version TEXT PRIMARY KEY, status TEXT, params TEXT, notes TEXT,
  proposed_on TEXT, decided_on TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, created_at TEXT, kind TEXT, rule_version TEXT,
  as_of TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS run_changes(
  id INTEGER PRIMARY KEY, run_id INTEGER, change_kind TEXT,
  scope TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS experiences(
  id TEXT PRIMARY KEY, run_id INTEGER, person_id TEXT, school_id TEXT,
  content_id TEXT, day TEXT, start_iso TEXT, end_iso TEXT, devices TEXT,
  active_seconds REAL, in_open_active_seconds REAL, audio_seconds REAL,
  page_seconds REAL,
  net_progress REAL, rewinds INTEGER, skips INTEGER, unknown_seconds REAL,
  uncertain INTEGER, is_valid INTEGER, provisional INTEGER, evidence_ids TEXT,
  correction_ids TEXT, rule_version TEXT
);
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY, run_id INTEGER, request_id TEXT, person_id TEXT,
  contract_id TEXT, at_iso TEXT, category TEXT, cause TEXT,
  incident_id TEXT, in_open INTEGER, reason TEXT, provisional INTEGER,
  rule_version TEXT
);
CREATE TABLE IF NOT EXISTS metrics(
  id INTEGER PRIMARY KEY, run_id INTEGER, contract_id TEXT, school_id TEXT,
  rule_version TEXT, as_of TEXT, provisional INTEGER, payload TEXT
);
CREATE TABLE IF NOT EXISTS adjudications(
  incident_id TEXT PRIMARY KEY, cause TEXT, dispute_id TEXT, decided_on TEXT,
  statement TEXT, run_id INTEGER
);
CREATE TABLE IF NOT EXISTS claims_state(
  claim_id TEXT PRIMARY KEY, level TEXT, contract_id TEXT,
  incident_ids TEXT, status TEXT, submitted_on TEXT, settled_on TEXT,
  paid_credit REAL, run_id INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS claim_losses(
  id INTEGER PRIMARY KEY, claim_id TEXT, contract_id TEXT, incident_id TEXT,
  loss_key TEXT, open_minutes REAL, platform_minutes REAL, payable_credit REAL,
  status TEXT, run_id
);
CREATE TABLE IF NOT EXISTS adjustments(
  id INTEGER PRIMARY KEY, created_at TEXT, claim_id TEXT, contract_id TEXT,
  reason TEXT, change_kind TEXT, delta REAL, prior_paid REAL, new_total REAL,
  related_decision_id TEXT, run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, action TEXT, actor TEXT,
  payload TEXT, run_id INTEGER
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self):
        self.con.close()

    # ---------- 原始资料 ----------
    def fingerprint_imported(self, sha):
        row = self.con.execute(
            "SELECT id FROM import_batches WHERE sha256=?", (sha,)).fetchone()
        return row["id"] if row else None

    def import_raw(self, data, sha, source_path, counts):
        ts = datetime.now().astimezone().isoformat()
        cur = self.con.execute(
            "INSERT INTO import_batches(created_at,source_path,sha256,table_counts)"
            " VALUES(?,?,?,?)", (ts, str(source_path), sha, json.dumps(counts)))
        batch_id = cur.lastrowid
        rows = []
        for table, records in data.items():
            for r in records:
                rows.append((table, r["id"], batch_id, json.dumps(r, ensure_ascii=False)))
        self.con.executemany(
            "INSERT INTO raw_records(table_name,record_id,batch_id,payload)"
            " VALUES(?,?,?,?)", rows)
        self.con.commit()
        return batch_id

    def get_raw(self, table):
        # 同一记录若多批次存在，取最新批次的版本（原始更正也留痕）。
        rows = self.con.execute(
            "SELECT payload FROM raw_records WHERE table_name=? "
            "ORDER BY batch_id DESC", (table,)).fetchall()
        seen, out = set(), []
        for row in rows:
            payload = json.loads(row["payload"])
            if payload["id"] in seen:
                continue
            seen.add(payload["id"])
            out.append(payload)
        return out

    # ---------- 口径版本 ----------
    def upsert_rule(self, rv, created_at):
        self.con.execute(
            "INSERT INTO rule_versions(version,status,params,notes,proposed_on,"
            "decided_on,created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(version) DO UPDATE SET status=excluded.status,"
            "params=excluded.params,notes=excluded.notes,proposed_on=excluded.proposed_on,"
            "decided_on=excluded.decided_on",
            (rv["version"], rv["status"], json.dumps(rv["params"], ensure_ascii=False),
             rv["notes"], rv.get("proposed_on"), rv.get("decided_on"), created_at))
        self.con.commit()

    def get_rule(self, version):
        row = self.con.execute(
            "SELECT * FROM rule_versions WHERE version=?", (version,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"])
        return d

    def list_rules(self):
        return [self.get_rule(r["version"]) for r in self.con.execute(
            "SELECT version FROM rule_versions ORDER BY version")]

    # ---------- 运行与变更 ----------
    def start_run(self, kind, rule_version, as_of, note=""):
        cur = self.con.execute(
            "INSERT INTO runs(created_at,kind,rule_version,as_of,note) VALUES(?,?,?,?,?)",
            (datetime.now().astimezone().isoformat(), kind, rule_version, as_of, note))
        self.con.commit()
        return cur.lastrowid

    def add_change(self, run_id, change_kind, scope, detail):
        self.con.execute(
            "INSERT INTO run_changes(run_id,change_kind,scope,detail) VALUES(?,?,?,?)",
            (run_id, change_kind, json.dumps(scope, ensure_ascii=False),
             json.dumps(detail, ensure_ascii=False, default=str)))

    def latest_run(self, kind=None):
        q = "SELECT * FROM runs"
        args = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        q += " ORDER BY id DESC LIMIT 1"
        row = self.con.execute(q, args).fetchone()
        return dict(row) if row else None

    def list_runs(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM runs ORDER BY id")]

    # ---------- 派生快照 ----------
    def replace_experiences(self, run_id, records):
        self.con.execute("DELETE FROM experiences WHERE run_id=?", (run_id,))
        self.con.executemany(
            "INSERT INTO experiences(id,run_id,person_id,school_id,content_id,"
            "day,start_iso,end_iso,devices,active_seconds,in_open_active_seconds,"
            "audio_seconds,page_seconds,net_progress,rewinds,skips,unknown_seconds,"
            "uncertain,is_valid,provisional,evidence_ids,correction_ids,rule_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"X{run_id}-{i}", run_id, r["person_id"], r["school_id"],
              r["content_id"], r.get("day"), r["start_iso"], r["end_iso"],
              json.dumps(r["devices"], ensure_ascii=False), r["active_seconds"],
              r.get("in_open_active_seconds", r["active_seconds"]),
              r["audio_seconds"], r["page_seconds"], r["net_progress"],
              r["rewinds"], r["skips"], r["unknown_seconds"], int(r["uncertain"]),
              int(r["is_valid"]), int(r["provisional"]),
              json.dumps(r["evidence_ids"], ensure_ascii=False),
              json.dumps(r.get("correction_ids", []), ensure_ascii=False),
              r["rule_version"]) for i, r in enumerate(records)])
        self.con.commit()

    def replace_attempts(self, run_id, records):
        self.con.execute("DELETE FROM attempts WHERE run_id=?", (run_id,))
        self.con.executemany(
            "INSERT INTO attempts(id,run_id,request_id,person_id,contract_id,"
            "at_iso,category,cause,incident_id,in_open,reason,provisional,"
            "rule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"A{run_id}-{i}", run_id, r["request_id"], r["person_id"],
              r["contract_id"], r["at_iso"], r["category"], r["cause"],
              r.get("incident_id"), int(r["in_open"]), r.get("reason"),
              int(r["provisional"]), r["rule_version"])
             for i, r in enumerate(records)])
        self.con.commit()

    def replace_metrics(self, run_id, records):
        self.con.execute("DELETE FROM metrics WHERE run_id=?", (run_id,))
        self.con.executemany(
            "INSERT INTO metrics(run_id,contract_id,school_id,rule_version,as_of,"
            "provisional,payload) VALUES(?,?,?,?,?,?,?)",
            [(run_id, r["contract_id"], r["school_id"], r["rule_version"],
              r.get("as_of"), int(r["provisional"]),
              json.dumps(r, ensure_ascii=False, default=str)) for r in records])
        self.con.commit()

    def latest_metrics(self, contract_id=None):
        run = self.latest_run("reprocess") or self.latest_run("import")
        if not run:
            return []
        q = "SELECT * FROM metrics WHERE run_id=?"
        args = [run["id"]]
        if contract_id:
            q += " AND contract_id=?"
            args.append(contract_id)
        return [dict(json.loads(r["payload"])) for r in
                self.con.execute(q, args)]

    def metrics_at_run(self, run_id):
        return [dict(json.loads(r["payload"])) for r in self.con.execute(
            "SELECT payload FROM metrics WHERE run_id=? ORDER BY contract_id", (run_id,))]

    def experiences_at_run(self, run_id, person_id=None):
        q = "SELECT * FROM experiences WHERE run_id=?"
        args = [run_id]
        if person_id:
            q += " AND person_id=?"
            args.append(person_id)
        out = []
        for r in self.con.execute(q, args):
            d = dict(r)
            for col in ("devices", "evidence_ids", "correction_ids"):
                if d.get(col) is not None:
                    d[col] = json.loads(d[col])
            out.append(d)
        return out

    def attempts_at_run(self, run_id, contract_id=None):
        q = "SELECT * FROM attempts WHERE run_id=?"
        args = [run_id]
        if contract_id:
            q += " AND contract_id=?"
            args.append(contract_id)
        return [dict(r) for r in self.con.execute(q, args)]

    # ---------- 裁定 ----------
    def put_adjudication(self, incident_id, cause, dispute_id, decided_on,
                         statement, run_id):
        self.con.execute(
            "INSERT INTO adjudications(incident_id,cause,dispute_id,decided_on,"
            "statement,run_id) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(incident_id) DO UPDATE SET cause=excluded.cause,"
            "dispute_id=excluded.dispute_id,decided_on=excluded.decided_on,"
            "statement=excluded.statement,run_id=excluded.run_id",
            (incident_id, cause, dispute_id, decided_on, statement, run_id))
        self.con.commit()

    def adjudications(self):
        return {r["incident_id"]: dict(r) for r in
                self.con.execute("SELECT * FROM adjudications")}

    # ---------- 申请与结算 ----------
    def upsert_claim(self, claim, run_id):
        ts = datetime.now().astimezone().isoformat()
        self.con.execute(
            "INSERT INTO claims_state(claim_id,level,contract_id,incident_ids,"
            "status,submitted_on,settled_on,paid_credit,run_id,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(claim_id) DO UPDATE SET level=excluded.level,"
            "contract_id=excluded.contract_id,incident_ids=excluded.incident_ids,"
            "status=excluded.status,submitted_on=excluded.submitted_on,"
            "settled_on=excluded.settled_on,paid_credit=excluded.paid_credit,"
            "run_id=excluded.run_id,updated_at=excluded.updated_at",
            (claim["claim_id"], claim["level"], claim["contract_id"],
             json.dumps(claim["incident_ids"]), claim["status"],
             claim.get("submitted_on"), claim.get("settled_on"),
             claim.get("paid_credit", 0.0), run_id, ts))
        self.con.commit()

    def get_claim(self, claim_id):
        row = self.con.execute(
            "SELECT * FROM claims_state WHERE claim_id=?", (claim_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["incident_ids"] = json.loads(d["incident_ids"])
        return d

    def all_claims(self):
        return [self.get_claim(r["claim_id"]) for r in self.con.execute(
            "SELECT claim_id FROM claims_state ORDER BY claim_id")]

    def replace_losses(self, run_id, losses):
        self.con.execute("DELETE FROM claim_losses WHERE run_id=?", (run_id,))
        self.con.executemany(
            "INSERT INTO claim_losses(claim_id,contract_id,incident_id,loss_key,"
            "open_minutes,platform_minutes,payable_credit,status,run_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(l["claim_id"], l["contract_id"], l["incident_id"], l["loss_key"],
              l["open_minutes"], l["platform_minutes"], l["payable_credit"],
              l["status"], run_id) for l in losses])
        self.con.commit()

    def losses_for_contract(self, contract_id):
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM claim_losses WHERE contract_id=? ORDER BY id", (contract_id,))]

    def add_adjustment(self, adj, run_id):
        self.con.execute(
            "INSERT INTO adjustments(created_at,claim_id,contract_id,reason,"
            "change_kind,delta,prior_paid,new_total,related_decision_id,run_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().astimezone().isoformat(), adj["claim_id"],
             adj["contract_id"], adj["reason"], adj["change_kind"], adj["delta"],
             adj["prior_paid"], adj["new_total"], adj.get("related_decision_id"),
             run_id))
        self.con.commit()

    def adjustments(self, claim_id=None):
        q = "SELECT * FROM adjustments"
        args = ()
        if claim_id:
            q += " WHERE claim_id=?"
            args = (claim_id,)
        return [dict(r) for r in self.con.execute(q + " ORDER BY id", args)]

    # ---------- 决定台账 ----------
    def append_ledger(self, action, actor, payload, run_id=None):
        self.con.execute(
            "INSERT INTO ledger(at,action,actor,payload,run_id) VALUES(?,?,?,?,?)",
            (datetime.now().astimezone().isoformat(), action, actor,
             json.dumps(payload, ensure_ascii=False, default=str), run_id))
        self.con.commit()

    def ledger(self, limit=200):
        rows = self.con.execute(
            "SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def changes(self, run_id):
        out = []
        for r in self.con.execute(
                "SELECT * FROM run_changes WHERE run_id=? ORDER BY id", (run_id,)):
            d = dict(r)
            d["scope"] = json.loads(d["scope"])
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out
