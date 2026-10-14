"""Role-bound onboarding plans, individual grants, and durable effect intent."""
import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .planner import plan_role
from .store import connect, event, get, put, rows, stamp, uid


def request(url, path, payload=None):
    encoded = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=encoded,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def spec_for(case, policy, version):
    return {"employee_id": case["employee_id"], "display_name": case["display_name"],
            "role": case["role"], "system": policy["system"], "rights": sorted(policy["rights"]),
            "active": True, "request_id": case["id"],
            "reference": case["id"] + ":" + policy["system"] + ":v" + str(version)}


def policy_hash(policy):
    return digest({key: policy[key] for key in ("role", "system", "rights", "sensitive", "version")})


def account_matches(account, spec):
    return (account and account.get("employee_id") == spec["employee_id"] and
            account.get("system") == spec["system"] and account.get("active") is True and
            set(account.get("rights", [])) == set(spec["rights"]))


def create_case(data_dir, world_url, text, role, employee_id, display_name, mode="reference"):
    if not employee_id or not display_name or len(employee_id) > 80 or len(display_name) > 120:
        raise ValueError("Provide a short employee identifier and display name")
    plan = plan_role(text[:5000], role, mode)
    source = request(world_url, "/api/business")
    policies = [p for p in source["policies"] if p["role"] == plan["role"]]
    if not policies:
        raise ValueError("Approved policy does not exist for the requested role")
    with connect(Path(data_dir) / "runtime.db") as db:
        open_duplicate = [c for c in rows(db, "cases") if c["employee_id"] == employee_id and c["status"] != "COMPLETE"]
        if open_duplicate:
            raise ValueError("An open onboarding request already exists for this employee")
        case = {"id": uid("ONB"), "employee_id": employee_id, "display_name": display_name,
                "role": plan["role"], "request": text[:5000], "plan": plan,
                "status": "READY", "created": stamp(), "last_reasked": 0, "approval_total": 0}
        put(db, "cases", case)
        for policy in policies:
            existing = next((a for a in source["accounts"] if a["employee_id"] == employee_id and
                             a["system"] == policy["system"]), None)
            spec = spec_for(case, policy, 1)
            needs_approval = policy["sensitive"] or bool(existing and not account_matches(existing, spec))
            group = {"id": uid("GRP"), "case_id": case["id"], "system": policy["system"],
                     "version": 1, "policy": policy, "policy_hash": policy_hash(policy), "spec": spec,
                     "status": "AWAITING_APPROVAL" if needs_approval else "READY",
                     "needs_approval": needs_approval,
                     "reason": "Sensitive role access requires manager review" if policy["sensitive"] else
                               "Replacing existing rights requires manager review" if needs_approval else
                               "Approved role policy permits automatic provisioning"}
            put(db, "groups", group)
        case["approval_total"] = sum(g["needs_approval"] for g in rows(db, "groups") if g["case_id"] == case["id"])
        put(db, "cases", case)
        event(db, "REQUEST", "New role-bound onboarding request created", case["id"], planner=mode)
        return case


def approve(data_dir, world_url, group_id, version):
    source = request(world_url, "/api/business")
    with connect(Path(data_dir) / "runtime.db") as db:
        group = get(db, "groups", group_id)
        if not group:
            raise ValueError("Approval group does not exist")
        case = get(db, "cases", group["case_id"])
        current = next((p for p in source["policies"] if p["role"] == case["role"] and p["system"] == group["system"]), None)
        if not current or policy_hash(current) != group["policy_hash"]:
            raise ValueError("Policy changed; refresh this request before approving")
        if version != group["version"] or group["status"] not in {"AWAITING_APPROVAL", "APPROVED"}:
            raise ValueError("This approval card is stale or no longer pending")
        grant = {"id": group_id + ":v" + str(version), "group_id": group_id, "case_id": case["id"],
                 "version": version, "effect_hash": digest(group["spec"]), "policy_hash": group["policy_hash"],
                 "approved_at": stamp(), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                 "status": "GRANTED"}
        put(db, "grants", grant)
        group["status"] = "APPROVED"
        put(db, "groups", group)
        event(db, "APPROVAL", "Manager approved only " + group["system"] + " access", case["id"],
              group_id=group_id, version=version)
        return grant


