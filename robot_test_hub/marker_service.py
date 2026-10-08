"""Opt-in note delivery independent of collection, operator pause and enabled mode."""
from __future__ import annotations
from dataclasses import dataclass,field
import math
from pathlib import Path
import threading
import time
from .marker_delivery import MarkerStore,MarkerError,strict_json,validate_ack,TERMINAL
from .marker_bridge import MarkerBridge
from .status_bridge import StatusBridge

@dataclass(frozen=True)
class MarkerConfig:
    nt_host:str=field(repr=False)
    nt_port:int
    robot_id:str
    runtime_mode:str
    install:str|None=field(default=None,repr=False)
    freshness:float=1.
    ack_timeout:float=2.
    retry_initial:float=1.
    retry_max:float=30.
    maximum_attempts:int=20
    maximum_pending:int=128
    def __post_init__(self):
        from .marker_delivery import identity
        identity(self.robot_id)
        if self.runtime_mode not in ('REAL','SIM'):raise MarkerError('invalid_marker_configuration')
        if (not isinstance(self.nt_host,str) or not self.nt_host or len(self.nt_host)>253 or self.nt_host.startswith('-')
                or any(c.isspace() or ord(c)<32 or c in '/@\\' for c in self.nt_host)
                or type(self.nt_port) is not int or not 1<=self.nt_port<=65535):raise MarkerError('invalid_marker_configuration')
        for key in ('freshness','ack_timeout','retry_initial','retry_max'):
            value=getattr(self,key)
            if type(value) not in (int,float) or not .05<=value<=300 or not math.isfinite(value):raise MarkerError('invalid_marker_configuration')
        if self.retry_max<self.retry_initial or self.freshness>5:raise MarkerError('invalid_marker_configuration')
        for key,maximum in (('maximum_attempts',100),('maximum_pending',1024)):
            value=getattr(self,key)
            if type(value) is not int or not 1<=value<=maximum:raise MarkerError('invalid_marker_configuration')
        if self.install is not None and (not isinstance(self.install,str) or not self.install or len(self.install)>4096):raise MarkerError('invalid_marker_configuration')

def load_marker_config(path):
    try:
        with Path(path).open('rb') as stream:value=strict_json(stream.read(16385))
    except (OSError,UnicodeError):raise MarkerError('invalid_marker_configuration') from None
    fields=set(MarkerConfig.__dataclass_fields__)
    if (not isinstance(value,dict) or value.keys()-(fields|{'schema_version','enabled'})
            or type(value.get('schema_version')) is not int or value['schema_version']!=1 or value.get('enabled') is not True
            or not {'nt_host','nt_port','robot_id','runtime_mode'}<=value.keys()):raise MarkerError('invalid_marker_configuration')
    return MarkerConfig(**{k:v for k,v in value.items() if k in fields})

