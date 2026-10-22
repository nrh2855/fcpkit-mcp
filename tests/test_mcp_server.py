"""The read-only MCP wrapper: protocol handshake, tool listing, tool calls on synthetic media."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fcpkit import mcp_server as M  # noqa: E402
from fixtures import asset, fcpxml, make_video, write  # noqa: E402

JUMP = ('<asset-clip ref="r2" offset="0s" start="1s" duration="3s" name="A"/>'
        '<transition name="Cross Dissolve" offset="2s" duration="2s"><filter-video ref="rx" name="Cross Dissolve"/>'
        '</transition>'
        '<asset-clip ref="r2" offset="3s" start="6s" duration="3s" name="B"/>')


def call(name, **arguments):
    reply = M.handle({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                      'params': {'name': name, 'arguments': arguments}})
    return reply['result']


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('FCPKIT_TEST_TMP'))
        cls.dir = Path(cls.tmp.name)
        cls.video = make_video(cls.dir / 'a.mp4', 10)
        cls.xml = write(cls.dir, 'jump.fcpxml', fcpxml(asset('r2', cls.video), JUMP, '6s'))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_initialize_and_list(self):
        init = M.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                         'params': {'protocolVersion': '2025-03-26', 'capabilities': {}}})
        self.assertEqual(init['result']['protocolVersion'], '2025-03-26')
        self.assertIn('tools', init['result']['capabilities'])
        self.assertIsNone(M.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        tools = M.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
        names = {t['name'] for t in tools}
        self.assertEqual(names, {'fcpkit_brief', 'fcpkit_brief_apply', 'fcpkit_handles', 'fcpkit_effects_check', 'fcpkit_timeline_diff',
                                 'fcpkit_relink_preview', 'fcpkit_filmstrip', 'fcpkit_libraries'})
        # filmstrip writes PNGs into a new folder; every other tool is read-only
        self.assertTrue(all(t['annotations']['readOnlyHint'] != (t['name'] in ('fcpkit_filmstrip', 'fcpkit_brief_apply')) for t in tools))
        self.assertFalse(any(w in n for n in names for w in ('import', 'snap', 'write', 'ledger')))  # no FCP import/snap/ledger over MCP

    def test_handles_finds_jump_cut_dissolve(self):
        res = call('fcpkit_handles', xml=str(self.xml))
        self.assertFalse(res['isError'], res)
        report = json.loads(res['content'][0]['text'])
        self.assertEqual(report['rows'][0]['kind'], 'jump')
        self.assertEqual(report['exit_status'], 'FAIL')

    def test_effects_and_diff_and_relink(self):
        self.assertFalse(call('fcpkit_effects_check', xml=str(self.xml))['isError'])
        diff = json.loads(call('fcpkit_timeline_diff', a=str(self.xml), b=str(self.xml))['content'][0]['text'])
        self.assertEqual(diff['changes'], [])
        rel = json.loads(call('fcpkit_relink_preview', xml=str(self.xml))['content'][0]['text'])
        self.assertEqual(rel['exit_status'], 'PASS')

    def test_filmstrip_returns_image_and_never_overwrites(self):
        out = self.dir / 'strips'
        res = call('fcpkit_filmstrip', xml=str(self.xml), at=[3], frames=4, out_dir=str(out))
        self.assertFalse(res['isError'], res)
        images = [c for c in res['content'] if c['type'] == 'image']
        self.assertEqual(len(images), 1)
        self.assertTrue(base64.b64decode(images[0]['data']).startswith(b'\x89PNG'))
        again = call('fcpkit_filmstrip', xml=str(self.xml), at=[3], out_dir=str(out))
        self.assertTrue(again['isError'])
        self.assertIn('exists', again['content'][0]['text'])

    def test_bad_calls(self):
        reply = M.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                          'params': {'name': 'fcpkit_import', 'arguments': {}}})
        self.assertEqual(reply['error']['code'], -32602)
        self.assertTrue(call('fcpkit_handles')['isError'])                       # missing xml
        self.assertTrue(call('fcpkit_handles', xml=str(self.xml), out='x')['isError'])  # unknown argument
        self.assertTrue(call('fcpkit_handles', xml=str(self.dir / 'nope.fcpxml'))['isError'])
        self.assertEqual(M.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list'})['error']['code'], -32601)

    def test_stdio_round_trip(self):
        lines = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                 {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                 {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                  'params': {'name': 'fcpkit_handles', 'arguments': {'xml': str(self.xml)}}}]
        proc = subprocess.run([sys.executable, '-m', 'fcpkit.mcp_server'], cwd=ROOT, capture_output=True, text=True,
                              input=''.join(json.dumps(m) + '\n' for m in lines) + 'not json\n', timeout=60)
        replies = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual([r.get('id') for r in replies], [1, 2, None])  # stdout carries protocol only
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'fcpkit')
        self.assertFalse(replies[1]['result']['isError'])
        self.assertEqual(replies[2]['error']['code'], -32700)


if __name__ == '__main__':
    unittest.main()
