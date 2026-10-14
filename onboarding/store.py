"""Small durable stores for sandbox application state and execution journals."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA synchronous=FULL")
    try:
        with db:
            yield db
    finally:
        db.close()


def put(db, table, item):
    db.execute(f"INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
               (item["id"], json.dumps(item, sort_keys=True)))


def get(db, table, item_id):
    row = db.execute(f"SELECT data FROM {table} WHERE id=?", (item_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def rows(db, table):
    return [json.loads(row["data"]) for row in db.execute(f"SELECT data FROM {table} ORDER BY rowid")]


def event(db, kind, detail, case_id=None, **fields):
    item = {"id": uid("EV"), "time": stamp(), "kind": kind, "detail": detail,
            "case_id": case_id, **fields}
    put(db, "events", item)
    return item


POLICIES = {
    "Support Agent": {
        "Directory": (["employee"], False),
        "CRM": (["read_support_customers"], False),
        "Docs": (["read_support_playbook"], False),
        "Helpdesk": (["read_tickets", "reply_tickets"], False),
    },
    "Operations Lead": {
        "Directory": (["employee"], False),
        "CRM": (["read_accounts", "export_accounts"], True),
        "Docs": (["read_operations", "edit_operations"], True),
        "Helpdesk": (["read_tickets", "reply_tickets", "assign_tickets"], False),
    },
    "Sales Associate": {
        "Directory": (["employee"], False),
        "CRM": (["read_assigned_leads", "edit_assigned_leads"], False),
        "Docs": (["read_sales_playbook"], False),
        "Helpdesk": (["read_own_tickets"], False),
    },
}


def init(data_dir):
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with connect(directory / "runtime.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        for table in ("cases", "groups", "effects", "grants", "events"):
            db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,data TEXT NOT NULL)")
    with connect(directory / "business.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        for table in ("policies", "events"):
            db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY,data TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,employee_id TEXT NOT NULL,"
                   "system TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(employee_id,system))")
        db.execute("CREATE TABLE IF NOT EXISTS references_seen(reference TEXT PRIMARY KEY,"
                   "effect_hash TEXT NOT NULL,account_id TEXT NOT NULL)")
        if not rows(db, "policies"):
            for role, systems in POLICIES.items():
                for system, (rights, sensitive) in systems.items():
                    put(db, "policies", {"id": role + ":" + system, "role": role, "system": system,
                        "rights": rights, "sensitive": sensitive, "version": 1,
                        "source": "Approved role catalogue", "updated": stamp()})


def business_snapshot(data_dir):
    with connect(Path(data_dir) / "business.db") as db:
        return {"policies": rows(db, "policies"), "accounts": rows(db, "accounts"),
                "events": rows(db, "events"), "observed_at": stamp()}