class MarkerWorker:
    def __init__(self,service,config=None,*,status_factory=StatusBridge,transport_factory=MarkerBridge,clock=time.monotonic):
        if config is not None and not isinstance(config,MarkerConfig):raise MarkerError('invalid_marker_configuration')
        self.service=service;self.config=config;self.clock=clock;self.status_factory=status_factory;self.transport_factory=transport_factory
        self.store=MarkerStore(service.root/'catalog.sqlite3',maximum_pending=config.maximum_pending if config else 128)
        self.status_reader=self.transport=None;self.lock=threading.Lock()
        self.health={'schema_version':1,'enabled':config is not None,'state':'starting' if config else 'disabled','error_code':None,
            'ack_scope':'contextual_receipt_only','usb_durability':'unavailable','physical_link_qualified':False}
        self._active=None;self._due=0.;self._awaiting=False
    def _health(self,state,error=None):
        with self.lock:self.health.update(state=state,error_code=error)
    def snapshot(self):
        with self.lock:result=dict(self.health)
        result.update(robot_id=self.config.robot_id if self.config else None,
                      runtime_mode=self.config.runtime_mode if self.config else None,
                      current_confirmed_boot_id=None,status_fresh=False,ready_for_delivery=False)
        transport_health=self.transport.diagnostics() if self.transport is not None and hasattr(self.transport,'diagnostics') else {'state':'starting','error_code':None}
        result['transport']=transport_health
        if self.config and self.status_reader is not None:
            status=self.status_reader.status()
            fresh=self._fresh(status.boot_id)
            result.update(current_confirmed_boot_id=status.boot_id if fresh else None,status_fresh=fresh,
                ready_for_delivery=fresh and transport_health['state']=='ready' and not self.service.stop.is_set() and result['state'] not in ('failed','stopping','stopped'))
        return result
    def _fresh(self,boot):
        if self.status_reader is None:return False
        status=self.status_reader.status();age=self.clock()-status.observed_at
        return (type(status.enabled) is bool and type(status.sequence) is int and status.sequence>=0
            and math.isfinite(age) and 0<=age<=self.config.freshness and status.boot_id==boot)
    def _send_guard(self,wire):
        value=strict_json(wire)
        return not self.service.stop.is_set() and self._fresh(value['destination_boot_id'])
    def schedule(self,request):
        if self.config is None:raise MarkerError('marker_delivery_disabled',409)
        with self.service.settings_lock:
            if self.service.stop.is_set() or self.service.closed:raise MarkerError('service_stopping',503)
            return self.store.schedule(request,robot_id=self.config.robot_id)
    def retry(self,delivery_id):
        if self.config is None:raise MarkerError('marker_delivery_disabled',409)
        with self.service.settings_lock:
            if self.service.stop.is_set() or self.service.closed:raise MarkerError('service_stopping',503)
            result=self.store.retry(delivery_id)
        return result
    def tick(self):
        job=self.store.pending()
        if job is None:self._health('ready');self._active=None;return
        now=self.clock()
        if job['delivery_id']!=self._active:
            self._active=job['delivery_id'];self._awaiting=False;self._due=now
        status=self.status_reader.status()
        age=now-status.observed_at
        proven=type(status.enabled) is bool and type(status.sequence) is int and math.isfinite(age) and 0<=age<=self.config.freshness
        if proven and status.boot_id!=job['destination_boot_id']:
            self.store.update(job['delivery_id'],'historical_boot_ended',error='destination_boot_ended');self._health('waiting_fresh_status','destination_boot_ended');return
        if not self._fresh(job['destination_boot_id']):
            self.store.update(job['delivery_id'],'waiting_fresh_status',error='awaiting_fresh_destination_status');self._health('waiting_fresh_status');return
        response=self.transport.poll()
        if response is not None:
            value,observed=response
            if not 0<=now-observed<=self.config.freshness:
                self._health('awaiting_ack','marker_ack_stale')
            else:
                try:
                    ack=validate_ack(value,job,self.config.runtime_mode)
                    # Only a job with a persisted publication attempt may accept ACKs.
                    if job['attempts']<1:raise MarkerError('marker_ack_without_attempt')
                    state='robot_acknowledged' if ack['state']=='accepted_into_log_input' else 'rejected'
                    self.store.update(job['delivery_id'],state,error=ack['reason'],ack=ack);self._health('ready');return
                except MarkerError:self._health('awaiting_ack','marker_ack_invalid')
        if now<self._due:return
        if self._awaiting:
            self._awaiting=False
            delay=min(self.config.retry_max,self.config.retry_initial*2**min(max(job['attempts']-1,0),20))
            self._due=now+delay;self.store.update(job['delivery_id'],'retry_wait',error='marker_ack_timeout');self._health('retry_wait');return
        if job['attempts']>=self.config.maximum_attempts:
            self.store.update(job['delivery_id'],'unavailable',error='marker_attempt_limit_unconfirmed');self._health('ready');return
        # FULL transaction is committed before the transport can observe bytes.
        self.store.update(job['delivery_id'],'awaiting_ack',attempt=True)
        if self.transport.send(job['wire_json']):
            self._awaiting=True;self._due=now+self.config.ack_timeout;self._health('awaiting_ack')
        else:
            self._due=now+self.config.retry_initial;self.store.update(job['delivery_id'],'retry_wait',error='marker_transport_unavailable');self._health('retry_wait')
    def run(self,stop):
        if self.config is None:return
        try:
            self.status_reader=self.status_factory(self.config.nt_host,self.config.nt_port,self.config.robot_id,
                runtime_mode=self.config.runtime_mode,install=self.config.install,clock=self.clock)
            self.transport=self.transport_factory(self.config,clock=self.clock)
            self.transport.send_guard=self._send_guard
            self.status_reader.start();self.transport.start()
            while not stop.wait(.05):self.tick()
        except Exception:self._health('failed','marker_worker_unavailable')
        finally:
            # Retain the service worker until all independent native lifetimes finish.
            while True:
                pending=False
                for adapter in (self.transport,self.status_reader):
                    if adapter is None:continue
                    try:
                        closed=adapter.close()
                        threads=getattr(adapter,'_threads',[])
                        if closed is False or any(t.is_alive() for t in threads):pending=True
                    except Exception:pending=True
                if not pending:break
                self._health('stopping','marker_cleanup_pending');time.sleep(.1)
            self._health('stopped')
