"""Known-working sample entry parity and competition-only HTTP contract."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Demo/CoreGeek/src'))
sys.path.insert(0,str(ROOT/'tools'))
import build_competition
from agent import telemetry

spec=importlib.util.spec_from_file_location('agent.competition_server', ROOT/'submission/server.py')
judge=importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)


class CompetitionHTTPTests(unittest.TestCase):
    def request(self, raw, expected, **kwargs):
        http=judge.ThreadingHTTPServer(('127.0.0.1',0),judge.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(judge,'respond',return_value=expected):
                with urlopen(Request(f'http://127.0.0.1:{http.server_port}/',data=raw),timeout=5) as response:
                    self.assertEqual(response.status,200)
                    self.assertEqual(response.version,10)  # Sample server uses HTTP/1.0.
                    return json.load(response)
        finally:
            http.shutdown();http.server_close();thread.join(timeout=3)

    def test_full_official_channels_are_preserved(self):
        expected={'roleCommandMap':{},'executeCmd':'echo sample','prompt':'task prompt'}
        self.assertEqual(self.request(b'{"roundNo":1}',expected),expected)

    def test_invalid_json_keeps_sample_fallback(self):
        self.assertEqual(self.request(b'not json',{'unexpected':True}),{'roleCommandMap':{}})

    def test_telemetry_failures_do_not_change_judge_response(self):
        expected={'roleCommandMap':{}}
        for name in ('begin','submit'):
            with patch.object(telemetry,name,side_effect=RuntimeError('trace failed')):
                self.assertEqual(self.request(b'{"roundNo":1}',expected),expected)


class FlatPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name); cls.repo=cls.root/'repo';cls.repo.mkdir()
        files=['Demo/CoreGeek.tar.gz','submission/server.py','run.sh','tools/trace_tool.py',
               'docs/TRACE_LOGGING.md','docs/request.txt','docs/response.txt','docs/任务书.md','docs/接口文档.md']
        files += [str(p.relative_to(ROOT)) for p in (ROOT/'Demo/CoreGeek/src/agent').glob('*.py')]
        for name in files:
            target=cls.repo/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
        for args in (['init','-q'],['add','.'],['-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Fixture']):
            subprocess.run(['git','-C',str(cls.repo),*args],check=True,capture_output=True)
        cls.output=cls.root/'first/CoreGeek.tar.gz'
        cls.result=build_competition.build(cls.repo,'HEAD',cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_entry_and_pyproject_are_exact_original_sample_bytes(self):
        with tarfile.open(ROOT/'Demo/CoreGeek.tar.gz') as original, tarfile.open(self.output) as candidate:
            for name in ('CoreGeek/main3.py','CoreGeek/pyproject.toml'):
                self.assertEqual(candidate.extractfile(name).read(),original.extractfile(name).read())
            names=candidate.getnames()
            self.assertTrue(all(not n.startswith(('CoreGeek/Demo/','CoreGeek/web/')) for n in names))
            self.assertIn('CoreGeek/src/agent/server.py',names)
            self.assertEqual(candidate.extractfile('CoreGeek/src/agent/server.py').read(),(ROOT/'submission/server.py').read_bytes())

    def test_repeated_build_is_byte_identical(self):
        second=self.root/'second/CoreGeek.tar.gz'
        build_competition.build(self.repo,'HEAD',second)
        self.assertEqual(self.output.read_bytes(),second.read_bytes())

    def test_flat_entry_imports_no_viewer_or_simulator_and_trace_tool_works(self):
        folder=self.root/'unpacked';folder.mkdir()
        # Inputs were just produced and verified by the bounded test fixture.
        with tarfile.open(self.output) as archive:
            for member in archive.getmembers():
                target=(folder/member.name).resolve()
                self.assertTrue(target.is_relative_to(folder.resolve()))
            archive.extractall(folder,filter='data')
        root=folder/'CoreGeek'
        code='''import sys;sys.path.insert(0,"src");from agent import server
assert server.Handler.protocol_version == "HTTP/1.0"
assert not ({"agent.debug","agent.simulator","agent.recordings","agent.twomatch","agent.local_llm"} & set(sys.modules))
'''
        done=subprocess.run([sys.executable,'-c',code],cwd=root,capture_output=True,text=True,timeout=20)
        self.assertEqual(done.returncode,0,done.stderr)
        done=subprocess.run([sys.executable,'tools/trace_tool.py','--help'],cwd=root,capture_output=True,text=True,timeout=20)
        self.assertEqual(done.returncode,0,done.stderr)
