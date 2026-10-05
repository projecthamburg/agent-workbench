# Machine lease foundation — WB001

Status: generic offline foundation. **No existing live entry point is protected by this module
yet.** Integration is a separate reviewed task. No GUI, VPN, network configuration or live query
is performed by the module or its tests.

## Contract and use

Requires Python 3.9+ and POSIX `fcntl.flock`, directory-relative filesystem operations,
`O_DIRECTORY`, `O_NOFOLLOW`, atomic same-directory replacement and directory `fsync`. Tested on
the current Mac with its Python 3.9 and the installed project interpreter. Windows is unsupported.
Local-filesystem semantics are assumed; do not use NFS, synchronized/cloud folders or separate
paths for the same machine. All participating projects/worktrees must share **one fixed absolute
private runtime directory** supplied by the caller, outside worktree-specific evidence paths.

```python
from machine_lease import MachineLease, LeaseBusy, RecoveryRequired

lease = MachineLease(PRIVATE_MACHINE_RUNTIME)  # caller-supplied, no built-in path
owner = lease.acquire(session_id, task_id, ttl_seconds=60,
                      recovery_check=verify_previous_cleanup)
try:
    lease.check(owner)                       # immediately before reservation/action
    reserve_declared_budget()
    with lease.nested(owner, token=owner.token):
        lease.check(owner)
        perform_bounded_action()
    owner.renew(ttl_seconds=60)               # explicit heartbeat; no background timer
    verify_current_cleanup()
except BaseException:
    owner.release()                          # records unverified release, requires recovery
    raise
else:
    owner.release(cleanup_verified=True,
                  cleanup_note="Caller cleanup receipts independently checked")
```

The caller must preserve its own evidence and apply project-specific cleanup/error handling. This
example has no real reservation, action or cleanup implementation. Initial acquisition does not
certify a clean machine; first use still requires the caller's normal preflight. Do not release
while an active worker can continue acting: drain or stop it, verify the outcome and then release.
Never equate lock availability, a successful `check`, or a cleanup attestation with proven safe
routing/GUI state. This module stores the caller's assertion, not independent external proof.

`acquire` is nonblocking. `LeaseBusy` is prompt contention, including a second `MachineLease`
instance in the same process. It does not change the current owner record. No blocking mode is
provided in WB001. A bounded caller may wait outside the primitive without replaying queries.

Owner metadata includes session/task/PID, UTC acquisition/expiry/renewal/release times, a 256-bit
random token, runtime/lock inode identities, cleanup attestation and prior-owner summary/hash.
The token authenticates a nesting request **with the actual same-process owner handle**. A token
alone cannot acquire, reconstruct or release ownership through the public API. Do not display it
in the panel, public logs or ordinary mail. Do not pass an owner across processes; acquire in the
process owning the whole bounded operation and pass the verified handle through its own calls.

The context-manager owner releases with cleanup **unverified**. Use explicit release plus real
cleanup evidence if the next owner should inherit a caller-attested clean baseline. Nested scopes
share the outer lock and cannot release it; wrong tokens, foreign handles and released handles
are rejected. Nesting is not a grant of task/tool/write authority: downstream code must still
enforce its assigned scope, budgets and consent. Call `check` at effect boundaries, not just once.

## Expiry, crash and recovery

Authorization expires if **either** the owning process's monotonic deadline or the metadata's UTC
wall-clock expiry has been reached. Some platform monotonic clocks can pause during sleep; the
wall-clock guard prevents that divergence from retaining authority after the UTC expiry. Once
observed, expiry is latched in the handle, so a later wall-clock rollback cannot revive it.
Monotonic expiry still applies if the wall clock moves backward. `check`, nesting and renewal
refuse an expired capability; renewal before either deadline refreshes both. Expiry does **not** unlock, steal a
held OS lock, terminate a worker or prove cleanup. The owner can still release after expiry.

Process exit releases its descriptor-backed OS lock. If metadata remains `owned`, or a previous
release was not cleanup-verified, the next `acquire` raises `RecoveryRequired`. Missing metadata
beside an existing stable lock also requires recovery. An optional `recovery_check(previous)` runs
**while the tentative new owner holds the OS lock**; it must perform caller-specific cleanup
verification and return a nonempty attestation string. Returning a Boolean/empty note or raising
fails acquisition without changing the prior record. A successful recovery records its timestamp,
note, previous-owner summary and previous metadata hash. The callback is trusted caller code;
the primitive cannot determine whether its assertion is truthful.

Release never deletes the lock inode. It atomically marks metadata `released`, retains the last
owner and records whether caller cleanup was verified. It revokes/closes the handle even if
metadata validation/write fails; the previous/changed record is preserved for recovery. Renewal
errors do not extend the in-memory deadline. If an error occurred after a metadata replacement,
the old handle no longer matches and later checks fail closed. Capture and investigate before
allowing another action. There is no automatic stale-record deletion or force-steal API.

