"""Opt-in, read-only SystemCore transport for T10 immutable manifests.

No discovery of credentials/endpoints, trust-on-first-use, shell, source writes,
delete, prefetch or robot control APIs. Physical performance remains unqualified.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import re
import stat
import threading
import time

from .collector import LogFile
from .transfer import AuthenticationError, IdentityError, ManifestPage, SourceMissing, TransferCancelled, TransferError


class SourceAttention(TransferError):
    code, retryable = 'source_recording_attention', False


def _created_at(value):
    if value is None:
        return 0.0  # Ordering fallback only; never evidence of the event's UTC.
    if not isinstance(value,str) or len(value)>64:
        raise ValueError('Segment creation time must be ISO UTC or null')
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset()!=timezone.utc.utcoffset(stamp):
            raise ValueError()
        return stamp.timestamp()
    except (ValueError,OverflowError):
        raise ValueError('Invalid segment UTC creation time') from None


@dataclass(frozen=True)
class SFTPConfig:
    host: str
    username: str
    known_hosts: str
    private_key: str
    log_root: str
    robot_id: str
    port: int = 22
    timeout: float = 1.0
    max_read: int = 262144
    freshness: float = .5

    def __post_init__(self):
        if any(not isinstance(v,str) or not v or '\x00' in v for v in
               (self.host,self.username,self.known_hosts,self.private_key,self.log_root,self.robot_id)):
            raise ValueError('All source identities, paths and credentials must be explicitly configured')
        if not self.log_root.startswith('/') or '..' in PurePosixPath(self.log_root).parts or self.log_root=='/':
            raise ValueError('Configure a specific absolute remote log directory')
        if type(self.port) is not int or not 1<=self.port<=65535:
            raise ValueError('Invalid SFTP port')
        if type(self.timeout) not in (float,int) or not .05<=self.timeout<=5 or not math.isfinite(self.timeout):
            raise ValueError('SFTP timeout must be 0.05 through 5 seconds')
        if type(self.max_read) is not int or not 1<=self.max_read<=262144:
            raise ValueError('SFTP read bound must be 1 through 262144 bytes')
        if type(self.freshness) not in (float,int) or not 0<self.freshness<=1:
            raise ValueError('Status freshness must be positive and at most one second')
        if (len(self.host)>253 or self.host.startswith('-') or any(c.isspace() or ord(c)<32 for c in self.host)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',self.robot_id)):
            raise ValueError('Invalid configured host or robot identity')

    @classmethod
    def load(cls,path):
        try:
            path=Path(path).resolve()
            value=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value.pop('schema_version')!=1:
                raise ValueError('Source configuration requires schema_version 1')
            if value.keys()-cls.__dataclass_fields__.keys():
                raise ValueError('Unknown source configuration field')
            for key in ('known_hosts','private_key'):
                if not isinstance(value.get(key),str):
                    raise ValueError('Explicit credential file references required')
                value[key]=str((path.parent/value[key]).resolve())
                if not Path(value[key]).is_file():
                    raise ValueError('Configured host-key or private-key file is unavailable')
            return cls(**value)
        except (OSError,TypeError,json.JSONDecodeError):
            raise ValueError('Cannot read explicit source configuration; check fields and local file references') from None


def _name(value):
    # Receiver artifacts use flat names. Deny directories and symlinks outright.
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.(?:json|wpilog)',value):
        raise ValueError('Manifest artifact path is outside the flat receiver contract')
    return value


class SFTPSource:
    source_type='systemcore_sftp_unqualified'

    def __init__(self, config, status_provider, *, client_factory=None):
        self.config,self.status_provider=config,status_provider
        self.client_factory=client_factory
        self.active_lock=threading.Lock()
        self.active_client=None
        self.session=None
        self.cleanup_pending=0
        self.cleanup_finished=threading.Event()
        self.cleanup_finished.set()
        self.operation_lock=threading.Lock()
        self.snapshot=None
        self.entries={}
        self.pending=0
        self.pending_segments={}
        self.page_cache={}

    def status(self):
        return self.status_provider.status()

    def cancel(self):
        self._disconnect_transport()

    def _disconnect_transport(self, expected_client=None):
        with self.active_lock:
            client=self.active_client
            if expected_client is not None and client is not expected_client:
                return
            self.active_client=None
            self.session=None
            if client is not None:
                self.cleanup_pending+=1
                self.cleanup_finished.clear()
        if client is not None:
            # The detached client remains owned by this cleanup call. Even an
            # unexpected close exception is unresolved cleanup, never success.
            # Only close is retried; source operations/errors are not replayed.
            while True:
                try:
                    client.close()
                    break
                except Exception:
                    time.sleep(.05)
            with self.active_lock:
                self.cleanup_pending-=1
                if not self.cleanup_pending:
                    self.cleanup_finished.set()

    def close(self):
        self.cancel()

    @staticmethod
    def _finish_watcher(watcher):
        # A blocked native close still belongs to this operation. Keep its
        # collector slot occupied rather than leaving cleanup behind a timeout.
        while watcher.is_alive():
            watcher.join(.05)

    def _await_transport_cleanup(self):
        while not self.cleanup_finished.wait(.05):
            pass

    def _check(self,token):
        token.check()
        status=self.status()
        age=time.monotonic()-status.observed_at
        if (status.enabled is not False or status.transfer_allowed is not True or not 0<=age<=self.config.freshness
                or token.generation[0]!=(status.boot_id,status.generation)):
            token.cancel()
            raise TransferCancelled('Authoritative source status no longer permits transfer')

    @contextmanager
    def _session(self, token):
        self._check(token)
        # Collector already has one outstanding slot. Fail visibly if another
        # caller bypasses it instead of sharing an SFTP channel unsafely.
        if not self.operation_lock.acquire(blocking=False):
            raise TransferError('Another source operation is outstanding')
        finished=threading.Event()
        watcher=None
        try:
            self._await_transport_cleanup()
            with self.active_lock:
                session=self.session
            if session is None:
                session=self._connect(token)
            client,sftp,root=session
            def cancel_watch():
                while not finished.wait(.02):
                    try:
                        self._check(token)
                    except OSError:
                        self._disconnect_transport(client)
                        return
            watcher=threading.Thread(target=cancel_watch,name='hub-sftp-cancel',daemon=True)
            watcher.start()
            self._check(token)
            yield sftp,root
            self._check(token)
        except Exception as exc:
            self._disconnect_transport()
            token.check()
            if type(exc).__name__ in ('BadHostKeyException','AuthenticationException','PasswordRequiredException'):
                raise AuthenticationError('Pinned host key or explicit key authentication failed') from None
            if isinstance(exc,FileNotFoundError):
                raise SourceMissing('Manifest or source recording unavailable') from None
            # Paramiko reports an unexpected connection loss as SSHException,
            # which is not an OSError and would escape the collector's retry
            # handling. Keep provider messages out of persisted diagnostics.
            if isinstance(exc,EOFError):
                raise TransferError('SFTP connection interrupted; reconnect required') from None
            try:
                import paramiko
            except ImportError:
                paramiko=None
            if paramiko is not None and isinstance(exc,paramiko.SSHException):
                raise TransferError('SFTP connection interrupted; reconnect required') from None
            raise
        finally:
            finished.set()
            if watcher is not None:
                self._finish_watcher(watcher)
            self._await_transport_cleanup()
            self.operation_lock.release()

    def _connect(self,token):
        if self.client_factory:
            client=self.client_factory()
            authentication={'key_filename':self.config.private_key}
        else:
            import paramiko
            if paramiko.__version__ != '4.0.0':
                raise AuthenticationError('Install the pinned SFTP extra before connecting')
            client=paramiko.SSHClient()
            class PinnedOnly(paramiko.MissingHostKeyPolicy):
                def missing_host_key(self,*ignored):
                    raise AuthenticationError('Server key is absent from the explicit pinned known-hosts file')
            client.set_missing_host_key_policy(PinnedOnly())
            # Deliberately do not merge ambient ~/.ssh trust or agent credentials.
            try:
                client.load_host_keys(self.config.known_hosts)
                private_key=paramiko.PKey.from_path(self.config.private_key)
            except (OSError,paramiko.SSHException):
                client.close()
                raise AuthenticationError('Explicit host-key or private-key file unavailable or invalid') from None
            except (ValueError,TypeError,paramiko.UnknownKeyType):
                client.close()
                raise AuthenticationError('Private key format/encryption is unsupported by this configured reader') from None
            authentication={'pkey':private_key}
        with self.active_lock:
            self.active_client=client
        finished=threading.Event()
        def cancel_watch():
            while not finished.wait(.02):
                try:
                    self._check(token)
                except OSError:
                    self._disconnect_transport(client)
                    return
        watcher=threading.Thread(target=cancel_watch,name='hub-sftp-cancel',daemon=True)
        watcher.start()
        try:
            self._check(token)
            client.connect(hostname=self.config.host,port=self.config.port,username=self.config.username,
                **authentication,allow_agent=False,look_for_keys=False,
                timeout=self.config.timeout,banner_timeout=self.config.timeout,auth_timeout=self.config.timeout,
                channel_timeout=self.config.timeout)
            self._check(token)
            sftp=client.open_sftp()
            sftp.get_channel().settimeout(self.config.timeout)
            self._check(token)
            root=sftp.normalize(self.config.log_root).rstrip('/')
            if not root.startswith('/') or not root:
                raise IdentityError('Remote root normalization failed')
            self._check(token)
            session=(client,sftp,root)
            with self.active_lock:
                if self.active_client is not client:
                    raise TransferCancelled('Connection canceled before publication')
                self.session=session
            return session
        except Exception as exc:
            token.check()
            # Types are checked without exporting provider exception strings.
            if type(exc).__name__ in ('BadHostKeyException','AuthenticationException','PasswordRequiredException'):
                raise AuthenticationError('Pinned host key or explicit key authentication failed') from None
            raise
        finally:
            finished.set()
            self._finish_watcher(watcher)
            self._await_transport_cleanup()

    def _open(self,sftp,root,name,token):
        self._check(token)
        path=root+'/'+_name(name)
        info=sftp.lstat(path)
        if not stat.S_ISREG(info.st_mode) or sftp.normalize(path)!=path:
            raise IdentityError('Source artifact is not a regular file within the configured root')
        self._check(token)
        return sftp.open(path,'rb',bufsize=0),info

    def _document(self,sftp,root,name,token,bound,expected=None):
        stream,info=self._open(sftp,root,name,token)
        with stream:
            if type(info.st_size) is not int or not 0<info.st_size<=bound:
                raise ValueError('Manifest exceeds the qualified size bound')
            if expected is not None and info.st_size!=expected['size_bytes']:
                raise IdentityError('Immutable manifest page size changed')
            chunks=[];remaining=info.st_size
            while remaining:
                self._check(token)
                data=stream.read(min(32768,self.config.max_read,remaining))
                self._check(token)
                if not data:
                    raise IdentityError('Manifest truncated during read')
                remaining-=len(data);chunks.append(data)
            data=b''.join(chunks)
        if expected is not None and hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise IdentityError('Immutable manifest page digest changed')
        value=json.loads(data)
        if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=1:
            raise ValueError('Unsupported source manifest schema')
        return value

    def discover_closed(self,cursor,limit,cancellation):
        if type(limit) is not int or not 1<=limit<=1000:
            raise ValueError('Invalid discovery limit')
        with self._session(cancellation) as (sftp,root):
            if cursor is None:
                manifest=self._document(sftp,root,'manifest.json',cancellation,1024*1024)
                if manifest.get('robot_id')!=self.config.robot_id or not manifest.get('boot_id'):
                    raise IdentityError('Source manifest robot identity mismatch')
                if manifest['boot_id']!=self.status().boot_id:
                    raise IdentityError('Source manifest belongs to a different current robot boot')
                if manifest.get('segments_complete') is not True:
                    raise ValueError('Receiver catalog exceeds page limit; increase explicit receiver limit before collection')
                revision=manifest.get('manifest_revision')
                if not isinstance(revision,str) or not re.fullmatch(r'[0-9]{1,19}',revision):
                    raise ValueError('Invalid manifest revision')
                pages=manifest.get('pages')
                if not isinstance(pages,list) or len(pages)>512:
                    raise ValueError('Unbounded manifest page list')
                for page in pages:
                    if (not isinstance(page,dict) or not re.fullmatch(r'[a-f0-9]{64}',str(page.get('sha256','')))
                            or type(page.get('size_bytes')) is not int or not 0<page['size_bytes']<=262144
                            or page.get('relative_path')!='manifest-page-'+page['sha256']+'.json'):
                        raise ValueError('Malformed immutable page reference')
                self.snapshot=manifest
                self.page_cache={};self.pending=0;self.pending_segments={}
                position,entry_position=0,0
            else:
                try:
                    revision,position,entry_position=json.loads(cursor)
                    if (self.snapshot is None or revision!=self._revision() or type(position) is not int
                            or type(entry_position) is not int or position<0 or entry_position<0
                            or position>=len(self.snapshot['pages'])):
                        raise ValueError()
                except (TypeError,ValueError):
                    raise ValueError('Stale or malformed manifest cursor') from None
            files=[];pages=self.snapshot['pages']
            # At most one physical manifest page per collector operation.
            if position<len(pages):
                page=pages[position]
                if position not in self.page_cache:
                    document=self._document(sftp,root,page['relative_path'],cancellation,262144,page)
                    entries=document.get('entries')
                    if not isinstance(entries,list) or len(entries)>128:
                        raise ValueError('Malformed segment page')
                    self.page_cache={position:entries}
                entries=self.page_cache[position]
                if entry_position>len(entries):
                    raise ValueError('Manifest cursor exceeds its page')
                while entry_position<len(entries) and len(files)<limit:
                    entry=entries[entry_position];entry_position+=1
                    if not isinstance(entry,dict) or entry.get('robot_id')!=self.config.robot_id:
                        raise IdentityError('Segment robot identity mismatch')
                    required={'segment_id','original_name','size_bytes','sha256','relative_path','boot_id','format','format_profile'}
                    if (not required<=entry.keys() or not isinstance(entry['segment_id'],str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',entry['segment_id'])
                            or not isinstance(entry['original_name'],str) or not 0<len(entry['original_name'])<=255
                            or not isinstance(entry['boot_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',entry['boot_id'])
                            or entry['format']!='wpilog' or not isinstance(entry['format_profile'],str)):
                        raise ValueError('Malformed segment metadata')
                    if entry.get('state')!='closed':
                        if entry.get('state')!='closed_pending_digest':
                            raise SourceAttention('Receiver has an incomplete, missing or conflicting recording; inspect its manifest')
                        size=entry.get('size_bytes')
                        if type(size) is not int or size<0:
                            raise ValueError('Incomplete source artifact size is unknown; receiver needs attention')
                        # A canceled page can be retried with the same cursor.
                        # Count each immutable pending segment once per snapshot.
                        prior=self.pending_segments.get(entry['segment_id'])
                        if prior is None:
                            self.pending_segments[entry['segment_id']]=size
                            self.pending+=size
                        elif prior!=size:
                            raise IdentityError('Pending recording identity changed within a snapshot')
                        continue
                    item=LogFile(entry['segment_id'],entry['original_name'],entry['size_bytes'],entry['sha256'],
                        _created_at(entry.get('created_at')),relative_path=entry['relative_path'],boot_id=entry['boot_id'],
                        format=entry['format'],format_profile=entry['format_profile'])
                    item.validate();_name(item.relative_path)
                    old=self.entries.get(item.id)
                    if old and (old.size,old.sha256)!=(item.size,item.sha256):
                        raise IdentityError('Closed source identity changed')
                    self.entries[item.id]=item;files.append(item)
                if entry_position>=len(entries):
                    position+=1;entry_position=0
            complete=position>=len(pages)
            open_segment=self.snapshot.get('open_segment')
            if open_segment is not None and not isinstance(open_segment,dict):
                raise ValueError('Invalid open segment metadata')
            open_bytes=open_segment.get('size_bytes',0) if open_segment is not None else 0
            if open_bytes is None:
                raise SourceAttention('Open recording size is unavailable; inspect receiver health')
            if type(open_bytes) is not int or open_bytes<0:
                raise ValueError('Invalid open segment size')
            return ManifestPage(self._revision(),tuple(files),None if complete else
                json.dumps([self._revision(),position,entry_position]),complete,open_bytes,self.pending)

    def _revision(self):
        return str(self.snapshot['boot_id'])+':'+str(self.snapshot['manifest_revision'])

    def read(self,file_id,offset,length,*,permission_generation=None,cancellation=None):
        if cancellation is None:
            raise TransferCancelled('Read requires collector permission token')
        item=self.entries.get(file_id)
        if item is None:
            raise SourceMissing('Recording is absent from the verified manifest')
        if type(offset) is not int or type(length) is not int or offset<0 or not 0<length<=self.config.max_read or offset+length>item.size:
            raise ValueError('Read is outside the declared immutable segment range')
        with self._session(cancellation) as (sftp,root):
            stream,info=self._open(sftp,root,item.relative_path,cancellation)
            with stream:
                if info.st_size!=item.size:
                    raise IdentityError('Closed source length changed')
                stream.seek(offset);chunks=[];remaining=length
                while remaining:
                    self._check(cancellation)
                    block=stream.read(min(32768,remaining))
                    self._check(cancellation)
                    if not block:
                        raise IdentityError('Closed source recording truncated')
                    chunks.append(block);remaining-=len(block)
                return b''.join(chunks)
