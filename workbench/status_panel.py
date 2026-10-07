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
import subprocess
import threading
import time
import tkinter as tk

from lease_coordinator import CoordinatorClient, root_directory, write_json, read_file


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def latest_request(runtime):
    """Restore by recorded time, not mtime; malformed receipts grant nothing."""
    candidates = []
    fd = root_directory(runtime)
    try:
        for path in Path(runtime).glob('human-request-*.json'):
            try:
                record = json.loads(read_file(fd, path.name))
                identifier = record.get('id', '')
                stamp = timestamp(record.get('timestamp'))
                if (isinstance(identifier, str) and len(identifier) == 32
                        and all(c in '0123456789abcdef' for c in identifier)
                        and path.name == 'human-request-' + identifier + '.json'
                        and record.get('event') in ('human_pause_request', 'human_return_control')
                        and stamp is not None):
                    candidates.append((stamp, identifier, record))
            except (OSError, ValueError, AttributeError, TypeError):
                continue
    finally:
        os.close(fd)
    return max(candidates, default=(None, None, None))[-1]


def elapsed_label(seconds, resolved=False, returning=False):
    elapsed = max(0, int(seconds))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    clock = '%02d:%02d:%02d' % (hours, minutes, seconds) if hours else '%02d:%02d' % (minutes, seconds)
    label = 'Handoff completed' if resolved else 'Waiting for return' if returning else 'Waiting for handoff'
    return label + ' · ' + clock


def system_dark():
    """Read appearance only; a failed check defaults to a readable light palette."""
    if os.uname().sysname != 'Darwin':
        return False
    try:
        result = subprocess.run(['/usr/bin/defaults', 'read', '-g', 'AppleInterfaceStyle'],
                                capture_output=True, text=True, timeout=1)
        return result.returncode == 0 and result.stdout.strip().lower() == 'dark'
    except (OSError, subprocess.TimeoutExpired):
        return False


def panel_log(runtime, event, **fields):
    fd = root_directory(runtime)
    try:
        name = 'panel-events-' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.jsonl'
        log = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            record = dict(at=datetime.now(timezone.utc).isoformat(), pid=os.getpid(), event=event, **fields)
            os.write(log, (json.dumps(record) + '\n').encode())
        finally:
            os.close(log)
    finally:
        os.close(fd)


def notify_supervisor(runtime, record, human_outbox=None, supervisor_inbox=None, supervisor_thread=None):
    """Durable role mail plus explicit runtime wake; filesystem mail alone cannot wake a model."""
    notification = dict(request_id=record['id'], state="NOT_CONFIGURED")
    if human_outbox and supervisor_inbox:
        for folder, prefix in ((human_outbox, 'REQUEST-'), (supervisor_inbox, 'HUMAN-REQUEST-')):
            fd = root_directory(folder)
            try:
                write_json(fd, prefix + record['id'] + '.json', record)
            finally:
                os.close(fd)
        notification['mail_recorded'] = True
    if supervisor_thread:
        message = ('Human clicked Request computer / share a thought. Request ID ' + record['id'] +
                   '. Yield GUI input, coordinate a safe pause, and acknowledge this request. '
                   'Receipt: ' + str(Path(runtime) / ('human-request-' + record['id'] + '.json')) +
                   '. This is a human UI '
                   'request, not authorization to resume or terminate unrelated work.')
        if record.get('event') == 'human_return_control':
            message = ('Human clicked Return control to supervisor. Request ID ' + record['id'] +
                       '. Acknowledge the handoff and update the panel before resuming authorized work. '
                       'Receipt: ' + str(Path(runtime) / ('human-request-' + record['id'] + '.json')) +
                       '. This does not authorize unrelated operations.')
        notification['exact_relay'] = message
        # Persist the exact relay before sending it, including crashes/timeouts.
        fd = root_directory(runtime)
        try:
            write_json(fd, 'human-notification-' + record['id'] + '.json', notification)
        finally:
            os.close(fd)
        try:
            result = subprocess.run(['codex', 'queue', '--thread', supervisor_thread,
                                     '--message', message], capture_output=True, text=True, timeout=15)
            notification['state'] = 'QUEUED' if result.returncode == 0 else 'DELIVERY_FAILED'
            notification['returncode'] = result.returncode
        except (OSError, subprocess.TimeoutExpired):
            notification['state'] = 'DELIVERY_FAILED'
    fd = root_directory(runtime)
    try:
        write_json(fd, 'human-notification-' + record['id'] + '.json', notification)
    finally:
        os.close(fd)
    return notification


