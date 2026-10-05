"""Cooperative persistent lease holder; Python 3.9+, POSIX, standard library.

No work execution: the protocol manages membership, not subprocesses or GUI.
An application must explicitly register its own semantic cleanup verifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import sys
import time

from machine_lease import MachineLease, LeaseError, RecoveryRequired

MAX_FRAME = 8192
MAX_EVIDENCE = 1048576
SOCKET_NAME = re.compile(r"holder-[0-9a-f]{32}\.sock\Z")


class CoordinatorFailure(RuntimeError):
    """Persistence failed; stop serving rather than grant unaudited authority."""


def utc():
    return datetime.now(timezone.utc).isoformat()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("nonfinite number")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError as error:
        raise ValueError("excessive JSON nesting") from error
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def duration(value, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("duration required")
    if value <= 0 or value > maximum or not math.isfinite(value):
        raise ValueError("duration outside bounds")
    return value


def private_stat(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("unsafe private file")
    return info


def open_directory(path):
    path = os.fspath(path)
    if not os.path.isabs(path) or ".." in path.split(os.sep):
        raise ValueError("absolute path without traversal required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(path).parts[1:]:
            other = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = other
        return fd
    except BaseException:
        os.close(fd)
        raise


def root_directory(path):
    fd = open_directory(path)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError("private runtime required")
    return fd


def read_file(root_fd, name, maximum=MAX_FRAME):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        info = private_stat(fd)
        if info.st_size > maximum:
            raise ValueError("oversized file")
        raw = b""
        while len(raw) <= maximum:
            chunk = os.read(fd, min(8192, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > maximum:
            raise ValueError("oversized file")
        return raw
    finally:
        os.close(fd)


def write_json(root_fd, name, value):
    temporary = ".coordinator-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class EvidenceSnapshot:
    path: str
    sha256: str
    data: bytes


def evidence_snapshot(path, expected):
    if not isinstance(path, str) or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("evidence path and SHA256 required")
    try:
        parent = open_directory(str(Path(path).parent)) if os.path.isabs(path) else None
    except OSError as error:
        raise ValueError("unsafe evidence parent") from error
    if parent is None or ".." in path.split(os.sep):
        if parent is not None:
            os.close(parent)
        raise ValueError("absolute evidence path required")
    try:
        try:
            raw = read_file(parent, Path(path).name, MAX_EVIDENCE)
        except OSError as error:
            raise ValueError("unsafe evidence file") from error
    finally:
        os.close(parent)
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise ValueError("evidence hash mismatch")
    return EvidenceSnapshot(path, digest, raw)


def receive_frame(connection):
    raw = b""
    while len(raw) <= MAX_FRAME:
        chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(raw)))
        if not chunk:
            raise ValueError("incomplete frame")
        raw += chunk
        if b"\n" in raw:
            frame, remainder = raw.split(b"\n", 1)
            if remainder or len(raw) > MAX_FRAME:
                raise ValueError("one bounded frame required")
            return decode(frame)
    raise ValueError("oversized frame")


class LeaseCoordinator:
    """One process holds ownership until verified release or process exit.

    cleanup_verifier(snapshot, context) must return exactly True, based on the
    application's actual restoration checks. No verifier is registered by default.
    """

    def __init__(self, runtime_root, session_id, task_id, ttl_seconds=60,
                 cleanup_verifier=None, recovery_evidence=None, max_step_seconds=600):
        self.root = Path(runtime_root)
        # Reject predictable bind failures before acquiring/poisoning ownership.
        limit = 108 if sys.platform.startswith('linux') else 104
        if len(os.fsencode(str(self.root / ('holder-' + '0' * 32 + '.sock')))) >= limit:
            raise ValueError('runtime socket path too long')
        self.lease = MachineLease(self.root)
        self.session_id, self.task_id = session_id, task_id
        self.verifier = cleanup_verifier
        if self.verifier is not None and not callable(self.verifier):
            raise ValueError("registered verifier must be callable")
        self.max_step_seconds = duration(max_step_seconds)
        self.holder_id = secrets.token_hex(16)
        self.started_at = utc()
        self.state = "RUNNING"
        self.steps = {}
        self.sequence = 0
        self.closed = False
        self.root_fd = self.audit_fd = None
        self.listener = None
        self.owner = None
        self.socket_name = "holder-" + self.holder_id + ".sock"
        self.socket_identity = None
        def recover(previous):
            if recovery_evidence is None:
                return None
            verified, snapshot = self.verify_evidence(recovery_evidence, "recovery", previous)
            if not verified:
                raise RecoveryRequired(previous)
            return "Registered recovery verifier; sha256=" + snapshot.sha256
        try:
            self.owner = self.lease.acquire(session_id, task_id, ttl_seconds,
                                            recovery_check=recover if recovery_evidence is not None else None)
            self.root_fd = root_directory(self.root)
            info = os.fstat(self.root_fd)
            if list((info.st_dev, info.st_ino)) != self.lease.check(self.owner)["runtime_identity"]:
                raise ValueError("runtime identity changed")
            self.audit_fd = os.open("transitions.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    0o600, dir_fd=self.root_fd)
            audit_info = private_stat(self.audit_fd)
            if audit_info.st_size and os.pread(self.audit_fd, 1, audit_info.st_size - 1) != b"\n":
                raise ValueError("incomplete audit tail requires repair")
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.root / self.socket_name))
            os.chmod(self.root / self.socket_name, 0o600)
            info = os.stat(self.socket_name, dir_fd=self.root_fd, follow_symlinks=False)
            self.socket_identity = (info.st_dev, info.st_ino)
            self.listener.listen(8)
            self.listener.settimeout(0.1)
            write_json(self.root_fd, "capability.json", dict(self.credentials(), socket_name=self.socket_name))
            self.transition("acquired")
        except BaseException:
            self.close()
            raise

    def credentials(self):
        return dict(holder_id=self.holder_id, session_id=self.session_id,
                    task_id=self.task_id, token=self.owner.token)

    def verify_evidence(self, descriptor, purpose, previous=None):
        if not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256"}:
            raise ValueError("evidence descriptor required")
        snapshot = evidence_snapshot(descriptor["path"], descriptor["sha256"])
        context = dict(purpose=purpose, holder_id=self.holder_id,
                       session_id=self.session_id, task_id=self.task_id,
                       previous_owner=previous, state=self.state, active_steps=len(self.steps))
        verified = False
        if self.verifier is not None:
            try:
                verified = self.verifier(snapshot, context) is True
            except Exception:
                verified = False
        # Reopen and hash after callback: reject changed artifacts, not just a false verdict.
        if evidence_snapshot(snapshot.path, snapshot.sha256).data != snapshot.data:
            raise ValueError("evidence changed")
        return verified, snapshot

    def status(self):
        return dict(holder_id=self.holder_id, pid=os.getpid(), started_at=self.started_at,
                    state=self.state, active_steps=len(self.steps), pause_eta_seconds=None,
                    session_id=self.session_id, task_id=self.task_id)

    def transition(self, event, publish=True, **details):
        try:
            self._transition(event, publish, **details)
        except (OSError, ValueError) as error:
            self.state = "RECOVERY_REQUIRED"
            raise CoordinatorFailure("coordinator audit/persistence failed") from error

    def _transition(self, event, publish, **details):
        self.sequence += 1
        record = dict(timestamp=utc(), holder_id=self.holder_id, sequence=self.sequence,
                      event=event, state=self.state, details=details)
        info = private_stat(self.audit_fd)
        named = os.stat("transitions.jsonl", dir_fd=self.root_fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("audit identity changed")
        raw = (json.dumps(record, sort_keys=True) + "\n").encode()
        if os.write(self.audit_fd, raw) != len(raw):
            raise ValueError("incomplete audit write")
        os.fsync(self.audit_fd)
        if publish:
            write_json(self.root_fd, "coordinator.json", dict(self.status(), socket_name=self.socket_name))

    def observe(self):
        if self.state == "RECOVERY_REQUIRED":
            return
        failed = False
        try:
            self.lease.check(self.owner)
        except LeaseError:
            failed = True
        if any(time.monotonic() >= item["monotonic_deadline"] or time.time() >= item["wall_deadline"]
               for item in self.steps.values()):
            failed = True
        if failed:
            self.state = "RECOVERY_REQUIRED"
            self.transition("authority_or_step_expired")

    def authenticate(self, auth):
        if not isinstance(auth, dict) or set(auth) != set(self.credentials()):
            return False
        return all(isinstance(auth[key], str) and hmac.compare_digest(auth[key].encode("utf-8"), value.encode("utf-8"))
                   for key, value in self.credentials().items())

    def handle(self, request):
        self.observe()
        action = request.get("action")
        allowed = {"status": set(), "renew": {"ttl_seconds"},
                   "begin_step": {"label", "duration_seconds"},
                   "end_step": {"step_id", "step_token", "outcome"},
                   "request_pause": {"reason"}, "release": {"evidence"}}
        if not isinstance(action, str) or action not in allowed:
            return dict(ok=False, code="INVALID_REQUEST", **self.status())
        fields = set(request) - {"action", "auth", "claimed_session_id"}
        if fields != allowed[action]:
            return dict(ok=False, code="INVALID_REQUEST", **self.status())
        if action != "status" or "auth" in request:
            if not self.authenticate(request.get("auth")):
                return dict(ok=False, code="UNAUTHORIZED", **self.status())
        if action not in ('status', 'request_pause'):
            if request.get('claimed_session_id') != self.session_id:
                return dict(ok=False, code='OWNER_SESSION_REQUIRED', **self.status())
        def reply(ok=True, code="OK", **extra):
            return dict(ok=ok, code=code, **self.status(), **extra)
        if action == "status":
            return reply()
        if action in ("renew", "begin_step") and self.state == "RECOVERY_REQUIRED":
            return reply(False, "RECOVERY_REQUIRED")
        if action == "renew":
            ttl = duration(request["ttl_seconds"])
            self.owner.renew(ttl)
            self.transition("renewed")
            return reply()
        if action == "begin_step":
            if self.state != "RUNNING":
                return reply(False, "DRAINING")
            label = request["label"]
            if not isinstance(label, str) or not label.strip() or len(label) > 128:
                raise ValueError("bounded label required")
            seconds = duration(request["duration_seconds"], self.max_step_seconds)
            if len(self.steps) >= 32:
                return reply(False, "STEP_LIMIT")
            # Revalidate immediately before granting new membership.
            self.lease.check(self.owner)
            identifier, token = secrets.token_hex(16), secrets.token_hex(32)
            self.steps[identifier] = dict(token=token, started_at=utc(), label=label,
                                         monotonic_deadline=time.monotonic() + seconds,
                                         wall_deadline=time.time() + seconds)
            self.transition("step_begun", step_id=identifier, label=label, duration_seconds=seconds)
            return reply(step_id=identifier, step_token=token)
        if action == "end_step":
            item = self.steps.get(request["step_id"]) if isinstance(request["step_id"], str) else None
            token = request["step_token"]
            if item is None or not isinstance(token, str) or not hmac.compare_digest(token, item["token"]):
                return reply(False, "STEP_MEMBERSHIP_REQUIRED")
            if request["outcome"] not in ("completed", "failed", "interrupted"):
                raise ValueError("invalid outcome")
            del self.steps[request["step_id"]]
            self.transition("step_ended", step_id=request["step_id"], outcome=request["outcome"])
            return reply()
        if action == "request_pause":
            reason = request["reason"]
            if not isinstance(reason, str) or len(reason) > 256:
                raise ValueError("bounded reason required")
            if self.state == "RUNNING":
                self.state = "DRAINING"
            self.transition("pause_requested", reason=reason)
            return reply()
        if self.steps:
            return reply(False, "ACTIVE_STEPS")
        verified, snapshot = self.verify_evidence(request["evidence"], "cleanup")
        self.observe()  # Verifier duration can exhaust authority.
        verified = verified and self.state != "RECOVERY_REQUIRED"
        self.state = "RELEASING" if verified else "RECOVERY_REQUIRED"
        self.transition("cleanup_verified" if verified else "cleanup_unverified",
                        evidence_sha256=snapshot.sha256, evidence_path=snapshot.path)
        self.close_socket()
        try:
            # Recheck after audit fsyncs and endpoint shutdown, before release.
            self.lease.check(self.owner)
        except LeaseError:
            verified = False
            self.state = "RECOVERY_REQUIRED"
            self.transition("release_authority_expired")
        try:
            self.owner.release(cleanup_verified=verified,
                               cleanup_note="Registered cleanup verifier; sha256=" + snapshot.sha256 if verified else None)
        except BaseException:
            self.state = "RECOVERY_REQUIRED"
            self.transition("release_failed", publish=False)
            self.closed = True
            raise
        self.closed = True
        self.state = 'HUMAN_SAFE' if verified else 'RECOVERY_REQUIRED'
        # Never replace discovery after dropping ownership: a successor may hold it.
        self.transition("released", publish=False)
        return reply()

    def serve(self):
        try:
            while not self.closed:
                self.observe()
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(1)
                    try:
                        request = receive_frame(connection)
                        response = self.handle(request)
                    except (ValueError, TypeError, KeyError, socket.timeout):
                        response = dict(ok=False, code="INVALID_REQUEST", **self.status())
                    except LeaseError:
                        self.state = "RECOVERY_REQUIRED"
                        self.transition("lease_refused")
                        response = dict(ok=False, code="RECOVERY_REQUIRED", **self.status())
                    try:
                        connection.sendall((json.dumps(response) + "\n").encode())
                    except OSError:
                        pass
        finally:
            self.close()

    def close_socket(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.socket_identity is not None and self.root_fd is not None:
            try:
                info = os.stat(self.socket_name, dir_fd=self.root_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == self.socket_identity:
                    os.unlink(self.socket_name, dir_fd=self.root_fd)
            except FileNotFoundError:
                pass
            self.socket_identity = None

    def close(self):
        try:
            self.close_socket()
        finally:
            try:
                if self.owner is not None and not self.closed:
                    self.state = "RECOVERY_REQUIRED"
                    try:
                        if self.audit_fd is not None:
                            self.transition("holder_stopped_unverified")
                    finally:
                        self.owner.release()
            finally:
                self.closed = True
                for name in ("audit_fd", "root_fd"):
                    fd = getattr(self, name)
                    if fd is not None:
                        os.close(fd)
                        setattr(self, name, None)


class CoordinatorClient:
    """Talk to the live holder; cached metadata is never a safe-state proof."""

    def __init__(self, runtime_root, session_id=None):
        self.root = Path(runtime_root)
        self.session_id = session_id

    def request(self, action, authenticated=True, credentials=None, **payload):
        try:
            root_fd = root_directory(self.root)
            try:
                discovery = decode(read_file(root_fd, "coordinator.json"))
                name = discovery["socket_name"]
                if not isinstance(name, str) or not SOCKET_NAME.fullmatch(name):
                    raise ValueError("unsafe socket name")
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("unsafe socket")
                request = dict(action=action, **payload)
                if action not in ('status', 'request_pause'):
                    if not self.session_id:
                        return dict(ok=False, code='OWNER_SESSION_REQUIRED', state='UNKNOWN')
                    request['claimed_session_id'] = self.session_id
                if authenticated:
                    if credentials is None:
                        stored = decode(read_file(root_fd, "capability.json"))
                        credentials = {key: stored[key] for key in ("holder_id", "session_id", "task_id", "token")}
                    request["auth"] = credentials
                raw = (json.dumps(request, allow_nan=False) + "\n").encode()
                if len(raw) > MAX_FRAME:
                    raise ValueError("request exceeds frame limit")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(3)
                    connection.connect(str(self.root / name))
                    connection.sendall(raw)
                    response = receive_frame(connection)
                if response.get("holder_id") != discovery["holder_id"]:
                    raise ValueError("holder identity mismatch")
                return response
            finally:
                os.close(root_fd)
        except (OSError, ValueError, KeyError, TypeError):
            return dict(ok=False, code="HOLDER_UNAVAILABLE", state="RECOVERY_REQUIRED")
