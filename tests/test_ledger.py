"""Tests for ledger.py: record, torn lines, undo moves (never deletes), edit refusal."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import ledger  # noqa: E402


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.led = self.root / 'job' / 'LEDGER.jsonl'
        self.src = self.root / 'in.fcpxml'
        self.src.write_text('<fcpxml/>')
        self.out = self.root / 'out' / 'v01.fcpxml'
        self.out.parent.mkdir()
        self.out.write_text('<fcpxml version="1.11"/>')

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_and_entries(self):
        e = ledger.record(self.led, 'cut_build', 'build', {'v': 1}, [self.src], [self.out])
        self.assertEqual(e['outputs'][0]['sha256'], ledger.digest_path(self.out))
        self.assertEqual(e['inputs'][0]['path'], str(self.src))
        self.assertEqual([x['id'] for x in ledger.entries(self.led)], [e['id']])

    def test_bundle_digest_covers_every_file(self):
        b = self.root / 'x.fcpxmld'
        b.mkdir()
        (b / 'Info.fcpxml').write_text('<fcpxml/>')
        (b / 'extra.bin').write_bytes(b'a')
        d1 = ledger.digest_path(b)
        (b / 'extra.bin').write_bytes(b'b')  # Info.fcpxml unchanged
        self.assertNotEqual(d1, ledger.digest_path(b))
        (b / '.DS_Store').write_bytes(b'finder')
        self.assertEqual(ledger.digest_path(b), ledger.digest_path(b))

    def test_torn_line_skipped_and_next_append_clean(self):
        first = ledger.record(self.led, 't', 'build', {}, [], [self.out])
        with self.led.open('ab') as f:
            f.write(b'{"id": "torn", "ts": ')  # crash mid-write, no newline
        skipped = []
        self.assertEqual([e['id'] for e in ledger.entries(self.led, skipped)], [first['id']])
        self.assertEqual(skipped, [2])
        second = ledger.record(self.led, 't', 'build', {}, [], [self.out])
        self.assertEqual([e['id'] for e in ledger.entries(self.led)], [first['id'], second['id']])

    def test_undo_moves_not_deletes(self):
        content = self.out.read_bytes()
        e = ledger.record(self.led, 't', 'build', {}, [self.src], [self.out])
        report = ledger.undo(self.led, e['id'])
        self.assertFalse(self.out.exists())
        moved = Path(report['moves'][0]['to'])
        self.assertEqual(moved.parent, self.out.parent / 'undone')
        self.assertTrue(moved.name.endswith('-v01.fcpxml'))
        self.assertEqual(moved.read_bytes(), content)
        self.assertTrue(self.src.exists())  # inputs untouched
        last = ledger.entries(self.led)[-1]
        self.assertEqual((last['action'], last['args']['target']), ('undo', e['id']))
        with self.assertRaises(ledger.UndoRefused):
            ledger.undo(self.led, e['id'])  # already undone
        with self.assertRaises(ledger.UndoRefused):
            ledger.undo(self.led, last['id'])  # undo of an undo

    def test_undo_bundle(self):
        b = self.root / 'out' / 'v02.fcpxmld'
        b.mkdir()
        (b / 'Info.fcpxml').write_text('<fcpxml/>')
        e = ledger.record(self.led, 't', 'build', {}, [], [b])
        report = ledger.undo(self.led, e['id'])
        self.assertTrue((Path(report['moves'][0]['to']) / 'Info.fcpxml').is_file())

    def test_undo_refuses_after_edit_and_moves_nothing(self):
        other = self.root / 'out' / 'v01b.fcpxml'
        other.write_text('<b/>')
        e = ledger.record(self.led, 't', 'build', {}, [], [other, self.out])
        self.out.write_text('<fcpxml edited-by-user="1"/>')
        with self.assertRaises(ledger.UndoRefused) as ctx:
            ledger.undo(self.led, e['id'])
        self.assertEqual(ctx.exception.problems[0]['problem'], 'changed since recorded')
        self.assertTrue(other.exists() and self.out.exists())
        self.assertFalse((self.out.parent / 'undone').exists())
        self.assertEqual(len(ledger.entries(self.led)), 1)

    def test_undo_refuses_missing_output(self):
        e = ledger.record(self.led, 't', 'build', {}, [], [self.out])
        self.out.rename(self.root / 'elsewhere.fcpxml')
        with self.assertRaises(ledger.UndoRefused) as ctx:
            ledger.undo(self.led, e['id'])
        self.assertEqual(ctx.exception.problems[0]['problem'], 'missing')

    def test_verify(self):
        a = ledger.record(self.led, 't', 'build', {}, [], [self.out])
        other = self.root / 'out' / 'c.fcpxml'
        other.write_text('<c/>')
        b = ledger.record(self.led, 't', 'build', {}, [], [other])
        other.write_text('<c changed="1"/>')
        status = {r['id']: r for r in ledger.verify(self.led)}
        self.assertTrue(status[a['id']]['ok'])
        self.assertEqual(status[b['id']]['outputs'][0]['status'], 'changed')
        ledger.undo(self.led, a['id'])
        status = {r['id']: r for r in ledger.verify(self.led)}
        self.assertEqual(status[a['id']]['outputs'][0]['status'], 'undone')

    def test_cli_undo_refusal_exit_code(self):
        e = ledger.record(self.led, 't', 'build', {}, [], [self.out])
        self.out.write_text('edited')
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ledger.main(['undo', '--ledger', str(self.led), '--id', e['id']])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buf.getvalue())['refused'], e['id'])


if __name__ == '__main__':
    unittest.main()
