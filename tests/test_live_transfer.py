import http.client
import json
import os
from pathlib import Path
from queue import Queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from robot_test_hub.config import Config
from robot_test_hub.live import LiveSource, configure
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.sftp_source import SFTPConfig
from robot_test_hub.status_bridge import StatusBridge
from test_sftp_network import LoopbackSFTP, paramiko
from test_sftp_source import manifest_files, segment
from test_status_bridge import envelope


class LiveConfigurationTests(unittest.TestCase):
    def test_missing_explicit_nt_settings_fail_before_source_access(self):
        with self.assertRaises(ValueError):configure(Config(),'missing.json',None,None)

    @unittest.skipIf(paramiko is None,'Pinned optional sftp extra required')
    def test_live_defaults_and_separate_credential_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'known').write_text('synthetic');(root/'key').write_text('synthetic')
            path=root/'source.json';path.write_text(json.dumps(dict(schema_version=1,host='configured-host',username='reader',
                known_hosts='known',private_key='key',log_root='/logs',robot_id='robot-6391')))
            config,source=configure(Config(),path,'explicit-status-host',5810)
            self.assertEqual(config.idle_delay,10);self.assertEqual(config.freshness,.5)
            self.assertNotIn('private_key',config.as_dict())
            self.assertEqual(source.status_provider.inbox.runtime_mode,'REAL')
            self.assertIsNone(source.status().enabled)
            config,_=configure(Config(idle_delay=15),path,'explicit-status-host',5810,idle_delay_explicit=True)
            self.assertEqual(config.idle_delay,15)
            with self.assertRaises(ValueError):configure(Config(chunk_size=524288),path,'explicit-status-host',5810)

    def test_transient_transport_cancel_does_not_stop_status_reader(self):
        class Reader:
            closed=False
            def close(self):self.closed=True
        reader=Reader()
        source=LiveSource(SFTPConfig('host','reader','known','key','/logs','robot'),reader)
        source._disconnect_transport()
        self.assertFalse(reader.closed)
        source.close();self.assertTrue(reader.closed)

    def test_connection_diagnostics_expose_recovery_without_source_secrets(self):
        class Reader:
            error_code='status_bridge_process_exited'
            def diagnostics(self):
                return dict(restart_count=2,consecutive_failures=2,restart_pending=True,
                    retry_in_seconds=3.,reader_running=False,error_code=self.error_code)
        source=LiveSource(SFTPConfig('private-host','private-user','private-known-hosts',
            'private-key','/private-logs','robot'),Reader())
        snapshot=source.connection_status()
        self.assertFalse(snapshot['hardware_qualified'])
        self.assertFalse(snapshot['ssh_connected'])
        self.assertTrue(snapshot['status_reader']['restart_pending'])
        self.assertEqual(snapshot['status_reader']['retry_in_seconds'],3.)
        self.assertNotIn('private-',json.dumps(snapshot))


@unittest.skipUnless(os.environ.get('ROBOT_HUB_STATUS_INTEGRATION')=='1' and paramiko is not None,
                     'Explicit Alpha7 + SFTP loopback qualification opt-in required')
