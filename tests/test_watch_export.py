"""Tests for watch_export.py: content digest, settle across polls, newest by mtime."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import watch_export as we  # noqa: E402


class FakeTime:
    """clock/sleep pair; `on_sleep` hooks run once per sleep, in order."""

    def __init__(self, hooks=()):
        self.now = 1000.0
        self.hooks = list(hooks)
        self.sleeps = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.hooks:
            self.hooks.pop(0)()


class WatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.old = self.dir / 'old.fcpxml'
        self.old.write_text('<fcpxml/>')
        self.base = we.baseline_of(we.scan(self.dir, '*.fcpxml*'))

    def tearDown(self):
        self._tmp.cleanup()

    def wait(self, ft, **kw):
        return we.wait_for_export(self.dir, '*.fcpxml*', self.base, timeout=60, interval=2,
                                  clock=ft.clock, sleep=ft.sleep, **kw)

    def test_mtime_only_touch_is_not_fresh(self):
        os.utime(self.old, (1, 1))
        ft = FakeTime()
        found, rows = self.wait(ft)
        self.assertIsNone(found)
        self.assertEqual(rows, [])

    def test_ignores_file_still_being_written(self):
        f = self.dir / 'export.fcpxml'
        f.write_text('<fcpxml>')
        grow = lambda: f.write_text('<fcpxml><spine/>')     # noqa: E731
        done = lambda: f.write_text('<fcpxml><spine/></fcpxml>')  # noqa: E731
        ft = FakeTime([grow, done])
        found, _ = self.wait(ft)
        self.assertEqual(found['path'], str(f))
        self.assertEqual(found['digest'], we.digest_path(f))  # final bytes, not a partial
        self.assertEqual(ft.sleeps, 3)  # poll1 partial, poll2 grew, poll3 done, poll4 same

    def test_changed_existing_file_detected(self):
        self.old.write_text('<fcpxml changed="1"/>')
        found, _ = self.wait(FakeTime())
        self.assertEqual((found['path'], found['new']), (str(self.old), False))

    def test_settle_needs_one_second(self):
        f = self.dir / 'e.fcpxml'
        f.write_text('<x/>')
        ft = FakeTime()
        found, _ = we.wait_for_export(self.dir, '*.fcpxml*', self.base, timeout=60, interval=0.25,
                                      settle=1.0, clock=ft.clock, sleep=ft.sleep)
        self.assertIsNotNone(found)
        self.assertEqual(ft.sleeps, 4)  # 4 x 0.25 s before the same digest counts as settled

    def test_picks_newest_by_mtime_not_path(self):
        a, z = self.dir / 'a.fcpxml', self.dir / 'z.fcpxml'
        a.write_text('<a/>')
        z.write_text('<z/>')
        os.utime(a, (2_000_000_000, 2_000_000_000))  # 'a' sorts first but is newer
        os.utime(z, (1_900_000_000, 1_900_000_000))
        found, rows = self.wait(FakeTime())
        self.assertEqual(found['path'], str(a))
        self.assertEqual(len(rows), 2)

    def test_waits_for_newer_export_still_writing(self):
        older, newer = self.dir / 'older.fcpxml', self.dir / 'newer.fcpxml'
        older.write_text('<o/>')
        os.utime(older, (1_900_000_000, 1_900_000_000))
        newer.write_text('<n')
        os.utime(newer, (2_000_000_000, 2_000_000_000))

        def grow():  # newer still being written between poll 1 and 2
            newer.write_text('<n/>')
            os.utime(newer, (2_000_000_000, 2_000_000_000))
        ft = FakeTime([grow])
        found, _ = self.wait(ft)
        # poll 2: older is settled, but the newer one changed -> keep waiting
        self.assertEqual(found['path'], str(newer))
        self.assertEqual(found['digest'], we.digest_path(newer))
        self.assertEqual(ft.sleeps, 2)

    def test_bundle_change_in_any_file(self):
        b = self.dir / 'cut.fcpxmld'
        b.mkdir()
        (b / 'Info.fcpxml').write_text('<fcpxml/>')
        (b / 'Assets.json').write_text('1')
        self.base = we.baseline_of(we.scan(self.dir, '*.fcpxml*'))
        (b / 'Assets.json').write_text('2')
        found, _ = self.wait(FakeTime())
        self.assertEqual(found['path'], str(b))

    def test_timeout(self):
        ft = FakeTime()
        found, _ = we.wait_for_export(self.dir, '*.fcpxml*', self.base, timeout=5, interval=2,
                                      clock=ft.clock, sleep=ft.sleep)
        self.assertIsNone(found)
        self.assertEqual(ft.sleeps, 3)

    def test_cli_once_state_cycle(self):
        state = self.dir / 'state' / 'watch.json'
        args = ['--dir', str(self.dir), '--since-state', str(state), '--once']
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(we.main(args), 2)  # first run: baseline only
        self.assertEqual(json.loads(buf.getvalue())['baseline_recorded'], 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(we.main(args), 2)  # nothing changed
        f = self.dir / 'new.fcpxml'
        f.write_text('<new/>')
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(we.main(args), 0)  # real 1 s settle poll
        self.assertEqual(json.loads(buf.getvalue())['found'], str(f))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(we.main(args), 2)  # baseline advanced


if __name__ == '__main__':
    unittest.main()
