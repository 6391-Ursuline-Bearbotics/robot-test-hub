"""Independent bounded native marker publisher; status transport stays read-only."""
from __future__ import annotations
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from .marker_delivery import MarkerError, strict_json, MAX_WIRE
from .status_bridge import StatusBridge, PROFILE, TOPIC, MAX_LINE

ACK_TOPIC='/Telemetry/TestHub/MarkerAck'
REQUEST_TOPIC='/TestHub/Notebook/MarkerRequest'

class _AckInbox:
    def __init__(self,protocol):self.protocol=protocol;self.error_code=None;self.queue=queue.Queue(maxsize=1)
    def disconnect(self):
        while True:
            try:self.queue.get_nowait()
            except queue.Empty:break
    def receive(self,encoded):
        value=strict_json(encoded)
        # A single outstanding job means retaining only the newest ACK is bounded.
        self.disconnect();self.queue.put_nowait((value,self.protocol._publication_at))

class _AckProtocol(StatusBridge):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.inbox=_AckInbox(self)
    def accept_event(self,event):
        if event.get('type')=='ready':
            if event.get('topic')!=ACK_TOPIC or event.get('request_topic')!=REQUEST_TOPIC:raise MarkerError('invalid_marker_bridge_profile')
            event=dict(event,topic=TOPIC);event.pop('request_topic')
        super().accept_event(event)

class MarkerBridge:
    """A worker owns this object until child AND both pipe threads are cleaned."""
    def __init__(self,config,*,clock=time.monotonic,command=None,env=None):
        self.config=config;self.clock=clock;self.command=command;self.env=env
        self.protocol=_AckProtocol(config.nt_host,config.nt_port,config.robot_id,runtime_mode=config.runtime_mode,clock=clock)
        self.stop=threading.Event();self.protocol._stop=self.stop
        self.requests=queue.Queue(maxsize=1);self.process=None;self.thread=None;self.send_guard=lambda _:False
        self._lock=threading.Lock();self._state='starting';self.error_code=None
    def start(self):
        if self.thread is not None:raise RuntimeError('Marker bridge starts once')
        self.thread=threading.Thread(target=self._run,name='marker-native-owner',daemon=True);self.thread.start();return self
    def _set(self,state,error=None):
        with self._lock:self._state=state;self.error_code=error
    def send(self,wire):
        if len(wire.encode('utf-8'))>MAX_WIRE:raise MarkerError('marker_payload_oversized')
        if self.stop.is_set():return False
        try:self.requests.put_nowait(wire);return True
        except queue.Full:return False
    def poll(self):
        with self.protocol._guard:
            if not self.protocol._connected or self.protocol._failed.is_set() or self.stop.is_set():return None
        try:return self.protocol.inbox.queue.get_nowait()
        except queue.Empty:return None
    def diagnostics(self):
        with self._lock:
            return {'state':self._state,'error_code':self.error_code,'reader_running':self.thread is not None and self.thread.is_alive()}
    def _reader(self,process):
        try:
            while not self.stop.is_set():
                line=process.stdout.readline(MAX_LINE+1)
                if not line or len(line)>MAX_LINE or not line.endswith(b'\n'):raise MarkerError('invalid_marker_bridge_record')
                event=strict_json(line,MAX_LINE)
                if not isinstance(event,dict):raise MarkerError('invalid_marker_bridge_record')
                with self.protocol._guard:
                    self.protocol.accept_event(event)
        except Exception:
            with self.protocol._guard:self.protocol._fail_attempt('marker_bridge_protocol_invalid')
    def _writer(self,process):
        try:
            next_probe=0.
            while not self.stop.wait(.02) and not self.protocol._failed.is_set():
                now=self.clock()
                if now>=next_probe:
                    with self.protocol._guard:
                        if len(self.protocol._probes)<2:
                            self.protocol._probe_id+=1;probe=self.protocol._probe_id
                            self.protocol._probes[probe]=now
                        else:probe=None
                    if probe is not None:
                        process.stdin.write(json.dumps({'type':'clock_probe','id':probe}).encode()+b'\n');process.stdin.flush()
                    next_probe=now+.5
                try:wire=self.requests.get_nowait()
                except queue.Empty:continue
                with self.protocol._guard:
                    ready=self.protocol._ready and self.protocol._connected and self.protocol._offset_ns is not None and now-self.protocol._last_probe_received<=2
                if ready and self.send_guard(wire) and not self.stop.is_set():
                    process.stdin.write(json.dumps({'type':'publish','payload':wire},ensure_ascii=False,separators=(',',':')).encode('utf-8')+b'\n');process.stdin.flush()
        except Exception:
            with self.protocol._guard:self.protocol._fail_attempt('marker_bridge_write_failed')
    def _run(self):
        failures=0
        while not self.stop.is_set():
            process=reader=writer=None
            self.protocol._reset_protocol();self._set('starting')
            while True:
                try:self.requests.get_nowait()
                except queue.Empty:break
            try:
                if self.command is None:
                    from tools.status_bridge.run import prepare,DEFAULT_INSTALL
                    self.command,self.env=prepare(Path(self.config.install) if self.config.install else DEFAULT_INSTALL)
                if self.stop.is_set():break
                process=subprocess.Popen(list(self.command)+['MarkerBridge',self.config.nt_host,str(self.config.nt_port)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=self.env,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.process=process;self.protocol.process=process
                self.protocol._last_probe_received=self.clock()
                reader=threading.Thread(target=self._reader,args=(process,),name='marker-native-reader',daemon=True)
                writer=threading.Thread(target=self._writer,args=(process,),name='marker-native-writer',daemon=True)
                reader.start();writer.start()
                while not self.stop.wait(.05):
                    with self.protocol._guard:
                        if process.poll() is not None:raise MarkerError('marker_bridge_process_exited')
                        if self.protocol._failed.is_set():raise MarkerError('marker_bridge_protocol_invalid')
                        if self.clock()-self.protocol._last_probe_received>2:raise MarkerError('marker_bridge_probe_timeout')
                        connected=self.protocol._connected and self.protocol._offset_ns is not None
                    self._set('ready' if connected else 'waiting_connection')
            except Exception:
                self._set('retry_wait','marker_bridge_unavailable');failures+=1
            finally:
                # Never abandon cleanup or create a replacement while an old pipe is owned.
                while process is not None:
                    try:
                        StatusBridge._reap(process)
                        for thread in (reader,writer):
                            if thread and thread.ident is not None:
                                thread.join(.5)
                                if thread.is_alive():raise OSError('Marker pipe thread pending')
                        for stream in (process.stdin,process.stdout):
                            if stream:stream.close()
                        break
                    except Exception:
                        self._set('stopping','marker_bridge_cleanup_pending');time.sleep(.1)
                self.protocol.inbox.disconnect()
            if self.stop.wait(min(30.,2**min(failures,5))):break
        self._set('stopped')
    def close(self):
        self.stop.set()
        process=self.process
        if process is not None:
            try:
                if process.poll() is None:process.terminate()
            except Exception:self._set('stopping','marker_bridge_cleanup_pending')
        if self.thread and self.thread.ident is not None:self.thread.join(.1)
        return self.thread is None or not self.thread.is_alive()
