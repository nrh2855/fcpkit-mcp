"""Offline tests for fcp_import_xml.py: the import copy, the checks, and the live flow with fakes.

No Apple event is sent and FCP is never opened: the live flow runs against a fake lister/opener/
dialog source, and the FCP.lock path is a temp file. Temp files go to $FCPKIT_TEST_TMP if set.
"""
import json
import os
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import fcp_import_xml as IX  # noqa: E402
from fcpkit import plan as PL  # noqa: E402

IO = IX.IO
EVENT, PROJECT = '테스트 이벤트 v01', '테스트 프로젝트 v01'
SYNTH = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    {{opts}}<resources>
        <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
    </resources>
    <event name="{EVENT}"{{uid}}>
        <project name="{PROJECT}">
            <sequence format="r1" duration="30s" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
                <spine>
                    <gap name="Gap" offset="0s" start="0s" duration="30s"/>
                </spine>
            </sequence>
        </project>
    </event>
</fcpxml>
'''
OLD_OPTS = ('<import-options>\n        <option key="suppress warnings" value="1"/>\n'
            '        <option key="library location" value="file:///old.fcpbundle"/>\n    </import-options>\n    ')
APP = '/Applications/Final Cut Pro.app'
OTHER_APP = '/Applications/Final Cut Pro 12.3.app'


def strip_opts(root):
    for el in [e for e in root if e.tag == 'import-options']:
        root.remove(el)
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get('FCPKIT_TEST_TMP'))
        self.root = Path(self._tmp.name)
        self.lib = str(self.root / '테스트 라이브러리 v01.fcpbundle')

    def tearDown(self):
        self._tmp.cleanup()

    def synth(self, name='v01.fcpxml', opts='', uid=''):
        p = self.root / name
        p.write_text(SYNTH.replace('{opts}', opts).replace('{uid}', uid))
        return p


class CopyTest(Base):
    def options(self, copy):
        tree, _ = IO.parse_fcpxml(Path(copy))
        root = tree.getroot()
        first = [e for e in root][0]
        return root, first, {o.get('key'): o.get('value') for o in first}

    def test_import_options_first_child(self):
        src = self.synth()
        res = IX.make_import_copy(src, self.lib)
        self.assertEqual(Path(res['copy']).name, 'v01_import.fcpxml')
        root, first, opts = self.options(res['copy'])
        self.assertEqual(first.tag, 'import-options')
        self.assertEqual(opts, {'library location': IX.library_url(self.lib), 'copy assets': '0',
                                'suppress warnings': '0'})
        self.assertEqual(len(root.findall('import-options')), 1)
        self.assertFalse(res['replaced_import_options'])

    def test_replaces_existing_import_options(self):
        src = self.synth(opts=OLD_OPTS)
        res = IX.make_import_copy(src, self.lib)
        root, first, opts = self.options(res['copy'])
        self.assertTrue(res['replaced_import_options'])
        self.assertEqual(len(root.findall('import-options')), 1)
        self.assertEqual(len(first), 3)
        self.assertEqual(opts['suppress warnings'], '0')
        self.assertEqual(opts['library location'], IX.library_url(self.lib))

    def test_korean_path_percent_encoding(self):
        path = '/Volumes/Media/Projects/한글/2. 편집 라이브러리/주간 라이브러리.fcpbundle'
        url = IX.library_url(path)
        self.assertTrue(url.startswith('file:///Volumes/Media/Projects/%ED%95%9C%EA%B8%80/2.%20'))
        self.assertTrue(url.endswith('.fcpbundle'))
        self.assertTrue(url.isascii())
        self.assertEqual(unquote(url[len('file://'):]), path)
        nfd = unicodedata.normalize('NFD', path)  # FCP's own spelling is kept as given
        self.assertEqual(unquote(IX.library_url(nfd + '/')[len('file://'):]), nfd)

    def test_original_untouched_and_rest_identical(self):
        src = self.synth(opts=OLD_OPTS)
        data, mtime = src.read_bytes(), src.stat().st_mtime_ns
        res = IX.make_import_copy(src, self.lib)
        self.assertEqual(src.read_bytes(), data)
        self.assertEqual(src.stat().st_mtime_ns, mtime)
        a, _ = IO.parse_fcpxml(src)
        b, _ = IO.parse_fcpxml(Path(res['copy']))
        self.assertEqual(IO._structural_signature(strip_opts(a.getroot()))[1:3],
                         IO._structural_signature(strip_opts(b.getroot()))[1:3])
        self.assertEqual(IO._structural_signature(a.getroot())[4], IO._structural_signature(b.getroot())[4])

    def test_copy_passes_dtd(self):
        res = IX.make_import_copy(self.synth(uid=' uid="ABC"'), self.lib)
        self.assertEqual(res['dtd_validation']['status'], 'pass')
        again = IO.validate_fcpxml_dtd(Path(res['copy']).read_bytes())
        self.assertEqual((again['version'], again['status']), ('1.11', 'pass'))

    def test_refuses_existing_copy(self):
        src = self.synth()
        IX.make_import_copy(src, self.lib)
        with self.assertRaises(FileExistsError):
            IX.make_import_copy(src, self.lib)


class CheckTest(Base):
    def snap(self, events):
        nfd = unicodedata.normalize('NFD', self.lib).upper() + '/'
        return {'libraries': [{'name': 'L', 'path': nfd, 'events': [
            {'name': unicodedata.normalize('NFD', n), 'persistent_id': f'E{i}', 'projects': []}
            for i, n in enumerate(events)]}]}

    def test_refuses_existing_event_name(self):
        status, detail = IX.check_event_free(self.snap(['다른 이벤트', EVENT]), self.lib, [EVENT])
        self.assertEqual(status, 'fail')
        self.assertIn('NEW event', detail)
        self.assertEqual(IX.check_event_free(self.snap(['다른 이벤트']), self.lib, [EVENT])[0], 'pass')
        self.assertEqual(IX.check_event_free({'libraries': []}, self.lib, [EVENT])[0], 'todo')

    def test_quiet_minutes(self):
        os.mkdir(self.lib)
        db = Path(self.lib) / 'CurrentVersion.flexolibrary'
        db.write_text('x')
        self.assertEqual(IX.check_quiet([self.lib], 10)[0], 'fail')
        old = time.time() - 3600
        os.utime(db, (old, old))
        os.utime(self.lib, (old, old))
        self.assertEqual(IX.check_quiet([self.lib], 10)[0], 'pass')
        ev = Path(self.lib) / 'ev' / 'proj'
        ev.mkdir(parents=True)
        (ev / 'CurrentVersion.fcpevent').write_text('x')  # a project store touched just now
        self.assertEqual(IX.check_quiet([self.lib], 10)[0], 'fail')

    def test_event_uid_warns(self):
        facts = IX.xml_facts(self.synth(uid=' uid="ABC"'))
        rows = IX.offline_checks(self.synth('b.fcpxml', uid=' uid="ABC"'), self.lib, facts=facts,
                                 lock_path=self.root / 'FCP.lock', plan_path=None, plan_id=None, app=APP, out=None,
                                 create_library=False, quiet_minutes=10, dry_run=True, running={})
        self.assertIn('event-uid', [r['check'] for r in rows if r['status'] == 'warn'])


    def test_unmounted_volume_fails(self):
        rows = IX.offline_checks(self.synth(), self.lib, facts={**IX.xml_facts(self.synth('c.fcpxml')),
                                 'media': ['/Volumes/NoSuchDrive-fcpkit/a.mov', '/Users/x/b.mov']},
                                 lock_path=self.root / 'FCP.lock', plan_path=None, plan_id=None, app=APP, out=None,
                                 create_library=False, quiet_minutes=10, dry_run=True, running={})
        vol = [r for r in rows if r['check'] == 'volume']
        self.assertEqual([(r['status'], r['detail'].split(';')[0]) for r in vol],
                         [('fail', '1 media on /Volumes/NoSuchDrive-fcpkit')])
        self.assertEqual(IX.media_volumes(['/Volumes/A/x', '/Volumes/A/y/z', '/tmp/q']), {'/Volumes/A': 2})

class LiveFlowTest(Base):
    """import_xml end to end with fakes: no osascript, no open, temp lock."""

    def setUp(self):
        super().setUp()
        os.mkdir(self.lib)
        db = Path(self.lib) / 'CurrentVersion.flexolibrary'
        db.write_text('x')
        old = time.time() - 3600
        os.utime(db, (old, old))
        self.lock = self.root / 'FCP.lock'
        self.xml = self.synth()
        self.inst = {'path': APP, 'pid': 4242, 'version': '12.3', 'splicekit': True}
        self.opened = []
        self.fcp_path = unicodedata.normalize('NFD', self.lib)  # FCP's spelling

    def lister(self, durations=True):
        events = [{'name': 'old', 'persistent_id': 'E0', 'projects': [{'name': 'x', 'persistent_id': 'P0'}]}]
        if self.opened:
            events.append({'name': EVENT, 'persistent_id': 'E9', 'projects': [
                {'name': PROJECT, 'id': 'pid9', 'persistent_id': 'P9', 'duration_seconds': 30.0}]})
        return {'libraries': [{'name': 'L', 'path': self.fcp_path, 'persistent_id': 'L1', 'events': events}]}

    def plan(self):
        p = PL.make_plan(IX.ACTION, IX.plan_params(self.xml, self.lib, EVENT, APP))
        path = self.root / 'plan.json'
        path.write_text(json.dumps(p))
        return path, p['plan_id']

    def run_import(self, dialogs=lambda: [], **kw):
        plan_path, plan_id = kw.pop('plan', None) or self.plan()
        args = dict(plan_path=plan_path, plan_id=plan_id, receipt=self.root / 'receipt.json', lock_path=self.lock,
                    lister=self.lister, opener=self.opened.append, dialogs=dialogs, running=self.inst, poll=0.01,
                    settle=0.05, timeout=5)
        args.update(kw)
        return IX.import_xml(self.xml, self.lib, **args)

    def test_verified_import_and_lock_cycle(self):
        rec = self.run_import()
        self.assertEqual(rec['status'], 'verified')
        self.assertEqual(rec['verdict'], 'PASS')
        self.assertEqual([p['persistent_id'] for p in rec['projects']], ['P9'])
        self.assertEqual(len(self.opened), 1)
        tree, _ = IO.parse_fcpxml(Path(self.opened[0]))
        loc = [o.get('value') for o in tree.getroot()[0] if o.get('key') == 'library location'][0]
        self.assertEqual(unquote(loc[len('file://'):]), self.fcp_path)  # FCP's own path spelling
        self.assertFalse(self.lock.exists())
        released = list(self.root.glob('FCP.lock.released-*'))
        self.assertEqual(len(released), 1)
        body = json.loads(released[0].read_text())
        self.assertEqual(set(body), {'owner', 'what', 'events', 'since', 'pid'})
        self.assertEqual(body['events'], [EVENT])
        self.assertEqual(json.loads((self.root / 'receipt.json').read_text())['status'], 'verified')
        self.assertEqual([x['name'] for x in rec['diff']['projects_added']], [PROJECT])

    def test_warning_dialog_fails_verdict(self):
        def dialogs():
            return [{'class': 'NSPanel', 'title': 'Final Cut Pro', 'labels': ['The XML encountered problems']}] \
                if self.opened else []
        rec = self.run_import(dialogs=dialogs)
        self.assertEqual(rec['status'], 'verified')
        self.assertEqual(rec['verdict'], 'FAIL')
        self.assertEqual(len(rec['warnings']), 1)

    def test_no_bridge_is_notchecked(self):
        rec = IX.import_xml(self.xml, self.lib, plan_path=self.plan()[0], plan_id=self.plan_id_of(),
                            receipt=None, lock_path=self.lock, lister=self.lister, opener=self.opened.append,
                            dialogs=None, running={**self.inst, 'splicekit': False}, poll=0.01, settle=0.05,
                            timeout=5, out=self.root / 'other_import.fcpxml')
        self.assertEqual(rec['verdict'], 'NOTCHECKED')

    def plan_id_of(self):
        return json.loads((self.root / 'plan.json').read_text())['plan_id']

    def must_not_touch(self, *_a, **_k):
        raise AssertionError('talked to FCP before refusing')

    def test_refuses_when_lock_held_before_any_apple_event(self):
        self.lock.write_text('{"owner": "someone"}')
        with self.assertRaises(IX.ImportRefused):
            IX.import_xml(self.xml, self.lib, plan_path=self.plan()[0], plan_id=self.plan_id_of(),
                          lock_path=self.lock, lister=self.must_not_touch, opener=self.must_not_touch,
                          dialogs=self.must_not_touch, running=self.inst)
        self.assertEqual(self.lock.read_text(), '{"owner": "someone"}')
        self.assertEqual(list(self.root.glob('FCP.lock.released-*')), [])

    def test_refuses_without_or_with_wrong_plan(self):
        for plan_path, plan_id in ((None, None), (self.plan()[0], '0' * 64)):
            with self.assertRaises(IX.ImportRefused):
                IX.import_xml(self.xml, self.lib, plan_path=plan_path, plan_id=plan_id, lock_path=self.lock,
                              lister=self.must_not_touch, opener=self.must_not_touch, running=self.inst)
        other = PL.make_plan(IX.ACTION, IX.plan_params(self.xml, self.lib + 'x', EVENT, APP))
        (self.root / 'p2.json').write_text(json.dumps(other))
        with self.assertRaises(IX.ImportRefused):
            IX.import_xml(self.xml, self.lib, plan_path=self.root / 'p2.json', plan_id=other['plan_id'],
                          lock_path=self.lock, lister=self.must_not_touch, opener=self.must_not_touch,
                          running=self.inst)
        self.assertFalse(self.lock.exists())

    def test_refuses_when_not_running_or_other_app(self):
        for inst in ({}, {**self.inst, 'path': OTHER_APP}):
            with self.assertRaises(IX.ImportRefused):
                IX.import_xml(self.xml, self.lib, plan_path=self.plan()[0], plan_id=self.plan_id_of(),
                              lock_path=self.lock, lister=self.must_not_touch, opener=self.must_not_touch,
                              running=inst)
            (self.root / 'plan.json').unlink()

    def test_refuses_existing_event_in_live_listing(self):
        self.opened.append('pretend')  # listing already shows the event before the import
        with self.assertRaises(IX.ImportRefused) as cm:
            self.run_import(out=self.root / 'e_import.fcpxml')
        self.assertIn('NEW event', str(cm.exception))
        self.assertEqual(self.opened, ['pretend'])  # never opened
        self.assertFalse(self.lock.exists())
        self.assertEqual(len(list(self.root.glob('FCP.lock.released-*'))), 1)

    def test_refuses_library_not_open(self):
        self.fcp_path = str(self.root / 'another.fcpbundle')
        with self.assertRaises(IX.ImportRefused) as cm:
            self.run_import()
        self.assertIn('not open', str(cm.exception))
        self.assertEqual(self.opened, [])

    def test_refuses_recent_library_edit(self):
        os.utime(Path(self.lib) / 'CurrentVersion.flexolibrary', None)  # touched now
        with self.assertRaises(IX.ImportRefused):
            self.run_import()
        self.assertEqual(self.opened, [])

    def test_timeout_when_projects_never_appear(self):
        rec = self.run_import(opener=lambda c: None, timeout=0.2)
        self.assertEqual(rec['status'], 'timeout')
        self.assertEqual(rec['verdict'], 'FAIL')
        self.assertFalse(self.lock.exists())


class DryRunTest(Base):
    def test_dry_run_writes_copy_and_steps(self):
        src = self.synth()
        rec = IX.import_xml(src, self.lib, dry_run=True, lock_path=self.root / 'FCP.lock', running={},
                            out=self.root / 'dry_import.fcpxml')
        self.assertEqual(rec['status'], 'dry-run')
        self.assertEqual(rec['copy']['dtd_validation']['status'], 'pass')
        self.assertEqual(rec['would_refuse'], [])
        self.assertEqual({r['check'] for r in rec['todo_before_live']}, {'plan', 'app', 'library-disk'})
        self.assertTrue(any('open -a' in s for s in rec['live_steps']))
        self.assertIn('fcpkit-plan make --action import', rec['plan_command'])
        self.assertFalse((self.root / 'FCP.lock').exists())


if __name__ == '__main__':
    unittest.main()
