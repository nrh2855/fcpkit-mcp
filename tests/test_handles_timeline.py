"""timeline resolution, handles (transition media + jump-cut dissolves), filmstrip pictures."""
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fcpkit import filmstrip  # noqa: E402
from fcpkit import handles  # noqa: E402
from fcpkit import retime  # noqa: E402
from fixtures import asset, fcpxml, make_still, make_video, write  # noqa: E402
from fcpkit.timeline import Timeline  # noqa: E402

TR = ('<transition name="Cross Dissolve" offset="{o}" duration="{d}">'
      '<filter-video ref="rx" name="Cross Dissolve"/></transition>')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.video = make_video(cls.dir / 'a.mp4', 10)
        cls.quiet = make_video(cls.dir / 'q.mp4', 10, loud=False)
        cls.still = make_still(cls.dir / 's.png')
        cls.res = asset('r2', cls.video) + asset('r3', cls.still, still=True) + asset('r4', cls.quiet)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def tl(self, spine, name='t.fcpxml', resources=''):
        return Timeline(write(self.dir, name, fcpxml(self.res + resources, spine)))


class TimelineTest(Base):
    def test_asset_clip_resolves_file_time_and_range(self):
        tl = self.tl('<asset-clip ref="r2" offset="0s" start="2s" duration="3s" name="A"/>')
        it = tl.main_items()[0]
        r = tl.resolve(it, Fraction(3), 'video')
        self.assertEqual(r.file, self.video)
        self.assertEqual(r.file_time, 3)
        self.assertEqual((r.lo, r.hi), (0, 10))

    def test_anchored_offsets_are_in_host_time(self):
        tl = self.tl('<asset-clip ref="r2" offset="0s" start="2s" duration="5s" name="A">'
                     '<title ref="rt" lane="1" offset="3s" duration="1s" name="T"/></asset-clip>')
        title = [i for i in tl.items if i.tag == 'title'][0]
        self.assertEqual(title.abs_start, 1)  # 0 + 3 - 2

    def test_still_is_unlimited(self):
        tl = self.tl('<video ref="r3" offset="0s" duration="2s" name="S"/>')
        self.assertTrue(tl.resolve(tl.main_items()[0], Fraction(1), 'video').unlimited)

    def test_multicam_angle_and_anchored_audio(self):
        media = ('<media id="m1" name="MC"><multicam format="r1" tcStart="0s">'
                 '<mc-angle name="A" angleID="a1"><clip offset="0s" duration="10s">'
                 '<video ref="r2" offset="0s" duration="10s"><audio ref="r4" lane="-1" offset="0s" duration="10s"/>'
                 '</video></clip></mc-angle></multicam></media>')
        tl = self.tl('<mc-clip ref="m1" offset="0s" start="4s" duration="2s" name="MC">'
                     '<mc-source angleID="a1" srcEnable="all"/></mc-clip>', 'mc.fcpxml', media)
        it = tl.main_items()[0]
        self.assertEqual(tl.resolve(it, Fraction(5), 'video').file, self.video)
        self.assertEqual(tl.resolve(it, Fraction(5), 'audio').file, self.quiet)

    def test_timemap_linear_and_refused_ramp(self):
        import xml.etree.ElementTree as ET
        lin = retime.TimeMap.from_element(ET.fromstring(
            '<timeMap><timept time="0s" value="0s" interp="linear"/><timept time="2s" value="4s" interp="linear"/></timeMap>'))
        self.assertEqual(lin.media_time(Fraction(1)), 2)
        self.assertEqual(lin.local_time(Fraction(3)), Fraction(3, 2))
        ramp = retime.TimeMap.from_element(ET.fromstring(
            '<timeMap><timept time="0s" value="0s"/><timept time="1s" value="1s"/><timept time="2s" value="4s"/></timeMap>'))
        self.assertFalse(ramp.supported)