On supported Python builds, an `after_in_child` fork hook closes inherited descriptors without
issuing `LOCK_UN`, avoiding an inherited child retaining/unlocking the parent's ownership. The
inherited handle is invalid. This hook has not been independently qualified for all forking
libraries or native processes; launched subprocesses use non-inheritable descriptors normally.

## Private-runtime assumptions and defenses

The caller must select trusted ancestors and keep the runtime private to its effective OS user.
The module creates only the final runtime directory (0700), never missing ancestor directories.
Every directory component is opened with no-symlink directory traversal. Supply the real absolute
path: macOS `/var` or `/tmp` aliases may be symlinks and are deliberately rejected. Reject `..`
paths. Existing runtime mode/owner must match; modes are rejected rather than silently repaired.

Lock and metadata must be regular, single-link, effective-user-owned files with exactly 0600
permissions. Symlinks, hardlinks, wrong ownership/modes, oversized/invalid metadata, replaced
lock/runtime identity, changed owned metadata bytes and even same-byte metadata inode replacement
are refused. Metadata replacement uses a random exclusive temporary file, flush/fsync, atomic
rename and directory fsync. Operation checks compare the named lock with the held descriptor;
new acquisition compares lock/runtime identities with the previous record. Never unlink or
manually replace a lock file to recover: doing so can create independent lock domains.

This is **cooperative coordination**, not confinement of hostile same-user code or root. Such code
can ignore the primitive, delete all historical files, alter ancestors, forge records or use Python
internals; no unsigned local metadata can prevent that. There is no protection against arbitrary
human input, nonintegrated applications, remote devices or a different runtime path. Endpoint
cleanup, persistence across power loss and cross-filesystem behavior remain separate obligations.
The module retains the latest record/prior summary, not a complete append-only event journal;
the integrating supervisor must archive relevant records/receipts before replacement.

## Required later integration

Acquire once at the outermost live operation **before** query/cost reservation; nested lower-level
calls receive the verified same-process owner. Define cancellation, budget outcomes and evidence
closure without adding hidden retries. A missing/foreign/expired handle must refuse entry.

The present project's candidate chokepoints for later review are `request_run.py`, `live.run_queue`,
`gig_client` dispatch/seal, native Mysterium/NordLayer app adapters, Tor/DataImpulse activation and
Little Snitch restart/global rule/configuration changes. This list is an adoption checklist, not
a claim that every indirect call path was traced. Separate campaigns must share the same lock.
The panel reads the coordinator's authoritative state, not an independent notion of ownership.

Passive Little Snitch CSV collection/read-only telemetry may coexist; do not hold the exclusive
lease for the lifetime of passive collection. Restart, authentication/configuration UI and global
changes need coordinated ownership. No collector/provider behavior was changed in WB001.

Two prerequisites remain for later live integration, based on the independent WB001 review:

1. **Persistent phase holder/coordinator (F1).** This primitive holds an OS lock in one process.
   It does not keep an agent/task phase owned across separately launched commands. A future
   persistent holder/coordinator must retain the lock across those steps and authenticate bounded
   command membership. Do not release/reacquire between VPN connect, query, capture and cleanup
   and claim continuous phase ownership. No holder, daemon, socket protocol or cross-process
   capability relaxation is implemented in WB001/WB001A.
2. **Sealed cleanup/recovery evidence (F3).** This primitive records caller-attested strings.
   The future coordinator must require and validate concrete sealed cleanup/recovery artifacts,
   their paths and hashes (routing/exit, process/port inventory and provider state), before admitting
   the next phase or showing `HUMAN_SAFE`. Bare strings are not integration evidence. That
   enforcement is not implemented here.

Later integration must also treat wake from sleep as requiring route/GUI re-verification. The
WB001A tests simulate forward-wall/stationary-monotonic and backward-wall/advancing-monotonic
divergence; they do not put the Mac to sleep or prove its actual sleep-clock behavior. A clock
jump forward can conservatively expire an owner early; that fail-closed refusal is intentional.

## Evidence and limits

Tests use temporary private directories and only subprocesses created by the test. They cover
process contention, expiry while the owner is alive, crash plus locked recovery callback, renewal,
bad nesting/stale handles, nested unwind/release rules, metadata tamper/same-byte replacement,
inode replacement, path/file safety, update failure and interrupted recovery. No unrelated process
is terminated. Source: `machine_lease.py` and `test_workbench_machine_lease.py`; actual commands,
interpreter versions, outcomes and hashes are in the canonical WB001 delivery.

Confidence is high for the tested local POSIX scenarios; live integration, independent evaluator
acceptance, adversarial same-user security, all OS/filesystem combinations and external cleanup
are not established. Confirmation bias is reduced by fault injection and cross-process contention;
selection bias remains because tests cover a single Mac and chosen failure cases. Offline passing
tests establish this foundation's behavior within that scope, not a functioning whole workbench.
