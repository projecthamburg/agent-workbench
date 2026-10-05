"""Offline lease tests: temporary runtimes and only child processes we start."""
import importlib.util
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "workbench/machine_lease.py"
spec = importlib.util.spec_from_file_location("workbench_machine_lease", MODULE_PATH)
lease_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease_module)

CHILD = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from machine_lease import MachineLease
lease = MachineLease(sys.argv[2])
try:
    owner = lease.acquire("child-session", "child-task", float(sys.argv[3]))
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__}), flush=True)
    raise SystemExit(0)
print(json.dumps({"acquired": True, "pid": os.getpid()}), flush=True)
for line in sys.stdin:
    command = line.strip().split()
    try:
        if command[0] == "crash":
            os._exit(17)
        if command[0] == "renew":
            record = owner.renew(float(command[1]))
            print(json.dumps({"renewed_at": record["renewed_at"], "expires_at": record["expires_at"]}), flush=True)
        elif command[0] == "check":
            lease.check(owner)
            print(json.dumps({"valid": True}), flush=True)
        elif command[0] == "release":
            owner.release(cleanup_verified=True, cleanup_note="Temporary fixture has no external effects")
            print(json.dumps({"released": True}), flush=True)
            break
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), flush=True)
'''


class MachineLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # macOS's default temporary path may contain /var -> /private/var.
        # Resolve the trusted test parent; the lease itself rejects symlink paths.
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "runtime"
        self.lease = lease_module.MachineLease(self.root)
        self.children = []
        self.owners = []

    def tearDown(self):
        for owner in self.owners:
            if owner._active:
                try:
                    owner.release()
                except lease_module.LeaseError:
                    pass
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        self.temporary.cleanup()

    def acquire(self, **kwargs):
        owner = self.lease.acquire("parent-session", "parent-task", **kwargs)
        self.owners.append(owner)
        return owner

    def child(self, ttl=30):
        child = subprocess.Popen([sys.executable, "-u", "-c", CHILD,
                                  str(MODULE_PATH.parent), str(self.root), str(ttl)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child, self.response(child)

    def response(self, child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=5):
                self.fail("Isolated child did not respond within five seconds")
        line = child.stdout.readline()
        if not line:
            self.fail("Isolated child exited before responding: " + child.stderr.read())
        return json.loads(line)

    def command(self, child, command):
        child.stdin.write(command + "\n")
        child.stdin.flush()
        return self.response(child)

    def metadata(self):
        return json.loads((self.root / "owner.json").read_text())

    def test_private_metadata_and_clean_release_preserve_stable_lock(self):
        owner = self.acquire()
        inode = (self.root / "machine.lock").stat().st_ino
        record = self.lease.check(owner)
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["session_id"], "parent-session")
        self.assertEqual(len(bytes.fromhex(record["token"])), 32)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for name in ("machine.lock", "owner.json"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)
        owner.release(cleanup_verified=True, cleanup_note="Fixture cleanup verified")
        released = self.metadata()
        self.assertEqual(released["state"], "released")
        self.assertTrue(released["cleanup"]["verified"])
        next_owner = self.acquire()
        self.assertNotEqual(next_owner.token, owner.token)
        self.assertEqual((self.root / "machine.lock").stat().st_ino, inode)
        self.assertEqual(self.metadata()["previous_owner"]["state"], "released")

    def test_process_contention_is_prompt_and_preserves_owner_record(self):
        first, result = self.child()
        self.assertTrue(result["acquired"])
        before = (self.root / "owner.json").read_bytes()
        start = time.monotonic()
        second, result = self.child()
        self.assertEqual(result, {"error": "LeaseBusy"})
        second.wait(timeout=3)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        self.assertIsNone(first.poll())
        self.assertEqual(self.command(first, "release"), {"released": True})

    def test_expiry_with_live_process_never_steals_lock(self):
        first, result = self.child(ttl=0.15)
        self.assertTrue(result["acquired"])
        time.sleep(0.25)
        self.assertEqual(self.command(first, "check"), {"error": "LeaseExpired"})
        before = (self.root / "owner.json").read_bytes()
        second, result = self.child()
        self.assertEqual(result, {"error": "LeaseBusy"})
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        self.assertEqual(self.command(first, "renew 10"), {"error": "LeaseExpired"})
        self.assertEqual(self.command(first, "release"), {"released": True})

    def test_crash_frees_os_lock_but_requires_verified_recovery(self):
        child, result = self.child()
        self.assertTrue(result["acquired"])
        before = (self.root / "owner.json").read_bytes()
        child.stdin.write("crash\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=3), 17)
        with self.assertRaises(lease_module.RecoveryRequired) as caught:
            self.acquire()
        self.assertEqual(caught.exception.previous["pid"], child.pid)
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        seen = []

        def verified(previous):
            seen.append(previous)
            # A second process must still be excluded during caller recovery.
            contender, answer = self.child()
            self.assertEqual(answer, {"error": "LeaseBusy"})
            contender.wait(timeout=3)
            return "Fixture child exited; no external side effects existed"

        owner = self.acquire(recovery_check=verified)
        record = self.lease.check(owner)
        self.assertEqual(len(seen), 1)
        self.assertEqual(record["previous_owner"]["pid"], child.pid)
        self.assertIsNotNone(record["recovery"]["verified_at"])

    def test_unverified_release_requires_recovery_and_bad_attestation_fails_closed(self):
        owner = self.acquire()
        owner.release()
        before = (self.root / "owner.json").read_bytes()
        with self.assertRaises(lease_module.RecoveryRequired):
            self.acquire()
        with self.assertRaises(ValueError):
            self.acquire(recovery_check=lambda _: True)
        with self.assertRaises(ValueError):
            self.acquire(recovery_check=lambda _: "")
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        self.acquire(recovery_check=lambda _: "Fixture cleanup verified")

    def test_renewal_preserves_exclusivity_and_updates_expiry(self):
        child, result = self.child(ttl=0.5)
        self.assertTrue(result["acquired"])
        before = self.metadata()
        answer = self.command(child, "renew 3")
        after = self.metadata()
        self.assertEqual(answer["renewed_at"], after["renewed_at"])
        self.assertGreater(after["expires_at"], before["expires_at"])
        self.assertEqual(after["token"], before["token"])
        contender, answer = self.child()
        self.assertEqual(answer, {"error": "LeaseBusy"})
        contender.wait(timeout=3)
        self.assertEqual(self.command(child, "check"), {"valid": True})
        self.assertEqual(self.command(child, "release"), {"released": True})

    def test_nesting_requires_same_handle_and_token_and_cannot_release_outer(self):
        owner = self.acquire()
        before = (self.root / "owner.json").read_bytes()
        with self.lease.nested(owner, owner.token) as nested:
            self.assertIs(nested, owner)
            with self.lease.nested(owner, owner.token):
                self.lease.check(owner)
                with self.assertRaises(lease_module.InvalidHandle):
                    owner.release(cleanup_verified=True, cleanup_note="Not outer owner")
        with self.assertRaises(lease_module.InvalidHandle):
            with self.lease.nested(owner, "foreign-token"):
                self.fail("Foreign token admitted")
        other = lease_module.MachineLease(self.root)
        with self.assertRaises(lease_module.InvalidHandle):
            with other.nested(owner, owner.token):
                self.fail("Foreign handle admitted")
        with self.assertRaises(lease_module.LeaseBusy):
            other.acquire("other", "other")
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        self.lease.check(owner)

    def test_nested_exception_unwinds_without_releasing_outer(self):
        owner = self.acquire()
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with self.lease.nested(owner, owner.token):
                raise RuntimeError("fixture")
        self.assertEqual(owner._depth, 1)
        self.lease.check(owner)

    def test_released_handles_and_tokens_never_reacquire(self):
        owner = self.acquire()
        token = owner.token
        owner.release(cleanup_verified=True, cleanup_note="Fixture clean")
        for action in (lambda: self.lease.check(owner), lambda: owner.renew(), lambda: owner.release()):
            with self.assertRaises(lease_module.InvalidHandle):
                action()
        with self.assertRaises(lease_module.InvalidHandle):
            with self.lease.nested(owner, token):
                self.fail("Released handle admitted")
        new_owner = self.acquire()
        with self.assertRaises(lease_module.InvalidHandle):
            with self.lease.nested(new_owner, token):
                self.fail("Stale token admitted")

    def test_metadata_edit_fails_closed_and_release_does_not_overwrite_it(self):
        owner = self.acquire()
        p = self.root / "owner.json"
        changed = p.read_text().replace("parent-task", "changed-task")
        p.write_text(changed)
        for action in (lambda: self.lease.check(owner), lambda: owner.renew(), lambda: owner.release()):
            with self.assertRaises(lease_module.LeaseTampered):
                action()
        self.assertEqual(p.read_text(), changed)
        self.assertFalse(owner._active)
        with self.assertRaises(lease_module.RecoveryRequired):
            self.acquire()

    def test_identical_metadata_replacement_is_detected(self):
        owner = self.acquire()
        p = self.root / "owner.json"
        replacement = self.root / "replacement"
        replacement.write_bytes(p.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, p)
        with self.assertRaises(lease_module.LeaseTampered):
            self.lease.check(owner)

    def test_lock_inode_replacement_cannot_create_second_owner(self):
        owner = self.acquire()
        lock = self.root / "machine.lock"
        replacement = self.root / "replacement"
        replacement.touch(mode=0o600)
        os.replace(replacement, lock)
        with self.assertRaises(lease_module.LeaseError):
            self.lease.check(owner)
        with self.assertRaises(lease_module.LeaseTampered):
            self.acquire()

    def test_symlinks_unsafe_modes_and_hardlinks_are_rejected(self):
        target = self.parent / "target"
        target.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(lease_module.UnsafeRuntime):
            self.acquire()
        self.root.unlink()
        self.root.mkdir(mode=0o755)
        with self.assertRaises(lease_module.UnsafeRuntime):
            self.acquire()
        self.root.chmod(0o700)
        lock = self.root / "machine.lock"
        victim = target / "victim"
        victim.write_text("Preserve me")
        victim.chmod(0o600)
        lock.symlink_to(victim)
        with self.assertRaises(lease_module.UnsafeRuntime):
            self.acquire()
        lock.unlink()
        os.link(victim, lock)
        with self.assertRaises(lease_module.UnsafeRuntime):
            self.acquire()
        self.assertEqual(victim.read_text(), "Preserve me")

    def test_metadata_symlink_and_wrong_permissions_rejected(self):
        owner = self.acquire()
        path = self.root / "owner.json"
        path.chmod(0o644)
        with self.assertRaises(lease_module.UnsafeRuntime):
            owner.renew()
        path.chmod(0o600)
        original = path.read_bytes()
        victim = self.parent / "victim"
        victim.write_bytes(original)
        victim.chmod(0o600)
        path.unlink()
        path.symlink_to(victim)
        with self.assertRaises(lease_module.UnsafeRuntime):
            self.lease.check(owner)
        self.assertEqual(victim.read_bytes(), original)

    def test_write_failure_does_not_extend_authorization_or_claim_cleanup(self):
        owner = self.acquire()
        deadline = owner._deadline
        before = (self.root / "owner.json").read_bytes()
        with mock.patch.object(lease_module.os, "replace", side_effect=OSError("fixture disk error")):
            with self.assertRaises(OSError):
                owner.renew(120)
            self.assertEqual(owner._deadline, deadline)
            self.lease.check(owner)
            with self.assertRaises(OSError):
                owner.release(cleanup_verified=True, cleanup_note="Fixture clean")
        self.assertFalse(owner._active)
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        self.assertEqual(list(self.root.glob(".owner-*.tmp")), [])
        with self.assertRaises(lease_module.RecoveryRequired):
            self.acquire()

    def test_invalid_ttl_and_relative_parent_paths_are_rejected(self):
        for ttl in (0, -1, float("nan"), float("inf"), True, 86401):
            with self.assertRaises(ValueError):
                self.acquire(ttl_seconds=ttl)
        for root in ("relative-runtime", str(self.parent / ".." / "runtime")):
            with self.assertRaises(lease_module.UnsafeRuntime):
                lease_module.MachineLease(root).acquire("session", "task")

    def test_interrupted_recovery_releases_tentative_lock_without_changing_record(self):
        owner = self.acquire()
        owner.release()
        before = (self.root / "owner.json").read_bytes()

        def interrupted(_):
            raise KeyboardInterrupt("Fixture interrupted recovery")

        with self.assertRaises(KeyboardInterrupt):
            self.acquire(recovery_check=interrupted)
        self.assertEqual((self.root / "owner.json").read_bytes(), before)
        self.acquire(recovery_check=lambda _: "Fixture cleanup verified")

    def test_forward_wall_clock_with_stationary_monotonic_expires_without_takeover(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(lease_module, "_now", return_value=start) as wall, \
                mock.patch.object(lease_module.time, "monotonic", return_value=1000):
            owner = self.acquire(ttl_seconds=60)
            before = (self.root / "owner.json").read_bytes()
            wall.return_value = start + timedelta(seconds=60)
            with self.assertRaises(lease_module.LeaseExpired):
                self.lease.check(owner)
            with self.assertRaises(lease_module.LeaseExpired):
                owner.renew(120)
            with self.assertRaises(lease_module.LeaseExpired):
                with self.lease.nested(owner, owner.token):
                    self.fail("Wall-expired nested authority admitted")
            self.assertEqual((self.root / "owner.json").read_bytes(), before)
            contender, result = self.child()
            self.assertEqual(result, {"error": "LeaseBusy"})
            contender.wait(timeout=3)
            owner.release(cleanup_verified=True, cleanup_note="Fixture cleanup verified after expiry")
            released = self.metadata()
            self.assertEqual(released["state"], "released")
            self.assertTrue(released["cleanup"]["verified"])
            self.assertEqual(released["released_at"], wall.return_value.isoformat())
            self.acquire()

    def test_observed_wall_expiry_cannot_be_revived_by_clock_rollback(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(lease_module, "_now", return_value=start) as wall, \
                mock.patch.object(lease_module.time, "monotonic", return_value=1000):
            owner = self.acquire(ttl_seconds=60)
            wall.return_value = start + timedelta(seconds=61)
            with self.assertRaises(lease_module.LeaseExpired):
                self.lease.check(owner)
            wall.return_value = start
            with self.assertRaises(lease_module.LeaseExpired):
                self.lease.check(owner)
            with self.assertRaises(lease_module.LeaseExpired):
                owner.renew()
            owner.release()
            self.assertFalse(self.metadata()["cleanup"]["verified"])
            with self.assertRaises(lease_module.RecoveryRequired):
                self.acquire()

    def test_wall_expired_renewal_without_prior_check_preserves_deadlines_and_record(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(lease_module, "_now", return_value=start) as wall, \
                mock.patch.object(lease_module.time, "monotonic", return_value=1000):
            owner = self.acquire(ttl_seconds=60)
            deadline = owner._deadline
            before = (self.root / "owner.json").read_bytes()
            wall.return_value = start + timedelta(hours=1)
            with self.assertRaises(lease_module.LeaseExpired):
                owner.renew(ttl_seconds=120)
            self.assertEqual(owner._deadline, deadline)
            self.assertEqual((self.root / "owner.json").read_bytes(), before)
            owner.release()

    def test_monotonic_expiry_still_applies_when_wall_clock_moves_backwards(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(lease_module, "_now", return_value=start) as wall, \
                mock.patch.object(lease_module.time, "monotonic", return_value=1000) as monotonic:
            owner = self.acquire(ttl_seconds=60)
            wall.return_value = start - timedelta(hours=1)
            monotonic.return_value = 1060
            with self.assertRaises(lease_module.LeaseExpired):
                self.lease.check(owner)
            with self.assertRaises(lease_module.LeaseExpired):
                owner.renew()
            owner.release(cleanup_verified=True, cleanup_note="Fixture cleanup verified")

    def test_renewal_refreshes_both_deadlines_before_either_expires(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with mock.patch.object(lease_module, "_now", return_value=start) as wall, \
                mock.patch.object(lease_module.time, "monotonic", return_value=1000) as monotonic:
            owner = self.acquire(ttl_seconds=10)
            wall.return_value = start + timedelta(seconds=5)
            monotonic.return_value = 1005
            renewed = owner.renew(ttl_seconds=20)
            self.assertEqual(renewed["expires_at"], (start + timedelta(seconds=25)).isoformat())
            self.assertEqual(owner._deadline, 1025)
            wall.return_value = start + timedelta(seconds=24)
            monotonic.return_value = 1024
            self.lease.check(owner)
            wall.return_value = start + timedelta(seconds=25)
            with self.assertRaises(lease_module.LeaseExpired):
                self.lease.check(owner)
            owner.release()


if __name__ == "__main__":
    unittest.main()
