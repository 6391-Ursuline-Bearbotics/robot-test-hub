import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.marker_service import MarkerConfig,MarkerWorker,load_marker_config
from robot_test_hub.marker_delivery import MarkerError,canonical
from robot_test_hub.notebook import Notebook
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import DataRootOwner,OwnershipError
from tests.test_notebook import annotation
from tests.test_marker_delivery import accepted_ack

class Status:
    def __init__(self,*a,**kw):self.value=RobotStatus(True,10.,'boot-a',1,False,2);self._threads=[]
    def start(self):return self
    def status(self):return self.value
    def close(self):return True
class Transport:
    def __init__(self,*a,**kw):self.sent=[];self.acks=[];self.closed=False;self.pending=None
    def start(self):return self
    def diagnostics(self):return {'state':'ready','error_code':None}
    def send(self,wire):self.sent.append(wire);return True
    def poll(self):return self.acks.pop(0) if self.acks else None
    def close(self):
        if self.pending is not None and not self.pending.is_set():return False
        self.closed=True;return True

class MarkerWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.service=SimpleNamespace(root=self.root,settings_lock=threading.Lock(),stop=threading.Event(),closed=False)
        self.now=10.;self.config=MarkerConfig('127.0.0.1',5810,'robot-a','SIM',ack_timeout=.1,retry_initial=.1,retry_max=.2)
        self.worker=MarkerWorker(self.service,self.config,clock=lambda:self.now)
        self.worker.status_reader=Status();self.worker.transport=Transport()
        book=Notebook(self.root/'catalog.sqlite3');book.save(annotation())
        self.request={'delivery_id':'delivery-a','event_id':'event-a','note_revision':1,'annotation_sha256':book.exact_revision('event-a',1)['sha256'],'destination_robot_id':'robot-a','destination_boot_id':'boot-a'}
        self.worker.schedule(self.request)
    def fresh(self):self.worker.status_reader.value=RobotStatus(True,self.now,'boot-a',1,False,3)
    def row(self):return self.worker.store.get('delivery-a',private=True)
    def test_enabled_and_bulk_pause_do_not_block_marker_and_commit_precedes_send(self):
        transport=self.worker.transport
        def send(wire):
            self.assertEqual(self.row()['attempts'],1);self.assertEqual(self.row()['state'],'awaiting_ack');transport.sent.append(wire);return True
        transport.send=send;self.worker.tick()
        self.assertEqual(len(transport.sent),1);self.assertEqual(self.row()['state'],'awaiting_ack')
        transport.acks.append((accepted_ack(self.row()),self.now));self.worker.tick()
        self.assertEqual(self.row()['state'],'robot_acknowledged');self.assertEqual(self.row()['receipt_robot_ns'],'1000000000')
    def test_lost_ack_retry_exact_bytes_no_new_event_time_and_restart(self):
        self.worker.tick();original=self.worker.transport.sent[0]
        self.now+=.11;self.fresh();self.worker.tick();self.assertEqual(self.row()['state'],'retry_wait')
        self.now+=.11;self.fresh();self.worker.tick();self.assertEqual(self.worker.transport.sent,[original,original])
        restarted=MarkerWorker(self.service,self.config,clock=lambda:self.now);restarted.status_reader=self.worker.status_reader;restarted.transport=Transport();restarted.tick()
        self.assertEqual(restarted.transport.sent,[original])
    def test_stale_unknown_disconnected_and_new_boot_never_retarget(self):
        self.now=12.;self.worker.tick();self.assertEqual(self.worker.transport.sent,[])
        self.worker.status_reader.value=RobotStatus(None,self.now,'boot-b',2,False,3);self.worker.tick()
        self.assertEqual(self.row()['state'],'waiting_fresh_status')
        self.worker.status_reader.value=RobotStatus(True,self.now,'boot-b',2,False,4);self.worker.tick()
        self.assertEqual(self.row()['state'],'historical_boot_ended');self.assertEqual(self.worker.transport.sent,[])
        with self.assertRaises(MarkerError):self.worker.retry('delivery-a')
    def test_wrong_or_stale_ack_cannot_acknowledge(self):
        self.worker.tick();ack=accepted_ack(self.row())
        self.worker.transport.acks=[(dict(ack,payload_sha256='1'*64),self.now),(ack,self.now-2)]
        self.worker.tick();self.worker.tick();self.assertEqual(self.row()['state'],'awaiting_ack')
    def test_failed_local_commit_produces_no_send(self):
        with patch.object(self.worker.store,'update',side_effect=OSError('private path')):
            with self.assertRaises(OSError):self.worker.tick()
        self.assertEqual(self.worker.transport.sent,[])
    def test_private_config_and_dynamic_health_redaction(self):
        self.assertTrue(self.worker.snapshot()['ready_for_delivery']);self.now+=2
        health=self.worker.snapshot();self.assertFalse(health['status_fresh']);self.assertIsNone(health['current_confirmed_boot_id'])
        self.assertNotIn('127.0.0.1',canonical(health));self.assertNotIn('Steering',canonical(health))
        for kw in ({'freshness':float('nan')},{'maximum_attempts':True},{'runtime_mode':'REPLAY'},{'nt_host':'user@private'}):
            fields=dict(nt_host='localhost',nt_port=5810,robot_id='r',runtime_mode='SIM');fields.update(kw)
            with self.assertRaises(MarkerError):MarkerConfig(**fields)

