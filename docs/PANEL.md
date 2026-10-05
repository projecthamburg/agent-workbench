# Human intervention panel

Run with an installed Python/Tk that actually creates windows on the host:

```sh
python3 status_panel.py --runtime-root /absolute/private/runtime
```

The root is shared across projects/worktrees, owned by the current user with mode700, and must be prepared explicitly. Unix socket path length also constrains its location. The panel persists private human-request receipts, asks the live coordinator to drain, and polls live status without freezing the GUI thread. It does not execute work, kill workers, change routing or infer cleanup from an expired lease. A missing holder is unverified; a saved safe status is never a handback witness. ETA remains unknown until measured task boundaries can support one.

Current status: top-right GUI smoke check and three contract tests passed. Host system Tk failed window creation; an existing project interpreter succeeded. Team-drain bridge, task-boundary estimates, complete provider/browser entry-point adoption, supervisor handback confirmation and human-tab activation detection are still pending. Closing the panel minimizes it and does not request pause.

Source: status_panel.py; tests/test_workbench_panel.py; private dated GUI smoke receipt. Confidence high for tested receipt/status behavior, not established for whole-machine exclusion.
