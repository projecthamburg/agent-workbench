"""POSIX machine ownership: an OS lock plus explicit, caller-attested recovery.

Standard library only, Python 3.9+. This coordinates cooperative callers; it is
not a security boundary against another program running as the same OS user.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
import weakref


class LeaseError(RuntimeError):
    """Ownership could not be established or maintained."""


class LeaseBusy(LeaseError):
    """Another open file description holds the machine lock."""


class UnsafeRuntime(LeaseError):
    """The runtime path or file identity/permissions are unsafe."""


class LeaseTampered(LeaseError):
    """Ownership metadata or a locked path changed unexpectedly."""


class RecoveryRequired(LeaseError):
    """Available lock has no verified cleanup from its previous owner."""

    def __init__(self, previous):
        super().__init__("Machine cleanup must be verified before new ownership")
        self.previous = previous


class InvalidHandle(LeaseError):
    """A foreign, released, inherited or incorrectly authenticated handle."""


class LeaseExpired(LeaseError):
    """Authorization expired; the OS lock is still held until release/exit."""


_HANDLES = weakref.WeakSet()
_MAX_METADATA = 65536


def _now():
    return datetime.now(timezone.utc)


def _ttl(value):
    if isinstance(value, bool):
        raise ValueError("ttl_seconds must be finite and positive")
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > 86400:
        raise ValueError("ttl_seconds must be positive and at most 86400")
    return value


def _label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(name + " must be a nonempty string of at most 512 characters")
    return value


def _identity(st):
    return st.st_dev, st.st_ino


def _private_file(fd):
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1):
        raise UnsafeRuntime("Lease file must be owner-owned, regular, single-link and mode 0600")
    return st


def _root_fd(root, create=False):
    """Traverse through directory descriptors, rejecting symlink components."""
    path = os.fspath(root)
    if not os.path.isabs(path) or ".." in path.split(os.sep):
        raise UnsafeRuntime("Use an absolute runtime path without '..' components")
    parts = Path(path).parts[1:]
    if not parts:
        raise UnsafeRuntime("The filesystem root cannot be the private runtime")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.sep, flags)
    try:
        for i, part in enumerate(parts):
            if create and i == len(parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise UnsafeRuntime("Private runtime must be owner-owned and mode 0700")
        return fd
    except (OSError, LeaseError) as exc:
        os.close(fd)
        if isinstance(exc, LeaseError):
            raise
        raise UnsafeRuntime("Unsafe or unavailable runtime directory") from exc


def _open_lock(root_fd):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open("machine.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
        created = True
    except FileExistsError:
        try:
            fd = os.open("machine.lock", flags, dir_fd=root_fd)
        except OSError as exc:
            raise UnsafeRuntime("Unsafe or unavailable machine lock") from exc
        created = False
    try:
        _private_file(fd)
    except Exception:
        os.close(fd)
        raise
    return fd, created


def _read_metadata(root_fd):
    try:
        fd = os.open("owner.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    except FileNotFoundError:
        return None, None, None
    except OSError as exc:
        raise UnsafeRuntime("Unsafe ownership metadata path") from exc
    try:
        st = _private_file(fd)
        if st.st_size > _MAX_METADATA:
            raise LeaseTampered("Oversized ownership metadata")
        raw = b""
        while len(raw) <= _MAX_METADATA:
            chunk = os.read(fd, min(8192, _MAX_METADATA + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > _MAX_METADATA:
            raise LeaseTampered("Oversized ownership metadata")
        try:
            record = json.loads(raw)
            valid = (isinstance(record, dict) and record.get("schema_version") == 1
                     and record.get("state") in ("owned", "released")
                     and isinstance(record.get("session_id"), str)
                     and isinstance(record.get("task_id"), str)
                     and type(record.get("pid")) is int
                     and isinstance(record.get("token"), str)
                     and len(record["token"]) == 64
                     and isinstance(record.get("acquired_at"), str)
                     and isinstance(record.get("expires_at"), str)
                     and isinstance(record.get("lock_identity"), list)
                     and len(record["lock_identity"]) == 2
                     and all(type(x) is int for x in record["lock_identity"])
                     and isinstance(record.get("runtime_identity"), list)
                     and len(record["runtime_identity"]) == 2
                     and all(type(x) is int for x in record["runtime_identity"])
                     and isinstance(record.get("cleanup"), dict)
                     and type(record["cleanup"].get("verified")) is bool)
            if not valid:
                raise ValueError("invalid ownership schema")
            acquired = datetime.fromisoformat(record["acquired_at"])
            expires = datetime.fromisoformat(record["expires_at"])
            if acquired.tzinfo is None or expires.tzinfo is None:
                raise ValueError("ownership timestamp timezone missing")
            if any(c not in "0123456789abcdef" for c in record["token"]):
                raise ValueError("invalid ownership token")
            if record["cleanup"]["verified"]:
                _label(record["cleanup"].get("attestation"), "cleanup attestation")
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise LeaseTampered("Invalid ownership metadata") from exc
        if _identity(os.stat("owner.json", dir_fd=root_fd, follow_symlinks=False)) != _identity(st):
            raise LeaseTampered("Metadata path changed during read")
        return record, hashlib.sha256(raw).hexdigest(), _identity(st)
    finally:
        os.close(fd)


class OwnerHandle:
    """Opaque in-process capability. A token alone cannot create a handle."""

    def __init__(self, lease, root_fd, lock_fd, record, digest, metadata_identity, deadline):
        self._lease = lease
        self._root_fd = root_fd
        self._lock_fd = lock_fd
        self._record = record
        self._digest = digest
        self._metadata_identity = metadata_identity
        self._deadline = deadline
        self._expired = False
        self._pid = os.getpid()
        self._active = True
        self._depth = 1
        _HANDLES.add(self)

    @property
    def token(self):
        return self._record["token"]

    def renew(self, ttl_seconds=60):
        return self._lease.renew(self, ttl_seconds)

    def release(self, cleanup_verified=False, cleanup_note=None):
        self._lease.release(self, cleanup_verified, cleanup_note)

    def __enter__(self):
        self._lease.check(self)
        return self

    def __exit__(self, *_):
        self.release()

    def _close(self):
        # Close rather than LOCK_UN: forked copies must never unlock a parent.
        for name in ("_lock_fd", "_root_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                setattr(self, name, -1)
                os.close(fd)
        self._active = False


class MachineLease:
    """All cooperative entry points must use the SAME fixed runtime path.

    ``recovery_check(previous)`` runs under the acquired OS lock and returns a
    nonempty attestation string only after caller-specific cleanup verification.
    This module cannot verify routing, GUI state or a worker's external effects.
    """

    def __init__(self, runtime_root):
        self.runtime_root = Path(runtime_root)
        self._guard = threading.RLock()

    def _paths(self, root_fd, lock_fd):
        fresh = _root_fd(self.runtime_root)
        try:
            if _identity(os.fstat(fresh)) != _identity(os.fstat(root_fd)):
                raise LeaseTampered("Runtime directory changed while locked")
            locked = _private_file(lock_fd)
            st = os.stat("machine.lock", dir_fd=fresh, follow_symlinks=False)
            if (not stat.S_ISREG(st.st_mode) or _identity(st) != _identity(locked)):
                raise LeaseTampered("Machine lock path changed; do not recreate it")
        finally:
            os.close(fresh)

    def _write(self, root_fd, lock_fd, record, expected_digest, expected_identity):
        self._paths(root_fd, lock_fd)
        _, digest, identity = _read_metadata(root_fd)
        if digest != expected_digest or identity != expected_identity:
            raise LeaseTampered("Ownership metadata changed unexpectedly")
        raw = (json.dumps(record, sort_keys=True) + "\n").encode()
        if len(raw) > _MAX_METADATA:
            raise ValueError("Ownership record too large")
        temporary = ".owner-" + secrets.token_hex(16) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=root_fd)
        try:
            _private_file(fd)
            with os.fdopen(fd, "wb", closefd=False) as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fd)
            self._paths(root_fd, lock_fd)
            _, current, identity = _read_metadata(root_fd)
            if current != expected_digest or identity != expected_identity:
                raise LeaseTampered("Ownership metadata changed during update")
            os.replace(temporary, "owner.json", src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
        result, digest, identity = _read_metadata(root_fd)
        if result != record or digest != hashlib.sha256(raw).hexdigest():
            raise LeaseTampered("Metadata changed after update")
        return digest, identity

    def acquire(self, session_id, task_id, ttl_seconds=60, recovery_check=None):
        """Nonblocking acquisition. Expiry never steals another owner's lock."""
        session_id = _label(session_id, "session_id")
        task_id = _label(task_id, "task_id")
        ttl_seconds = _ttl(ttl_seconds)
        with self._guard:
            root_fd = _root_fd(self.runtime_root, create=True)
            lock_fd = -1
            try:
                lock_fd, created = _open_lock(root_fd)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EACCES):
                        raise LeaseBusy("Machine is owned; acquisition is nonblocking") from exc
                    raise
                self._paths(root_fd, lock_fd)
                previous, digest, metadata_identity = _read_metadata(root_fd)
                if previous and (previous["lock_identity"] != list(_identity(os.fstat(lock_fd)))
                                 or previous["runtime_identity"] != list(_identity(os.fstat(root_fd)))):
                    raise LeaseTampered("Stable lock or runtime identity changed")
                needs_recovery = ((previous is None and not created)
                                  or (previous is not None and
                                      (previous["state"] != "released" or not previous["cleanup"]["verified"])))
                recovery = None
                if needs_recovery:
                    if recovery_check is None:
                        raise RecoveryRequired(previous)
                    note = recovery_check(json.loads(json.dumps(previous)))
                    note = _label(note, "recovery attestation")
                    recovery = {"verified_at": _now().isoformat(), "attestation": note}
                now = _now()
                deadline = time.monotonic() + ttl_seconds
                record = {"schema_version": 1, "state": "owned", "session_id": session_id,
                          "task_id": task_id, "pid": os.getpid(), "token": secrets.token_hex(32),
                          "lock_identity": list(_identity(os.fstat(lock_fd))),
                          "runtime_identity": list(_identity(os.fstat(root_fd))),
                          "acquired_at": now.isoformat(), "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                          "renewed_at": None, "cleanup": {"verified": False, "attestation": None},
                          "recovery": recovery, "previous_record_sha256": digest,
                          "previous_owner": ({k: previous.get(k) for k in
                                              ("session_id", "task_id", "pid", "state", "acquired_at",
                                               "expires_at", "released_at", "cleanup")} if previous else None)}
                new_digest, new_identity = self._write(root_fd, lock_fd, record, digest, metadata_identity)
                return OwnerHandle(self, root_fd, lock_fd, record, new_digest, new_identity, deadline)
            except BaseException:
                if lock_fd >= 0:
                    os.close(lock_fd)
                os.close(root_fd)
                raise

    def check(self, owner, token=None):
        """Validate ownership and unexpired authorization; never bypass expiry."""
        return self._check(owner, token)

    def _check(self, owner, token=None, allow_expired=False):
        with self._guard:
            if (not isinstance(owner, OwnerHandle) or owner._lease is not self
                    or not owner._active or owner._pid != os.getpid()):
                raise InvalidHandle("Owner handle is foreign, inherited or released")
            if token is not None and (not isinstance(token, str) or not hmac.compare_digest(token, owner.token)):
                raise InvalidHandle("Invalid nesting token")
            self._paths(owner._root_fd, owner._lock_fd)
            record, digest, identity = _read_metadata(owner._root_fd)
            if digest != owner._digest or record != owner._record or identity != owner._metadata_identity:
                raise LeaseTampered("Owned metadata is missing or changed")
            if not allow_expired:
                expires_at = datetime.fromisoformat(record["expires_at"])
                if owner._expired or time.monotonic() >= owner._deadline or _now() >= expires_at:
                    # Wall time may advance during sleep while a platform's
                    # monotonic clock pauses. Either deadline is authoritative;
                    # an observed expiry cannot be revived by clock rollback.
                    owner._expired = True
                    raise LeaseExpired("Authorization expired; OS lock remains held")
            return json.loads(json.dumps(record))

    @contextmanager
    def nested(self, owner, token):
        with self._guard:
            self.check(owner, token)
            owner._depth += 1
        try:
            yield owner
        finally:
            with self._guard:
                owner._depth -= 1

    def renew(self, owner, ttl_seconds=60):
        ttl_seconds = _ttl(ttl_seconds)
        with self._guard:
            record = self.check(owner)
            now = _now()
            deadline = time.monotonic() + ttl_seconds
            record["renewed_at"] = now.isoformat()
            record["expires_at"] = (now + timedelta(seconds=ttl_seconds)).isoformat()
            # An update error never expands the existing deadline or capability.
            digest, identity = self._write(owner._root_fd, owner._lock_fd, record, owner._digest, owner._metadata_identity)
            owner._record, owner._digest, owner._metadata_identity = record, digest, identity
            owner._deadline = deadline
            return self.check(owner)

    def release(self, owner, cleanup_verified=False, cleanup_note=None):
        if type(cleanup_verified) is not bool:
            raise ValueError("cleanup_verified must be boolean")
        if cleanup_verified:
            cleanup_note = _label(cleanup_note, "cleanup attestation")
        with self._guard:
            if (not isinstance(owner, OwnerHandle) or owner._lease is not self
                    or not owner._active or owner._pid != os.getpid()):
                raise InvalidHandle("Owner handle is foreign, inherited or released")
            if owner._depth != 1:
                raise InvalidHandle("Nested callers cannot release the outer owner")
            try:
                record = self._check(owner, allow_expired=True)
                record["state"] = "released"
                record["released_at"] = _now().isoformat()
                record["cleanup"] = {"verified": cleanup_verified,
                                     "attestation": cleanup_note if cleanup_verified else None}
                self._write(owner._root_fd, owner._lock_fd, record, owner._digest, owner._metadata_identity)
            finally:
                # Failed metadata validation/write leaves recovery evidence intact.
                # Release still closes our descriptors and revokes this capability.
                owner._close()


def _after_fork():
    for owner in tuple(_HANDLES):
        if owner._active:
            owner._close()
        owner._lease._guard = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