def request_pause(runtime, reason="Human requests computer", human_outbox=None,
                  supervisor_inbox=None, supervisor_thread=None, return_control=False):
    # The runtime must already be a private, non-symlink directory.
    fd = root_directory(runtime)
    try:
        identifier = secrets.token_hex(16)
        record = dict(id=identifier, event="human_return_control" if return_control else "human_pause_request", reason=reason,
                      timestamp=datetime.now(timezone.utc).isoformat(),
                      state="REQUESTED", safe_to_use=False)
        write_json(fd, "human-request-" + identifier + ".json", record)
    finally:
        os.close(fd)
    # Drain independently of mail health or an external queue timeout.
    response = (dict(ok=False, state="AWAITING_SUPERVISOR") if return_control else
                CoordinatorClient(runtime).request("request_pause", reason=reason))
    try:
        notification = notify_supervisor(runtime, record, human_outbox, supervisor_inbox, supervisor_thread)
    except Exception as exc:
        notification = dict(request_id=identifier, state='DELIVERY_FAILED', error=type(exc).__name__)
    response['notification'] = notification
    return record, response


def status_text(response, pause_requested=False, notification_state=None, acknowledged=False):
    if pause_requested:
        if acknowledged:
            return "Request acknowledged", "See supervisor chat for handback confirmation."
        if notification_state == 'REQUEST_NOT_SAVED':
            return 'Pause request failed', 'Request could not be saved. Please use supervisor chat.'
        if notification_state == 'DELIVERY_FAILED':
            return "Pause requested", "Request saved; alert failed. Please use supervisor chat."
        if notification_state == 'NOT_CONFIGURED':
            return "Pause requested", "Request saved; supervisor alert not configured. Use chat."
        return "Pause requested", "Await supervisor acknowledgment; ETA unknown."
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


def control_record(runtime, request_id):
    """An explicit matching supervisor handoff, not a inferred lease safety claim."""
    if not request_id:
        return None
    fd = root_directory(runtime)
    try:
        try:
            record = json.loads(read_file(fd, 'human-control-' + request_id + '.json'))
        except (OSError, ValueError):
            return None
    finally:
        os.close(fd)
    if not isinstance(record, dict) or record.get('request_id') != request_id or record.get('actor') != 'supervisor':
        return None
    return record if record.get('state') in ('HUMAN_CONTROL', 'AGENT_CONTROL') else None


def control_text(runtime, request_id):
    record = control_record(runtime, request_id)
    if record is None:
        return None
    if record.get('state') == 'HUMAN_CONTROL':
        return 'Computer is yours', 'Supervisor reports GUI yielded; return control when ready.'
    if record.get('state') == 'AGENT_CONTROL':
        return 'Supervisor has control', 'Authorized work may resume; request computer to interrupt.'
    return None


def attention_text(runtime):
    """Supervisor's explicit attention notice, independent of handoff authority."""
    fd = root_directory(runtime)
    try:
        try:
            record = json.loads(read_file(fd, 'human-attention.json'))
        except (OSError, ValueError):
            return None
    finally:
        os.close(fd)
    if not isinstance(record, dict) or record.get('actor') != 'supervisor' or record.get('state') == 'CLOSED':
        return None
    if record.get('state') != 'OPEN':
        return 'Notice format invalid; see supervisor.'
    summary = record.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        return 'Notice format invalid; see supervisor.'
    return 'Attention needed: ' + summary.strip()[:180]


