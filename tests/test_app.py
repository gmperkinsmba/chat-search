import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

TMP=tempfile.TemporaryDirectory();os.environ['EVIDENCE_NOTEBOOK_DATA']=TMP.name
spec=importlib.util.spec_from_file_location('evidence_app',Path(__file__).parents[1]/'server.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.seed();cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);cls.server.daemon_threads=True
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.port=cls.server.server_port
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join();TMP.cleanup()
    def request(self,method,path,obj=None,token=True,origin=None,host=None):
        c=HTTPConnection('127.0.0.1',self.port);headers={'Content-Type':'application/json'}
        if token:headers['X-Local-Action']=app.TOKEN
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        c.request(method,path,json.dumps(obj) if obj is not None else None,headers);r=c.getresponse();body=r.read();ctype=r.getheader('Content-Type','');c.close()
        return r.status,json.loads(body) if body and ctype.startswith('application/json') else body

    def test_supported_adapters_preserve_source_offsets(self):
        fixtures={
            'a.json':'{\n  "messages": [\n    {"role":"user","content":"same fact"},\n    {"role":"assistant","content":"later correction"}\n  ]\n}',
            'wrapped.json':'{"note":"messages", "metadata":{"list":[1,2]}, "messages":[ {"content":"same fact"} ]}',
            'a.jsonl':'{"role":"user","content":"same fact"}\n{"role":"assistant","content":"later correction"}\n',
            'a.csv':'role,content\nuser,"same fact"\nassistant,"later correction"\n',
            'caps.csv':'Role,Content,Conversation_ID\nuser,"same fact",thread-1\n',
            'a.txt':'same fact\nlater correction\n'}
        for name,raw in fixtures.items():
            with self.subTest(name=name):
                _,records=app.parse_file(name,raw)
                self.assertEqual(records[0]['content'],'same fact')
                self.assertTrue(0<=records[0]['start_offset']<records[0]['end_offset']<=len(raw))
                if name=='caps.csv':self.assertEqual(records[0]['conversation'],'thread-1')
                if name=='a.json':self.assertIn('same fact',raw[records[0]['start_offset']:records[0]['end_offset']])
                else:self.assertIn('same fact',raw[records[0]['start_offset']:records[0]['end_offset']])

    def test_redaction_precedes_commit_and_offsets_stay_original(self):
        raw='{"role":"user","content":"private finding"}\n'
        fmt,rows=app.parse_file('notes.jsonl',raw,['content'])
        self.assertEqual(rows[0]['content'],'[REDACTED]')
        self.assertEqual(raw[rows[0]['start_offset']:rows[0]['end_offset']],raw.strip())

    def test_preview_commit_dedupe_search_and_source_deletion(self):
        raw='{"conversation_id":"thread-a","role":"user","timestamp":"2026-10-10","content":"The launch uses the blue label."}\n'
        payload={'name':'facts.jsonl','text':raw,'redact':[]}
        status,preview=self.request('POST','/api/preview',payload);self.assertEqual(status,200)
        self.assertEqual(preview['records'][0]['content'],'The launch uses the blue label.')
        payload['preview_id']=preview['preview_id']
        status,created=self.request('POST','/api/import',payload);self.assertEqual(status,201)
        self.assertEqual(created['records'],1)
        _,preview_again=self.request('POST','/api/preview',{'name':'facts.jsonl','text':raw,'redact':[]})
        payload['preview_id']=preview_again['preview_id']
        status,duplicate=self.request('POST','/api/import',payload);self.assertEqual(status,200);self.assertTrue(duplicate['duplicate'])
        matches=self.request('GET','/api/search?q=blue')[1]['records'];target=next(r for r in matches if r['source_name']=='facts.jsonl')
        original=self.request('GET','/api/records/'+target['id'])[1]
        self.assertEqual(raw[original['record']['start_offset']:original['record']['end_offset']],raw.strip())
        self.assertEqual(self.request('DELETE','/api/sources/'+original['source']['id'])[0],204)
        self.assertFalse(any(r['source_name']=='facts.jsonl' for r in self.request('GET','/api/search?q=blue')[1]['records']))

    def test_unsupported_or_malformed_imports_fail_without_partial_rows(self):
        before=self.request('GET','/api/sources')[1]['count']
        for name,raw in [('backup.zip','not a zip'),('bad.jsonl','{"content":"ok"}\nnot json\n'),('bad.csv','speaker,time\nA,now\n')]:
            status,result=self.request('POST','/api/preview',{'name':name,'text':raw,'redact':[]})
            self.assertEqual(status,400,name);self.assertIn('error',result)
        self.assertEqual(self.request('GET','/api/sources')[1]['count'],before)

    def test_commit_requires_matching_preview_and_json_types(self):
        payload={'name':'notes.txt','text':'one line\n','redact':[]}
        _,preview=self.request('POST','/api/preview',payload);payload['preview_id']=preview['preview_id'];payload['text']='changed'
        self.assertEqual(self.request('POST','/api/import',payload)[0],400)
        self.assertEqual(self.request('POST','/api/preview',{'name':5,'text':'x','redact':[]})[0],400)

    def test_redaction_snapshot_cannot_change_after_preview(self):
        raw='{"role":"user","content":"private words"}\n'
        preview_payload={'name':'redacted.jsonl','text':raw,'redact':['content']}
        _,preview=self.request('POST','/api/preview',preview_payload)
        tampered=preview_payload|{'preview_id':preview['preview_id'],'redact':[]}
        self.assertEqual(self.request('POST','/api/import',tampered)[0],400)
        _,preview=self.request('POST','/api/preview',preview_payload)
        saved=preview_payload|{'preview_id':preview['preview_id']}
        self.assertEqual(self.request('POST','/api/import',saved)[0],201)
        results=self.request('GET','/api/sources')[1]['records']
        item=next(r for r in results if r['source_name']=='redacted.jsonl')
        self.assertEqual(item['content'],'[REDACTED]')

    def test_conversation_ids_stay_strings_and_results_are_keyboard_buttons(self):
        with self.assertRaisesRegex(ValueError,'IDs must be strings'):
            app.parse_file('ids.jsonl','{"conversation_id":123,"content":"fact"}\n')
        with self.assertRaisesRegex(ValueError,'Duplicate JSON key'):
            app.parse_file('duplicate.json','{"messages":[{"content":"first"}],"messages":[{"content":"second"}]}')
        self.assertIn('type="button" class="result"',app.APPJS)

    def test_request_boundaries_and_html_text_escaping(self):
        payload={'name':'a.txt','text':'<img src=x onerror=alert(1)>','redact':[]}
        self.assertEqual(self.request('POST','/api/preview',payload,token=False)[0],403)
        self.assertEqual(self.request('POST','/api/preview',payload,origin='https://outside.invalid')[0],403)
        self.assertEqual(self.request('GET','/',host='outside.invalid')[0],403)
        self.assertIn("'&lt;'",app.APPJS);self.assertIn("'&#39;'",app.APPJS)

if __name__=='__main__':unittest.main()
