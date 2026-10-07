import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from queue import Queue

from robot_test_hub.status_bridge import PROFILE, TOPIC, StatusBridge


def envelope(sequence, **changes):
    value = dict(schema_version=1,robot_id='synthetic-6391',runtime_mode='REAL',boot_id='synthetic-boot',
                 sequence=sequence,mode_generation=1,enabled=False,mode='disabled',operating_mode='TELEOP',
                 transfer_allowed=True,robot_monotonic_ns=str(sequence*20_000_000))
    value.update(changes)
    return json.dumps(value)


class StatusBridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.bridge = StatusBridge('127.0.0.1',5810,'synthetic-6391',clock=lambda:self.now)
        self.bridge.accept_event(dict(type='ready',protocol=1,profile=PROFILE,topic=TOPIC))
        self.bridge._probes[1] = self.now
        self.now += .02
        self.bridge.accept_event(dict(type='clock_probe',id=1,nt_local_ns='1000000000000'))
        self.bridge.accept_event(dict(type='connected',connection_epoch=1))

    def publish(self, sequence, timestamp=None, **changes):
        stamp = timestamp if timestamp is not None else 1_000_000_000_000+sequence*1_000_000
        self.bridge.accept_event(dict(type='publication',connection_epoch=self.bridge._epoch,
            nt_local_ns=str(stamp),nt_server_ns=str(stamp),payload=envelope(sequence,**changes)))

    def test_explicit_configuration_and_exact_profile_required(self):
        for host,port in (('',5810),('host',0),('host',True),('unsafe host',5810),('-option',5810)):
            with self.assertRaises(ValueError):
                StatusBridge(host,port,'synthetic-6391')
        with self.assertRaises(ValueError):
            StatusBridge('host',5810,'')
        fresh = StatusBridge('host',5810,'synthetic-6391')
        with self.assertRaises(ValueError):
            fresh.accept_event(dict(type='ready',protocol=1,profile='2026',topic=TOPIC))

    def test_advancing_pair_and_duplicate_payload_never_refresh(self):
        self.publish(1)
        self.assertIsNone(self.bridge.status().enabled)
        self.publish(2)
        self.assertFalse(self.bridge.status().enabled)
        received = self.bridge.status().observed_at
        self.publish(2,timestamp=1_000_003_000_000)
        self.assertEqual(self.bridge.status().observed_at,received)
        self.now = 1000
        self.assertEqual(self.bridge.status().observed_at,received)

    def test_delayed_stdout_uses_original_publication_time(self):
        self.now = 160
        self.publish(1)
        self.publish(2)
        self.assertFalse(self.bridge.status().enabled)
        self.assertAlmostEqual(self.bridge.status().observed_at,100.002)
        self.assertGreater(self.now-self.bridge.status().observed_at,59)

    def test_invalid_heartbeat_between_polls_requires_new_pair_and_permission_token(self):
        self.publish(1); self.publish(2)
        previous=self.bridge.status()
        self.publish(3,robot_id='wrong-robot')
        self.assertEqual(self.bridge.error_code,'invalid_robot_status')
        self.assertIsNone(self.bridge.inbox.previous)
        self.publish(4)
        self.assertIsNone(self.bridge.status().enabled)
        self.assertFalse(self.bridge.status().transfer_allowed)
        self.publish(5)
        recovered=self.bridge.status()
        self.assertFalse(recovered.enabled)
        self.assertTrue(recovered.transfer_allowed)
        self.assertEqual(previous.boot_id,recovered.boot_id)
        self.assertNotEqual(previous.generation,recovered.generation)

    def test_reconnect_newer_cached_value_needs_two_new_publications(self):
        self.publish(1); self.publish(2)
        self.bridge.accept_event(dict(type='disconnected',connection_epoch=1))
        self.bridge.accept_event(dict(type='connected',connection_epoch=2))
        self.publish(3)
        self.assertIsNone(self.bridge.status().enabled)
        self.publish(4)
        self.assertFalse(self.bridge.status().enabled)

    def test_nonadvancing_timestamp_bad_identity_and_future_fail_closed(self):
        self.publish(1); self.publish(2)
        self.publish(3,timestamp=1_000_001_000_000)
        self.assertIsNone(self.bridge.status().enabled)
        self.publish(4,robot_id='other')
        self.assertIsNone(self.bridge.status().enabled)
        self.publish(5,timestamp=1_100_000_000_000)
        self.assertIsNone(self.bridge.status().enabled)
        self.assertEqual(self.bridge.error_code,'status_publication_future_timestamp')
        with self.assertRaises(ValueError):
            self.bridge.accept_event(dict(type='unknown'))

    def test_slow_probe_and_unqualified_connection_do_not_refresh(self):
        self.publish(1); self.publish(2)
        self.bridge._probes[2] = self.now
        self.now += .8
        self.bridge.accept_event(dict(type='clock_probe',id=2,nt_local_ns='1000800000000'))
        self.assertIsNone(self.bridge.status().enabled)
        self.assertEqual(self.bridge.error_code,'status_bridge_clock_probe_delay')
        self.bridge.accept_event(dict(type='disconnected',connection_epoch=1))
        self.publish(3)
        self.assertIsNone(self.bridge.status().enabled)

    def test_child_process_exit_and_oversized_output_fail_closed(self):
        for body in ('print("invalid protocol",flush=True)', 'print("x"*140000,flush=True)'):
            bridge = StatusBridge('127.0.0.1',5810,'synthetic-6391',
                                  command=[sys.executable,'-u','-c',body])
            try:
                bridge.start()
                deadline = time.monotonic()+2
                while (bridge.process is None or bridge.process.poll() is None) and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertIsNone(bridge.status().enabled)
                self.assertFalse(bridge.status().transfer_allowed)
                self.assertIsNotNone(bridge.process.poll())
            finally:
                bridge.close()


