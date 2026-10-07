# Human intervention panel

Use an installed Python/Tk that creates native windows on the host. Prepare a private, user-owned runtime directory with mode 0700, then run:

```sh
python3 workbench/status_panel.py --runtime-root /absolute/private/runtime
```

Request computer saves a durable receipt and asks the live coordinator to drain. Optional `--human-outbox`, `--supervisor-inbox`, and `--supervisor-thread` configure private JSON mail and a Codex queue notification. Queue acceptance means enqueued, not acknowledged. A failed alert exposes Retry; retries preserve the request identity and elapsed timer, including after restart.

The timer starts immediately on a request, updates each second, and stops only when a matching explicit supervisor handoff record arrives. It measures elapsed waiting, not estimated time until safe. Restart restores by the receipt timestamp rather than file modification time. Corrupt or mismatched records cannot grant control. A matching acknowledgment alone does not stop the timer or authorize work.

A private `human-control-<request_id>.json` with matching `request_id`, `actor: supervisor`, and `state: HUMAN_CONTROL` displays **Computer is yours**. Return control creates a separate request; it never resumes a drained coordinator automatically. The supervisor must acknowledge that handoff and coordinate the authorized next phase.

Appearance follows macOS light/dark mode automatically, checked every ten seconds. The **Appearance** menu also offers Light and Dark for the current panel session. Labels, timer, notices and buttons remain readable in both modes. Window height expands to fit notices.

Closing minimizes instead of terminating or pausing work. Clicking the application in the macOS Dock restores the same window. **Window → Show Agent Workbench** is a second restore path. Both paths were exercised with the real native window on macOS; pending-request restart, Request, Retry, Return and explicit handoff were also exercised in an isolated runtime without sending real team instructions.

`panel-events-YYYY-MM-DD.jsonl` stores private timestamped startup, button, retry, save, error, display-change, minimize and restore events. `panel-health.json` identifies the actual running PID and current display. Logs do not establish a safe-state witness. A malformed attention notice is surfaced rather than silently ignored; notices use `actor: supervisor`, `state: OPEN`, and `summary`.

Verification: 19 panel contract tests and 21 coordinator tests pass in the reference environment. GUI evidence supports the tested host behavior, not every operating system. A missing live coordinator remains visibly unverified. Cooperative handoff records do not protect against unrelated same-user programs or prove whole-machine network restoration. ETA, universal team-drain adoption and automatic detection of a human terminal tab remain separate work.
