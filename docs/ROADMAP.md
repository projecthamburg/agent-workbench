# Implementation batches and release criteria

1. **Plan and role ACKs.** Inventory existing methods; establish task boundaries and model rules.
   Independent advisor/curator findings revise the design. Retain previous roles as history.
2. **Isolation and executor fork.** Fork at the implementation checkpoint, verify parent/new session,
   model, working root and separate branch. Failed isolation blocks shared writes.
3. **Capture, retrieval and mail.** Prove full available native event capture and source attribution;
   exercise append, earlier-byte edit, partial tail, rotation and shrink. Retrieve a known tool
   event. Exercise idle wake and expired watcher; don't substitute file existence for model activity.
4. **Ownership, panel and recovery.** Test competing owners, stale leases, graceful drain, missing
   ACK, crashed worker and emergency interruption. Vision-test panel presence/focus behavior on
   the supported OS. Human-safe requires actual cleanup evidence.
5. **Budget, compaction and notifications.** Confirm active models and bounded measured usage;
   restore a task after compaction. Test send/reply, idempotency, spoofed inbound rejection and
   disabled optional integrations.
6. **Worked example and publication.** Run a project-specific declared integration batch; independently
   review evidence. Release generic launcher/plugin only after a clean setup reproduction and
   privacy review. Publish measured case-study results separately from design claims.

Before public export, create fresh repository history rather than reusing a private project's
branch. Review every exported blob and commit author/committer identity. In the coordination
implementation, use one machine-wide lock across all live entry points before resource
reservation, with valid same-owner nested calls and compatible passive telemetry. Test watcher
expiry and model-transition effects explicitly; idle wake must be demonstrated, not assumed.

First release target: a configurable launcher and role adapters, not an unrestricted autonomous
swarm. Packaging a plugin is a later adapter deliverable, with installation and permissions tested.
Tests that pass offline cannot certify external-provider behavior. No timeline promises without
an observed task, platform and permission inventory.
