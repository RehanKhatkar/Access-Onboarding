"""Independent source-state verifier. Imports no worker or runtime implementation."""
import argparse
import hashlib
import json
import sqlite3
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def check(runtime_path, checker_path, world_url, case_id):
    verdicts = []
    status = "BLOCKED"
    detail = "Check did not complete"
    evidence = {}
    try:
        with closing(sqlite3.connect(Path(runtime_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT data FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                raise ValueError("Request does not exist")
            case = json.loads(row[0])
        with urllib.request.urlopen(world_url.rstrip("/") + "/api/business", timeout=10) as response:
            source = json.load(response)
        policies = {p["system"]: p for p in source["policies"] if p["role"] == case["role"]}
        accounts = [a for a in source["accounts"] if a["employee_id"] == case["employee_id"]]
        authority = {"policies": source["policies"], "accounts": source["accounts"]}
        evidence = {"snapshot_sha256": hashlib.sha256(json.dumps(authority, sort_keys=True).encode()).hexdigest(),
                    "employee_id": case["employee_id"], "role": case["role"], "accounts": accounts,
                    "policies": list(policies.values()), "source": "Sandbox app records"}
        if not policies:
            raise ValueError("No authoritative policy exists for this role")
        for system, policy in policies.items():
            matches = [a for a in accounts if a["system"] == system]
            if len(matches) != 1:
                verdicts.append({"system": system, "status": "MISSING" if not matches else "DUPLICATE",
                                 "detail": "Exactly one account is required"})
                continue
            account = matches[0]
            required, actual = set(policy["rights"]), set(account["rights"])
            extra, missing = sorted(actual - required), sorted(required - actual)
            ok = not extra and not missing and account.get("active") is True and account.get("role") == case["role"]
            verdicts.append({"system": system, "status": "CHECKED" if ok else "DRIFT",
                             "detail": "Exact required rights verified" if ok else
                                       "Unexpected access: " + ", ".join(extra) + "; missing: " + ", ".join(missing),
                             "account_id": account["id"], "extra": extra, "missing": missing,
                             "required": sorted(required), "actual": sorted(actual)})
        unexpected = [a for a in accounts if a["system"] not in policies]
        for account in unexpected:
            verdicts.append({"system": account["system"], "status": "EXTRA_ACCOUNT",
                             "detail": "Account has no place in this role's policy", "account_id": account["id"]})
        count = sum(v["status"] == "CHECKED" for v in verdicts)
        if count == len(policies) and len(verdicts) == len(policies):
            status, detail = "CLEAN", f"{count} of {len(policies)} app accounts checked; no additional rights"
        else:
            detail = f"Completion refused: {count} of {len(policies)} app accounts satisfy current policy"
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        detail = "Independent source check unavailable: " + str(exc)
    result = {"case_id": case_id, "status": status, "detail": detail, "verdicts": verdicts,
              "evidence": evidence, "checked_at": datetime.now(timezone.utc).isoformat()}
    target = Path(checker_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target, timeout=10)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS checks(case_id TEXT PRIMARY KEY,data TEXT NOT NULL)")
        db.execute("INSERT INTO checks VALUES (?,?) ON CONFLICT(case_id) DO UPDATE SET data=excluded.data",
                   (case_id, json.dumps(result, sort_keys=True)))
        db.commit()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--checker", required=True)
    parser.add_argument("--world", required=True)
    parser.add_argument("--case", required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.runtime, args.checker, args.world, args.case)))


if __name__ == "__main__":
    main()
