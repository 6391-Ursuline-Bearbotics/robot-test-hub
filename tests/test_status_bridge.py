import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
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
                while bridge.process.poll() is None and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertIsNone(bridge.status().enabled)
                self.assertFalse(bridge.status().transfer_allowed)
                self.assertIsNotNone(bridge.process.poll())
            finally:
                bridge.close()


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
            bridge.process.terminate()
            self.wait_for(lambda:bridge.process.poll() is not None)
            self.assertIsNone(bridge.status().enabled)
            self.assertFalse(bridge.status().transfer_allowed)
        finally:
            bridge.close()
            if publisher.poll() is None:
                publisher.terminate()
            publisher.wait(timeout=3)
            for stream in (publisher.stdin,publisher.stdout,publisher.stderr):
                stream.close()
