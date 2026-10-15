"""Explicit offline cross-release rehearsal using only owned synthetic evidence.

The supplied releases are trusted executable source chosen by the operator.
Their manifest is an integrity receipt, not an authenticity signature.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

MAX_OUTPUT = 65536
CHILD_TIMEOUT = 180
_ACTIVE_CHILDREN = []
STAGES = ('verify_extract', 'older_prepare', 'newer_upgrade', 'rollback_restore', 'older_validate', 'newer_exercise')
GUIDANCE = {
    'verify_extract': 'Verify both complete source releases and select an explicit installed Python interpreter.',
    'older_prepare': 'Inspect compatibility of the older release with the bundled public synthetic sample and backup API.',
    'newer_upgrade': 'Inspect migration and retained synthetic original, note, review, job and report receipts.',
    'rollback_restore': 'Inspect verified pre-upgrade backup restoration into a fresh directory.',
    'older_validate': 'Inspect the older release against the restored pre-upgrade catalog and exact evidence receipts.',
    'newer_exercise': 'Inspect the newer isolated sample exercise; requested analytics needs its pinned installed dependency.',
}

# This driver intentionally imports no test fixtures or checkout tooling.  Its
# bytes belong to the rehearsal controller; every hub API comes from root.
DRIVER = r"""
import sys
import importlib.machinery
# -I alone still permits system-site editable finders. Keep standard loaders
# so a missing historical module cannot fall through to a source checkout.
sys.meta_path=[importlib.machinery.BuiltinImporter,importlib.machinery.FrozenImporter,importlib.machinery.PathFinder]
from pathlib import Path
root=Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0,str(root))
import hashlib,json,socket,subprocess
work=Path(sys.argv[2]).resolve(strict=True)
mode=sys.argv[3]
fixture=work/'sample.wpilog'
archive=work/('restored' if mode=='validate' else 'archive')
def require(value):
 if not value:raise ValueError('rehearsal_expectation_failed')
