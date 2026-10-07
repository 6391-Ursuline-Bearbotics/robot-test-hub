"""Explicit read-only NT status process; never fabricates a robot heartbeat."""
from __future__ import annotations
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time

from .source_status import StatusInbox

PROFILE = 'wpilib-2027.0.0-alpha-7-windowsx86-64'
TOPIC = '/Telemetry/TestHub/Status'
MAX_LINE = 131072


class StatusBridge:
    def __init__(self, host, port, robot_id, *, runtime_mode='REAL', install=None,
                 clock=time.monotonic, command=None, env=None):
        if (not isinstance(host,str) or not host or len(host)>253 or host.startswith('-')
                or any(c.isspace() or ord(c)<32 for c in host) or type(port) is not int or not 1<=port<=65535):
            raise ValueError('Explicit NT host and port are required')
        self.host,self.port,self.install,self.clock = host,port,install,clock
        self.command,self.env = command,env
        self._publication_at = 0.
        self.inbox = StatusInbox(robot_id,runtime_mode=runtime_mode,clock=lambda:self._publication_at,
                                 link_profile=PROFILE)
        self._guard = threading.RLock()
        self._stop = threading.Event()
        self.process = None
        self._threads = []
        self._ready = self._connected = False
        self._epoch = self._last_local = self._last_server = 0
        self._offset_ns = None
        self._probes = {}
        self._probe_id = 0
        self._last_probe_received = self.clock()
        self.error_code = 'status_bridge_not_started'

    def _disconnect(self, code):
        self.inbox.disconnect()
        self.error_code = code

    def start(self):
        with self._guard:
            if self.process is not None:
                raise RuntimeError('Status bridge instances start once; create a new instance to restart')
            self._disconnect('status_bridge_starting')
            try:
                command, env = self.command,self.env
                if command is None:
                    from tools.status_bridge.run import prepare, DEFAULT_INSTALL
                    command,env = prepare(Path(self.install) if self.install else DEFAULT_INSTALL)
                self.process = subprocess.Popen(list(command)+['StatusBridge',self.host,str(self.port)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self._last_probe_received = self.clock()
                self._threads = [threading.Thread(target=self._read,name='nt-status-reader',daemon=True),
                                 threading.Thread(target=self._monitor,name='nt-status-monitor',daemon=True)]
                for thread in self._threads:
                    thread.start()
            except Exception:
                self._disconnect('status_bridge_start_failed')
                if self.process is not None and self.process.poll() is None:
                    self.process.terminate()
                raise
        return self

    def _send_probe(self):
        with self._guard:
            if len(self._probes)>=2:
                return
            self._probe_id += 1
            self._probes[self._probe_id] = self.clock()
            request = json.dumps({'type':'clock_probe','id':self._probe_id}).encode()+b'\n'
            self.process.stdin.write(request)
            self.process.stdin.flush()

    def _monitor(self):
        try:
            while not self._stop.wait(.1):
                with self._guard:
                    if self.process.poll() is not None:
                        self._disconnect('status_bridge_process_exited')
                        return
                    if self.clock()-self._last_probe_received>2:
                        self._disconnect('status_bridge_clock_probe_timeout')
                        self.process.terminate()
                        return
                self._send_probe()
                if self._stop.wait(.4):
                    return
        except (OSError,ValueError):
            with self._guard:
                self._disconnect('status_bridge_io_failed')

    def _read(self):
        try:
            while not self._stop.is_set():
                line = self.process.stdout.readline(MAX_LINE+1)
                if not line:
                    break
                if len(line)>MAX_LINE or not line.endswith(b'\n'):
                    raise ValueError('Oversized or incomplete bridge line')
                self.accept_event(json.loads(line.decode('utf-8')))
        except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError):
            with self._guard:
                self._disconnect('status_bridge_protocol_invalid')
                if self.process.poll() is None:
                    self.process.terminate()
        finally:
            with self._guard:
                self._disconnect('status_bridge_stopped' if self._stop.is_set() else
                                 self.error_code if self.error_code=='status_bridge_protocol_invalid' else
                                 'status_bridge_process_exited')

    @staticmethod
    def _ns(value):
        if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,19}',value) or not 0<int(value)<(1<<63):
            raise ValueError('Invalid bridge nanosecond timestamp')
        return int(value)

    def accept_event(self, event):
        """Validate bridge framing. Exposed for deterministic protocol qualification."""
        with self._guard:
            if not isinstance(event,dict):
                raise ValueError('Bridge event must be an object')
            kind = event.get('type')
            if kind=='ready':
                if (self._ready or type(event.get('protocol')) is not int or event['protocol']!=1
                        or event.get('profile')!=PROFILE or event.get('topic')!=TOPIC):
                    raise ValueError('Unexpected status bridge profile')
                self._ready = True
                self.error_code = 'awaiting_status_connection'
                return
            if not self._ready:
                raise ValueError('Bridge readiness must precede events')
            if kind=='clock_probe':
                probe_id = event.get('id')
                if type(probe_id) is not int or probe_id not in self._probes:
                    raise ValueError('Unknown clock probe response')
                sent = self._probes.pop(probe_id)
                now = self.clock()
                roundtrip = now-sent
                if not math.isfinite(roundtrip) or roundtrip<0 or roundtrip>.5:
                    self._disconnect('status_bridge_clock_probe_delay')
                    return
                # Probe execution is after the Python send. This offset puts
                # converted publications no later than their true local time,
                # including publications buffered in the child/stdout pipe.
                self._offset_ns = int(sent*1e9)-self._ns(event.get('nt_local_ns'))
                self._last_probe_received = now
                return
            if kind in ('connected','disconnected'):
                epoch = event.get('connection_epoch')
                if type(epoch) is not int or epoch<0 or epoch<self._epoch:
                    raise ValueError('Invalid connection generation')
                if kind=='connected' and (epoch==0 or epoch<=self._epoch):
                    raise ValueError('Connection generation must advance')
                self._epoch = epoch
                self._connected = kind=='connected'
                self._last_local = self._last_server = 0
                self._disconnect('awaiting_advancing_robot_status' if self._connected else 'status_disconnected')
                return
            if kind=='rejected':
                self._disconnect('status_publication_rejected')
                return
            if kind!='publication':
                raise ValueError('Unknown bridge event')
            if not self._connected or event.get('connection_epoch')!=self._epoch:
                self._disconnect('publication_outside_connection')
                return
            if self._offset_ns is None:
                self._disconnect('awaiting_status_clock_probe')
                return
            local,server = self._ns(event.get('nt_local_ns')),self._ns(event.get('nt_server_ns'))
            encoded = event.get('payload')
            if (local<=self._last_local or server<=self._last_server
                    or not isinstance(encoded,str) or len(encoded.encode('utf-8'))>16384):
                self._disconnect('status_publication_timestamp_or_size_invalid')
                return
            observed = (local+self._offset_ns)/1e9
            if not math.isfinite(observed) or observed>self.clock()+.000001:
                self._disconnect('status_publication_future_timestamp')
                return
            self._last_local,self._last_server = local,server
            self._publication_at = observed
            self.inbox.receive(encoded)
            self.error_code = self.inbox.error_code

    def status(self):
        with self._guard:
            if self.process is not None and self.process.poll() is not None:
                self._disconnect('status_bridge_process_exited')
            return self.inbox.status()

    def close(self):
        self._stop.set()
        with self._guard:
            self._disconnect('status_bridge_stopped')
            process = self.process
            if process is not None and process.poll() is None:
                process.terminate()
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for thread in self._threads:
            thread.join(timeout=2)
        if process is not None:
            for stream in (process.stdin,process.stdout):
                stream.close()

    def __enter__(self):
        return self.start()

    def __exit__(self,*ignored):
        self.close()