# This child only implements the JSON bridge protocol over pipes. It does not
# connect to NetworkTables or supply a simulated production heartbeat.
CONTROLLED_CHILD='''
import json,sys,time
def emit(value):
    print(json.dumps(value),flush=True)
emit(dict(type='ready',protocol=1,profile='wpilib-2027.0.0-alpha-7-windowsx86-64',topic='/Telemetry/TestHub/Status'))
emit(dict(type='connected',connection_epoch=1))
sequence=0
for line in sys.stdin:
    request=json.loads(line)
    stamp=time.monotonic_ns()
    emit(dict(type='clock_probe',id=request['id'],nt_local_ns=str(stamp)))
    for _ in range(2):
        sequence+=1
        stamp+=1
        payload=dict(schema_version=1,robot_id='synthetic-6391',runtime_mode='REAL',boot_id='synthetic-boot',
            sequence=sequence,mode_generation=1,enabled=False,mode='disabled',operating_mode='TELEOP',
            transfer_allowed=True,robot_monotonic_ns=str(sequence*20000000))
        emit(dict(type='publication',connection_epoch=1,nt_local_ns=str(stamp),nt_server_ns=str(stamp),payload=json.dumps(payload)))
'''


class StatusBridgeSupervisionTests(unittest.TestCase):
    def wait_for(self,predicate,limit=3):
        deadline=time.monotonic()+limit
        while time.monotonic()<deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Controlled status-child condition did not complete')

    def bridge(self,body=CONTROLLED_CHILD,**kwargs):
        bridge=StatusBridge('127.0.0.1',5810,'synthetic-6391',
            command=[sys.executable,'-u','-c',body],restart_initial=.02,restart_max=.08,**kwargs)
        self.addCleanup(bridge.close)
        return bridge

    def test_child_death_restarts_without_permission_generation_collision(self):
        bridge=self.bridge()
        bridge.start()
        self.wait_for(lambda:bridge.status().enabled is False)
        old=bridge.process
        generation=bridge.status().generation
        old.kill();old.wait(timeout=1)
        self.assertIsNone(bridge.status().enabled)
        self.assertFalse(bridge.status().transfer_allowed)
        self.wait_for(lambda:bridge.process is not None and bridge.process is not old
            and bridge.status().enabled is False)
        self.assertNotEqual(bridge.status().generation,generation)
        self.assertEqual(bridge.status().boot_id,'synthetic-boot')
        self.assertTrue(old.stdin.closed)
        self.assertTrue(old.stdout.closed)
        self.assertEqual(bridge.diagnostics()['restart_count'],1)
        self.assertEqual(bridge.diagnostics()['consecutive_failures'],0)
        bridge.close()
        self.assertFalse(bridge.diagnostics()['reader_running'])
        self.assertFalse(bridge.diagnostics()['restart_pending'])
        self.assertIsNotNone(bridge.process.poll())
        with self.assertRaises(RuntimeError):
            bridge.start()

    def test_start_failure_and_repeated_protocol_failures_are_bounded_and_redacted(self):
        bridge=self.bridge('print("invalid framing",flush=True)')
        original=subprocess.Popen
        children=[]
        calls=[]
        def launch(*args,**kwargs):
            calls.append(time.monotonic())
            if len(calls)==1:
                raise OSError('Secret endpoint/credential must not appear in diagnostics')
            if children:
                self.assertIsNotNone(children[-1].poll())
                self.assertTrue(children[-1].stdout.closed)
            child=original(*args,**kwargs);children.append(child);return child
        with patch('robot_test_hub.status_bridge.subprocess.Popen',side_effect=launch), \
                patch.object(bridge._stop,'wait',wraps=bridge._stop.wait) as waited:
            bridge.start()
            self.wait_for(lambda:bridge.diagnostics()['consecutive_failures']>=3)
            health=bridge.diagnostics()
            self.assertTrue(health['restart_pending'])
            self.assertLessEqual(health['retry_in_seconds'],.08)
            self.assertEqual(health['error_code'],'status_bridge_protocol_invalid')
            self.assertNotIn('Secret',json.dumps(health))
            self.assertIsNone(bridge.status().enabled)
            self.assertTrue(any(call.args==(.02,) for call in waited.call_args_list))
            self.assertTrue(any(call.args==(.04,) for call in waited.call_args_list))
            bridge.close()
        self.assertTrue(all(child.poll() is not None and child.stdout.closed for child in children))

    def test_probe_stall_reaps_before_replacement_and_shutdown_interrupts_backoff(self):
        body='import time; time.sleep(30)'
        bridge=self.bridge(body,probe_timeout=.12)
        bridge.restart_initial=bridge.restart_max=5.
        bridge.start()
        self.wait_for(lambda:bridge.diagnostics()['restart_pending'])
        old=bridge.process
        self.assertEqual(bridge.diagnostics()['error_code'],'status_bridge_clock_probe_timeout')
        self.assertIsNotNone(old.poll())
        self.assertTrue(old.stdout.closed)
        started=time.monotonic()
        bridge.close()
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(bridge.diagnostics()['restart_count'],0)
        self.assertFalse(bridge.status().transfer_allowed)

    def test_protocol_reset_requires_fresh_advancing_pair_and_new_clock(self):
        bridge=self.bridge()
        bridge.accept_event(dict(type='ready',protocol=1,profile=PROFILE,topic=TOPIC))
        bridge._probes[1]=time.monotonic()
        bridge.accept_event(dict(type='clock_probe',id=1,nt_local_ns=str(time.monotonic_ns())))
        bridge.accept_event(dict(type='connected',connection_epoch=1))
        for sequence in (1,2):
            stamp=time.monotonic_ns()
            bridge.accept_event(dict(type='publication',connection_epoch=1,
                nt_local_ns=str(stamp),nt_server_ns=str(stamp),payload=envelope(sequence)))
        old_generation=bridge.status().generation
        with bridge._guard:
            bridge._reset_protocol()
        self.assertFalse(bridge._ready)
        self.assertFalse(bridge._connected)
        self.assertEqual(bridge._epoch,0)
        self.assertIsNone(bridge._offset_ns)
        self.assertEqual(bridge._probes,{})
        self.assertIsNone(bridge.inbox.previous)
        self.assertNotEqual(bridge.status().generation,old_generation)
        bridge.accept_event(dict(type='ready',protocol=1,profile=PROFILE,topic=TOPIC))
        bridge.accept_event(dict(type='connected',connection_epoch=1))
        for sequence in (1,2):
            stamp=time.monotonic_ns()
            bridge.accept_event(dict(type='publication',connection_epoch=1,
                nt_local_ns=str(stamp),nt_server_ns=str(stamp),payload=envelope(sequence)))
        self.assertIsNone(bridge.status().enabled)
        self.assertEqual(bridge.error_code,'awaiting_status_clock_probe')
        bridge._probes[1]=time.monotonic()
        bridge.accept_event(dict(type='clock_probe',id=1,nt_local_ns=str(time.monotonic_ns())))
        for sequence in (1,2):
            stamp=time.monotonic_ns()
            bridge.accept_event(dict(type='publication',connection_epoch=1,
                nt_local_ns=str(stamp),nt_server_ns=str(stamp),payload=envelope(sequence)))
            if sequence==1:
                self.assertIsNone(bridge.status().enabled)
            else:
                self.assertFalse(bridge.status().enabled)
        self.assertNotEqual(bridge.status().generation,old_generation)

    def test_backoff_and_supervision_configuration(self):
        bridge=self.bridge()
        for failures,expected in ((1,.02),(2,.04),(3,.08),(100,.08)):
            bridge.consecutive_failures=failures
            self.assertEqual(bridge._retry_delay(),expected)
        for changes in ({'restart_initial':0},{'restart_max':float('inf')},
                        {'restart_initial':2,'restart_max':1},{'probe_timeout':True}):
            with self.assertRaises(ValueError):
                StatusBridge('localhost',5810,'synthetic-6391',**changes)

    def test_close_retries_transient_cleanup_failure_without_abandoning_pipes(self):
        bridge=self.bridge()
        bridge.start()
        self.wait_for(lambda:bridge.status().enabled is False)
        child=bridge.process
        original=bridge._reap
        attempts=[]
        def transient(process):
            attempts.append(process)
            if len(attempts)==1:
                raise OSError('Transient cleanup failure with private endpoint text')
            return original(process)
        with patch.object(bridge,'_reap',side_effect=transient):
            bridge.close()
        self.assertGreaterEqual(len(attempts),2)
        self.assertTrue(all(process is child for process in attempts))
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertFalse(bridge.diagnostics()['reader_running'])
        self.assertFalse(bridge.diagnostics()['restart_pending'])
        self.assertEqual(bridge.diagnostics()['restart_count'],0)
        self.assertFalse(bridge.status().transfer_allowed)