def forbidden(*args,**kwargs):raise RuntimeError('offline_rehearsal_network_or_child_forbidden')
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
socket.socket.bind=forbidden
socket.socket.sendto=forbidden
socket.create_connection=forbidden
subprocess.Popen=forbidden
owner=None
db=None
output={'schema_version':1,'success':False}
try:
 import robot_test_hub
 require(Path(robot_test_hub.__file__).resolve().is_relative_to(root))
 from robot_test_hub.storage import DataRootOwner,open_catalog
 from robot_test_hub.importer import Importer
 from robot_test_hub.pipeline import Pipeline
 from robot_test_hub.runs import RunCatalog
 from robot_test_hub.notebook import Notebook
 from robot_test_hub.review import ReviewStore
 from robot_test_hub.backup import Backup,verify,restore
 if mode=='restore':
  restore(work/'backups'/'pre-upgrade',archive.parent/'restored')
  output.update(success=True)
 elif mode=='exercise':
  import importlib.util
  available=importlib.util.find_spec('robot_test_hub.selftest') is not None
  if not available:
   require(sys.argv[4]=='0')
   output.update(success=True,availability='unavailable')
  else:
   from robot_test_hub.selftest import run_selftest
   result=run_selftest(analytics=sys.argv[4]=='1')
   require(result.get('success') is True and result.get('cleanup',{}).get('state')=='passed')
   output.update(success=True,availability='available')
 else:
  owner=DataRootOwner(archive)
  db=open_catalog(archive)
  schema=db.execute('PRAGMA user_version').fetchone()[0]
  def snapshot():
   tables={}
   for table in ('import_artifacts','import_jobs','analyzer_jobs','analysis_reports','annotation_revisions','review_records','run_catalog_state','run_catalog_revisions'):
    rows=[dict(row) for row in db.execute('SELECT * FROM '+table)]
    encoded=json.dumps(sorted(rows,key=lambda row:json.dumps(row,sort_keys=True)),sort_keys=True,separators=(',',':')).encode()
    tables[table]={'rows':len(rows),'sha256':hashlib.sha256(encoded).hexdigest()}
   jobs=Importer(archive,db).list_jobs()
   require(jobs and all(job['state'] in ('succeeded','succeeded_with_unsupported') for job in jobs))
   artifacts={}
   for job in jobs:
    manifest=Importer(archive,db).read_manifest(job['id'])
    for path in (manifest['raw_path'],job['dataset_path'],job['manifest_path']):
     data=(archive/path).read_bytes()
     require(len(data)<=16*1024*1024)
     artifacts[path]={'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
   require(db.execute('PRAGMA foreign_key_check').fetchone() is None)
   runs=RunCatalog(db).current()
   require(len(runs['runs'])==2)
   reports=[]
   for row in db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id'):
    body=json.loads(row['result_json'])
    require(body['overall_health']=='not_assessed' and body['source_type']=='synthetic')
    reports.append({'id':row['report_id'],'sha256':hashlib.sha256(row['result_json'].encode()).hexdigest()})
   require(len(reports)==2 and tables['analyzer_jobs']['rows']>0)
   return {'schema':schema,'tables':tables,'artifacts':artifacts,'reports':reports,'run_revision':runs['revision']}
  if mode=='prepare':
   imported=Importer(archive,db).import_file(fixture,source_type='SYNTHETIC')
   require(imported['state']=='succeeded')
   result=Pipeline(archive,db).tick()
   require(result['runs']==2 and result['reports']==2)
   note=dict(schema_version=1,event_id='release-rehearsal-note',revision=1,submitted_utc_ns='1791003630020000000',client_monotonic_ns='90000000000',event_utc_start_ns='1791003600020000000',event_utc_end_ns='1791003600020000000',when={'kind':'seconds_ago','seconds':30},clock_domain='synthetic-rehearsal',clock_quality='unverified_client',uncertainty_ms=1000,text='Invented pre-upgrade observation',tags=['synthetic'],source='practice-notebook',run_id=None)
   Notebook(archive/'catalog.sqlite3',clock_ns=lambda:1791003640020000000).save(note)
   maintenance=dict(id='release-rehearsal-review',robot_id='synthetic-6391',kind='inspection',reviewer='synthetic-rehearsal',rationale='Invented pre-upgrade inspection record',effective_utc_ns='1791003700000000000')
   ReviewStore(db).record_maintenance(maintenance)
   before=snapshot()
   (work/'before.json').write_text(json.dumps(before,sort_keys=True),encoding='utf-8')
   db.close();db=None;owner.close();owner=None
   require(Backup(archive,work/'backups').create('pre-upgrade')['state']=='complete')
   verify(work/'backups'/'pre-upgrade')
   output.update(success=True,schema=schema,receipt_sha256=hashlib.sha256((work/'before.json').read_bytes()).hexdigest(),report_count=len(before['reports']))
  elif mode in ('upgrade','validate'):
   before=json.loads((work/'before.json').read_text(encoding='utf-8'))
   actual=snapshot()
   require({k:v for k,v in actual.items() if k!='schema'}=={k:v for k,v in before.items() if k!='schema'})
   if mode=='validate':require(schema==before['schema'])
   else:
    old=Notebook(archive/'catalog.sqlite3').history('release-rehearsal-note')['annotation']
    note={k:v for k,v in old.items() if k not in ('hub_received_utc_ns','storage_state','delivery_state')}
    note.update(revision=2,text='Invented post-upgrade observation')
    Notebook(archive/'catalog.sqlite3',clock_ns=lambda:1791003740020000000).save(note,expected_previous_revision=1)
    review=ReviewStore(db).records('maintenance')[0]
    review.update(revision=2,expected_revision=1,rationale='Invented post-upgrade inspection record')
    ReviewStore(db).record_maintenance(review)
    job=Importer(archive,db).import_file(fixture,profile='release-rehearsal-unsupported-profile',source_type='SYNTHETIC')
    require(job['state']=='unsupported')
    require(db.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0]==before['tables']['import_jobs']['rows']+1)
    require(Notebook(archive/'catalog.sqlite3').history('release-rehearsal-note')['annotation']['revision']==2)
    require(ReviewStore(db).records('maintenance')[0]['revision']==2)
   output.update(success=True,schema=schema,retained_receipts=True)
  else:raise ValueError('unknown_rehearsal_phase')
 for name,module in list(sys.modules.items()):
  if name=='robot_test_hub' or name.startswith('robot_test_hub.'):
   require(getattr(module,'__file__',None) and Path(module.__file__).resolve().is_relative_to(root))
