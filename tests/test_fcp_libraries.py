"""Offline tests for fcp_libraries.py: app identification by path, AppleScript text, parsing, diff.

Nothing here sends an Apple event. `osacompile` only compiles (reads the app's sdef from disk);
the test checks afterwards that no FCP process appeared. Temp files go to $FCPKIT_TEST_TMP if set.
"""
import os
import plistlib
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import config  # noqa: E402
from fcpkit import fcp_libraries as FL  # noqa: E402

US, RS = FL.US, FL.RS


def fake_app(root: Path, name: str, version: str) -> Path:
    """A minimal FCP-like .app bundle (Info.plist only) so tests do not depend on what is installed."""
    app = root / name
    (app / 'Contents/MacOS').mkdir(parents=True)
    with open(app / 'Contents/Info.plist', 'wb') as fh:
        plistlib.dump({'CFBundleExecutable': 'Final Cut Pro', 'CFBundleShortVersionString': version,
                       'CFBundleVersion': '1', 'CFBundleIdentifier': FL.BUNDLE_ID}, fh)
    return app


def listing(*records):
    return RS.join(US.join(r) for r in records) + RS + '\n'


class RunningTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get('FCPKIT_TEST_TMP'))
        root = Path(self._tmp.name)
        self.sk = fake_app(root, 'Final Cut Pro (SpliceKit).app', '12.3')
        self.stock = fake_app(root, 'Final Cut Pro.app', '12.4')
        self.apps = (self.sk, self.stock)
        self.sk_exe = str(self.sk / 'Contents/MacOS/Final Cut Pro')
        self.stock_exe = str(self.stock / 'Contents/MacOS/Final Cut Pro')

    def tearDown(self):
        self._tmp.cleanup()

    def test_running_fcp_matches_process_table(self):
        # FCP may or may not be running here; whatever runs must be an FCP executable inside an .app
        got = FL.running_fcp()
        if got is not None:
            self.assertTrue(got['executable'].endswith('.app/Contents/MacOS/Final Cut Pro'))

    def test_identifies_splicekit_by_path(self):
        procs = [(1, '/sbin/launchd'), (4242, self.sk_exe), (77, '/bin/zsh')]
        got = FL.running_fcp(procs, pid_path=lambda pid: None, apps=self.apps)
        self.assertEqual(got['path'], str(self.sk))
        self.assertEqual(got['pid'], 4242)
        self.assertTrue(got['splicekit'])
        self.assertEqual(got['version'], '12.3')
        self.assertTrue(got['known'])

    def test_identifies_stock_by_path(self):
        got = FL.running_fcp([(9, self.stock_exe)], pid_path=lambda pid: None, apps=self.apps)
        self.assertEqual(got['path'], str(self.stock))
        self.assertFalse(got['splicekit'])
        self.assertEqual(got['version'], '12.4')

    def test_unknown_copy_is_still_found(self):
        got = FL.running_fcp([(9, self.stock_exe)], pid_path=lambda pid: None, apps=())
        self.assertEqual(got['version'], '12.4')
        self.assertFalse(got['known'])

    def test_pidpath_wins_over_comm(self):
        got = FL.running_fcp([(9, 'Final Cut Pro')], pid_path=lambda pid: self.sk_exe, apps=self.apps)
        self.assertEqual(got['path'], str(self.sk))

    def test_two_instances_are_ambiguous(self):
        with self.assertRaises(FL.FCPAmbiguous):
            FL.running_fcp([(1, self.sk_exe), (2, self.stock_exe)], pid_path=lambda pid: None, apps=self.apps)

    def test_apps_have_same_bundle_id_different_versions(self):
        apps = {a['path']: a for a in FL.fcp_apps(self.apps)}
        self.assertEqual({a['bundle_id'] for a in apps.values()}, {FL.BUNDLE_ID})
        self.assertNotEqual(apps[str(self.stock)]['version'], apps[str(self.sk)]['version'])

    def test_config_apps_from_env(self):
        old = {k: os.environ.get(k) for k in ('FCPKIT_FCP_APP', 'FCPKIT_FCP_APPS')}
        try:
            os.environ['FCPKIT_FCP_APP'] = str(self.sk)
            os.environ['FCPKIT_FCP_APPS'] = str(self.stock)
            apps = config.fcp_apps()
            self.assertEqual(apps[:2], (self.sk, self.stock))
            self.assertEqual(config.fcp_app(), self.sk)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get('FCPKIT_TEST_TMP'))
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_script_shape(self):
        s = FL.applescript_list_libraries(str(FL.SPLICEKIT_APP))
        guard = s.index(f'application "{FL.SPLICEKIT_APP}" is running')
        tell = s.index(f'tell application "{FL.SPLICEKIT_APP}"')
        self.assertLess(s.index('set out to ""'), tell)  # output declared before the tell block
        self.assertLess(guard, tell)                       # no launch: running check first
        self.assertIn('POSIX path of ((file of lib) as text)', s)
        self.assertIn('character id 30', s)
        self.assertIn('character id 31', s)
        self.assertIn('persistent ID of pj', s)
        self.assertNotIn('duration of', FL.applescript_list_libraries(str(FL.STOCK_APP), durations=False))

    def test_quotes_are_escaped(self):
        s = FL.applescript_list_libraries('/tmp/a "b"\\c.app')
        self.assertIn('application "/tmp/a \\"b\\"\\\\c.app"', s)

    @unittest.skipUnless(any(a.is_dir() for a in config.fcp_apps()), 'no Final Cut Pro installed')
    def test_script_compiles_without_launching(self):
        before = (FL.running_fcp() or {}).get('pid')
        for app in [a for a in config.fcp_apps() if a.is_dir()]:
            for durations in (True, False):
                src = self.root / 'list.applescript'
                src.write_text(FL.applescript_list_libraries(str(app), 20, durations))
                out = self.root / 'x.scpt'
                r = subprocess.run(['/usr/bin/osacompile', '-o', str(out), str(src)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(out.exists())
                out.unlink()
        self.assertEqual((FL.running_fcp() or {}).get('pid'), before, 'osacompile must not launch or restart FCP')


class ParseTest(unittest.TestCase):
    TEXT = listing(['A', '12.3'],
                   ['L', '주간 라이브러리', '/Volumes/Media/Lib.fcpbundle/', 'lib-id', 'LIBPID'],
                   ['E', '2. 테스트_편집_v11', 'ev-id', 'EVPID'],
                   ['P', '2. 테스트_편집_v11', 'pj-id', 'PJPID', '49889840', '30000'],
                   ['P', 'no duration', 'pj2', 'PJ2', '', ''],
                   ['E', 'empty', 'ev2', 'EV2'],
                   ['L', 'Other', '', 'l2', 'L2'])

    def test_parse(self):
        version, libs = FL.parse_listing(self.TEXT)
        self.assertEqual(version, '12.3')
        self.assertEqual([l['name'] for l in libs], ['주간 라이브러리', 'Other'])
        lib = libs[0]
        self.assertEqual(lib['path'], '/Volumes/Media/Lib.fcpbundle')
        self.assertEqual(lib['persistent_id'], 'LIBPID')
        pj = lib['events'][0]['projects'][0]
        self.assertAlmostEqual(pj['duration_seconds'], 49889840 / 30000, places=5)
        self.assertNotIn('duration_seconds', lib['events'][0]['projects'][1])
        self.assertEqual(lib['events'][1]['projects'], [])

    def test_real_formatted_numbers(self):
        _, libs = FL.parse_listing(listing(['A', '12.3'], ['L', 'x', '/x.fcpbundle', 'i', 'p'], ['E', 'e', 'i', 'p'],
                                           ['P', 'p', 'i', 'p', '4.989984E+7', '30000']))
        self.assertAlmostEqual(libs[0]['events'][0]['projects'][0]['duration_seconds'], 1663.328, places=3)

    def test_not_running_marker(self):
        with self.assertRaises(FL.FCPNotRunning):
            FL.parse_listing('ERR' + US + 'not running\n')

    def test_list_refuses_when_not_running(self):
        def runner(*_):
            raise AssertionError('must not run osascript')
        with self.assertRaises(FL.FCPNotRunning):
            FL.list_libraries(runner=runner, running={})  # {} = nothing running

    def test_list_checks_responding_version(self):
        inst = {'path': '/Applications/Final Cut Pro 12.3.app', 'pid': 1, 'version': '12.3', 'splicekit': False}
        libs = FL.list_libraries(runner=lambda s, t: self.TEXT, running=inst)
        self.assertEqual(len(libs), 2)
        wrong = self.TEXT.replace('A' + US + '12.3', 'A' + US + '12.4')
        with self.assertRaises(FL.FCPError):
            FL.list_libraries(runner=lambda s, t: wrong, running=inst)
        with self.assertRaises(FL.FCPError):
            FL.list_libraries(app='/Applications/Final Cut Pro.app', runner=lambda s, t: self.TEXT, running=inst)

    def test_error_hints(self):
        err = 'execution error: Not authorized to send Apple events to Final Cut Pro. (-1743)'
        self.assertEqual(FL._error_code(err), -1743)
        self.assertIn('Automation', FL.HINTS[-1743])
        self.assertIn('dialog', FL.HINTS[-1712])


class PathAndDiffTest(unittest.TestCase):
    def test_norm_path(self):
        nfc = '/Volumes/Media/한글/라이브러리.fcpbundle'
        nfd = unicodedata.normalize('NFD', nfc).upper() + '/'
        self.assertTrue(FL.same_path(nfc, nfd))
        self.assertFalse(FL.same_path(nfc, nfc.replace('라이브러리', '다른')))

    def test_diff(self):
        before = {'libraries': [{'name': 'L', 'path': '/v/L.fcpbundle', 'events': [
            {'name': 'old', 'persistent_id': 'E1', 'projects': [
                {'name': 'a', 'persistent_id': 'P1', 'duration_seconds': 10.0},
                {'name': 'gone', 'persistent_id': 'P2'}]}]}]}
        after = {'libraries': [
            {'name': 'L', 'path': unicodedata.normalize('NFD', '/v/L.fcpbundle/'), 'events': [
                {'name': 'old', 'persistent_id': 'E1', 'projects': [
                    {'name': 'a', 'persistent_id': 'P1', 'duration_seconds': 11.0}]},
                {'name': 'new', 'persistent_id': 'E2', 'projects': [{'name': 'b', 'persistent_id': 'P3'}]}]},
            {'name': 'M', 'path': '/v/M.fcpbundle', 'events': []}]}
        d = FL.snapshot_diff(before, after)
        self.assertEqual([x['path'] for x in d['libraries_added']], ['/v/M.fcpbundle'])
        self.assertEqual([x['event'] for x in d['events_added']], ['new'])
        self.assertEqual([x['name'] for x in d['projects_added']], ['b'])
        self.assertEqual([x['name'] for x in d['projects_removed']], ['gone'])
        self.assertEqual([x['name'] for x in d['projects_changed']], ['a'])
        self.assertTrue(d['changed'])
        self.assertFalse(FL.snapshot_diff(before, before)['changed'])


if __name__ == '__main__':
    unittest.main()