class Panel:
    def __init__(self, runtime, human_outbox=None, supervisor_inbox=None, supervisor_thread=None):
        self.runtime = runtime
        self.notification_options = dict(human_outbox=human_outbox, supervisor_inbox=supervisor_inbox,
                                         supervisor_thread=supervisor_thread)
        self.pause_requested = False
        self.pause_notification = None
        self.request_id = None
        self.return_requested = False
        self.human_control = False
        self.timer_start = None
        self.timer_stop = None
        self.timer_returning = False
        self.display_signature = None
        self.logging_error = False
        self.theme_mode = 'automatic'
        self.theme_checked = 0
        self.theme_dark = None
        saved = latest_request(runtime)
        if saved:
            self.request_id = saved['id']
            fd = root_directory(runtime)
            try:
                try:
                    notification = json.loads(read_file(fd, 'human-notification-' + self.request_id + '.json'))
                    if isinstance(notification, dict) and notification.get('request_id') == self.request_id:
                        self.pause_notification = notification.get('state')
                except (OSError, ValueError):
                    pass
            finally:
                os.close(fd)
            self.return_requested = saved['event'] == 'human_return_control'
            self.pause_requested = not self.return_requested
            self.timer_returning = self.return_requested
            self.timer_start = time.monotonic() - max(0, (datetime.now(timezone.utc) - timestamp(saved['timestamp'])).total_seconds())
        self.events = queue.Queue()
        self.pending = False
        self.root = tk.Tk()
        self.root.title("Agent Workbench")
        self.root.attributes("-topmost", True)
        width, height = 320, 205
        self.root.geometry("%dx%d+%d+30" %
                           (width, height, max(0, self.root.winfo_screenwidth() - width - 16)))
        self.root.resizable(False, False)
        self.title = tk.StringVar(value="Control unverified")
        self.detail = tk.StringVar(value="Supervisor must confirm handback.")
        self.attention = tk.StringVar(value="")
        self.timer = tk.StringVar(value='')
        self.labels = []
        label = tk.Label(self.root, textvariable=self.title, font=("Helvetica", 13, "bold"))
        label.pack(pady=(8, 2)); self.labels.append(label)
        label = tk.Label(self.root, textvariable=self.detail, wraplength=300)
        label.pack(); self.labels.append(label)
        label = tk.Label(self.root, textvariable=self.timer, font=('Menlo', 11))
        label.pack(pady=(5, 0)); self.labels.append(label)
        self.button = tk.Button(self.root, text="Request computer / share a thought", command=self.pause)
        self.button.pack(pady=7)
        self.attention_label = tk.Label(self.root, textvariable=self.attention, wraplength=300,
                                       font=("Helvetica", 11, "bold"))
        self.attention_label.pack()
        self.root.protocol("WM_DELETE_WINDOW", self.minimize)
        # On macOS clicking an already-running Tk application in the Dock calls
        # this hook; iconify alone left the panel with no reliable reopen path.
        if self.root.tk.call('tk', 'windowingsystem') == 'aqua':
            self.root.createcommand('::tk::mac::ReopenApplication', self.restore)
        menu = tk.Menu(self.root)
        window_menu = tk.Menu(menu, tearoff=False)
        window_menu.add_command(label='Show Agent Workbench', command=self.restore)
        menu.add_cascade(label='Window', menu=window_menu)
        appearance = tk.Menu(menu, tearoff=False)
        for mode in ('automatic', 'light', 'dark'):
            appearance.add_command(label=mode.capitalize(), command=lambda m=mode: self.set_theme(m))
        menu.add_cascade(label='Appearance', menu=appearance)
        self.root.config(menu=menu)
        self.apply_theme()
        self.log('started', request_id=self.request_id, restored_request=bool(saved))
        self.root.after(100, self.tick)
        self.root.after(100, self.poll)

    def log(self, event, **fields):
        try:
            panel_log(self.runtime, event, **fields)
        except (OSError, ValueError):
            # Background workers must not mutate Tk widgets.
            self.logging_error = True

    def set_theme(self, mode):
        self.theme_mode = mode
        self.apply_theme(force=True)
        self.log('appearance_changed', mode=mode)

    def apply_theme(self, force=False):
        if not force and time.monotonic() - self.theme_checked < 10:
            return
        self.theme_checked = time.monotonic()
        dark = system_dark() if self.theme_mode == 'automatic' else self.theme_mode == 'dark'
        if dark == self.theme_dark and not force:
            return
        self.theme_dark = dark
        bg, fg, accent = ('#202124', '#f5f5f5', '#ffcc80') if dark else ('#f7f7f8', '#202124', '#803900')
        self.root.config(bg=bg)
        for label in self.labels:
            label.config(bg=bg, fg=fg)
        self.attention_label.config(bg=bg, fg=accent)
        # Aqua buttons may ignore custom fill colours: a neutral light face and
        # dark text remain readable even when the rest of the panel is dark.
        self.button.config(bg='#e5e7eb', fg='#202124', activebackground='#d1d5db',
                           activeforeground='#202124', disabledforeground='#666666', highlightbackground=bg)

    def tick(self):
        try:
            if self.timer_start is not None:
                end = self.timer_stop if self.timer_stop is not None else time.monotonic()
                self.timer.set(elapsed_label(end - self.timer_start, self.timer_stop is not None, self.timer_returning))
            else:
                self.timer.set('')
            self.apply_theme()
        finally:
            self.root.after(1000, self.tick)

    def minimize(self):
        # Keep the panel available without mistaking window closure for pause.
        self.root.iconify()
        self.log('minimized')

    def restore(self):
        self.root.deiconify()
        self.root.attributes('-topmost', True)
        self.root.lift()
        self.log('restored')

    def worker(self, pause=False, return_control=False):
        try:
            if pause:
                record, response = request_pause(self.runtime, return_control=return_control,
                                                 **self.notification_options)
                response['request_id'] = record['id']
                self.log('request_saved', request_id=record['id'], kind=record['event'],
                         notification_state=response.get('notification', {}).get('state'))
                self.events.put(("pause", response))
            else:
                response = CoordinatorClient(self.runtime).request("status", authenticated=False)
                self.events.put(("status", response))
        except Exception as exc:
            self.log('worker_error', operation='request' if pause else 'status', error=type(exc).__name__)
            self.events.put(("pause" if pause else "status", dict(ok=False, notification=dict(state="REQUEST_NOT_SAVED"))))

    def pause(self):
        if (self.pause_requested or self.return_requested) and self.request_id:
            self.button.config(state='disabled')
            self.log('retry_clicked', request_id=self.request_id)
            threading.Thread(target=self.retry_alert, daemon=True).start()
            return
        returning = self.human_control
        self.return_requested = returning
        self.request_id = None
        self.human_control = False
        self.pause_requested = not returning
        self.timer_start = time.monotonic()
        self.timer_stop = None
        self.timer_returning = returning
        self.timer.set(elapsed_label(0, returning=returning))
        self.log('button_clicked', kind='return' if returning else 'pause')
        self.button.config(state="disabled")
        self.title.set("Return requested" if returning else "Pause requested")
        self.detail.set("Await supervisor; handback time unknown.")
        threading.Thread(target=self.worker, kwargs=dict(pause=True, return_control=returning), daemon=True).start()

    def retry_alert(self):
        try:
            fd = root_directory(self.runtime)
            try:
                record = json.loads(read_file(fd, 'human-request-' + self.request_id + '.json'))
            finally:
                os.close(fd)
            notification = notify_supervisor(self.runtime, record, **self.notification_options)
            self.events.put(('pause', dict(ok=False, request_id=self.request_id, notification=notification)))
        except Exception:
            self.events.put(('pause', dict(ok=False, request_id=self.request_id,
                                         notification=dict(state='DELIVERY_FAILED'))))

    def display(self, response):
        acknowledged = False
        self.attention.set(attention_text(self.runtime) or "")
        if self.request_id:
            fd = root_directory(self.runtime)
            try:
                try:
                    ack = json.loads(read_file(fd, 'human-ack-' + self.request_id + '.json'))
                    acknowledged = isinstance(ack, dict) and ack.get('request_id') == self.request_id and ack.get('state') == 'ACKNOWLEDGED'
                except (OSError, ValueError):
                    pass
            finally:
                os.close(fd)
        control = control_text(self.runtime, self.request_id)
        if control:
            title, detail = control
            self.human_control = title == 'Computer is yours'
            self.pause_requested = False
            self.return_requested = False
            if self.timer_start is not None and self.timer_stop is None:
                self.timer_stop = time.monotonic()
                receipt = control_record(self.runtime, self.request_id)
                stamp = timestamp(receipt.get('at', receipt.get('timestamp'))) if receipt else None
                if stamp:
                    delay = max(0, (datetime.now(timezone.utc) - stamp).total_seconds())
                    self.timer_stop = max(self.timer_start, self.timer_stop - delay)
                self.tick_timer_only()
        elif self.return_requested:
            title, detail = 'Return requested', 'Await supervisor acknowledgment before agents resume.'
            self.human_control = False
        else:
            title, detail = status_text(response, self.pause_requested, self.pause_notification, acknowledged)
        if control and (not response.get('ok') or response.get('state') == 'RECOVERY_REQUIRED'):
            detail += ' Live coordinator unverified.'
        if self.logging_error:
            detail += ' Panel log unavailable.'
        retry = (self.pause_requested or self.return_requested) and self.pause_notification in ('DELIVERY_FAILED', 'NOT_CONFIGURED', 'REQUEST_NOT_SAVED')
        self.button.config(text='Retry supervisor alert' if retry else 'Return control to supervisor' if self.human_control else
                           'Request computer / share a thought',
                           state='disabled' if (self.pause_requested or self.return_requested) and not retry else 'normal')
        self.title.set(title)
        self.detail.set(detail)
        self.root.update_idletasks()
        self.root.geometry('320x%d' % max(205, self.root.winfo_reqheight() + 8))
        snapshot = dict(request_id=self.request_id, title=title, detail=detail,
                        notification_state=self.pause_notification, coordinator_state=response.get('state'),
                        coordinator_ok=bool(response.get('ok')), human_control=self.human_control)
        signature = json.dumps(snapshot, sort_keys=True)
        if signature != self.display_signature:
            self.log('display_state', **snapshot)
            self.display_signature = signature
        fd = root_directory(self.runtime)
        try:
            write_json(fd, 'panel-health.json', dict(pid=os.getpid(), at=datetime.now(timezone.utc).isoformat(),
                       timer=self.timer.get(), appearance='dark' if self.theme_dark else 'light', **snapshot))
        finally:
            os.close(fd)

    def tick_timer_only(self):
        if self.timer_start is not None:
            end = self.timer_stop if self.timer_stop is not None else time.monotonic()
            self.timer.set(elapsed_label(end - self.timer_start, self.timer_stop is not None, self.timer_returning))

    def poll(self):
        try:
            self.poll_once()
        except Exception as exc:
            self.pending = False
            self.title.set('Panel error: control unverified')
            self.detail.set('Use supervisor chat; panel will retry.')
            self.log('poll_error', error=type(exc).__name__)
        finally:
            self.root.after(2000, self.poll)

    def poll_once(self):
        while True:
            try:
                kind, response = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "pause":
                self.button.config(state="normal")
                self.request_id = response.get('request_id')
                self.pause_notification = response.get('notification', {}).get('state')
                self.display(response)
            else:
                self.pending = False
                self.display(response)
        if not self.pending:
            self.pending = True
            threading.Thread(target=self.worker, daemon=True).start()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--human-outbox", type=Path)
    parser.add_argument("--supervisor-inbox", type=Path)
    parser.add_argument("--supervisor-thread")
    args = parser.parse_args()
    fd = root_directory(args.runtime_root)
    os.close(fd)
    Panel(args.runtime_root, args.human_outbox, args.supervisor_inbox, args.supervisor_thread).run()
