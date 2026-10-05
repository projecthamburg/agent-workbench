# Persistent cooperative machine coordinator

WB002 adds a persistent holder around the accepted `MachineLease` API. It does
not change that primitive or protect existing application entry points.
Python 3.9+ and POSIX Unix-domain sockets are required; dependencies are standard
library only. No GUI, VPN, browser, command execution or shell dispatch is implemented.

The application registers **one fixed absolute private runtime root shared across
all projects**, outside worktrees. There is no default. Its parent must already
exist; the accepted primitive creates the final root with mode 0700. Symlink
components and `..` are rejected. Keep the path short enough for the platform's
Unix socket pathname limit (on macOS, the complete socket path must fit 104 bytes).
Socket, capability, discovery and audit files are private to the effective user.
Same-user/root programs can bypass this cooperative mechanism; it is not confinement.

## Registering the holder

Add this module's directory to the trusted application's import path, then run
one long-lived foreground process with this shape:

```python
from lease_coordinator import LeaseCoordinator

# Configured by the application, never inferred from its current directory.
# The application supplies verify_restoration; there is no default verifier.
holder = LeaseCoordinator(
    configured_machine_runtime_root,
    session_id=registered_session_id,
    task_id=registered_task_id,
    ttl_seconds=120,
    cleanup_verifier=verify_restoration,
    max_step_seconds=600,
)
holder.serve()
```

The OS lock stays in this process between commands. Separate client processes
use `CoordinatorClient(configured_machine_runtime_root, session_id=registered_session_id).request(...)`. A client
loads a private capability containing the random holder identity, session/task
identity and opaque token; the live holder checks every field. A PID alone grants
nothing. Each `begin_step` returns another opaque step token. Keep both kinds of
token private; do not put them in arguments, public logs or client deliverables.
Clients explicitly claim the registered owner session for work, renewal, step completion and release. A different or unspecified session is refused even with the shared capability. Human pause may drain a different session; it never grants work. This prevents accidental cross-role membership, not a hostile same-user caller impersonating the owner.

One newline-terminated JSON object is served per socket connection, at most
8192 bytes including newline. Duplicate keys, nonfinite numbers, nonobjects,
extra fields and unknown actions are refused. Read timeout is one second. The
client has a three-second socket timeout. No action takes a shell command.

| Action | Required payload | Effect |
|---|---|---|
| `status` | none | Live identity/state/count; may be unauthenticated |
| `renew` | `ttl_seconds` | Refresh both accepted lease deadlines while valid |
| `begin_step` | `label`, `duration_seconds` | New membership in RUNNING only |
| `end_step` | `step_id`, `step_token`, `outcome` | Finish own membership; outcome completed/failed/interrupted |
| `request_pause` | `reason` | Enter DRAINING; refuse new steps |
| `release` | `evidence: {path, sha256}` | Zero active steps required; validate artifact and registered verifier |

All actions except unauthenticated status require the live capability. Status
reveals holder ID, actual holder PID, start timestamp, state and active count;
it includes non-secret session/task labels for visible ownership but omits tokens and private paths. Keep credentials or private prose out of those labels. It gives no pause ETA.
A finished step cannot be ended twice. At most 32 steps may be active. Steps
have a caller-declared positive deadline bounded by the configured maximum.
A timeout **does not declare the step finished**, kill it or certify cleanup.

RUNNING permits new steps. DRAINING refuses them and waits for acknowledged
completion of active steps, then still requires evidence. Expired lease or step
authority latches RECOVERY_REQUIRED. Both monotonic and wall-clock expiry are
checked; renewal cannot revive expired authority. Authenticated end-step,
pause and unverified release remain available for bookkeeping after expiry.
The OS lock remains held until explicit release or holder exit.

There is no resume action on a drained holder: complete the phase, verify release,
and acquire a new phase. A client missing its explicit owner session reports UNKNOWN
for that refused call; it does not infer that the holder needs recovery.

## Evidence and recovery

`release` requires an absolute evidence path and its lowercase SHA256. Evidence
must be an owned regular single-link mode-0600 file, at most 1 MiB; all parent
components must be nonsymlinks. The holder hashes bytes before **and after** the
registered callback. Invalid paths, hashes or callback-time tampering reject
release and retain ownership. The semantic verifier receives an immutable
`EvidenceSnapshot(path, sha256, data)` and a context containing purpose
(`cleanup`/`recovery`), phase identity, state, active count and previous owner
for recovery. It must return exactly `True`, not a truthy string.

Hash equality establishes artifact integrity, not restored routing or a safe
screen. The **application** must verify its own routing, provider/process and
other restoration evidence. There is no generic routing verifier. A missing,
false, throwing or nonboolean verifier leaves cleanup unverified: the validated
artifact may release the lock, but the result is RECOVERY_REQUIRED. Authority
expiring during verification also prevents HUMAN_SAFE. Verifiers run synchronously
in the trusted holder; callers must keep them bounded. A hung verifier blocks
service, never turns a cached status into a safe-state proof.

After holder death or unverified release, reacquisition needs an explicit
`recovery_evidence={"path": ..., "sha256": ...}` and registered verifier. Recovery
validation runs while the accepted primitive holds the tentative OS lock, before
it replaces previous ownership. Wrong evidence or missing semantic acceptance
cannot override the recovery gate. A pristine runtime is not evidence of real
machine cleanliness; any initial safety checks remain application responsibilities.

Each holder uses a unique socket, so it never deletes a predecessor's socket to
steal ownership. Shutdown removes only the socket inode it created, closes that
endpoint before releasing ownership and never overwrites successor discovery
after unlocking. Crash sockets may remain for separately reviewed housekeeping.
`CoordinatorClient` always contacts the live holder. Missing/dead/stale endpoints
report RECOVERY_REQUIRED, including when discovery previously said HUMAN_SAFE.
Discovery publishes RELEASING before cleanup release; it never publishes HUMAN_SAFE. HUMAN_SAFE appears only in a successful release response/audit for the verified cooperative phase,
not continuing global safety after another holder starts.

Private `transitions.jsonl` records UTC timestamps, holder ID, per-holder sequence,
state and transition details without capability/step tokens. Evidence paths and
hashes appear in this private audit only. The append file is fsynced and inode
checked; persistence failure stops service without verified cleanup. An incomplete
last audit line blocks startup pending reviewed repair; recovery evidence alone
does not repair a damaged journal. No public export of private audit is provided.

## Qualification and integration boundaries

`test_workbench_coordinator.py` exercises real holder/client processes and temp
private sockets: exclusion between commands, authentication, pause/drain,
premature release, dual-clock expiry, renewal, holder crash/recovery, malformed
frames, tampered evidence/audit and privacy. Fixture verifiers certify only that
the test fixture has no external effects. Mocked clock divergence is tested;
actual system sleep and real routing restoration are not tested here.

Future integration must register one root and one persistent holder, require
cooperative membership at **every** relevant entry point, and supply a sealed
restoration evidence contract with an independently qualified semantic verifier.
A coordinator does not execute/stop work, monitor child lifetimes or know when
a crashed client actually stopped using the machine. A higher-level adapter must
handle those cases and reconcile overdue steps. No existing browser/VPN/runner
entry point is protected by this packet, and no whole-system protection is claimed.

Predictably overlong platform socket paths are refused before acquisition. Other failures after acquiring ownership still require explicit recovery; startup failure is not silently treated as machine cleanliness.
