# Panel reliability and appearance repair

The operator reported delayed or confusing notices and an unreliable minimized panel. Inspection distinguished automatic task-monitor notices from human button requests: existing completion notices were state-change relays, not evidence of repeated clicks. Enqueueing was not treated as acknowledgment.

Repair added an elapsed request timer, automatic light/dark appearance with manual overrides, private event logs and current-process health records. Restart now selects valid receipt timestamps, restores saved alert failures and retains the same request for retry. Invalid attention notices are visible. A stale launcher PID and an outdated public panel implementation were corrected.

Real native-window checks exercised Request, Retry, Return, explicit handoff, pending-request restart, close/minimize, Window-menu restore and Dock restore. Both palettes were visually inspected. Isolated request tests did not notify active research workers. The deployed panel preserves human control and honestly reports the unavailable live coordinator.

Sources: [panel implementation](../workbench/status_panel.py), [contract tests](../tests/test_workbench_panel.py), [behavior documentation](../docs/PANEL.md). Nineteen panel tests and twenty-one coordinator tests passed. Confidence is high for these tested host behaviors; portability and whole-machine exclusion are not established. Avoided conflating elapsed time with a safe-handoff estimate, queue acceptance with receipt, and an explicit handoff assertion with hardware-enforced exclusion. Host-specific GUI evidence limits generalization.
