"""Independent HTTP and check fixtures; no model or official grading claims."""
import hashlib,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Demo/CoreGeek/src'))
from agent import task_tools
from agent.task_context import COMMAND_LIMIT

class TaskToolsTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                part=urlsplit(self.path); params=parse_qs(part.query)
                outer.requests.append({'path':part.path,'params':params,'auth':self.headers.get('Authorization')})
                status=200
                if part.path=='/query':
                    if self.headers.get('Authorization')!='Bearer fixture-key':
                        status=401;data={'message':"Missing Authorization header. Expected Authorization: Bearer <api_key>"}
                    elif 'location' not in params:
                        status=400;data={'message':'Missing required parameter: location'}
                    else:
                        data={'total':2,'items':{'one':{'name':'a'},'two':{'name':'b'}}}
                elif part.path=='/unknown':
                    status=401;data={'message':'denied'}
                elif part.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/leak');self.end_headers();return
                elif part.path=='/large':
                    data='x'*12000
                elif part.path=='/empty':
                    data={'total':0,'count':0}
                else:
                    data=[{'value':5}]
                body=json.dumps(data,ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close(),thread.join(timeout=2)))
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def query(self,path='/query',headers=None,params=None):
        config={'url':self.base+path,'headers':headers or {},'params':params or {}}
        command=task_tools.command('http',config)
        self.assertLessEqual(len(command),COMMAND_LIMIT)
        self.assertEqual(task_tools.http_request(command),config)
        proc=subprocess.run([sys.executable,'-c',task_tools.HTTP_SOURCE,json.dumps(config)],
            capture_output=True,text=True,encoding='utf-8',timeout=8)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return json.loads(proc.stdout)

    def test_error_driven_auth_and_parameter_recovery_encodes_unicode(self):
        result=self.query(headers={'X-API-Key':'fixture-key'},params={'city':'北京','page':'1'})
        self.assertEqual([r['status'] for r in result['attempts']],[401,400,200])
        self.assertEqual(self.requests[-1]['params']['location'],['北京'])
        self.assertNotIn('city',self.requests[-1]['params'])
        self.assertEqual(result['shape']['type'],'dict')
        self.assertEqual(result['data']['items']['two']['name'],'b')
        self.assertEqual(result['profile']['aliases'],{'city':'location'})
        self.assertEqual(result['profile']['auth'],'bearer')
        self.assertNotIn('fixture-key',json.dumps(result))
        self.assertNotIn('北京',json.dumps(result['profile'],ensure_ascii=False))

    def test_list_and_empty_data_are_preserved_without_guessing_answer(self):
        result=self.query('/list')
        self.assertEqual(result['shape']['type'],'list')
        self.assertEqual(result['data'],[{'value':5}])
        empty=self.query('/empty')
        self.assertEqual(empty['data'],{'total':0,'count':0})
        self.assertNotIn('answer',empty)

    def test_no_auth_hint_no_guess_and_multiple_business_params_no_rename(self):
        self.assertEqual(len(self.query('/unknown',headers={'X-API-Key':'fixture-key'})['attempts']),1)
        result=self.query(headers={'Authorization':'Bearer fixture-key'},params={'city':'北京','district':'a'})
        self.assertEqual(result['status'],400)
        self.assertEqual(len(result['attempts']),1)

    def test_redirect_does_not_leak_headers_and_truncation_prevents_profile(self):
        result=self.query('/redirect',headers={'Authorization':'Bearer fixture-key'})
        self.assertEqual(result['status'],302)
        self.assertEqual(len(self.requests),1)
        large=self.query('/large')
        self.assertTrue(large['truncated'])
        self.assertIsNone(large['profile'])

    def test_existing_query_unicode_is_encoded_and_profile_has_no_query(self):
        result=self.query('/list?city=南京')
        self.assertEqual(self.requests[-1]['params'],{'city':['南京']})
        self.assertNotIn('?',result['profile']['endpoint'])

    def test_invalid_arguments_and_unrecognized_command_rejected(self):
        for args in ({'url':'file:///tmp/x'},{'url':self.base,'headers':{'X-API-Key':'a\nb'}},
                     {'url':self.base,'params':{'x':5}},{'url':self.base,'password':'x'}):
            with self.assertRaises(ValueError): task_tools.command('http',args)
        self.assertIsNone(task_tools.http_request('# task-http/1\necho fake'))
        with self.assertRaises(ValueError):task_tools.clean_profile({'endpoint':self.base,'auth':'bearer','aliases':{},'api_key':'x'})

    @unittest.skipUnless(os.name=='posix','Linux check execution')
    def test_crlf_check_preserves_source_and_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'check'
            raw=b'#!/bin/sh\r\nprintf "TOKEN: fixture-token\\n"\r\n'
            path.write_bytes(raw)
            before=hashlib.sha256(raw).hexdigest()
            config={'path':str(path)}
            self.assertLessEqual(len(task_tools.command('check',config)),COMMAND_LIMIT)
            proc=subprocess.run([sys.executable,'-c',task_tools.CHECK_SOURCE,json.dumps(config)],
                capture_output=True,text=True,encoding='utf-8',timeout=8)
            self.assertEqual(proc.returncode,0,proc.stderr)
            result=json.loads(proc.stdout)
            self.assertTrue(result['crlf_normalized'])
            self.assertEqual(result['stdout'],'TOKEN: fixture-token\n')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            path.write_bytes(b'#!/bin/sh\r\nexit 7\r\n')
            failed=subprocess.run([sys.executable,'-c',task_tools.CHECK_SOURCE,json.dumps(config)],
                capture_output=True,text=True,encoding='utf-8',timeout=8)
            self.assertEqual(failed.returncode,7)
            self.assertEqual(json.loads(failed.stdout)['exit_code'],7)

    @unittest.skipUnless(os.name=='posix','rendered POSIX shell command')
    def test_rendered_shell_http_command_keeps_unicode_parameters(self):
        cmd=task_tools.command('http',{'url':self.base+'/query','headers':{'X-API-Key':'fixture-key'},
                                      'params':{'city':'南京'}})
        proc=subprocess.run(['sh','-c',cmd],cwd='/',capture_output=True,text=True,encoding='utf-8',timeout=8)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'],200)
        self.assertEqual(self.requests[-1]['params']['location'],['南京'])

    @unittest.skipUnless(os.name=='posix','Linux process-group timeout')
    def test_check_timeout_is_bounded_and_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'check'
            path.write_text('#!/bin/sh\nsleep 30\n')
            proc=subprocess.run(['sh','-c',task_tools.command('check',{'path':str(path)})],
                capture_output=True,text=True,encoding='utf-8',timeout=9)
            self.assertEqual(proc.returncode,124)
            self.assertEqual(json.loads(proc.stdout)['exit_code'],124)