class LiveNativeTests(unittest.TestCase):
    def wait_for(self,predicate,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('Integrated live transfer condition timed out')

    def test_real_nt_status_and_sftp_service_pause_resume_import_and_http_source_separation(self):
        from tools.status_bridge.run import prepare
        command,env=prepare()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data=(Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes()
            entry=segment('synthetic-live',data=data,robot_id='synthetic-6391')
            files=manifest_files([entry],robot_id='synthetic-6391',boot_id='synthetic-boot');files[entry['relative_path']]=data
            ssh=LoopbackSFTP(root,files)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            publisher=subprocess.Popen(command+['SyntheticPublisher',str(port)],env=env,stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            status=StatusBridge('127.0.0.1',port,'synthetic-6391',command=command,env=env)
            source=LiveSource(SFTPConfig('127.0.0.1','reader',str(ssh.known_hosts),str(ssh.key_path),'/logs',
                'synthetic-6391',port=ssh.port,timeout=.5),status)
            service=HubService(Config(data_dir=str(root/'hub'),idle_delay=.1,freshness=.5,chunk_size=512),source)
            httpd=create_http_server(service,source,0)
            thread=threading.Thread(target=httpd.serve_forever,kwargs={'poll_interval':.01});thread.start()
            state={'enabled':True,'generation':1};lock=threading.Lock();stop=threading.Event()
            def publish():
                sequence=100
                while not stop.is_set():
                    with lock:
                        value=envelope(sequence,enabled=state['enabled'],transfer_allowed=not state['enabled'],
                            mode='teleop' if state['enabled'] else 'disabled',mode_generation=state['generation'])
                    publisher.stdin.write(json.dumps(dict(type='publish',payload=value)).encode()+b'\n');publisher.stdin.flush()
                    sequence+=1;stop.wait(.04)
            pump=None
            try:
                ready=Queue();threading.Thread(target=lambda:ready.put(publisher.stdout.readline()),daemon=True).start()
                self.assertEqual(json.loads(ready.get(timeout=3)),{'type':'publisher_ready'})
                source.start();service.start();pump=threading.Thread(target=publish);pump.start()
                self.wait_for(lambda:source.status().enabled is True)
                self.assertEqual(ssh.requests,[])
                with lock:state.update(enabled=False,generation=2)
                self.wait_for(lambda:service.snapshot().get('completed_files')==1)
                self.wait_for(lambda:service.settings_db.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0]==1)
                self.assertEqual((root/'hub/archive/synthetic-live.logdata').read_bytes(),data)
                connection=http.client.HTTPConnection('127.0.0.1',httpd.server_address[1],timeout=2)
                connection.request('GET','/api/v1/status');response=connection.getresponse();snapshot=json.loads(response.read())
                self.assertEqual(snapshot['source_type'],'systemcore_sftp_unqualified');self.assertIsNone(snapshot['demo'])
                connection.request('POST','/api/control',json.dumps(dict(action='enabled',value=True)),
                    {'Content-Type':'application/json','X-Hub-Request':'1'})
                response=connection.getresponse();self.assertEqual(response.status,404);response.read();connection.close()
                stop.set();pump.join(2)
                self.wait_for(lambda:not service.snapshot()['source_status']['fresh'])
                reads=len(ssh.requests);time.sleep(.1);self.assertEqual(len(ssh.requests),reads)
            finally:
                stop.set()
                if pump is not None:pump.join(2)
                httpd.shutdown();thread.join(2);httpd.server_close();service.close();source.close()
                if publisher.poll() is None:publisher.terminate()
                publisher.wait(timeout=3)
                for stream in (publisher.stdin,publisher.stdout):stream.close()
                ssh.close()

    def test_killed_status_child_cancels_inflight_chunk_then_reproves_idle_before_resume(self):
        from tools.status_bridge.run import prepare
        command,env=prepare()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data=(Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes()
            entry=segment('synthetic-recovery',data=data,robot_id='synthetic-6391')
            files=manifest_files([entry],robot_id='synthetic-6391',boot_id='synthetic-boot')
            files[entry['relative_path']]=data
            ssh=LoopbackSFTP(root,files)
            # Commit one chunk, then deterministically delay the next actual
            # SFTP request. This seam observes transport work, not fake bytes.
            delay=threading.Event();delay.set()
            request_times=[]
            original_open=ssh.files_type.open
            def delayed_open(receiver,path,flags,attributes):
                handle=original_open(receiver,path,flags,attributes)
                if not isinstance(handle,paramiko.SFTPHandle):return handle
                original_read=handle.read
                def read(offset,length):
                    if path.endswith('.wpilog'):
                        request_times.append((offset,time.monotonic()))
                        if offset>=512 and delay.is_set():ssh.block=True
                    return original_read(offset,length)
                handle.read=read
                return handle
            ssh.files_type.open=delayed_open
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            publisher=subprocess.Popen(command+['SyntheticPublisher',str(port)],env=env,stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            status=StatusBridge('127.0.0.1',port,'synthetic-6391',command=command,env=env,
                                restart_initial=.8,restart_max=.8)
            source=LiveSource(SFTPConfig('127.0.0.1','reader',str(ssh.known_hosts),str(ssh.key_path),'/logs',
                'synthetic-6391',port=ssh.port,timeout=2),status)
            idle_delay=.5
            service=HubService(Config(data_dir=str(root/'hub'),idle_delay=idle_delay,freshness=.5,
                                     chunk_size=512,status_interval=.01),source)
            stop=threading.Event();publishing=threading.Event();publishing.set();pump_paused=threading.Event()
            lock=threading.Lock();last_payload=[None];sequence=[100]
            def send_next():
                with lock:
                    payload=envelope(sequence[0])
                    sequence[0]+=1
                    publisher.stdin.write(json.dumps(dict(type='publish',payload=payload)).encode()+b'\n')
                    publisher.stdin.flush();last_payload[0]=payload
            def publish():
                while not stop.is_set():
                    if publishing.is_set():
                        pump_paused.clear();send_next()
                    else:
                        pump_paused.set()
                    stop.wait(.04)
            pump=None
            try:
                ready=Queue()
                threading.Thread(target=lambda:ready.put(publisher.stdout.readline()),daemon=True).start()
                self.assertEqual(json.loads(ready.get(timeout=3)),{'type':'publisher_ready'})
                source.start();service.start();pump=threading.Thread(target=publish);pump.start()
                self.assertTrue(ssh.read_started.wait(8),'Second SFTP chunk did not enter delayed transport')
                self.assertEqual(service.settings_db.execute('SELECT offset FROM files WHERE id=?',
                                                           (entry['segment_id'],)).fetchone()[0],512)
                generation=source.status().generation
                publishing.clear()
                self.assertTrue(pump_paused.wait(1),'Publisher did not reach the bounded pause')
                # Drain a possible pump send before killing so replacement sees
                # only a retained/repeated heartbeat until explicitly advanced.
                with lock:retained=last_payload[0]
                killed=status.process
                killed.kill();killed.wait(timeout=2)
                revoked_at=time.monotonic()
                self.assertIsNone(source.status().enabled)
                self.assertFalse(source.status().transfer_allowed)
                self.wait_for(lambda:status.diagnostics()['restart_pending'],timeout=2)
                self.wait_for(lambda:source.session is None and
                              service.snapshot().get('outstanding_bytes')==0,timeout=2)
                self.assertLess(time.monotonic()-revoked_at,1,'Cancellation exceeded the local regression bound')
                self.assertFalse(service.snapshot()['source_status']['fresh'])
                self.assertEqual(service.snapshot()['workers']['collector']['state'],'running')
                self.assertEqual(service.settings_db.execute('SELECT offset FROM files WHERE id=?',
                                                           (entry['segment_id'],)).fetchone()[0],512)
                self.assertEqual((root/'hub/partial/synthetic-recovery.part').read_bytes(),data[:512])
                requests=len(ssh.requests)
                delay.clear();ssh.block=False;ssh.release.set()
                self.wait_for(lambda:status.diagnostics()['restart_count']>=1 and
                              status.diagnostics()['reader_running'] and status.process is not killed)
                self.wait_for(lambda:status._connected and status._offset_ns is not None)
                with lock:
                    publisher.stdin.write(json.dumps(dict(type='publish',payload=retained)).encode()+b'\n')
                    publisher.stdin.flush()
                self.wait_for(lambda:status.inbox.previous is not None)
                time.sleep(.1)  # Cached/repeated sequence cannot establish liveness.
                self.assertIsNone(source.status().enabled)
                self.assertFalse(service.snapshot()['source_status']['fresh'])
                self.assertEqual(len(ssh.requests),requests,'Unknown/recovering status allowed source reads')
                send_next()
                self.wait_for(lambda:source.status().enabled is False)
                proof_at=source.status().observed_at
                self.assertNotEqual(source.status().generation,generation)
                publishing.set()
                # Full idle delay begins after replacement status establishes
                # advancing robot publications; the old disabled timer is lost.
                while time.monotonic()<proof_at+idle_delay-.02:
                    self.assertEqual(len(ssh.requests),requests,'Recovery bypassed the full idle delay')
                    time.sleep(.01)
                self.wait_for(lambda:len(request_times)>2)
                resumed=[timestamp for offset,timestamp in request_times[2:]]
                self.assertTrue(resumed)
                self.assertGreaterEqual(resumed[0]-proof_at,idle_delay-.02)
                self.wait_for(lambda:service.snapshot().get('completed_files')==1)
                self.wait_for(lambda:service.settings_db.execute(
                    "SELECT COUNT(*) FROM import_jobs WHERE state IN ('succeeded','succeeded_with_unsupported')").fetchone()[0]==1)
                self.assertEqual((root/'hub/archive/synthetic-recovery.logdata').read_bytes(),data)
                self.assertGreaterEqual(ssh.requests.count((entry['relative_path'],512,512)),2)
                self.assertEqual(service.snapshot()['workers']['collector']['state'],'running')
            finally:
                stop.set();publishing.clear();delay.clear();ssh.block=False;ssh.release.set()
                if pump is not None:pump.join(2)
                service.close();source.close()
                if publisher.poll() is None:publisher.terminate()
                publisher.wait(timeout=3)
                for stream in (publisher.stdin,publisher.stdout):stream.close()
                ssh.close()
