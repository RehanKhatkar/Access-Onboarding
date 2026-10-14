"""Local control room and four persistent sandbox workplace applications."""
import argparse
import hashlib
import json
import mimetypes
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import engine
from .store import business_snapshot, connect, event, get, init, put, rows, stamp, uid


ROOT = Path(__file__).resolve().parents[1]


def provision(data_dir, payload):
    spec = payload["spec"]
    required_keys = {"employee_id", "display_name", "role", "system", "rights", "active", "request_id", "reference"}
    if set(spec) != required_keys or spec["active"] is not True:
        raise ValueError("Account effect has an invalid shape")
    runtime_path = Path(data_dir) / "runtime.db"
    with connect(runtime_path) as runtime:
        group = get(runtime, "groups", payload["group_id"])
        if not group or group["spec"] != spec:
            raise ValueError("Write is not one of the planned account effects")
        grant = get(runtime, "grants", group["id"] + ":v" + str(group["version"]))
        if group["needs_approval"]:
            from datetime import datetime, timezone
            if not (grant and grant["status"] == "GRANTED" and
                    grant["effect_hash"] == engine.digest(spec) and
                    grant["policy_hash"] == payload["expected_policy_hash"] and
                    datetime.fromisoformat(grant["expires_at"]) > datetime.now(timezone.utc)):
                raise ValueError("Exact, fresh manager grant is required")
    with connect(Path(data_dir) / "business.db") as db:
        db.execute("BEGIN IMMEDIATE")
        policy = get(db, "policies", spec["role"] + ":" + spec["system"])
        if not policy or engine.policy_hash(policy) != payload["expected_policy_hash"]:
            raise ValueError("Role policy changed before application write")
        if sorted(spec["rights"]) != sorted(policy["rights"]):
            raise ValueError("Requested rights exceed or omit the approved role policy")
        seen = db.execute("SELECT * FROM references_seen WHERE reference=?", (spec["reference"],)).fetchone()
        if seen:
            if seen["effect_hash"] != engine.digest(spec):
                raise ValueError("The reference belongs to a different account effect")
            account = get(db, "accounts", seen["account_id"])
            return {"account": account, "reused": True}
        old = db.execute("SELECT data FROM accounts WHERE employee_id=? AND system=?",
                         (spec["employee_id"], spec["system"])).fetchone()
        previous = json.loads(old["data"]) if old else None
        account = {"id": previous["id"] if previous else uid("ACC"), **spec, "updated": stamp(),
                   "version": previous["version"] + 1 if previous else 1}
        db.execute("INSERT INTO accounts VALUES (?,?,?,?) ON CONFLICT(employee_id,system) DO UPDATE SET data=excluded.data",
                   (account["id"], spec["employee_id"], spec["system"], json.dumps(account, sort_keys=True)))
        db.execute("INSERT INTO references_seen VALUES (?,?,?)",
                   (spec["reference"], engine.digest(spec), account["id"]))
        event(db, "ACCOUNT_WRITE", spec["system"] + " account rights saved", spec["request_id"],
              employee_id=spec["employee_id"], account_id=account["id"], reference=spec["reference"],
              before=previous["rights"] if previous else [], after=spec["rights"])
        return {"account": account, "reused": False}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, data_dir):
        super().__init__(address, Handler)
        self.data_dir = Path(data_dir).resolve()
        init(self.data_dir)
        self.world_url = f"http://127.0.0.1:{self.server_port}"
        self.workers = {}
        self.worker_lock = threading.Lock()

    def launch_worker(self, case_id, crash=False):
        with self.worker_lock:
            if any(p.poll() is None for p in self.workers.values()):
                raise ValueError("A worker is already running; wait for this pass to finish")
            command = [sys.executable, "-m", "onboarding.worker", "--data", str(self.data_dir),
                       "--world", self.world_url, "--case", case_id]
            if crash:
                command.append("--crash-after-write")
            logs = self.data_dir / "worker.log"
            with logs.open("a") as output:
                process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=output)
            self.workers[case_id] = process
            return {"pid": process.pid, "case_id": case_id, "crash_after_write": crash}

    def source_check(self, case_id):
        process = subprocess.run([sys.executable, "-m", "onboarding.checker", "--runtime",
                                  str(self.data_dir / "runtime.db"), "--checker", str(self.data_dir / "checker.db"),
                                  "--world", self.world_url, "--case", case_id], cwd=ROOT,
                                 capture_output=True, text=True, timeout=15, check=True)
        return json.loads(process.stdout)

    def state(self):
        with connect(self.data_dir / "runtime.db") as db:
            result = {name: rows(db, name) for name in ("cases", "groups", "effects", "grants", "events")}
        result["business"] = business_snapshot(self.data_dir)
        result["checks"] = []
        if (self.data_dir / "checker.db").exists():
            with connect(self.data_dir / "checker.db") as db:
                result["checks"] = [json.loads(r["data"]) for r in db.execute("SELECT data FROM checks")]
        authority = {key: result["business"][key] for key in ("policies", "accounts")}
        source_hash = hashlib.sha256(json.dumps(authority, sort_keys=True).encode()).hexdigest()
        for check in result["checks"]:
            check["stale"] = check.get("evidence", {}).get("snapshot_sha256") != source_hash
        result["workers"] = [{"case_id": case_id, "pid": p.pid, "exit_code": p.poll(),
                              "status": "RUNNING" if p.poll() is None else "CRASHED" if p.returncode else "FINISHED"}
                             for case_id, p in self.workers.items()]
        result["planner"] = {"reference": "Deterministic approved-role mapping", "claude": "Live Claude Code request interpretation"}
        return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def json_response(self, value, code=200):
        encoded = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/state":
                return self.json_response(self.server.state())
            if path == "/api/business":
                return self.json_response(business_snapshot(self.server.data_dir))
            if path == "/api/business/lookup":
                reference = urllib.parse.parse_qs(parsed.query).get("reference", [""])[0]
                with connect(self.server.data_dir / "business.db") as db:
                    record = db.execute("SELECT account_id FROM references_seen WHERE reference=?", (reference,)).fetchone()
                    account = get(db, "accounts", record["account_id"]) if record else None
                return self.json_response({"found": bool(record), "account": account})
            file_path = ROOT / "web" / ("index.html" if path == "/" else
                                          "apps.html" if path.startswith("/apps/") else path.lstrip("/"))
            if not file_path.resolve().is_relative_to((ROOT / "web").resolve()) or not file_path.is_file():
                return self.json_response({"error": "Page does not exist"}, 404)
            encoded = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            allowed = {self.server.world_url, "http://localhost:" + str(self.server.server_port)}
            if origin and origin not in allowed:
                return self.json_response({"error": "Origin is not the local control room"}, 403)
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("Request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urllib.parse.urlparse(self.path).path
            directory, url = self.server.data_dir, self.server.world_url
            if path == "/api/request":
                result = engine.create_case(directory, url, payload.get("text", ""), payload.get("role", "Operations Lead"),
                                            payload.get("employee_id", "EMP-204"), payload.get("display_name", "Maya Chen"),
                                            payload.get("mode", "reference"))
            elif path == "/api/approve":
                result = engine.approve(directory, url, payload["group_id"], payload["version"])
            elif path == "/api/run":
                result = self.server.launch_worker(payload["case_id"], payload.get("crash_after_write", False))
            elif path == "/api/business/provision":
                result = provision(directory, payload)
            elif path == "/api/check":
                result = self.server.source_check(payload["case_id"])
                if result["status"] != "CLEAN":
                    with connect(directory / "runtime.db") as db:
                        case = get(db, "cases", payload["case_id"])
                        if case["status"] == "COMPLETE":
                            case["status"] = "NEEDS_REVIEW"
                            put(db, "cases", case)
                            event(db, "REOPEN", "Current source check no longer meets completion policy", case["id"])
            elif path == "/api/complete":
                if any(p.poll() is None for p in self.server.workers.values()):
                    raise ValueError("Wait for the worker pass to finish before checking completion")
                result = self.server.source_check(payload["case_id"])
                if result["status"] != "CLEAN":
                    return self.json_response({"error": result["detail"], "check": result}, 409)
                with connect(directory / "business.db") as source_db:
                    source_db.execute("BEGIN IMMEDIATE")
                    authority = {"policies": rows(source_db, "policies"), "accounts": rows(source_db, "accounts")}
                    current_hash = hashlib.sha256(json.dumps(authority, sort_keys=True).encode()).hexdigest()
                    if current_hash != result["evidence"]["snapshot_sha256"]:
                        raise ValueError("Source records changed during the check; check again")
                    with connect(directory / "runtime.db") as db:
                        case = get(db, "cases", payload["case_id"])
                        case["status"], case["completed"] = "COMPLETE", stamp()
                        put(db, "cases", case)
                        event(db, "COMPLETE", "Independent checker confirmed every account and no extra rights", case["id"])
            elif path == "/api/reconcile":
                result = engine.reconcile(directory, url, payload["case_id"])
            elif path == "/api/policy-change":
                role = payload.get("role", "Operations Lead")
                system = payload.get("system", "CRM")
                with connect(directory / "business.db") as db:
                    db.execute("BEGIN IMMEDIATE")
                    policy = get(db, "policies", role + ":" + system)
                    if not policy:
                        raise ValueError("Policy does not exist")
                    if system != "CRM" or role != "Operations Lead":
                        raise ValueError("This demonstration changes only Operations Lead CRM policy")
                    policy["rights"] = ["read_accounts"] if "export_accounts" in policy["rights"] else ["read_accounts", "export_accounts"]
                    policy["version"] += 1
                    policy["updated"] = stamp()
                    put(db, "policies", policy)
                    event(db, "POLICY", "Operations Lead CRM export permission changed", policy_version=policy["version"])
                with connect(directory / "runtime.db") as db:
                    ids = [c["id"] for c in rows(db, "cases") if c["role"] == role]
                result = {"policy": policy, "requests": [engine.reconcile(directory, url, i) for i in ids]}
            elif path == "/api/drift":
                with connect(directory / "business.db") as db:
                    account = next((a for a in rows(db, "accounts") if a["employee_id"] == payload["employee_id"] and
                                    a["system"] == "CRM"), None)
                    if not account:
                        raise ValueError("Provision CRM first before demonstrating unexpected access")
                    if "tenant_admin" not in account["rights"]:
                        account["rights"].append("tenant_admin")
                    db.execute("UPDATE accounts SET data=? WHERE id=?", (json.dumps(account, sort_keys=True), account["id"]))
                    event(db, "FAULT", "Controlled test: unexpected CRM admin right introduced", employee_id=payload["employee_id"])
                result = {"account": account, "disclosure": "Deliberate sandbox fault injection"}
                with connect(directory / "runtime.db") as db:
                    for case in rows(db, "cases"):
                        if case["employee_id"] == payload["employee_id"] and case["status"] == "COMPLETE":
                            case["status"] = "NEEDS_REVIEW"
                            put(db, "cases", case)
                            event(db, "REOPEN", "Sandbox source changed; fresh verification required", case["id"])
            else:
                return self.json_response({"error": "Action does not exist"}, 404)
            self.json_response(result)
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.json_response({"error": str(exc)}, 400)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8892)
    parser.add_argument("--data", default="data")
    args = parser.parse_args()
    server = AppServer(("127.0.0.1", args.port), args.data)
    print("Access Onboarding is ready at " + server.world_url, flush=True)
    try:
        server.serve_forever()
    finally:
        for process in server.workers.values():
            if process.poll() is None:
                process.terminate()
        server.server_close()


if __name__ == "__main__":
    main()
