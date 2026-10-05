"""Offline coordinator qualification with temporary roots and owned children."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_DIR = Path(__file__).resolve().parents[1] / "workbench"
sys.path.insert(0, str(MODULE_DIR))
import lease_coordinator as module
from machine_lease import MachineLease, LeaseBusy, RecoveryRequired

HOLDER = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from lease_coordinator import LeaseCoordinator
options = json.loads(sys.argv[3])
def verify(snapshot, context):
    value = json.loads(snapshot.data)
    if options.get("tamper"):
        with open(snapshot.path, "wb") as stream:
            stream.write(b'changed')
    if options.get("slow"):
        import time
        time.sleep(options["slow"])
    return value.get("fixture") == "no-external-effects" and value.get("restored") is True
try:
    holder = LeaseCoordinator(sys.argv[2], "test-session", "test-task",
        ttl_seconds=options.get("ttl", 30),
        cleanup_verifier=None if options.get("no_verifier") else verify,
        recovery_evidence=options.get("recovery"))
    print(json.dumps({"ready": True, "pid": os.getpid(), "holder_id": holder.holder_id}), flush=True)
    holder.serve()
except Exception as error:
    print(json.dumps({"error": type(error).__name__}), flush=True)
'''
CLIENT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from lease_coordinator import CoordinatorClient
request = json.loads(sys.stdin.read())
print(json.dumps(CoordinatorClient(sys.argv[2], session_id="test-session").request(**request)), flush=True)
'''


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wb2-", dir="/private/tmp" if Path("/private/tmp").exists() else "/tmp")
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "r"
        self.children = []
        self.holders = []
        self.client = module.CoordinatorClient(self.root, session_id="test-session")
        self.artifact = self.parent / "cleanup.json"
        self.artifact.write_text(json.dumps(dict(fixture="no-external-effects", restored=True)))
        self.artifact.chmod(0o600)
        self.evidence = dict(path=str(self.artifact), sha256=hashlib.sha256(self.artifact.read_bytes()).hexdigest())

    def tearDown(self):
        for holder in self.holders:
            if not holder.closed:
                holder.close()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream:
                    stream.close()
        self.temporary.cleanup()

    def spawn(self, **options):
        child = subprocess.Popen([sys.executable, "-B", "-u", "-c", HOLDER, str(MODULE_DIR), str(self.root), json.dumps(options)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), "holder startup timeout")
        line = child.stdout.readline()
        self.assertTrue(line, "holder did not report startup")
        return child, json.loads(line)

    def start(self, **options):
        child, record = self.spawn(**options)
        self.assertTrue(record.get("ready"), record)
        return child, record

    def independent_client(self, action, **payload):
        result = subprocess.run([sys.executable, "-B", "-c", CLIENT, str(MODULE_DIR), str(self.root)],
                                input=json.dumps(dict(action=action, **payload)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def begin(self, seconds=10):
        result = self.client.request("begin_step", label="offline fixture", duration_seconds=seconds)
        self.assertTrue(result["ok"], result)
        return {key: result[key] for key in ("step_id", "step_token")}

    def finish(self, step):
        return self.client.request("end_step", outcome="completed", **step)

    def release(self):
        return self.client.request("release", evidence=self.evidence)

    def wait_state(self, state):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.client.request("status")
            if result["state"] == state:
                return result
            time.sleep(0.03)
        self.fail("state not reached: " + state)

    def test_holder_retains_exclusion_between_two_client_processes(self):
        child, identity = self.start()
        first = self.independent_client("begin_step", label="client-one", duration_seconds=10)
        self.assertTrue(first["ok"])
        with self.assertRaises(LeaseBusy):
            MachineLease(self.root).acquire("competitor", "between-commands")
        second = self.independent_client("end_step", step_id=first["step_id"], step_token=first["step_token"], outcome="completed")
        self.assertTrue(second["ok"])
        self.assertEqual(second["pid"], identity["pid"])
        self.assertIsNone(child.poll())
        with self.assertRaises(LeaseBusy):
            MachineLease(self.root).acquire("competitor", "after-client-exit")
        self.assertEqual(self.release()["state"], "HUMAN_SAFE")
        child.wait(timeout=3)

    def test_wrong_capability_and_step_membership(self):
        self.start()
        stored = json.loads((self.root / "capability.json").read_text())
        auth = {key: stored[key] for key in ("holder_id", "session_id", "task_id", "token")}
        for key in auth:
            wrong = dict(auth, **{key: "wrong"})
            result = self.client.request("begin_step", credentials=wrong, label="no", duration_seconds=1)
            self.assertEqual(result["code"], "UNAUTHORIZED")
        step = self.begin()
        self.assertEqual(self.client.request("end_step", **dict(step, step_token="wrong"), outcome="completed")["code"], "STEP_MEMBERSHIP_REQUIRED")
        self.assertTrue(self.finish(step)["ok"])
        self.assertEqual(self.finish(step)["code"], "STEP_MEMBERSHIP_REQUIRED")
        self.release()

    def test_pause_drains_without_eta_or_early_release(self):
        self.start()
        step = self.begin()
        paused = self.client.request("request_pause", reason="human needs machine")
        self.assertEqual(paused["state"], "DRAINING")
        self.assertIsNone(paused["pause_eta_seconds"])
        self.assertEqual(self.client.request("begin_step", label="no", duration_seconds=1)["code"], "DRAINING")
        self.assertEqual(self.release()["code"], "ACTIVE_STEPS")
        self.assertEqual(self.finish(step)["state"], "DRAINING")
        self.assertEqual(self.release()["state"], "HUMAN_SAFE")
        events = [json.loads(line) for line in (self.root / "transitions.jsonl").read_text().splitlines()]
        names = [event["event"] for event in events]
        self.assertLess(names.index("pause_requested"), names.index("step_ended"))
        self.assertTrue(all(event["timestamp"] for event in events))
        self.assertNotIn(step["step_token"], json.dumps(events))

    def test_renewal_and_ttl_expiry_fail_closed(self):
        self.start(ttl=0.8)
        self.assertTrue(self.client.request("renew", ttl_seconds=2)["ok"])
        time.sleep(0.9)
        self.assertEqual(self.client.request("status")["state"], "RUNNING")
        self.wait_state("RECOVERY_REQUIRED")
        for action, fields in (("renew", dict(ttl_seconds=5)), ("begin_step", dict(label="no", duration_seconds=1))):
            self.assertEqual(self.client.request(action, **fields)["code"], "RECOVERY_REQUIRED")
        with self.assertRaises(LeaseBusy):
            MachineLease(self.root).acquire("competitor", "expired")
        self.assertEqual(self.release()["state"], "RECOVERY_REQUIRED")
        with self.assertRaises(RecoveryRequired):
            MachineLease(self.root).acquire("competitor", "unclean")

    def test_forward_wall_clock_stationary_monotonic(self):
        holder = module.LeaseCoordinator(self.root, "session", "task", ttl_seconds=30)
        self.holders.append(holder)
        import machine_lease
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        with mock.patch.object(machine_lease, "_now", return_value=future), mock.patch.object(machine_lease.time, "monotonic", return_value=time.monotonic()):
            holder.observe()
        self.assertEqual(holder.state, "RECOVERY_REQUIRED")
        result = holder.handle(dict(action="renew", auth=holder.credentials(), claimed_session_id=holder.session_id, ttl_seconds=30))
        self.assertEqual(result["code"], "RECOVERY_REQUIRED")

    def test_step_expiry_never_auto_completes(self):
        self.start()
        step = self.begin(0.1)
        state = self.wait_state("RECOVERY_REQUIRED")
        self.assertEqual(state["active_steps"], 1)
        self.assertEqual(self.release()["code"], "ACTIVE_STEPS")
        self.assertTrue(self.finish(step)["ok"])
        self.assertEqual(self.release()["state"], "RECOVERY_REQUIRED")

    def test_crash_requires_semantic_recovery_and_live_status(self):
        child, _ = self.start()
        child.kill()
        child.wait(timeout=3)
        self.assertEqual(self.client.request("status")["state"], "RECOVERY_REQUIRED")
        failed, result = self.spawn()
        self.assertEqual(result["error"], "RecoveryRequired")
        failed.wait(timeout=3)
        failed, result = self.spawn(no_verifier=True, recovery=self.evidence)
        self.assertEqual(result["error"], "RecoveryRequired")
        failed.wait(timeout=3)
        successor, _ = self.start(recovery=self.evidence)
        self.assertEqual(self.client.request("status")["state"], "RUNNING")
        self.assertEqual(self.release()["state"], "HUMAN_SAFE")
        successor.wait(timeout=3)
        # Persisted HUMAN_SAFE alone cannot prove a live holder.
        self.assertEqual(self.client.request("status")["state"], "RECOVERY_REQUIRED")

    def test_malformed_and_oversize_frames_do_not_dispatch(self):
        self.start()
        discovery = json.loads((self.root / "coordinator.json").read_text())
        for raw in (b'not-json\n', b'[]\n', b'{"action":"status","action":"release"}\n', b'{}\n{}\n', b'x' * (module.MAX_FRAME + 1)):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(self.root / discovery["socket_name"]))
                connection.sendall(raw)
                result = module.receive_frame(connection)
                self.assertEqual(result["code"], "INVALID_REQUEST")
        self.assertEqual(self.client.request("status")["state"], "RUNNING")
        self.release()

    def test_hash_and_symlink_rejection_preserve_lock(self):
        self.start()
        self.artifact.write_text("tampered")
        self.assertEqual(self.release()["code"], "INVALID_REQUEST")
        self.artifact.unlink()
        target = self.parent / "real.json"
        target.write_text("contents")
        target.chmod(0o600)
        self.artifact.symlink_to(target)
        result = self.release()
        self.assertFalse(result["ok"])
        with self.assertRaises(LeaseBusy):
            MachineLease(self.root).acquire("competitor", "tampered")

    def test_verifier_tamper_is_rejected(self):
        self.start(tamper=True)
        self.assertEqual(self.release()["code"], "INVALID_REQUEST")
        with self.assertRaises(LeaseBusy):
            MachineLease(self.root).acquire("competitor", "tampered-during-verifier")

    def test_no_verifier_or_negative_verdict_never_marks_safe(self):
        child, _ = self.start(no_verifier=True)
        self.assertEqual(self.release()["state"], "RECOVERY_REQUIRED")
        child.wait(timeout=3)
        child, _ = self.start(recovery=self.evidence)
        self.artifact.write_text(json.dumps(dict(fixture="no-external-effects", restored=False)))
        self.evidence["sha256"] = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.assertEqual(self.release()["state"], "RECOVERY_REQUIRED")
        child.wait(timeout=3)

    def test_expiry_during_verifier_never_marks_safe(self):
        self.start(ttl=0.8, slow=1)
        self.assertEqual(self.release()["state"], "RECOVERY_REQUIRED")

    def test_audit_tamper_stops_holder_without_safe_claim(self):
        child, _ = self.start()
        audit = self.root / "transitions.jsonl"
        audit.rename(self.root / "old-audit.jsonl")
        audit.write_text("")
        audit.chmod(0o600)
        result = self.client.request("begin_step", label="must-not-grant", duration_seconds=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "RECOVERY_REQUIRED")
        child.wait(timeout=3)
        with self.assertRaises(RecoveryRequired):
            MachineLease(self.root).acquire("competitor", "audit-loss")

    def test_bad_recovery_hash_preserves_previous_owner(self):
        child, _ = self.start()
        child.kill()
        child.wait(timeout=3)
        before = (self.root / "owner.json").read_bytes()
        bad = dict(self.evidence, sha256="0" * 64)
        refused, record = self.spawn(recovery=bad)
        self.assertEqual(record["error"], "ValueError")
        refused.wait(timeout=3)
        self.assertEqual((self.root / "owner.json").read_bytes(), before)

    def test_truthy_verifier_is_not_semantic_acceptance(self):
        def unsupported_claim(snapshot, context):
            return "probably fine"
        holder = module.LeaseCoordinator(self.root, "session", "task", cleanup_verifier=unsupported_claim)
        self.holders.append(holder)
        result = holder.handle(dict(action="release", auth=holder.credentials(), claimed_session_id=holder.session_id, evidence=self.evidence))
        self.assertEqual(result["state"], "RECOVERY_REQUIRED")
        holder.close()
        self.assertFalse(json.loads((self.root / "owner.json").read_text())["cleanup"]["verified"])

    def test_monotonic_expiry_with_stationary_wall(self):
        holder = module.LeaseCoordinator(self.root, "session", "task", ttl_seconds=30)
        self.holders.append(holder)
        import machine_lease
        future = time.monotonic() + 60
        with mock.patch.object(machine_lease.time, "monotonic", return_value=future):
            holder.observe()
        self.assertEqual(holder.state, "RECOVERY_REQUIRED")
        self.assertEqual(holder.handle(dict(action="begin_step", auth=holder.credentials(), claimed_session_id=holder.session_id, label="no", duration_seconds=1))["code"], "RECOVERY_REQUIRED")

    def test_unknown_actions_and_unbounded_steps_are_rejected(self):
        self.start()
        for action, payload in (("execute", dict(command="do-not-run")),
                                ("begin_step", dict(label="too-long", duration_seconds=601)),
                                ("begin_step", dict(label="negative", duration_seconds=-1))):
            self.assertEqual(self.client.request(action, **payload)["code"], "INVALID_REQUEST")
        self.assertEqual(self.client.request("status")["active_steps"], 0)
        self.release()

    def test_unauthenticated_status_is_private_and_identity_is_actual(self):
        child, identity = self.start()
        step = self.begin()
        record = self.client.request("status", authenticated=False)
        self.assertEqual(record["pid"], child.pid)
        self.assertEqual(record["holder_id"], identity["holder_id"])
        raw = json.dumps(record)
        stored = json.loads((self.root / "capability.json").read_text())
        for secret in (stored["token"], str(self.root), step["step_token"]):
            self.assertNotIn(secret, raw)
        self.assertEqual((self.root / "capability.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "coordinator.json").stat().st_mode & 0o777, 0o600)
        self.finish(step)
        self.release()

    def test_other_session_cannot_join_or_release_current_phase(self):
        self.start()
        unspecified = module.CoordinatorClient(self.root).request('renew', ttl_seconds=20)
        self.assertEqual(unspecified['state'], 'UNKNOWN')
        self.assertEqual(unspecified['code'], 'OWNER_SESSION_REQUIRED')
        other = module.CoordinatorClient(self.root, session_id='another-agent')
        for action, fields in (
            ('begin_step', dict(label='wrong agent', duration_seconds=1)),
            ('renew', dict(ttl_seconds=20)),
            ('release', dict(evidence=self.evidence)),
        ):
            self.assertEqual(other.request(action, **fields)['code'], 'OWNER_SESSION_REQUIRED')
        status = other.request('status', authenticated=False)
        self.assertEqual(status['session_id'], 'test-session')
        self.assertEqual(status['active_steps'], 0)
        # Human pause is explicitly cross-session and only drains, never grants work.
        self.assertEqual(other.request('request_pause', reason='human needs screen')['state'], 'DRAINING')
        self.release()

    def test_release_failure_never_publishes_safe_discovery(self):
        holder = module.LeaseCoordinator(self.root, 'test-session', 'test-task',
                                        cleanup_verifier=lambda *args: True)
        self.holders.append(holder)
        with mock.patch.object(holder.owner, 'release', side_effect=RuntimeError('fixture release failure')):
            with self.assertRaises(RuntimeError):
                holder.handle(dict(action='release', auth=holder.credentials(),
                                   claimed_session_id='test-session', evidence=self.evidence))
        saved = json.loads((self.root / 'coordinator.json').read_text())
        self.assertNotEqual(saved['state'], 'HUMAN_SAFE')
        # The fixture failed before the real lease release; explicitly clean it up.
        holder.owner.release()

    def test_overlong_socket_path_refused_before_acquisition(self):
        root = self.parent / ('x' * 120)
        with self.assertRaisesRegex(ValueError, 'socket path too long'):
            module.LeaseCoordinator(root, 'test-session', 'test-task')
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