class MarkerHTTPTests(unittest.TestCase):
    def test_explicit_http_schedule_source_pin_disabled_origin_and_owner_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            source=DemoSource();release=threading.Event();transport=Transport();transport.pending=release
            status=Status();status.value=RobotStatus(True,time.monotonic(),'boot-a',1,False,2)
            service=HubService(Config(data_dir=directory,shutdown_timeout=.1),source,
                marker_config=MarkerConfig('localhost',5810,'robot-a','SIM'),marker_status_factory=lambda *a,**k:status,
                marker_transport_factory=lambda *a,**k:transport)
            server=create_http_server(service,source,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                service.start()
                def request(method,path,value=None,headers=None):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=2)
                    try:
                        body=json.dumps(value) if value is not None else None
                        conn.request(method,path,body,headers or {'Content-Type':'application/json','X-Hub-Request':'1'})
                        response=conn.getresponse();return response.status,json.loads(response.read())
                    finally:conn.close()
                self.assertEqual(request('POST','/api/v1/annotations',annotation())[0],201)
                code,pin=request('GET','/api/v1/markers/annotations/event-a?revision=1');self.assertEqual(code,200)
                job={'delivery_id':'http-delivery','event_id':'event-a','note_revision':1,'annotation_sha256':pin['annotation_sha256'],'destination_robot_id':'robot-a','destination_boot_id':'boot-a'}
                self.assertEqual(request('POST','/api/v1/markers/deliveries',job,{'Content-Type':'application/json'})[0],403)
                code,receipt=request('POST','/api/v1/markers/deliveries',job);self.assertEqual(code,200)
                self.assertEqual(receipt['delivery']['destination_boot_id'],'boot-a')
                self.assertEqual(request('POST','/api/v1/markers/deliveries',job)[1]['idempotent'],True)
                self.assertEqual(request('GET','/api/v1/markers/deliveries?limit=1')[0],200)
                self.assertEqual(request('GET','/api/v1/markers/deliveries?limit=1&limit=2')[0],400)
                self.assertEqual(request('GET','/api/v1/annotations/event-a')[1]['revisions'][0]['delivery_state'],'historical_hub_only')
                self.assertFalse(service.close())
                with self.assertRaises(OwnershipError):DataRootOwner(Path(directory))
                self.assertEqual(request('GET','/api/v1/markers')[0],200)
                self.assertEqual(request('POST','/api/v1/markers/deliveries/http-delivery/retry',{})[0],503)
            finally:
                release.set();server.shutdown();server.server_close();thread.join(2)
                deadline=time.monotonic()+5
                while not service.close() and time.monotonic()<deadline:time.sleep(.02)
                if not service.closed:raise AssertionError('Marker service failed to release cleanup ownership')
            owner=DataRootOwner(Path(directory))
            owner.close()
    def test_default_off_never_constructs_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            factory=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Transport constructed'))
            service=HubService(Config(data_dir=directory),DemoSource(),marker_transport_factory=factory)
            try:
                service.start();self.assertEqual(service.markers.snapshot()['state'],'disabled')
                with self.assertRaises(MarkerError):service.markers.schedule({})
            finally:self.assertTrue(service.close())
