"""Human pause persistence and honest status; no GUI needed for these checks."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workbench"))
from status_panel import request_pause, status_text, control_text, Panel, attention_text, latest_request, elapsed_label
from lease_coordinator import root_directory, write_json
import os
import queue

def private_write(path, text):
    path.write_text(text)
    path.chmod(0o600)


class PanelTests(unittest.TestCase):
    def test_pause_survives_missing_holder_without_claiming_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            record, response = request_pause(root)
            saved = json.loads(next(root.glob("human-request-*.json")).read_text())
            self.assertEqual(record, saved)
            self.assertFalse(saved["safe_to_use"])
            self.assertEqual(response["state"], "RECOVERY_REQUIRED")

    def test_cached_safe_label_never_grants_human_control(self):
        title, detail = status_text(dict(ok=True, state="HUMAN_SAFE", active_steps=0))
        self.assertEqual(title, "Await handback confirmation")
        self.assertIn("Supervisor", detail)
        self.assertEqual(status_text(dict(ok=False, state="HUMAN_SAFE"))[0], "Control unverified")

    def test_drain_reports_unknown_eta(self):
        title, detail = status_text(dict(ok=True, state="DRAINING", active_steps=2))
        self.assertEqual(title, "Pause requested")
        self.assertIn("2", detail)
        self.assertIn("unknown", detail)

    def test_pending_pause_survives_status_poll(self):
        self.assertEqual(status_text(dict(ok=False), pause_requested=True)[0], 'Pause requested')
        self.assertEqual(status_text(dict(ok=True, state='RUNNING'), pause_requested=True)[0],
                         'Pause requested')
        self.assertIn('alert failed', status_text(dict(ok=False), True, 'DELIVERY_FAILED')[1])
        self.assertEqual(status_text(dict(ok=False), True, 'QUEUED', True)[0], 'Request acknowledged')
        self.assertIn('confirmation', status_text(dict(ok=False), True, 'QUEUED', True)[1])

    def test_button_records_mail_and_explicit_queue_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            outbox, inbox = root / 'outbox', root / 'inbox'
            for p in (outbox, inbox):
                p.mkdir(mode=0o700)
            with patch('status_panel.subprocess.run') as run:
                run.return_value.returncode = 0
                record, response = request_pause(root, human_outbox=outbox,
                                                 supervisor_inbox=inbox, supervisor_thread='session')
            self.assertEqual(response['notification']['state'], 'QUEUED')
            self.assertEqual(json.loads(next(outbox.glob('REQUEST-*.json')).read_text()), record)
            self.assertEqual(json.loads(next(inbox.glob('HUMAN-REQUEST-*.json')).read_text()), record)
            self.assertEqual(run.call_args[0][0][:4], ['codex', 'queue', '--thread', 'session'])
            self.assertFalse(record['safe_to_use'])

    def test_failed_queue_keeps_durable_request_and_delivery_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            with patch('status_panel.subprocess.run', side_effect=OSError('not installed')):
                record, response = request_pause(root, supervisor_thread='session')
            self.assertEqual(response['notification']['state'], 'DELIVERY_FAILED')
            self.assertTrue((root / ('human-request-' + record['id'] + '.json')).exists())

    def test_return_control_requests_ack_without_resuming_holder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            with patch('status_panel.CoordinatorClient') as client:
                record, response = request_pause(root, return_control=True)
            client.assert_not_called()
            self.assertEqual(record['event'], 'human_return_control')
            self.assertFalse(record['safe_to_use'])
            self.assertEqual(response['state'], 'AWAITING_SUPERVISOR')

    def test_control_requires_matching_explicit_supervisor_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            identifier = 'a' * 32
            fd = root_directory(root)
            try:
                for actor, rid, state, expected in (
                    ('other', identifier, 'HUMAN_CONTROL', None),
                    ('supervisor', 'b' * 32, 'HUMAN_CONTROL', None),
                    ('supervisor', identifier, 'ACKNOWLEDGED', None),
                    ('supervisor', identifier, 'HUMAN_CONTROL', 'Computer is yours'),
                    ('supervisor', identifier, 'AGENT_CONTROL', 'Supervisor has control')):
                    write_json(fd, 'human-control-' + identifier + '.json',
                               dict(actor=actor, request_id=rid, state=state))
                    result = control_text(root, identifier)
                    self.assertEqual(result[0] if result else None, expected)
            finally:
                os.close(fd)

    def test_restore_deiconifies_and_raises_existing_panel(self):
        panel = object.__new__(Panel)
        panel.root = Mock()
        panel.log = Mock()
        panel.restore()
        panel.root.deiconify.assert_called_once()
        panel.root.attributes.assert_called_once_with('-topmost', True)
        panel.root.lift.assert_called_once()

    def test_mail_failure_does_not_skip_drain_and_keeps_request_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); root.chmod(0o700)
            with patch('status_panel.CoordinatorClient') as client, patch('status_panel.notify_supervisor', side_effect=ValueError('bad mail')):
                client.return_value.request.return_value = dict(ok=True, state='DRAINING')
                record, result = request_pause(root)
            client.return_value.request.assert_called_once_with('request_pause', reason='Human requests computer')
            self.assertEqual(result['notification']['state'], 'DELIVERY_FAILED')
            self.assertEqual(result['notification']['request_id'],record['id'])

    def test_poll_error_is_visible_and_always_reschedules(self):
        panel = object.__new__(Panel)
        panel.root, panel.title, panel.detail = Mock(), Mock(), Mock()
        panel.log = Mock()
        panel.poll_once = Mock(side_effect=OSError('runtime missing'))
        panel.poll()
        panel.title.set.assert_called_once_with('Panel error: control unverified')
        panel.root.after.assert_called_once_with(2000, panel.poll)

    def test_retry_reuses_durable_request_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); root.chmod(0o700)
            record, _ = request_pause(root)
            panel=object.__new__(Panel)
            panel.runtime=root;panel.request_id=record['id'];panel.notification_options={};panel.events=queue.Queue()
            with patch('status_panel.notify_supervisor',return_value=dict(state='QUEUED')) as notify:
                panel.retry_alert()
            notify.assert_called_once_with(root,record)
            kind,response=panel.events.get_nowait()
            self.assertEqual(kind,'pause')
            self.assertEqual(response['request_id'],record['id'])
            self.assertEqual(len(list(root.glob('human-request-*.json'))),1)

    def test_request_not_saved_does_not_claim_persistence(self):
        title,detail=status_text(dict(ok=False),True,'REQUEST_NOT_SAVED')
        self.assertEqual(title,'Pause request failed')
        self.assertIn('could not be saved',detail)

    def test_attention_notice_is_explicit_and_does_not_change_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();root.chmod(0o700);fd=root_directory(root)
            try:
                self.assertIsNone(attention_text(root))
                for actor,state,expected in (('other','OPEN',None),('supervisor','CLOSED',None),('supervisor','OPEN','Attention needed: Authentication approval needed; see chat.')):
                    write_json(fd,'human-attention.json',dict(actor=actor,state=state,summary='Authentication approval needed; see chat.'))
                    self.assertEqual(attention_text(root),expected)
                self.assertIsNone(control_text(root,None))
            finally:os.close(fd)

    def test_timer_formats_wait_and_completed_without_claiming_an_eta(self):
        self.assertEqual(elapsed_label(65), 'Waiting for handoff · 01:05')
        self.assertEqual(elapsed_label(3601, returning=True), 'Waiting for return · 01:00:01')
        self.assertEqual(elapsed_label(8, resolved=True), 'Handoff completed · 00:08')
        self.assertEqual(elapsed_label(-3), 'Waiting for handoff · 00:00')

    def test_restart_uses_receipt_time_not_mtime_and_skips_bad_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();root.chmod(0o700)
            for identifier,stamp in [('a'*32,'2026-10-07T01:00:00Z'),('b'*32,'2026-10-07T02:00:00Z')]:
                private_write(root/('human-request-'+identifier+'.json'),json.dumps(dict(id=identifier,event='human_pause_request',timestamp=stamp)))
            os.utime(root/('human-request-'+'a'*32+'.json'),(2000000000,2000000000))
            private_write(root/('human-request-'+'c'*32+'.json'),'{broken')
            private_write(root/('human-request-'+'d'*32+'.json'),'[]')
            self.assertEqual(latest_request(root)['id'],'b'*32)

    def test_malformed_control_cannot_grant_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();root.chmod(0o700);identifier='a'*32
            private_write(root/('human-control-'+identifier+'.json'),'[]')
            self.assertIsNone(control_text(root,identifier))

    def test_wrong_attention_schema_is_visible_instead_of_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();root.chmod(0o700)
            private_write(root/'human-attention.json',json.dumps(dict(actor='supervisor',active=True,message='Old schema')))
            self.assertIn('format invalid',attention_text(root))

    def test_timer_survives_status_poll_and_stops_only_at_explicit_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();root.chmod(0o700);identifier='a'*32
            panel=object.__new__(Panel);panel.runtime=root;panel.request_id=identifier
            panel.root=Mock();panel.root.winfo_reqheight.return_value=150
            panel.title=Mock();panel.detail=Mock();panel.attention=Mock();panel.button=Mock();panel.timer=Mock();panel.timer.get.return_value=''
            panel.log=Mock();panel.display_signature=None;panel.logging_error=False;panel.theme_dark=False
            panel.pause_requested=True;panel.return_requested=False;panel.human_control=False;panel.pause_notification='QUEUED'
            panel.timer_start=100;panel.timer_stop=None;panel.timer_returning=False
            with patch('status_panel.time.monotonic',return_value=107):
                panel.display(dict(ok=False,state='RECOVERY_REQUIRED'))
                self.assertIsNone(panel.timer_stop)
                panel.tick_timer_only();panel.timer.set.assert_called_with('Waiting for handoff · 00:07')
                private_write(root/('human-control-'+identifier+'.json'),json.dumps(dict(actor='supervisor',request_id=identifier,state='HUMAN_CONTROL')))
                panel.display(dict(ok=False,state='RECOVERY_REQUIRED'))
            self.assertEqual(panel.timer_stop,107)
            self.assertTrue(panel.human_control)
            with patch('status_panel.time.monotonic',return_value=150):panel.tick_timer_only()
            panel.timer.set.assert_called_with('Handoff completed · 00:07')


if __name__ == "__main__":
    unittest.main()