class HandlesTest(Base):
    def check(self, spine, name):
        return handles.check(self.tl(spine, name))['rows']

    def test_jump_cut_dissolve_fails(self):
        rows = self.check('<asset-clip ref="r2" offset="0s" start="1s" duration="3s" name="A"/>'
                          + TR.format(o='2.5s', d='1s')
                          + '<asset-clip ref="r2" offset="3s" start="6s" duration="2s" name="B"/>', 'j.fcpxml')
        self.assertEqual(rows[0]['kind'], 'jump')
        self.assertEqual(rows[0]['status'], 'FAIL')

    def test_continuous_angle_switch_passes(self):
        rows = self.check('<asset-clip ref="r2" offset="0s" start="1s" duration="3s" name="A"/>'
                          + TR.format(o='2.5s', d='1s')
                          + '<asset-clip ref="r2" offset="3s" start="4s" duration="2s" name="B"/>', 'c.fcpxml')
        self.assertEqual((rows[0]['kind'], rows[0]['status']), ('continuous', 'PASS'))

    def test_missing_head_handle_fails_and_names_max(self):
        rows = self.check('<asset-clip ref="r2" offset="0s" start="1s" duration="3s" name="A"/>'
                          + TR.format(o='2.5s', d='1s')
                          + '<asset-clip ref="r4" offset="3s" start="0.2s" duration="2s" name="B"/>', 'h.fcpxml')
        self.assertEqual(rows[0]['status'], 'FAIL')
        self.assertEqual(rows[0]['sides']['video_in']['status'], 'FAIL')
        self.assertEqual(rows[0]['max_centred_frames'], 12)  # 2 x 6 frames of head

    def test_off_centre_transition_measured_where_it_sits(self):
        rows = self.check('<asset-clip ref="r2" offset="0s" start="1s" duration="3s" name="A"/>'
                          + TR.format(o='2s', d='1s')  # all of it before the cut
                          + '<asset-clip ref="r4" offset="3s" start="0.5s" duration="2s" name="B"/>', 'o.fcpxml')
        self.assertEqual((rows[0]['before_frames'], rows[0]['after_frames']), (30, 0))
        self.assertEqual(rows[0]['sides']['video_in']['status'], 'FAIL')  # needs 30 f, has 15

    def test_fade_from_nothing_needs_no_handles(self):
        rows = self.check('<gap name="Gap" offset="0s" duration="5s">'
                          '<spine lane="1" offset="1s">' + TR.format(o='0s', d='1s')
                          + '<video ref="r3" offset="0s" duration="2s" name="S"/></spine></gap>', 'f.fcpxml')
        self.assertEqual((rows[0]['kind'], rows[0]['status']), ('fade', 'PASS'))


class FilmstripTest(Base):
    def test_waveform_is_drawn_and_differs_with_loudness(self):
        loud = filmstrip.source_strip(self.video, 1, 2, self.dir / 'l.png', frames=4)
        quiet = filmstrip.source_strip(self.quiet, 1, 2, self.dir / 'q.png', frames=4)
        self.assertTrue(loud['waveform'] and quiet['waveform'])
        # same picture frames (testsrc2), different audio -> the images must differ
        self.assertNotEqual((self.dir / 'l.png').read_bytes(), (self.dir / 'q.png').read_bytes())

    def test_program_strip_marks_cuts(self):
        tl = self.tl('<asset-clip ref="r2" offset="0s" start="1s" duration="2s" name="A"/>'
                     '<asset-clip ref="r4" offset="2s" start="5s" duration="2s" name="B"/>', 'p.fcpxml')
        meta = filmstrip.program_strip(tl, Fraction(2), Fraction(1, 2), Fraction(1, 2), self.dir / 'p.png', frames=6)
        self.assertEqual(meta['cuts'], [2.0])
        self.assertEqual({r['file'] for r in meta['frames']}, {str(self.video), str(self.quiet)})


if __name__ == '__main__':
    unittest.main()
