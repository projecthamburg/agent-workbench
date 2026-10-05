#!/usr/bin/env python3
"""Small human intervention panel. No cached file is a safe-state witness.

An absent live coordinator is visibly unverified. Pause requests are persisted
for supervisor review as well as delivered to the cooperative live holder.
This panel neither stops workers nor changes machine routing.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import secrets
import threading
import tkinter as tk

from lease_coordinator import CoordinatorClient, root_directory, write_json


def request_pause(runtime, reason="Human requests computer"):
    # The runtime must already be a private, non-symlink directory.
    fd = root_directory(runtime)
    try:
        identifier = secrets.token_hex(16)
        record = dict(id=identifier, event="human_pause_request", reason=reason,
                      timestamp=datetime.now(timezone.utc).isoformat(),
                      state="REQUESTED", safe_to_use=False)
        write_json(fd, "human-request-" + identifier + ".json", record)
    finally:
        os.close(fd)
    response = CoordinatorClient(runtime).request("request_pause", reason=reason)
    return record, response


def status_text(response):
    if not response.get("ok"):
        return "Control unverified", "Supervisor must confirm handback."
    state = response.get("state")
    count = response.get("active_steps")
    if state == "RUNNING":
        return "Agent working", "Active steps: %s" % count
    if state == "DRAINING":
        return "Pause requested", "Finishing steps: %s; ETA unknown." % count
    if state == "RECOVERY_REQUIRED":
        return "Recovery required", "Supervisor must inspect before handback."
    # A terminated holder cannot remain a live proof. Even an unfamiliar or
    # transient safe label is not permission to take over from this panel.
    return "Await handback confirmation", "Supervisor must confirm handback."


class Panel:
    def __init__(self, runtime):
        self.runtime = runtime
        self.events = queue.Queue()
        self.pending = False
        self.root = tk.Tk()
        self.root.title("Agent Workbench")
        self.root.attributes("-topmost", True)
        width, height = 300, 125
        self.root.geometry("%dx%d+%d+30" %
                           (width, height, max(0, self.root.winfo_screenwidth() - width - 16)))
        self.root.resizable(False, False)
        self.title = tk.StringVar(value="Control unverified")
        self.detail = tk.StringVar(value="Supervisor must confirm handback.")
        tk.Label(self.root, textvariable=self.title, font=("Helvetica", 13, "bold")).pack(pady=(8, 2))
        tk.Label(self.root, textvariable=self.detail, wraplength=285).pack()
        self.button = tk.Button(self.root, text="Request computer / share a thought", command=self.pause)
        self.button.pack(pady=7)
        self.root.protocol("WM_DELETE_WINDOW", self.minimize)
        self.root.after(100, self.poll)

    def minimize(self):
        # Keep the panel available without mistaking window closure for pause.
        self.root.iconify()

    def worker(self, pause=False):
        try:
            if pause:
                record, response = request_pause(self.runtime)
                self.events.put(("pause", response))
            else:
                response = CoordinatorClient(self.runtime).request("status", authenticated=False)
                self.events.put(("status", response))
        except Exception:
            self.events.put(("pause" if pause else "status", dict(ok=False)))

    def pause(self):
        self.button.config(state="disabled")
        self.title.set("Pause requested")
        self.detail.set("Await supervisor; handback time unknown.")
        threading.Thread(target=self.worker, kwargs=dict(pause=True), daemon=True).start()

    def poll(self):
        while True:
            try:
                kind, response = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "pause":
                self.button.config(state="normal")
                title, detail = status_text(response)
                self.title.set(title)
                self.detail.set(detail)
            else:
                self.pending = False
                title, detail = status_text(response)
                self.title.set(title)
                self.detail.set(detail)
        if not self.pending:
            self.pending = True
            threading.Thread(target=self.worker, daemon=True).start()
        self.root.after(2000, self.poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    args = parser.parse_args()
    fd = root_directory(args.runtime_root)
    os.close(fd)
    Panel(args.runtime_root).run()
