"""Cooperative resource admission for private streamed regression transport/work."""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from . import regression as legacy
from .wpilog import ResourceLimit, records, _control

@dataclass(frozen=True)
class ResourceLimits:
    max_sources:int=128
    max_original_bytes:int=2*1024**3
    max_original_total:int=8*1024**3
    max_bundle_bytes:int=16*1024**3
    max_temp_bytes:int=16*1024**3
    min_free_bytes:int=2*1024**3
    metadata_bytes:int=4*1024**2
    inventory_line_bytes:int=8192
    page_rows:int=128
    sql_steps:int=5_000_000
    max_chunks:int=65536
    row_bytes:int=1024**2
    record_bytes:int=1024**2
    entry_bytes:int=4*1024**2
    max_entries:int=65536
    schema_bytes:int=4*1024**2
    held_payload_bytes:int=8*1024**2
    held_row_bytes:int=16*1024**2
    metadata_values_bytes:int=1024**2
    max_metadata_fields:int=4096
    transaction_headroom:int=16*1024**2
    def __post_init__(self):
        for key,value in self.__dict__.items():
            if type(value) is not int or value<(0 if key=='min_free_bytes' else 1):raise ValueError('invalid_stream_regression_limits')
        if self.max_sources>128 or self.metadata_bytes>4*1024**2 or self.inventory_line_bytes>8192 or self.page_rows>128 or self.row_bytes>1024**2 or self.record_bytes>1024**2:
            raise ValueError('invalid_stream_regression_limits')


def limits(value=None):
    if value is None:return ResourceLimits()
    if isinstance(value,ResourceLimits):return value
    return ResourceLimits(**value)


def preflight(path,bounds):
    """Admit framing/entry allocation before the installed decoder materializes rows."""
    entries=set();declarations=0;active={};generations={};schemas={};held={};schema_total=held_total=0
    with path.open('rb') as stream:
        for record in records(stream,max_record_bytes=bounds.record_bytes,truncated_tail={}):
            if record.entry==0:
                declarations+=len(record.payload)
                if declarations>bounds.entry_bytes:raise ResourceLimit('stream_regression_entry_resource_limit')
                if len(record.payload)>=5:
                    entry=int.from_bytes(record.payload[1:5],'little');entries.add(entry)
                    if len(entries)>bounds.max_entries:raise ResourceLimit('stream_regression_entry_resource_limit')
                    old_field=active.get(entry,{}).get('field')
                    control=_control(record,active,generations)
                    if control['control'] in ('start','finish'):
                        for field in (old_field,control['field']):held_total-=held.pop(field,0)
                continue
            entry=active.get(record.entry)
            if entry is None:continue  # The installed decoder reports invalid framing.
            field=entry['field'];size=len(record.payload)
            held_total+=size-held.get(field,0);held[field]=size
            if held_total>bounds.held_payload_bytes:raise ResourceLimit('stream_regression_held_resource_limit')
            if entry['type']=='structschema':
                schema_total+=size-schemas.get(field,0);schemas[field]=size
                if schema_total>bounds.schema_bytes or len(schemas)>bounds.max_entries:raise ResourceLimit('stream_regression_schema_resource_limit')



class WorkBudget:
    """Planned-byte admission plus bounded transaction/WAL headroom, not OS quota."""
    def __init__(self,root,bounds):
        self.root=Path(root);self.limits=bounds;self.metadata={};self.metadata_bytes=0;self.external_bytes=0;self.held={};self.held_bytes=0;self.entries={}
    def begin_decode(self):
        self.metadata={};self.metadata_bytes=0;self.held={};self.held_bytes=0;self.entries={}
    def check(self,extra=0):
        total=0;count=0
        # Private owned workspace only; no source directories are traversed here.
        stack=[self.root]
        while stack:
            directory=stack.pop()
            if not directory.exists():continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    count+=1
                    if count>self.limits.max_chunks+8*self.limits.max_sources+64:raise ResourceLimit('stream_regression_temp_resource_limit')
                    if entry.is_symlink():raise ResourceLimit('stream_regression_temp_resource_limit')
                    if entry.is_dir(follow_symlinks=False):stack.append(Path(entry.path))
                    else:total+=entry.stat(follow_symlinks=False).st_size
        reserve=extra+self.limits.transaction_headroom
        if total+self.external_bytes+reserve>self.limits.max_temp_bytes or shutil.disk_usage(self.root).free-reserve<self.limits.min_free_bytes:
            raise ResourceLimit('stream_regression_temp_resource_limit')
        return total
    def before_write(self,stage,planned_bytes):
        if type(planned_bytes) is not int or planned_bytes<0:raise ResourceLimit('stream_regression_temp_resource_limit')
        self.check(planned_bytes)
    def decoded_row(self,row):
        try:encoded=legacy._json_bytes(row,self.limits.row_bytes)
        except legacy.RegressionError:raise ResourceLimit('stream_regression_row_resource_limit') from None
        if row.get('kind')=='control' and row.get('control') in ('start','finish'):
            previous=self.entries.pop(row.get('entry_id'),None)
            for field in (previous,row.get('field')):self.held_bytes-=self.held.pop(field,0)
            if row['control']=='start':self.entries[row.get('entry_id')]=row.get('field')
        if row.get('kind')=='observation':
            field=row.get('field');proposed=self.held_bytes-self.held.get(field,0)+len(encoded)
            if proposed>self.limits.held_row_bytes or field not in self.held and len(self.held)>=self.limits.max_entries:raise ResourceLimit('stream_regression_held_resource_limit')
            self.held[field]=len(encoded);self.held_bytes=proposed
        if row.get('kind')=='observation' and row.get('category')=='metadata':
            key=row.get('field');size=len(encoded)
            if key not in self.metadata and len(self.metadata)>=self.limits.max_metadata_fields:raise ResourceLimit('stream_regression_metadata_resource_limit')
            proposed=self.metadata_bytes-self.metadata.get(key,0)+size
            if proposed>self.limits.metadata_values_bytes:raise ResourceLimit('stream_regression_metadata_resource_limit')
            self.metadata[key]=size;self.metadata_bytes=proposed
