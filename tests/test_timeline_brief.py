"""timeline_brief: every timed item is shown once, under the cut on screen when it starts."""
import sys
import tempfile
import unittest
from pathlib import Path

from fcpkit import timeline_brief
from fixtures import asset, fcpxml, write  # noqa: E402


def cap(offset, duration, text, lane=1):
    return (f'<title ref="rt" lane="{lane}" offset="{offset}" duration="{duration}" name="{text}">'
            f'<text><text-style ref="s">{text}\n   </text-style></text>'
            '<text-style-def id="s"><text-style font="Helvetica" fontSize="60"/></text-style-def></title>')


class BriefTest(unittest.TestCase):
    def test_brief_lists_everything_once(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / 'a.mp4'
            src.write_bytes(b'')
            spine = ('<asset-clip ref="r2" offset="0s" start="10s" duration="5s" name="A">'
                     '<adjust-transform scale="1.25 1.25"/>'
                     + cap('11s', '1s', '하나')
                     # a connected storyline anchored at 0 whose title starts at 7 s (under cut B)
                     + '<spine lane="2" offset="10s"><gap name="g" offset="0s" duration="7s"/>'
                     + '<title ref="rt" offset="7s" duration="1s" name="late"/></spine>'
                     + '<marker start="12s" duration="1/30s" value="확인"/></asset-clip>'
                     '<asset-clip ref="r2" offset="5s" start="40s" duration="5s" name="B">'
                     '<adjust-transform scale="1.25 1.25"/><adjust-volume amount="-6dB"/>'
                     + cap('41s', '1s', '둘') + '</asset-clip>')
            text, rows = timeline_brief.render(write(d, 'x.fcpxml', fcpxml(asset('r2', src, 100), spine, '10s')), lang='ko')
        self.assertIn('표시한 시간 항목', text)
        self.assertIn('(전부)', text)
        self.assertIn('본선 기본값(줄에서 생략): scale 1.25 1.25', text)
        self.assertIn('"하나"', text)              # caption text, whitespace cleaned
        self.assertNotIn('하나 /', text)
        self.assertIn('vol -6dB', text)            # deviation from the defaults is kept
        lines = text.splitlines()
        b = next(i for i, l in enumerate(lines) if l.startswith('▌00:05.00'))
        late = next(i for i, l in enumerate(lines) if '00:07.00' in l)
        self.assertGreater(late, b)                # grouped by time, not by its anchor at 0:00
        self.assertTrue(any('◆ 00:02.00 확인' in l for l in lines))
        self.assertEqual(len([r for r in rows if r['tag'] == 'title']), 3)


class CompoundTest(unittest.TestCase):
    def test_main_storyline_compound_is_opened(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / 'a.mp4'
            src.write_bytes(b'')
            res = (asset('r2', src, 100)
                   + '<media id="c1" name="BASE"><sequence format="r1" duration="8s" tcStart="0s"><spine>'
                   '<asset-clip ref="r2" offset="0s" start="10s" duration="3s" name="in1"/>'
                   '<asset-clip ref="r2" offset="3s" start="40s" duration="5s" name="in2"/>'
                   '</spine></sequence></media>')
            spine = ('<ref-clip ref="c1" offset="0s" start="1s" duration="6s" name="BASE">'
                     + cap('3.5s', '1s', '둘째 컷 자막') + '</ref-clip>')
            text, _ = timeline_brief.render(write(d, 'c.fcpxml', fcpxml(res, spine, '6s')), lang='ko')
        lines = text.splitlines()
        self.assertTrue(any(l.startswith('⊂00:00.00') and '2 컷' in l for l in lines))
        second = next(i for i, l in enumerate(lines) if l.startswith('▌00:02.00'))  # in2 starts at 3-1 = 2 s
        self.assertIn('둘째 컷 자막', lines[second + 1])                         # caption at 2.5 s under in2
        self.assertIn('(전부)', text)



class BriefEnglishTest(unittest.TestCase):
    def test_english_labels(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / 'a.mp4'
            src.write_bytes(b'')
            spine = '<asset-clip ref="r2" offset="0s" start="10s" duration="5s" name="A"/>'
            text, _ = timeline_brief.render(write(d, 'x.fcpxml', fcpxml(asset('r2', src, 100), spine, '5s')), lang='en')
        self.assertIn('timed items shown 1/1 (all)', text)


if __name__ == '__main__':
    unittest.main()
