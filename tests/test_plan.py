"""Tests for plan.py: id binds every param and the XML bytes; expiry; tampering."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import plan  # noqa: E402

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.xml = self.root / 'v01.fcpxml'
        self.xml.write_text('<fcpxml version="1.11"/>')
        self.params = {**plan.xml_params(self.xml), 'library': '/Volumes/Media/a.fcpbundle',
                       'event': 'v01 test event', 'app': '/Applications/Final Cut Pro.app'}

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_inputs_same_id(self):
        a = plan.make_plan('import', self.params, now=T0)
        b = plan.make_plan('import', dict(reversed(list(self.params.items()))), now=T0)
        self.assertEqual(a['plan_id'], b['plan_id'])
        plan.check_plan(a, 'import', self.params, a['plan_id'], now=T0 + timedelta(minutes=1))

    def test_id_changes_with_each_param_and_action(self):
        base = plan.make_plan('import', self.params, now=T0)['plan_id']
        for key in self.params:
            changed = {**self.params, key: self.params[key] + 'x'}
            self.assertNotEqual(plan.make_plan('import', changed, now=T0)['plan_id'], base, key)
        self.assertNotEqual(plan.make_plan('relink', self.params, now=T0)['plan_id'], base)
        self.assertNotEqual(plan.make_plan('import', {**self.params, 'extra': 1}, now=T0)['plan_id'], base)

    def test_xml_bytes_change_refused(self):
        p = plan.make_plan('import', self.params, now=T0)
        self.xml.write_text('<fcpxml version="1.11"><rebuilt/></fcpxml>')
        live = {**self.params, **plan.xml_params(self.xml)}
        with self.assertRaises(plan.PlanError) as ctx:
            plan.check_plan(p, 'import', live, now=T0)
        self.assertIn('xml_sha256', str(ctx.exception))

    def test_bundle_xml_params(self):
        b = self.root / 'v02.fcpxmld'
        b.mkdir()
        (b / 'Info.fcpxml').write_text('<fcpxml/>')
        (b / 'side.json').write_text('1')
        before = plan.xml_params(b)['xml_sha256']
        (b / 'side.json').write_text('2')
        self.assertNotEqual(before, plan.xml_params(b)['xml_sha256'])

    def test_expired_refused(self):
        p = plan.make_plan('import', self.params, ttl_minutes=30, now=T0)
        plan.check_plan(p, 'import', self.params, now=T0 + timedelta(minutes=29))
        with self.assertRaises(plan.PlanExpired):
            plan.check_plan(p, 'import', self.params, now=T0 + timedelta(minutes=30))

    def test_tampered_file_refused(self):
        p = plan.make_plan('import', self.params, ttl_minutes=30, now=T0)
        p['expires_at'] = '2099-01-01T00:00:00Z'  # extend without re-approval
        with self.assertRaises(plan.PlanError):
            plan.check_plan(p, 'import', self.params, now=T0)

    def test_wrong_approved_id(self):
        p = plan.make_plan('import', self.params, now=T0)
        with self.assertRaises(plan.PlanError):
            plan.check_plan(p, 'import', self.params, '0' * 64, now=T0)

    def test_cli_make_and_check(self):
        out = self.root / 'plan.json'
        buf = io.StringIO()
        args = ['--xml', str(self.xml), '--param', 'event=v01', '--param', 'library=/L.fcpbundle']
        with redirect_stdout(buf):
            self.assertEqual(plan.main(['make', '--action', 'import', '--out', str(out)] + args), 0)
        pid = json.loads(buf.getvalue().splitlines()[0])['plan_id']
        with redirect_stdout(io.StringIO()):
            self.assertEqual(plan.main(['check', '--plan', str(out), '--id', pid] + args), 0)
            self.assertEqual(plan.main(['check', '--plan', str(out), '--xml', str(self.xml),
                                        '--param', 'event=v02', '--param', 'library=/L.fcpbundle']), 1)
            with self.assertRaises(SystemExit):  # never overwrite a plan
                plan.main(['make', '--action', 'import', '--out', str(out)] + args)


if __name__ == '__main__':
    unittest.main()