def reconcile(data_dir, world_url, case_id):
    source = request(world_url, "/api/business")
    changed = []
    with connect(Path(data_dir) / "runtime.db") as db:
        case = get(db, "cases", case_id)
        if not case:
            raise ValueError("Onboarding request does not exist")
        policies = {p["system"]: p for p in source["policies"] if p["role"] == case["role"]}
        for group in rows(db, "groups"):
            if group["case_id"] != case_id:
                continue
            policy = policies[group["system"]]
            existing = next((a for a in source["accounts"] if a["employee_id"] == case["employee_id"] and
                             a["system"] == group["system"]), None)
            policy_changed = policy_hash(policy) != group["policy_hash"]
            drift = group["status"] == "APPLIED" and not account_matches(existing, group["spec"])
            if policy_changed or drift:
                old_grant = get(db, "grants", group["id"] + ":v" + str(group["version"]))
                if old_grant:
                    old_grant["status"] = "INVALIDATED"
                    put(db, "grants", old_grant)
                group["version"] += 1
                group["policy"] = policy
                group["policy_hash"] = policy_hash(policy)
                group["spec"] = spec_for(case, policy, group["version"])
                group["needs_approval"] = True
                group["status"] = "AWAITING_APPROVAL"
                group["reason"] = ("Policy changed; approve the exact replacement rights" if policy_changed else
                                   "Source check found unexpected access; approve correction")
                group.pop("account_id", None)
                put(db, "groups", group)
                changed.append(group["system"])
                event(db, "REASK", group["system"] + " approval refreshed; other app grants remain unchanged", case_id,
                      group_id=group["id"], version=group["version"])
        case["last_reasked"] = len(changed)
        case["status"] = "READY"
        put(db, "cases", case)
    return {"changed": changed, "reasked": len(changed)}


def run(data_dir, world_url, case_id, crash_after_write=False):
    runtime = Path(data_dir) / "runtime.db"
    reconcile(data_dir, world_url, case_id)
    with connect(runtime) as db:
        case = get(db, "cases", case_id)
        groups = [g for g in rows(db, "groups") if g["case_id"] == case_id]
        event(db, "WORKER", "Worker started; checking source records before each write", case_id)
    wrote = False
    for group in groups:
        if group["status"] == "APPLIED":
            continue
        source = request(world_url, "/api/business")
        policy = next(p for p in source["policies"] if p["role"] == case["role"] and p["system"] == group["system"])
        if policy_hash(policy) != group["policy_hash"]:
            reconcile(data_dir, world_url, case_id)
            continue
        reference = group["spec"]["reference"]
        lookup = request(world_url, "/api/business/lookup?reference=" + urllib.parse.quote(reference))
        existing = lookup.get("account")
        if lookup.get("found") and account_matches(existing, group["spec"]):
            with connect(runtime) as db:
                group["status"] = "APPLIED"
                group["account_id"] = existing["id"]
                put(db, "groups", group)
                put(db, "effects", {"id": reference, "case_id": case_id, "status": "APPLIED",
                                    "spec": group["spec"], "account_id": existing["id"], "reconciled": True})
                grant = get(db, "grants", group["id"] + ":v" + str(group["version"]))
                if grant:
                    grant["status"] = "USED"
                    put(db, "grants", grant)
                event(db, "READBACK", "Found existing " + group["system"] + " account after interruption; no duplicate write", case_id,
                      reference=reference, account_id=existing["id"])
            continue
        if group["needs_approval"]:
            with connect(runtime) as db:
                grant = get(db, "grants", group["id"] + ":v" + str(group["version"]))
            valid = (grant and grant["status"] == "GRANTED" and grant["effect_hash"] == digest(group["spec"]) and
                     grant["policy_hash"] == group["policy_hash"] and
                     datetime.fromisoformat(grant["expires_at"]) > datetime.now(timezone.utc))
            if not valid:
                with connect(runtime) as db:
                    group["status"] = "AWAITING_APPROVAL"
                    put(db, "groups", group)
                continue
        with connect(runtime) as db:
            put(db, "effects", {"id": reference, "case_id": case_id, "status": "DISPATCHING", "spec": group["spec"]})
            group["status"] = "DISPATCHING"
            put(db, "groups", group)
            event(db, "INTENT", "Durable intent saved before " + group["system"] + " app write", case_id, reference=reference)
        try:
            response = request(world_url, "/api/business/provision", {"spec": group["spec"],
                               "expected_policy_hash": group["policy_hash"], "group_id": group["id"]})
        except Exception:
            with connect(runtime) as db:
                group["status"] = "IN_DOUBT"
                put(db, "groups", group)
                event(db, "IN_DOUBT", group["system"] + " response unavailable; resume will inspect reference before retrying", case_id)
            continue
        wrote = True
        if crash_after_write:
            os._exit(75)
        account = response["account"]
        if not account_matches(account, group["spec"]):
            raise ValueError("Application read-back does not match requested account rights")
        with connect(runtime) as db:
            group["status"] = "APPLIED"
            group["account_id"] = account["id"]
            put(db, "groups", group)
            put(db, "effects", {"id": reference, "case_id": case_id, "status": "APPLIED", "spec": group["spec"],
                                "account_id": account["id"], "reconciled": False})
            grant = get(db, "grants", group["id"] + ":v" + str(group["version"]))
            if grant:
                grant["status"] = "USED"
                put(db, "grants", grant)
            event(db, "WRITE", group["system"] + " account provisioned and read back", case_id, account_id=account["id"])
    with connect(runtime) as db:
        current = [g for g in rows(db, "groups") if g["case_id"] == case_id]
        case["status"] = "AWAITING_CHECK" if all(g["status"] == "APPLIED" for g in current) else "NEEDS_REVIEW"
        put(db, "cases", case)
        event(db, "WORKER", "Worker finished this pass; independent check still required", case_id, wrote=wrote)
    return case