except BaseException:
 output={'schema_version':1,'success':False,'error_code':'rehearsal_child_failed'}
finally:
 try:
  if db is not None:db.close()
  if owner is not None:owner.close()
 except BaseException:output={'schema_version':1,'success':False,'error_code':'rehearsal_child_cleanup_failed'}
print(json.dumps(output,sort_keys=True,separators=(',',':')))
raise SystemExit(0 if output['success'] else 1)
"""


class RehearsalError(ValueError):
    pass


def _run(python, source, workspace, mode, analytics=False):
    """Bound output on disk, stop and reap the only owned child on every exit."""
    import os
    env = {key: value for key, value in os.environ.items() if key.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT')}
    env['PYTHONUTF8'] = '1'
    child_temp = workspace / 't'
    child_temp.mkdir(exist_ok=True)
    for name in ('TMP', 'TEMP', 'TMPDIR'):
        env[name] = str(child_temp)
    process = None
    with (workspace / 'child.stdout').open('wb') as stdout, (workspace / 'child.stderr').open('wb') as stderr:
        try:
            process = subprocess.Popen([str(python), '-I', '-c', DRIVER, str(source), str(workspace), mode, '1' if analytics else '0'], cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            _ACTIVE_CHILDREN.append((workspace, process))
            deadline = time.monotonic() + CHILD_TIMEOUT
            while process.poll() is None:
                if time.monotonic() >= deadline or (workspace / 'child.stdout').stat().st_size > MAX_OUTPUT or (workspace / 'child.stderr').stat().st_size > MAX_OUTPUT:
                    raise RehearsalError('rehearsal_child_limit')
                try:
                    process.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    pass
            if (workspace / 'child.stdout').stat().st_size > MAX_OUTPUT or (workspace / 'child.stderr').stat().st_size > MAX_OUTPUT:
                raise RehearsalError('rehearsal_child_limit')
            if process.returncode != 0:
                raise RehearsalError('rehearsal_child_failed')
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process is not None and process.poll() is not None:
                _ACTIVE_CHILDREN.remove((workspace, process))
    try:
        encoded = (workspace / 'child.stdout').read_bytes()
        if len(encoded) > MAX_OUTPUT:
            raise ValueError()
        result = json.loads(encoded)
        if not isinstance(result, dict) or type(result.get('schema_version')) is not int or result['schema_version'] != 1 or result.get('success') is not True:
            raise ValueError()
        fields = {'prepare': {'schema','receipt_sha256','report_count'}, 'upgrade': {'schema','retained_receipts'},
                  'validate': {'schema','retained_receipts'}, 'restore': set(), 'exercise': {'availability'}}
        if mode not in fields or set(result) != {'schema_version','success'} | fields[mode]:
            raise ValueError()
        if 'schema' in result:
            from .storage import SCHEMA_VERSION
            if type(result['schema']) is not int or not 1 <= result['schema'] <= SCHEMA_VERSION:
                raise ValueError()
        if mode == 'prepare' and (type(result['report_count']) is not int or result['report_count'] != 2 or
                not isinstance(result['receipt_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', result['receipt_sha256'])):
            raise ValueError()
        if mode in ('upgrade','validate') and result['retained_receipts'] is not True:
            raise ValueError()
        if mode == 'exercise' and (result['availability'] not in ('available','unavailable') or analytics and result['availability'] != 'available'):
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise RehearsalError('rehearsal_child_receipt_invalid') from None


def run_rehearsal(older, newer, *, python, analytics=False):
    """Execute operator-trusted releases against fresh, owned sample data only."""
    if type(analytics) is not bool:
        raise ValueError('analytics_must_be_boolean')
    report = dict(schema_version=1, success=False, source_type='SYNTHETIC', hardware_qualified=False,
        network_contacted=False, active_archive_opened=False, overall_health='not_assessed', analytics_requested=analytics,
        older_commit=None, newer_commit=None, schema_before=None, schema_after=None, schema_rollback=None,
        migration_exercised=False, cleanup={'state':'passed'}, stages=[])
    workspace = None
    stage = 'verify_extract'
    try:
        executable = Path(python)
        if not executable.is_absolute() or not executable.is_file():
            raise RehearsalError('explicit_installed_python_required')
        executable = executable.resolve(strict=True)
        from . import release
        old = release.verify(Path(older))
        new = release.verify(Path(newer))
        report.update(older_commit=old['source_commit'], newer_commit=new['source_commit'])
        workspace = Path(tempfile.mkdtemp(prefix='rth-rr-')).resolve()
        old_source, new_source = workspace / 'older', workspace / 'newer'
        release.extract(Path(older), old_source)
        release.extract(Path(newer), new_source)
        from .selftest import _sample
        (workspace / 'sample.wpilog').write_bytes(_sample())
        def passed(identity):
            report['stages'].append(dict(id=identity, state='passed', code='rehearsal_'+identity+'_passed', guidance='Disposable synthetic evidence matched the expected release receipts.'))
        passed(stage)
        stage = 'older_prepare'
        result = _run(executable, old_source, workspace, 'prepare')
        report['schema_before'] = result['schema']
        report['preupgrade_receipt_sha256'] = result['receipt_sha256']
        passed(stage)
        stage = 'newer_upgrade'
        result = _run(executable, new_source, workspace, 'upgrade')
        report['schema_after'] = result['schema']
        report['migration_exercised'] = report['schema_before'] != report['schema_after']
        passed(stage)
        stage = 'rollback_restore'
        _run(executable, new_source, workspace, 'restore')
        passed(stage)
        stage = 'older_validate'
        result = _run(executable, old_source, workspace, 'validate')
        report['schema_rollback'] = result['schema']
        passed(stage)
        stage = 'newer_exercise'
        result = _run(executable, new_source, workspace, 'exercise', analytics)
        if result.get('availability') == 'unavailable':
            report['stages'].append(dict(id=stage,state='unavailable',code='release_selftest_unavailable',guidance='This release predates the isolated sample exercise; the cross-release evidence driver still ran.'))
        else:
            passed(stage)
    except BaseException:
        report['stages'].append(dict(id=stage,state='failed',code='rehearsal_'+stage+'_failed',guidance=GUIDANCE[stage]))
    finally:
        if workspace is not None and any(root == workspace for root, child in _ACTIVE_CHILDREN):
            report['cleanup'] = {'state':'failed'}
        elif workspace is not None:
            try:
                shutil.rmtree(workspace)
            except BaseException:
                report['cleanup'] = {'state':'failed'}
    completed = {item['id'] for item in report['stages']}
    for identity in STAGES:
        if identity not in completed:
            report['stages'].append(dict(id=identity,state='unavailable',code='dependency_failed',guidance='Resolve the preceding failed stage before repeating the rehearsal.'))
    report['success'] = report['cleanup']['state'] == 'passed' and all(item['state']=='passed' or item['id']=='newer_exercise' and item['code']=='release_selftest_unavailable' for item in report['stages'])
    return report


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Execute trusted source releases with disposable synthetic evidence; manifests are not authenticity signatures.')
    parser.add_argument('--older', required=True, type=Path)
    parser.add_argument('--newer', required=True, type=Path)
    parser.add_argument('--python', required=True, type=Path)
    parser.add_argument('--analytics', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = run_rehearsal(args.older, args.newer, python=args.python, analytics=args.analytics)
    if args.json:
        print(json.dumps(report,sort_keys=True,separators=(',',':')))
    else:
        print('SYNTHETIC offline release rehearsal: '+('passed' if report['success'] else 'failed'))
        print('Schemas: '+str(report['schema_before'])+' → '+str(report['schema_after'])+'; rollback '+str(report['schema_rollback'])+'. Migration exercised: '+str(report['migration_exercised']))
        for stage in report['stages']:
            print(stage['id']+': '+stage['state']+' — '+stage['guidance'])
        print('Cleanup: '+report['cleanup']['state']+'. Hardware health is not assessed.')
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
