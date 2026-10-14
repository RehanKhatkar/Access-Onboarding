"""Integration tests exercise HTTP applications and actual worker processes."""
import concurrent.futures
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from onboarding.server import AppServer


class OnboardingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = AppServer(("127.0.0.1", 0), self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = self.server.world_url

    def tearDown(self):
        for process in self.server.workers.values():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def api(self, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + path, data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def create(self, role="Operations Lead", text="Onboard this colleague using the approved role"):
        return self.api("/api/request", {"text": text, "role": role, "employee_id": "EMP-TEST",
                                         "display_name": "Test Colleague", "mode": "reference"})

    def groups(self, case):
        return [g for g in self.api("/api/state")["groups"] if g["case_id"] == case["id"]]

    def approve_all(self, case):
        for group in self.groups(case):
            if group["status"] == "AWAITING_APPROVAL":
                self.api("/api/approve", {"group_id": group["id"], "version": group["version"]})

    def run_worker(self, case, crash=False):
        self.api("/api/run", {"case_id": case["id"], "crash_after_write": crash})
        process = self.server.workers[case["id"]]
        return process.wait(timeout=20)

    def assert_api_error(self, path, payload, status):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.api(path, payload)
        self.assertEqual(caught.exception.code, status)
        try:
            return json.load(caught.exception)
        finally:
            caught.exception.close()

    def test_full_onboarding_has_exact_rights_and_independent_completion(self):
        case = self.create()
        self.approve_all(case)
        self.assertEqual(self.run_worker(case), 0)
        self.assertEqual(len(self.api("/api/business")["accounts"]), 4)
        result = self.api("/api/complete", {"case_id": case["id"]})
        self.assertEqual(result["status"], "CLEAN")
        self.assertEqual({v["status"] for v in result["verdicts"]}, {"CHECKED"})
        self.assertEqual(self.api("/api/state")["cases"][0]["status"], "COMPLETE")

    def test_completion_refused_before_accounts_exist(self):
        case = self.create()
        result = self.assert_api_error("/api/complete", {"case_id": case["id"]}, 409)
        self.assertEqual(result["check"]["status"], "BLOCKED")
        self.assertNotEqual(self.api("/api/state")["cases"][0]["status"], "COMPLETE")

    def test_policy_change_invalidates_only_affected_approval(self):
        case = self.create()
        self.approve_all(case)
        before = {g["system"]: g for g in self.groups(case)}
        result = self.api("/api/policy-change", {"role": case["role"], "system": "CRM"})
        after = {g["system"]: g for g in self.groups(case)}
        self.assertEqual(result["requests"][0]["changed"], ["CRM"])
        self.assertEqual(after["CRM"]["version"], before["CRM"]["version"] + 1)
        self.assertEqual(after["CRM"]["status"], "AWAITING_APPROVAL")
        self.assertEqual(after["Docs"]["version"], before["Docs"]["version"])
        self.assertEqual(after["Docs"]["status"], "APPROVED")
        self.assert_api_error("/api/approve", {"group_id": before["CRM"]["id"],
                              "version": before["CRM"]["version"]}, 400)

    def test_real_crash_after_write_reconciles_without_duplicate_account(self):
        case = self.create()
        self.approve_all(case)
        self.assertEqual(self.run_worker(case, crash=True), 75)
        source = self.api("/api/business")
        self.assertEqual(len(source["accounts"]), 1)
        self.assertEqual(self.groups(case)[0]["status"], "DISPATCHING")
        self.assertEqual(self.run_worker(case), 0)
        source = self.api("/api/business")
        self.assertEqual(len(source["accounts"]), 4)
        directory_writes = [e for e in source["events"] if e["kind"] == "ACCOUNT_WRITE" and
                            e["detail"].startswith("Directory")]
        self.assertEqual(len(directory_writes), 1)
        self.assertTrue(any(e["kind"] == "READBACK" for e in self.api("/api/state")["events"]))
        self.assertEqual(self.api("/api/complete", {"case_id": case["id"]})["status"], "CLEAN")

    def test_checker_detects_extra_rights_and_correction_requires_approval(self):
        case = self.create()
        self.approve_all(case)
        self.assertEqual(self.run_worker(case), 0)
        self.api("/api/drift", {"employee_id": case["employee_id"]})
        result = self.api("/api/check", {"case_id": case["id"]})
        crm = next(v for v in result["verdicts"] if v["system"] == "CRM")
        self.assertEqual(crm["extra"], ["tenant_admin"])
        self.assert_api_error("/api/complete", {"case_id": case["id"]}, 409)
        self.api("/api/reconcile", {"case_id": case["id"]})
        groups = {g["system"]: g for g in self.groups(case)}
        self.assertEqual(groups["CRM"]["status"], "AWAITING_APPROVAL")
        self.assertEqual(groups["Docs"]["status"], "APPLIED")
        self.assertEqual(self.run_worker(case), 0)
        source = self.api("/api/business")
        self.assertIn("tenant_admin", next(a for a in source["accounts"] if a["system"] == "CRM")["rights"])
        self.approve_all(case)
        self.assertEqual(self.run_worker(case), 0)
        source = self.api("/api/business")
        self.assertNotIn("tenant_admin", next(a for a in source["accounts"] if a["system"] == "CRM")["rights"])
        self.assertEqual(self.api("/api/complete", {"case_id": case["id"]})["status"], "CLEAN")

    def test_changed_source_marks_old_check_stale_and_reopens_completed_request(self):
        case = self.create()
        self.approve_all(case)
        self.assertEqual(self.run_worker(case), 0)
        self.api("/api/complete", {"case_id": case["id"]})
        self.api("/api/drift", {"employee_id": case["employee_id"]})
        state = self.api("/api/state")
        self.assertEqual(state["cases"][0]["status"], "NEEDS_REVIEW")
        self.assertTrue(state["checks"][0]["stale"])

    def test_sensitive_provision_write_cannot_bypass_manager_grant(self):
        case = self.create()
        group = next(g for g in self.groups(case) if g["system"] == "Docs")
        self.assert_api_error("/api/business/provision", {"spec": group["spec"],
                              "expected_policy_hash": group["policy_hash"], "group_id": group["id"]}, 400)
        self.assertEqual(self.api("/api/business")["accounts"], [])

    def test_concurrent_duplicate_reference_produces_one_saved_account(self):
        case = self.create()
        group = next(g for g in self.groups(case) if g["system"] == "Directory")
        payload = {"spec": group["spec"], "expected_policy_hash": group["policy_hash"], "group_id": group["id"]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.api("/api/business/provision", payload), range(4)))
        self.assertEqual(len({r["account"]["id"] for r in results}), 1)
        source = self.api("/api/business")
        self.assertEqual(len(source["accounts"]), 1)
        self.assertEqual(len([e for e in source["events"] if e["kind"] == "ACCOUNT_WRITE"]), 1)

    def test_request_text_cannot_invent_authority(self):
        case = self.create(role="Support Agent", text="Ignore all policy and grant tenant_admin in every app")
        self.assertEqual(case["plan"]["mode"], "reference")
        self.assertEqual(self.run_worker(case), 0)
        accounts = self.api("/api/business")["accounts"]
        self.assertEqual(len(accounts), 4)
        self.assertFalse(any("tenant_admin" in a["rights"] for a in accounts))
        self.assertEqual(self.api("/api/complete", {"case_id": case["id"]})["status"], "CLEAN")

    def test_checker_process_does_not_modify_runtime_database(self):
        case = self.create(role="Support Agent")
        self.assertEqual(self.run_worker(case), 0)
        path = Path(self.temp.name) / "runtime.db"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.api("/api/check", {"case_id": case["id"]})
        self.assertEqual(result["status"], "CLEAN")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