@unittest.skipUnless(os.environ.get('ROBOT_HUB_STATUS_INTEGRATION')=='1',
                     'Explicit loopback native qualification opt-in required')
class StatusBridgeNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.status_bridge.run import prepare
        cls.command,cls.env = prepare()

    def wait_for(self,predicate,limit=6):
        deadline = time.monotonic()+limit
        while time.monotonic()<deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('Loopback condition not reached before bounded deadline')

    def test_actual_alpha7_retained_progress_stall_disconnect_and_process_death(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0))
            port = reservation.getsockname()[1]
        publisher = subprocess.Popen(self.command+['SyntheticPublisher',str(port)],env=self.env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        bridge = StatusBridge('127.0.0.1',port,'synthetic-6391',command=self.command,env=self.env)
        def send(kind, **fields):
            publisher.stdin.write(json.dumps(dict(type=kind,**fields)).encode()+b'\n')
            publisher.stdin.flush()
        try:
            ready = Queue()
            threading.Thread(target=lambda:ready.put(publisher.stdout.readline()),daemon=True).start()
            self.assertEqual(json.loads(ready.get(timeout=3)),{'type':'publisher_ready'})
            send('publish',payload=envelope(100))  # Retained value exists before subscriber startup.
            bridge.start()
            self.wait_for(lambda:bridge.inbox.previous is not None)
            self.assertIsNone(bridge.status().enabled)
            send('publish',payload=envelope(101))
            self.wait_for(lambda:bridge.status().enabled is False)
            original = bridge.status().observed_at
            self.assertLessEqual(original,time.monotonic())
            self.assertLess(time.monotonic()-original,.75)  # Correct ns/seconds conversion and useful local age.
            time.sleep(.35)  # Publisher is reachable but its robot heartbeat is stalled.
            self.assertEqual(bridge.status().observed_at,original)
            self.assertGreater(time.monotonic()-original,.3)
            send('publish',payload=envelope(101))
            time.sleep(.15)
            self.assertEqual(bridge.status().observed_at,original)
            send('disconnect')
            self.wait_for(lambda:not bridge._connected)
            self.assertIsNone(bridge.status().enabled)
            send('reconnect')
            self.wait_for(lambda:bridge._connected)
            self.assertIsNone(bridge.status().enabled)
            # Whether NT resends its retained value or not, one explicit newer
            # value cannot establish progress if no retained value was received.
            before = bridge.inbox.previous
            send('publish',payload=envelope(102))
            self.wait_for(lambda:bridge.inbox.previous and bridge.inbox.previous[1]==102)
            if before is None:
                self.assertIsNone(bridge.status().enabled)
            send('publish',payload=envelope(103))
            self.wait_for(lambda:bridge.status().enabled is False)
            old_child=bridge.process
            old_child.terminate()
            self.wait_for(lambda:old_child.poll() is not None)
            self.assertIsNone(bridge.status().enabled)
            self.assertFalse(bridge.status().transfer_allowed)
        finally:
            bridge.close()
            if publisher.poll() is None:
                publisher.terminate()
            publisher.wait(timeout=3)
            for stream in (publisher.stdin,publisher.stdout,publisher.stderr):
                stream.close()
