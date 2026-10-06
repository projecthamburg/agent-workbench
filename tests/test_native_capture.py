import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).parents[1] / 'workbench/native_capture.py'
SPEC = importlib.util.spec_from_file_location('native_capture', PATH)
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'log.jsonl'
        self.path.write_bytes(b'{"type":"user","text":"SECRET_BASELINE"}\n')
    def tearDown(self): self.tmp.cleanup()
    def test_baseline_never_parses_and_delta_never_exports_text(self):
        with patch.object(M, 'metadata', side_effect=AssertionError('No baseline parse')):
            prior = M.capture(self.path, 'session')
        with self.path.open('ab') as out:
            out.write(json.dumps({'type':'assistant','timestamp':'SECRET_TIMESTAMP',
                'text':'SECRET_TEXT','tool_calls':['SECRET_TOOL'], 'source':'SECRET_SOURCE'}).encode()+b'\n')
        result = M.capture(self.path, 'session', prior)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['event_counts']['assistant'], 1)
        self.assertEqual(result['previous_receipt_sha256'], prior['receipt_sha256'])
    def test_mutation_after_first_4096_is_refused(self):
        self.path.write_bytes(b'x'*8192+b'\n'); prior=M.capture(self.path,'session')
        with self.path.open('r+b') as f: f.seek(6000);f.write(b'y')
        with self.assertRaisesRegex(ValueError,'mutated'):M.capture(self.path,'session',prior)
    def test_truncation_and_replacement_preserve_prior(self):
        prior=M.capture(self.path,'session'); self.path.write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'truncation'):M.capture(self.path,'session',prior)
        self.path.unlink();self.path.write_bytes(b'new\n')
        with self.assertRaises(ValueError):M.capture(self.path,'session',prior)
    def test_partial_utf8_is_counted_once_after_completion(self):
        prior=M.capture(self.path,'session')
        with self.path.open('ab') as f:f.write(b'{"type":"assistant","text":"\xe3\x82')
        half=M.capture(self.path,'session',prior)
        self.assertEqual(half['event_counts'],{})
        with self.path.open('ab') as f:f.write(b'\xbf"}\n')
        done=M.capture(self.path,'session',half)
        self.assertEqual(done['event_counts'],{'assistant':1})
        self.assertEqual(M.capture(self.path,'session',done)['event_counts'],{})
    def test_large_record_is_bounded_and_valid_following_record_survives(self):
        prior=M.capture(self.path,'session')
        with self.path.open('ab') as f:f.write(b'x'*(M.BLOCK+50)+b'\n{"type":"user"}\n')
        result=M.capture(self.path,'session',prior)
        self.assertEqual(result['event_counts'],{'OVERSIZED_RECORD':1,'user':1})
    def test_observed_size_does_not_chase_new_append(self):
        prior=M.capture(self.path,'session'); original=M.digest_range; appended=False
        def digest(f,a,b):
            nonlocal appended
            value=original(f,a,b)
            if not appended:
                appended=True
                with self.path.open('ab') as out:out.write(b'{"type":"user"}\n')
            return value
        with patch.object(M,'digest_range',side_effect=digest): result=M.capture(self.path,'session',prior)
        self.assertEqual(result['observed_size'],prior['observed_size'])
        self.assertEqual(result['event_counts'],{})
        self.assertEqual(M.capture(self.path,'session',result)['event_counts'],{'user':1})
    def test_state_corruption_and_atomic_publish_failure(self):
        prior=M.capture(self.path,'session'); prior['parse_offset']=0
        with self.assertRaisesRegex(ValueError,'integrity'):M.capture(self.path,'session',prior)
        state=Path(self.tmp.name)/'state.json';M.publish(state,{'old':True})
        with patch.object(M.os,'replace',side_effect=OSError('Interrupted')):
            with self.assertRaises(OSError):M.publish(state,{'new':True})
        self.assertEqual(json.loads(state.read_text()),{'old':True})


if __name__ == '__main__': unittest.main()
