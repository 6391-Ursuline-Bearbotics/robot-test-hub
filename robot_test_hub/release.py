"""Deterministic committed-source releases; integrity receipts are not signatures.

Publication reserves a fresh directory exclusively and publishes manifest.json
last. A partial group is retained and never verifies; directory-wide crash
atomicity and storage power-loss durability are not claimed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import unicodedata
import zipfile

TOOL_VERSION='source-release-1'
PUBLICATION='exclusive_directory_manifest_last'
MAX_FILES=5000
MAX_FILE_BYTES=8*1024*1024
MAX_TOTAL_BYTES=64*1024*1024
MAX_ZIP_BYTES=96*1024*1024
MAX_MANIFEST_BYTES=2*1024*1024
MAX_TREE_BYTES=2*1024*1024
MAX_CENTRAL_BYTES=4*1024*1024
MAX_PATH_BYTES=240
GIT_TIMEOUT=30
REQUIRED_PATHS=frozenset(('README.md','AGENTS.md','pyproject.toml','robot_test_hub/__init__.py',
 'robot_test_hub/server.py','robot_test_hub/storage.py','docs/WINDOWS_SETUP.md',
 'scripts/windows/SetupHub.ps1','scripts/windows/SetupHub.cmd','scripts/windows/StartHub.cmd',
 'scripts/windows/CheckHub.cmd','scripts/windows/OpenHub.cmd','scripts/windows/StopHub.cmd',
 'tools/status_bridge/run.py','tools/status_bridge/README.md','tools/status_bridge/dependencies.lock.json',
 'tools/status_bridge/src/StatusBridge.java','tools/status_bridge/src/MarkerBridge.java','tools/status_bridge/src/SyntheticPublisher.java'))
_PUBLIC_NAMES={
 'alpha7-main.wpilog':'df79f80165f2a4577913d86a503e11069fa89037ae6ee2931d8dcbe7480cb3d5',
 'alpha7-boot-b.wpilog':'cb0624f6ba29550546fb4969cc5efcf099dc593b11a0efd32ddbc021ce88071c',
 'alpha7-dst-overlap.wpilog':'d02761e224e4c47891020e2cb932bca9a99e5a7fbe08bd853c4e0f9c52439bc1',
 'alpha7-dst-gap.wpilog':'efe65346fea3d8fc08a4bbca898b3b504f17b7fac14bec16abd131fd0087caf8'}
PUBLIC_FIXTURES={'tests/fixtures/synthetic/'+name:digest for name,digest in _PUBLIC_NAMES.items()}
PUBLIC_FIXTURES['robot_test_hub/sample_data/alpha7-main.wpilog']=_PUBLIC_NAMES['alpha7-main.wpilog']
REQUIRED_PATHS=REQUIRED_PATHS | frozenset('tests/fixtures/synthetic/'+name for name in _PUBLIC_NAMES) | {'tests/fixtures/synthetic/expected.json'}
_FORBIDDEN_PARTS={'.git','.venv','venv','__pycache__','.pytest_cache','.cache','.gradle','node_modules',
 'data','archive','partial','raw','derived','analytics','backup','backups','build','dist','private','secrets','credentials'}
_FORBIDDEN_SUFFIXES={'.wpilog','.hoot','.revlog','.dslog','.dsevents','.logdata','.part','.sqlite','.sqlite3','.db',
 '.wal','.shm','.parquet','.mp4','.mkv','.mov','.avi','.webm','.mp3','.wav','.zip','.7z','.tar','.gz',
 '.jar','.class','.pyc','.pyo','.pyd','.dll','.exe','.so','.dylib','.pem','.key','.p12','.pfx','.jks','.kdbx'}
_FORBIDDEN_NAMES={'known_hosts','authorized_keys','id_rsa','id_ed25519','credentials.json','secrets.json',
 'token.json','hub-config.json','source-config.json','launch-profile.json'}


class ReleaseError(ValueError):
    def __init__(self,code,*,cleanup='not_needed'):
        super().__init__(code);self.code=code;self.cleanup_state=cleanup


def _fail(code):raise ReleaseError(code)
def _json(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
def _sha(value):return type(value) is str and re.fullmatch('[a-f0-9]{64}',value) is not None
def _oid(value):return type(value) is str and re.fullmatch('(?:[a-f0-9]{40}|[a-f0-9]{64})',value) is not None
def _number(value,maximum):return type(value) is int and 0<=value<=maximum


def _pairs(pairs):
    value={}
    for key,item in pairs:
        if key in value:_fail('release_invalid_manifest')
        value[key]=item
    return value


def _path_name(name):
    if type(name) is not str or not name or len(name.encode('utf-8'))>MAX_PATH_BYTES or name.startswith('/') or '\\' in name:
        _fail('release_invalid_path')
    for part in name.split('/'):
        base=part.split('.',1)[0].upper()
        if (not part or part in ('.','..') or part.endswith((' ','.')) or unicodedata.normalize('NFC',part)!=part
            or any(character in '<>:"|?*' or unicodedata.category(character) in ('Cc','Cf','Cs') for character in part)
            or base in ('CON','PRN','AUX','NUL','CLOCK$','CONIN$','CONOUT$') or re.fullmatch(r'(COM|LPT)[1-9¹²³]',base)):
            _fail('release_invalid_path')
    return name


def _source_policy(path):
    _path_name(path)
    parts=path.casefold().split('/');name=parts[-1]
    if (any((part in _FORBIDDEN_PARTS and not (part=='analytics' and index==1 and parts[0]=='tools')) or part.endswith('.egg-info') for index,part in enumerate(parts))
        or name=='.env' or name.startswith('.env.') or name in _FORBIDDEN_NAMES
        or (Path(name).suffix in _FORBIDDEN_SUFFIXES and path not in PUBLIC_FIXTURES)):
        _fail('release_forbidden_source')


def _inventory_shape(items):
    if type(items) is not list or not 0<len(items)<=MAX_FILES:_fail('release_resource_limit')
    names={};files=set();total=0;previous=None
    for item in items:
        if type(item) is not dict or set(item)!={'path','size_bytes','sha256','git_mode'}:_fail('release_invalid_manifest')
        name=item['path'];_source_policy(name)
        if previous is not None and name<=previous:_fail('release_invalid_manifest')
        previous=name
        if not _number(item['size_bytes'],MAX_FILE_BYTES) or not _sha(item['sha256']) or item['git_mode'] not in ('100644','100755'):_fail('release_invalid_manifest')
        if name in PUBLIC_FIXTURES and item['sha256']!=PUBLIC_FIXTURES[name]:_fail('release_forbidden_source')
        total+=item['size_bytes']
        if total>MAX_TOTAL_BYTES:_fail('release_resource_limit')
        parts=name.split('/')
        for index in range(1,len(parts)+1):
            prefix='/'.join(parts[:index]);key=prefix.casefold()
            if key in names and names[key]!=prefix:_fail('release_invalid_path')
            names[key]=prefix
            if index<len(parts) and key in files:_fail('release_invalid_path')
        key=name.casefold()
        if key in files or any(existing.startswith(key+'/') for existing in files):_fail('release_invalid_path')
        files.add(key)
    if not REQUIRED_PATHS.issubset({item['path'] for item in items}):_fail('release_incomplete_source')
    return total


def _absolute(path):return Path(os.path.abspath(os.fspath(path)))


def _check_chain(path,*,directory=True):
    path=_absolute(path)
    for candidate in reversed((path,*path.parents)):
        try:info=candidate.lstat()
        except FileNotFoundError:_fail('release_invalid_destination')
        if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
            or not stat.S_ISDIR(info.st_mode)):_fail('release_invalid_destination')
    return path.resolve()


def _regular(path):
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
        or getattr(info,'st_nlink',1)!=1):_fail('release_invalid_archive')
    stream=path.open('rb');opened=os.fstat(stream.fileno())
    if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):
        stream.close();_fail('release_invalid_archive')
    return stream


def _digest(stream,maximum):
    digest=hashlib.sha256();size=0
    for block in iter(lambda:stream.read(65536),b''):
        size+=len(block)
        if size>maximum:_fail('release_resource_limit')
        digest.update(block)
    return digest.hexdigest(),size


def _sync_dir(path):
    if os.name!='nt':
        fd=os.open(path,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


def _git(repo,*arguments,maximum):
    env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0',GIT_NO_REPLACE_OBJECTS='1',GIT_NO_LAZY_FETCH='1')
    process=subprocess.Popen(['git','--no-optional-locks','--no-replace-objects','-C',str(repo),*arguments],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    overflow=threading.Event();errors=[]
    def stderr():
        try:
            if len(process.stderr.read(4097))>4096:overflow.set();process.kill()
        except OSError:errors.append(True)
        finally:process.stderr.close()
    watcher=threading.Thread(target=stderr,name='hub-release-git-stderr')
    expired=threading.Event()
    def timeout():
        expired.set()
        try:process.kill()
        except OSError:pass
    timer=threading.Timer(GIT_TIMEOUT,timeout)
    watcher_started=False;timer_started=False
    try:
        watcher.start();watcher_started=True
        timer.start();timer_started=True
        output=process.stdout.read(maximum+1)
        if len(output)>maximum:overflow.set();process.kill()
        process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            try:process.kill()
            except ProcessLookupError:pass
        process.wait(timeout=5)
        process.stdout.close()
        if watcher_started:watcher.join(timeout=5)
        else:process.stderr.close()
        if timer_started:timer.join(timeout=5)
        if watcher.is_alive() or timer.is_alive():raise ReleaseError('release_git_cleanup_failed',cleanup='retained')
    if overflow.is_set():_fail('release_resource_limit')
    if expired.is_set() or errors or process.returncode:_fail('release_git_failed')
    return output


def _write(path,data):
    with path.open('xb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())


def _remove_stage(stage,parent):
    # Only this exact, freshly allocated staging child is recursively removed.
    if stage.parent!=parent or not stage.name.startswith('.release-stage-') or stage.is_symlink() or stage.resolve()!=stage:
        return False
    try:shutil.rmtree(stage);return True
    except OSError:return False


def _manifest_integer(value):
    if len(value)>20:_fail('release_invalid_manifest')
    return int(value)


def _manifest(raw):
    if not 0<len(raw)<=MAX_MANIFEST_BYTES:_fail('release_resource_limit')
    value=json.loads(raw,object_pairs_hook=_pairs,parse_int=_manifest_integer,parse_constant=lambda _:_fail('release_invalid_manifest'))
    if (type(value) is not dict or set(value)!={'schema_version','tool_version','source_commit','source_tree','publication','inventory','archive'}
        or type(value['schema_version']) is not int or value['schema_version']!=1 or value['tool_version']!=TOOL_VERSION
        or value['publication']!=PUBLICATION or not _oid(value['source_commit']) or not _oid(value['source_tree'])
        or len(value['source_commit'])!=len(value['source_tree']) or raw!=_json(value)):_fail('release_invalid_manifest')
    _inventory_shape(value['inventory'])
    archive=value['archive']
    if (type(archive) is not dict or set(archive)!={'path','size_bytes','sha256'} or archive['path']!='source.zip'
        or not _number(archive['size_bytes'],MAX_ZIP_BYTES) or not _sha(archive['sha256'])):_fail('release_invalid_manifest')
    return value


def _preflight(stream,size):
    if size<22:_fail('release_invalid_archive')
    stream.seek(size-22);tail=stream.read(22)
    signature,disk,start_disk,count_disk,count,total,offset,comment=struct.unpack('<4s4H2IH',tail)
    if (signature!=b'PK\x05\x06' or disk or start_disk or count_disk!=count or not 0<count<=MAX_FILES
        or total>MAX_CENTRAL_BYTES or offset+total!=size-22 or comment):_fail('release_invalid_archive')
    stream.seek(0)
    return count,offset


def _verify_open(folder,operation=None):
    folder=_check_chain(folder)
    if {item.name for item in folder.iterdir()}!={'manifest.json','source.zip'}:_fail('release_invalid_manifest')
    with _regular(folder/'manifest.json') as stream:manifest=_manifest(stream.read(MAX_MANIFEST_BYTES+1))
    with _regular(folder/'source.zip') as stream:
        digest,size=_digest(stream,MAX_ZIP_BYTES)
        if (digest,size)!=(manifest['archive']['sha256'],manifest['archive']['size_bytes']):_fail('release_invalid_archive')
        count,central=_preflight(stream,size)
        with zipfile.ZipFile(stream,'r') as archive:
            entries=archive.infolist()
            if len(entries)!=count or len(entries)!=len(manifest['inventory']) or archive.comment:_fail('release_invalid_archive')
            offset=0
            for info,item in zip(entries,manifest['inventory']):
                mode=(stat.S_IFREG | (0o755 if item['git_mode']=='100755' else 0o644))<<16
                encoded=item['path'].encode('utf-8')
                if (info.filename!=item['path'] or info.header_offset!=offset or info.date_time!=(1980,1,1,0,0,0)
                    or info.create_system!=3 or info.external_attr!=mode or info.extra or info.comment
                    or info.compress_type!=zipfile.ZIP_STORED or info.flag_bits & ~0x800
                    or info.file_size!=item['size_bytes'] or info.compress_size!=item['size_bytes']):_fail('release_invalid_archive')
                with archive.open(info) as content:
                    if _digest(content,MAX_FILE_BYTES)!=(item['sha256'],item['size_bytes']):_fail('release_invalid_archive')
                offset+=30+len(encoded)+item['size_bytes']
            if offset!=central:_fail('release_invalid_archive')
            if operation is not None:return operation(manifest,archive)
    return dict(manifest,verified=True)


def verify(folder):
    """Verify an integrity receipt without executing any released code."""
    try:return _verify_open(folder)
    except ReleaseError:raise
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError,zipfile.BadZipFile,struct.error):
        raise ReleaseError('release_invalid_archive') from None


def build(repo,output,commit='HEAD'):
    """Read only committed local Git blobs; preserve dirty files and conflicts."""
    stage=None;partial=False
    try:
        repo=_check_chain(repo);output=_absolute(output)
        output=_check_chain(output.parent)/output.name
        if output.exists():output=_check_chain(output)
        root=Path(_git(repo,'rev-parse','--show-toplevel',maximum=4096).decode('utf-8').strip()).resolve()
        if output==root or root in output.parents or output in root.parents:_fail('release_invalid_destination')
        for ancestor in (output,*output.parents):
            if (ancestor/'catalog.sqlite3').exists() or (ancestor/'.owner.lock').exists():_fail('release_invalid_destination')
        if type(commit) is not str or not 0<len(commit)<=256 or any(ch in commit for ch in '\x00\r\n'):_fail('release_invalid_source')
        identity=_git(repo,'rev-parse','--verify','--end-of-options',commit+'^{commit}',maximum=256).decode().strip()
        tree=_git(repo,'rev-parse','--verify',identity+'^{tree}',maximum=256).decode().strip()
        if not _oid(identity) or not _oid(tree):_fail('release_invalid_source')
        raw=_git(repo,'ls-tree','-rz','-l','--full-tree',identity,maximum=MAX_TREE_BYTES)
        entries=[]
        for record in raw.split(b'\0'):
            if not record:continue
            metadata,path=record.split(b'\t',1);mode,kind,blob,size=metadata.split()
            if mode not in (b'100644',b'100755') or kind!=b'blob':_fail('release_forbidden_source')
            path=path.decode('utf-8');_source_policy(path)
            size=int(size)
            if not 0<=size<=MAX_FILE_BYTES:_fail('release_resource_limit')
            entries.append((path,mode.decode(),blob.decode(),size))
            if len(entries)>MAX_FILES:_fail('release_resource_limit')
        entries.sort()
        provisional=[dict(path=path,git_mode=mode,size_bytes=size,sha256=PUBLIC_FIXTURES.get(path,'0'*64)) for path,mode,blob,size in entries]
        _inventory_shape(provisional)
        _check_chain(output.parent)
        output.mkdir(exist_ok=True);output=_check_chain(output)
        stage=Path(tempfile.mkdtemp(prefix='.release-stage-',dir=output)).resolve()
        inventory=[]
        with zipfile.ZipFile(stage/'source.zip','w',compression=zipfile.ZIP_STORED,allowZip64=False) as archive:
            for path,mode,blob,size in entries:
                data=_git(repo,'cat-file','blob',blob,maximum=min(MAX_FILE_BYTES,size))
                if len(data)!=size:_fail('release_invalid_source')
                digest=hashlib.sha256(data).hexdigest()
                if path in PUBLIC_FIXTURES and digest!=PUBLIC_FIXTURES[path]:_fail('release_forbidden_source')
                info=zipfile.ZipInfo(path,date_time=(1980,1,1,0,0,0));info.create_system=3
                info.external_attr=(stat.S_IFREG | (0o755 if mode=='100755' else 0o644))<<16
                archive.writestr(info,data)
                inventory.append(dict(path=path,size_bytes=size,sha256=digest,git_mode=mode))
        with (stage/'source.zip').open('r+b') as stream:stream.flush();os.fsync(stream.fileno());stream.seek(0);digest,size=_digest(stream,MAX_ZIP_BYTES)
        manifest=dict(schema_version=1,tool_version=TOOL_VERSION,source_commit=identity,source_tree=tree,publication=PUBLICATION,
                      inventory=inventory,archive=dict(path='source.zip',size_bytes=size,sha256=digest))
        encoded=_json(manifest)
        if len(encoded)>MAX_MANIFEST_BYTES:_fail('release_resource_limit')
        _write(stage/'manifest.json',encoded);_sync_dir(stage);verify(stage)
        target=output/('robot-test-hub-'+identity)
        try:target.mkdir()
        except FileExistsError:
            try:existing=verify(target)
            except ReleaseError:raise ReleaseError('release_conflict') from None
            if {key:existing[key] for key in manifest}!=manifest:_fail('release_conflict')
            state='existing'
        else:
            partial=True
            for name in ('source.zip','manifest.json'):
                with (stage/name).open('rb') as inp,(target/name).open('xb') as out:
                    shutil.copyfileobj(inp,out,65536);out.flush();os.fsync(out.fileno())
                _sync_dir(target)
            verify(target);_sync_dir(output);partial=False;state='built'
        return dict(schema_version=1,state=state,source_commit=identity,source_tree=tree,release_folder=str(target),
                    inventory_count=len(inventory),archive_sha256=digest,publication=PUBLICATION)
    except ReleaseError as exc:
        if partial:exc.cleanup_state='retained'
        raise
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,zipfile.BadZipFile,subprocess.SubprocessError):
        raise ReleaseError('release_build_failed',cleanup='retained' if partial else 'not_needed') from None
    finally:
        if stage is not None and not _remove_stage(stage,output):
            raise ReleaseError('release_cleanup_incomplete',cleanup='retained') from None


def _same(path,identity,directory=False):
    try:
        info=path.lstat()
        return ((info.st_dev,info.st_ino)==identity and not stat.S_ISLNK(info.st_mode)
                and not getattr(info,'st_file_attributes',0)&0x400 and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))
    except OSError:return False


def extract(folder,destination):
    """Verify first, then exclusively create a fresh source tree; never merge."""
    files=[];directories=[];created=False
    try:
        folder=_check_chain(folder);destination=_absolute(destination)
        parent=_check_chain(destination.parent);destination=parent/destination.name
        if destination==folder or folder in destination.parents or destination in folder.parents:_fail('release_invalid_destination')
        def extraction(manifest,archive):
            nonlocal created
            destination.mkdir();created=True
            info=destination.lstat();directories.append((destination,(info.st_dev,info.st_ino)))
            known={destination}
            for item in manifest['inventory']:
                path=destination.joinpath(*item['path'].split('/'))
                chain=[];cursor=path.parent
                while cursor!=destination:chain.append(cursor);cursor=cursor.parent
                for directory in reversed(chain):
                    if directory not in known:
                        directory.mkdir();info=directory.lstat();directories.append((directory,(info.st_dev,info.st_ino)));known.add(directory)
                    _check_chain(directory)
                _check_chain(path.parent)
                with archive.open(item['path']) as inp,path.open('xb') as out:
                    info=os.fstat(out.fileno());files.append((path,(info.st_dev,info.st_ino)))
                    digest=hashlib.sha256();size=0
                    for block in iter(lambda:inp.read(65536),b''):
                        size+=len(block)
                        if size>item['size_bytes']:_fail('release_invalid_archive')
                        digest.update(block);out.write(block)
                    out.flush();os.fsync(out.fileno())
                if (size,digest.hexdigest())!=(item['size_bytes'],item['sha256']):_fail('release_invalid_archive')
                os.chmod(path,0o755 if item['git_mode']=='100755' else 0o644)
            _sync_dir(destination);_sync_dir(parent)
            return dict(schema_version=1,state='extracted',source_commit=manifest['source_commit'],source_tree=manifest['source_tree'],
                        destination=str(destination),inventory_count=len(files))
        return _verify_open(folder,extraction)
    except BaseException as exc:
        clean=True
        if created:
            for path,identity in reversed(files):
                try:
                    _check_chain(path.parent)
                    if not _same(path,identity):clean=False;continue
                    path.unlink()
                except (OSError,ReleaseError):clean=False
            for path,identity in reversed(directories):
                try:
                    if not _same(path,identity,True):clean=False;continue
                    path.rmdir()
                except OSError:clean=False
        if isinstance(exc,ReleaseError):
            if not clean:exc.cleanup_state='retained'
            raise
        raise ReleaseError('release_extract_failed',cleanup='removed' if created and clean else 'retained' if created else 'not_needed') from None


def main(argv=None):
    parser=argparse.ArgumentParser(description='Assemble or verify local committed-source integrity receipts; no signature or hardware qualification.')
    commands=parser.add_subparsers(dest='action',required=True)
    command=commands.add_parser('build');command.add_argument('--repo',type=Path,required=True);command.add_argument('--output',type=Path,required=True);command.add_argument('--commit',default='HEAD')
    command=commands.add_parser('verify');command.add_argument('--release',type=Path,required=True)
    command=commands.add_parser('extract');command.add_argument('--release',type=Path,required=True);command.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='build':result=build(args.repo,args.output,args.commit)
        elif args.action=='verify':
            receipt=verify(args.release);result=dict(schema_version=1,state='verified',source_commit=receipt['source_commit'],source_tree=receipt['source_tree'],inventory_count=len(receipt['inventory']),archive_sha256=receipt['archive']['sha256'])
        else:result=extract(args.release,args.destination)
        print(json.dumps(dict(result,success=True),sort_keys=True));return 0
    except ReleaseError as exc:
        print(json.dumps(dict(schema_version=1,success=False,error_code=exc.code,cleanup=dict(state=exc.cleanup_state)),sort_keys=True));return 1


if __name__=='__main__':raise SystemExit(main())
