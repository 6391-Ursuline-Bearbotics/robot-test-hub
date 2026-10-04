import http.client
import json
import tempfile
import threading
import unittest

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.queue_api import transfer_page
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService


class QueueApiTests(unittest.TestCase):
    def test_keyset_page_ignores_progress_changes_and_newer_arrivals(self):
        rows=[{'id':str(i),'created_at':i,'offset':0} for i in range(5)]
        page=transfer_page({'files':rows},limit=2)
        self.assertEqual([r['id'] for r in page['items']],['4','3'])
        rows.append({'id':'new','created_at':6,'offset':0})
        rows[3]['offset']=99
        second=transfer_page({'files':rows},limit=2,cursor=page['next_cursor'])
        self.assertEqual([r['id'] for r in second['items']],['2','1'])
        for cursor in ('garbage','W10=','e30=','W05hTiwiYSJd'):
            with self.assertRaises(ValueError):
                transfer_page({'files':rows},cursor=cursor)

    def test_versioned_status_actions_and_demo_separation(self):
        with tempfile.TemporaryDirectory() as folder:
            source=DemoSource()
            service=HubService(Config(data_dir=folder),source)
            service.cache['files']=[{'id':'file1','name':'<img src=x onerror=alert(1)>','created_at':1,'offset':0}]
            server=create_http_server(service,source,0)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01})
            thread.start()
            def call(method,path,payload=None,headers=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=2)
                self.addCleanup(conn.close)
                conn.request(method,path,json.dumps(payload) if payload is not None else None,
                    headers or {'Content-Type':'application/json','X-Hub-Request':'1'})
                response=conn.getresponse()
                data=response.read()
                return response.status,json.loads(data)
            try:
                status,snapshot=call('GET','/api/v1/status')
                self.assertEqual(status,200)
                self.assertNotIn('files',snapshot)
                self.assertEqual(snapshot['source_type'],'synthetic_demo')
                self.assertEqual(call('GET','/api/v1/transfers?limit=1')[1]['items'][0]['name'],'<img src=x onerror=alert(1)>')
                self.assertEqual(call('GET','/api/v1/transfers?limit=999')[0],400)
                self.assertEqual(call('POST','/api/v1/collector/pause',{})[0],200)
                self.assertTrue(service.snapshot()['paused_by_operator'])
                self.assertEqual(call('POST','/api/v1/collector/resume',{})[0],200)
                self.assertEqual(service.snapshot()['state'],'paused') # unknown source stays blocked
                self.assertEqual(call('POST','/api/v1/transfers/file1/priority',{'priority':50})[0],202)
                self.assertEqual(call('POST','/api/v1/transfers/missing/retry',{})[0],400)
                self.assertEqual(call('POST','/api/v1/collector/pause',{}, {'Content-Type':'application/json','X-Hub-Request':'1','Origin':'https://attacker.invalid'})[0],403)
            finally:
                server.shutdown();thread.join(2);server.server_close();service.close()
