import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from robot_test_hub.marker_bridge import MarkerBridge,_AckProtocol,ACK_TOPIC,REQUEST_TOPIC
from robot_test_hub.marker_service import MarkerConfig
from robot_test_hub.status_bridge import PROFILE,StatusBridge

HELPER = r'''import json,sys,time
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
profile="wpilib-2027.0.0-alpha-7-windowsx86-64"
def emit(value):print(json.dumps(value),flush=True)
emit({"type":"ready","protocol":1,"profile":profile,"topic":"/Telemetry/TestHub/MarkerAck","request_topic":"/TestHub/Notebook/MarkerRequest"})
emit({"type":"connected","connection_epoch":1})
for line in sys.stdin:
    value=json.loads(line)
    if value["type"]=="clock_probe":emit({"type":"clock_probe","id":value["id"],"nt_local_ns":str(time.monotonic_ns())})
    else:
        now=time.monotonic_ns()
        emit({"type":"publication","connection_epoch":1,"nt_local_ns":str(now),"nt_server_ns":str(now),"payload":value["payload"]})
'''

def wait(predicate,timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=predicate()
        if result:return result
        time.sleep(.01)
    raise AssertionError('Controlled marker process deadline exceeded')

class MarkerBridgeTests(unittest.TestCase):
    def test_actual_controlled_child_pipe_roundtrip_and_complete_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            helper=Path(directory)/'helper.py';helper.write_text(HELPER,encoding='utf-8')
            bridge=MarkerBridge(MarkerConfig('localhost',5810,'robot','SIM'),command=[sys.executable,str(helper)])
            bridge.send_guard=lambda _:True
            try:
                bridge.start();wait(lambda:bridge.diagnostics()['state']=='ready')
                wire=json.dumps({'probe':'exact UTF8 cue '+chr(0x263a)},ensure_ascii=False)
                self.assertTrue(bridge.send(wire));received=wait(bridge.poll)
                self.assertEqual(received[0],json.loads(wire));self.assertLessEqual(received[1],time.monotonic())
            finally:
                bridge.close();wait(lambda:not bridge.thread.is_alive())
            self.assertIsNotNone(bridge.process.poll());self.assertTrue(bridge.process.stdin.closed);self.assertTrue(bridge.process.stdout.closed)
    def test_cleanup_failure_retains_native_owner_until_reap_retry_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            helper=Path(directory)/'helper.py';helper.write_text(HELPER,encoding='utf-8')
            bridge=MarkerBridge(MarkerConfig('localhost',5810,'robot','SIM'),command=[sys.executable,str(helper)])
            original=StatusBridge._reap;held=[True];calls=[]
            def reap(process):
                calls.append(process)
                if held[0]:raise OSError('private cleanup detail')
                return original(process)
            with patch.object(StatusBridge,'_reap',side_effect=reap):
                try:
                    bridge.start();wait(lambda:bridge.diagnostics()['state']=='ready')
                    self.assertFalse(bridge.close());wait(lambda:len(calls)>1)
                    self.assertTrue(bridge.thread.is_alive());self.assertEqual(bridge.diagnostics()['error_code'],'marker_bridge_cleanup_pending')
                finally:
                    held[0]=False;bridge.close();wait(lambda:not bridge.thread.is_alive())
            self.assertEqual(len({id(p) for p in calls}),1);self.assertTrue(bridge.process.stdout.closed)
    def test_writer_start_failure_reaps_child_and_started_reader_before_return(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            helper=Path(directory)/'helper.py';helper.write_text(HELPER,encoding='utf-8')
            bridge=MarkerBridge(MarkerConfig('localhost',5810,'robot','SIM'),command=[sys.executable,str(helper)])
            original=threading.Thread.start
            def start(thread):
                if thread.name=='marker-native-writer':raise RuntimeError('start failed')
                return original(thread)
            with patch.object(threading.Thread,'start',start):
                try:
                    bridge.start();wait(lambda:bridge.process is not None and bridge.process.poll() is not None)
                finally:bridge.close();wait(lambda:not bridge.thread.is_alive())
            self.assertTrue(bridge.process.stdin.closed);self.assertTrue(bridge.process.stdout.closed)
    def test_ack_clock_connection_and_advancing_publication_proof(self):
        now=[10.];protocol=_AckProtocol('localhost',5810,'r',runtime_mode='SIM',clock=lambda:now[0])
        protocol.accept_event({'type':'ready','protocol':1,'profile':PROFILE,'topic':ACK_TOPIC,'request_topic':REQUEST_TOPIC})
        protocol.accept_event({'type':'connected','connection_epoch':1})
        protocol._probes[1]=10.;protocol.accept_event({'type':'clock_probe','id':1,'nt_local_ns':'1000000000'})
        event={'type':'publication','connection_epoch':1,'nt_local_ns':'1000000000','nt_server_ns':'1000000000','payload':'{"ack":1}'}
        protocol.accept_event(event);self.assertEqual(protocol.inbox.queue.qsize(),1)
        protocol.accept_event(event);self.assertEqual(protocol.inbox.queue.qsize(),0)
        protocol.accept_event(dict(event,connection_epoch=2));self.assertEqual(protocol.inbox.queue.qsize(),0)
