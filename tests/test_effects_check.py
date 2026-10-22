"""effects_check: synthetic FCPXMLs in a temp dir."""
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fcpkit import effects_check  # noqa: E402

CROSS_DISSOLVE = '4731E73A-8DAC-4113-9A30-AE85B1761265'
BOGUS = '00000000-1111-2222-3333-444444444444'


def fcpxml(effects: str, spine: str = '') -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
 <resources>
  <format id="r1" frameDuration="1/30s" width="1920" height="1080"/>
  {effects}
 </resources>
 <library><event name="e"><project name="p">
  <sequence format="r1" duration="10s" tcStart="0s"><spine>
   <gap name="Gap" offset="0s" duration="10s">{spine}</gap>
  </spine></sequence>
 </project></event></library>
</fcpxml>'''


class EffectsCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = effects_check.load_fxplug_registry()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.templates = self.dir / 'Motion Templates.localized'
        moti = self.templates / 'Titles.localized' / '테스트' / 'T1.localized' / 'T1.moti'
        moti.parent.mkdir(parents=True)
        moti.write_text('x')
        self.moti = moti

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, effects, spine=''):
        p = self.dir / 'a.fcpxml'
        p.write_text(fcpxml(effects, spine), encoding='utf-8')
        rep = effects_check.check(p, registry=self.registry, templates_dir=self.templates)
        return {r['id']: r for r in rep['rows']}, rep

    def test_template_without_src_is_missing(self):
        rows, rep = self.run_check('<effect id="r2" name="T1" uid="~/Titles.localized/테스트/T1.localized/T1.moti"/>',
                                   '<title ref="r2" lane="1" offset="0s" duration="1s"/>'
                                   '<title ref="r2" lane="1" offset="2s" duration="1s"/>')
        self.assertEqual(rows['r2']['status'], 'MISSING')
        self.assertIn('without src', rows['r2']['detail'])
        self.assertTrue(rows['r2']['uid_exists'])
        self.assertEqual(rows['r2']['used_by_count'], 2)
        self.assertEqual(effects_check.exit_code(rep), 1)

    def test_template_with_existing_src_is_ok(self):
        src = 'file://' + quote(str(self.moti))
        rows, _ = self.run_check(f'<effect id="r2" name="T1" uid="~/Titles.localized/테스트/T1.localized/T1.moti" src="{src}"/>')
        self.assertEqual(rows['r2']['status'], 'OK')
        self.assertTrue(rows['r2']['src_matches_uid'])

    def test_template_with_missing_src_is_missing(self):
        rows, _ = self.run_check('<effect id="r2" name="T1" uid="~/Titles.localized/테스트/T1.localized/T1.moti" '
                                 'src="file:///nonexistent/Motion%20Templates.localized/T1.moti"/>')
        self.assertEqual(rows['r2']['status'], 'MISSING')
        self.assertIn('src file not found', rows['r2']['detail'])

    def test_library_template_without_src_is_missing(self):
        rows, _ = self.run_check('<effect id="r2" name="T1" uid="..../165B68C8-9FC9-4B48-B884-EDA98DEE1EC8/'
                                 'Motion Templates.localized/Titles.localized/x/x.moti"/>')
        self.assertEqual(rows['r2']['status'], 'MISSING')

    def test_bare_uuid_is_missing(self):
        rows, _ = self.run_check(f'<effect id="r2" name="Cross Dissolve" uid="{CROSS_DISSOLVE}"/>')
        self.assertEqual(rows['r2']['status'], 'MISSING')
        self.assertIn('FxPlug:', rows['r2']['detail'])

    def test_fxplug_cross_dissolve(self):
        rows, _ = self.run_check(f'<effect id="r2" name="Cross Dissolve" uid="FxPlug:{CROSS_DISSOLVE}"/>',
                                 '<transition offset="1s" duration="1s"><filter-video ref="r2"/></transition>')
        expected = 'OK' if self.registry.get('has_fcp_registry') else 'REVIEW'
        self.assertEqual(rows['r2']['status'], expected, rows['r2']['detail'])
        self.assertEqual(rows['r2']['used_by'], {'filter-video': 1})

    def test_fxplug_unknown_uuid(self):
        rows, _ = self.run_check(f'<effect id="r2" name="X" uid="FxPlug:{BOGUS}"/>')
        expected = 'MISSING' if self.registry.get('has_fcp_registry') else 'REVIEW'
        self.assertEqual(rows['r2']['status'], expected)

    def test_no_registry_is_review_not_ok(self):
        p = self.dir / 'a.fcpxml'
        p.write_text(fcpxml(f'<effect id="r2" name="Cross Dissolve" uid="FxPlug:{CROSS_DISSOLVE}"/>'), encoding='utf-8')
        empty = {'uuids': {}, 'scanned': [], 'has_fcp_registry': False}
        rep = effects_check.check(p, registry=empty, templates_dir=self.templates, fcp_apps=[])
        self.assertEqual(rep['rows'][0]['status'], 'REVIEW')
        self.assertEqual(effects_check.exit_code(rep), 2)

    def test_builtin_template(self):
        rows, _ = self.run_check(
            '<effect id="r2" name="Custom" uid=".../Titles.localized/Build In:Out.localized/Custom.localized/Custom.moti"/>'
            '<effect id="r3" name="Nope" uid=".../Titles.localized/Nope.localized/Nope.moti"/>')
        if effects_check.builtin_template_roots():
            self.assertEqual(rows['r2']['status'], 'OK', rows['r2']['detail'])
            self.assertEqual(rows['r3']['status'], 'MISSING')
        else:
            self.assertEqual(rows['r2']['status'], 'REVIEW')

    def test_internal_names_are_info(self):
        rows, rep = self.run_check('<effect id="r2" name="Audio Crossfade" uid="FFAudioTransition"/>'
                                   '<effect id="r3" name="Compressor" uid="AudioUnit: 0x617566780000009a454d4147"/>'
                                   '<effect id="r4" name="Pitch" uid="Pitch.Voice.audio.effectBundle"/>')
        self.assertEqual({r['status'] for r in rows.values()}, {'INFO'})
        self.assertEqual(effects_check.exit_code(rep), 0)

    def test_fcpxmld_bundle(self):
        bundle = self.dir / 'b.fcpxmld'
        bundle.mkdir()
        (bundle / 'Info.fcpxml').write_text(fcpxml('<effect id="r2" name="A" uid="FFAudioTransition"/>'), encoding='utf-8')
        rep = effects_check.check(bundle, registry=self.registry, templates_dir=self.templates)
        self.assertEqual(rep['effects'], 1)


if __name__ == '__main__':
    unittest.main()
