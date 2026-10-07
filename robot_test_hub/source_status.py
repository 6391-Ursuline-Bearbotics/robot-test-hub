"""Strict receiver for the robot's atomic T09 status envelope.

The channel calls receive only for actual received publications. Polling a cached
value cannot refresh its age. An advancing pair is required after disconnect.
"""
import json
import re
import threading
import time

from .collector import RobotStatus


class StatusInbox:
    def __init__(self, robot_id, *, runtime_mode='REAL', clock=time.monotonic, link_profile='unqualified'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', robot_id):
            raise ValueError('Explicit expected robot identity is required')
        if runtime_mode not in ('REAL','SIM'):
            raise ValueError('Only explicit REAL or SIM status profiles are supported')
        self.robot_id,self.runtime_mode,self.clock,self.link_profile=robot_id,runtime_mode,clock,link_profile
        self.lock=threading.Lock()
        self.previous=None
        self.proven=False
        self.latest=RobotStatus(None,0,'unknown',0,False)
        self.error_code='awaiting_advancing_robot_status'

    def disconnect(self):
        with self.lock:
            self.proven=False
            self.previous=None
            self.latest=RobotStatus(None,self.latest.observed_at,self.latest.boot_id,self.latest.generation,False)
            self.error_code='status_disconnected'

    def receive(self, encoded):
        with self.lock:
            try:
                if not isinstance(encoded,str) or len(encoded.encode('utf-8'))>16384:
                    raise ValueError()
                value=json.loads(encoded)
                if (not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=1
                        or value.get('robot_id')!=self.robot_id or value.get('runtime_mode')!=self.runtime_mode
                        or type(value.get('enabled')) not in (bool,type(None))
                        or type(value.get('transfer_allowed')) is not bool
                        or not isinstance(value.get('boot_id'),str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value['boot_id'])
                        or any(type(value.get(k)) is not int or not 0<=value[k]<(1<<63) for k in ('sequence','mode_generation'))
                        or not isinstance(value.get('robot_monotonic_ns'),str)
                        or not re.fullmatch(r'[0-9]{1,19}',value['robot_monotonic_ns'])
                        or int(value['robot_monotonic_ns'])>=(1<<63)
                        or (value['transfer_allowed'] and (value['enabled'] is not False or value.get('mode')!='disabled'))):
                    raise ValueError()
                current=(value['boot_id'],value['sequence'],int(value['robot_monotonic_ns']),
                         value['mode_generation'],value['enabled'],value['transfer_allowed'],value.get('mode'),value.get('operating_mode'))
                previous=self.previous
                if previous and current[0]==previous[0]:
                    if current==previous:
                        return self.latest  # Retained/repeated publication is not fresh.
                    if current[1]<=previous[1] or current[2]<=previous[2] or current[3]<previous[3]:
                        raise ValueError()
                    if current[4:]!=previous[4:] and current[3]==previous[3]:
                        raise ValueError()
                    self.proven=True
                else:
                    self.proven=False
                self.previous=current
                self.latest=RobotStatus(value['enabled'] if self.proven else None,self.clock(),value['boot_id'],
                    value['mode_generation'],value['transfer_allowed'] and self.proven,value['sequence'],self.link_profile)
                self.error_code=None if self.proven else 'awaiting_advancing_robot_status'
            except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
                self.proven=False
                self.latest=RobotStatus(None,self.latest.observed_at,self.latest.boot_id,self.latest.generation,False)
                self.error_code='invalid_robot_status'
            return self.latest

    def status(self):
        with self.lock:
            return self.latest
