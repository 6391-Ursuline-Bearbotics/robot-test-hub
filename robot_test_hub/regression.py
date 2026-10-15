"""Private offline analyzer regression cases; no robot replay or package code execution."""
from __future__ import annotations
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import stat

FORMAT = 'offline-analyzer-regression-1'
MAX_METADATA = 64 * 1024 * 1024
MAX_EXPECTATIONS = 65536
MAX_ORIGINAL = 2 * 1024 * 1024 * 1024
MAX_ORIGINAL_TOTAL = 8 * 1024 * 1024 * 1024
MAX_ORIGINALS = 16
MAX_ROWS = 50000
SHA = re.compile('[a-f0-9]{64}')
ID = re.compile('[A-Za-z0-9_-]{1,128}')
EVIDENCE_FIELDS = {'profile','source_hashes','rows','context','mapping_revision','importer_version','source_type'}
JOB_FIELDS = {'outcome','metrics','coverage','findings','unavailable','job_id','analyzer_id','analyzer_version','provenance','configuration','declaration'}
CASE_FIELDS = {'schema_version','format','framework_version','review','job_id','analyzer_id','analyzer_version','expectations','source_hashes','reference_scope','review_links','imports'}
SELECTOR_FIELDS = {'name','unit','module_id','module_position','module_index','signal'}


class RegressionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code='invalid_regression_case'):
    raise RegressionError(code)


def _finite(value):
    try:
        return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value, limit=2000):
    return type(value) is str and 0 < len(value) <= limit and '\x00' not in value


def _id(value):
    return type(value) is str and ID.fullmatch(value) is not None


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _structure(value, depth=0):
    if depth > 64:
        _fail('regression_resource_limit')
    if value is None or type(value) is bool:
        return
    if type(value) in (int,float):
        if not _finite(value):
            _fail()
    elif type(value) is str:
        if len(value) > 1024 * 1024:
            _fail('regression_resource_limit')
    elif type(value) in (list,tuple):
        if len(value) > 100000:
            _fail('regression_resource_limit')
        for item in value:
            _structure(item, depth+1)
    elif type(value) is dict:
        if len(value) > 4096:
            _fail('regression_resource_limit')
        for key,item in value.items():
            if type(key) is not str or len(key) > 512:
                _fail()
            _structure(item, depth+1)
    else:
        _fail()


def _pairs(items):
    result = {}
    for key,value in items:
        if key in result:
            _fail()
        result[key] = value
    return result


def _json_bytes(value, maximum=MAX_METADATA):
    _structure(value)
    encoded = (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
    if len(encoded) > maximum:
        _fail('regression_resource_limit')
    return encoded


def _parse(encoded):
    try:
        def integer(text):
            if len(text.lstrip('-')) > 30:
                _fail('regression_resource_limit')
            return int(text)
        def decimal(text):
            if len(text) > 128:
                _fail('regression_resource_limit')
            return float(text)
        value = json.loads(encoded,object_pairs_hook=_pairs,parse_int=integer,parse_float=decimal,parse_constant=lambda _: _fail())
        _structure(value)
        return value
    except RegressionError:
        raise
    except (ValueError,TypeError,RecursionError,OverflowError):
        _fail()


def _local(path, *, exists=True):
    path = Path(path)
    if str(path).startswith(('\\\\','//')):
        _fail('unsafe_regression_path')
    path = Path(os.path.abspath(path))
    for ancestor in [path,*path.parents]:
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            if ancestor == path and not exists:
                continue
            _fail('unsafe_regression_path')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            _fail('unsafe_regression_path')
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
            _fail('unsafe_regression_path')
    return path


def _read(path, maximum):
    path = _local(path)
    if not path.is_file():
        _fail()
    expected = path.lstat()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_nlink != 1 or (before.st_dev,before.st_ino) != (expected.st_dev,expected.st_ino):
            _fail('unsafe_regression_path')
        encoded = stream.read(maximum+1)
        after = os.fstat(stream.fileno())
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or after.st_nlink != 1:
            _fail('regression_integrity')
    if len(encoded) > maximum:
        _fail('regression_resource_limit')
    return encoded


def _write(path, encoded):
    with path.open('xb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _hash(path, maximum, *, target=None, wpilog=False):
    path = _local(path)
    expected = path.lstat()
    digest,size = hashlib.sha256(),0
    output = target.open('xb') if target is not None else None
    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if before.st_nlink != 1 or (before.st_dev,before.st_ino) != (expected.st_dev,expected.st_ino):
                _fail('unsafe_regression_path')
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                _fail('regression_resource_limit')
            first = True
            for block in iter(lambda:stream.read(1024*1024),b''):
                if first and wpilog and not block.startswith(b'WPILOG'):
                    _fail('regression_integrity')
                first = False
                size += len(block)
                if size > maximum:
                    _fail('regression_resource_limit')
                digest.update(block)
                if output is not None:
                    output.write(block)
            after = os.fstat(stream.fileno())
            if first or after.st_nlink != 1 or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or size != before.st_size:
                _fail('regression_integrity')
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        return digest.hexdigest(),size
    finally:
        if output is not None:
            output.close()


def _expectations(value):
    if type(value) is not dict or set(value) != {'schema_version','expected_outcome','checks'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        _fail('invalid_regression_expectations')
    from .analysis import OUTCOMES
    checks = value['checks']
    if value['expected_outcome'] not in OUTCOMES or type(checks) is not list or not 1 <= len(checks) <= 64:
        _fail('invalid_regression_expectations')
    for check in checks:
        if type(check) is not dict or check.get('kind') not in ('metric','finding_count'):
            _fail('invalid_regression_expectations')
        required = {'kind','selector'} if check['kind'] == 'metric' else {'kind','finding_kind'}
        if not required <= set(check) or set(check)-required-{'min','max'} or not ({'min','max'} & set(check)):
            _fail('invalid_regression_expectations')
        for key in ('min','max'):
            if key in check and (not _finite(check[key]) or check['kind']=='finding_count' and not _integer(check[key],1000000)):
                _fail('invalid_regression_expectations')
        if 'min' in check and 'max' in check and check['min'] > check['max']:
            _fail('invalid_regression_expectations')
        if check['kind'] == 'metric':
            selector = check['selector']
            if type(selector) is not dict or not {'name','unit'} <= set(selector) or set(selector)-SELECTOR_FIELDS:
                _fail('invalid_regression_expectations')
            for key,item in selector.items():
                if not (_integer(item,3) if key=='module_index' else _text(item,256)):
                    _fail('invalid_regression_expectations')
        elif not _text(check['finding_kind'],256):
            _fail('invalid_regression_expectations')
    _json_bytes(value,MAX_EXPECTATIONS)
    return value


def _validate(case,evidence,job):
    from .analysis import Evidence, Declaration, identity, OUTCOMES
    if type(case) is not dict or set(case) != CASE_FIELDS or type(case['schema_version']) is not int or case['schema_version'] != 1 or case['format'] != FORMAT:
        _fail()
    if not _text(case['framework_version'],128) or not _id(case['analyzer_id']) or not _text(case['analyzer_version'],128) or not _sha(case['job_id']):
        _fail()
    _expectations(case['expectations'])
    imports = case['imports']
    import_fields = {'job_id','artifact_sha256','profile','extractor_version','mapping_revision','dataset_sha256','manifest_sha256','source_type'}
    if type(imports) is not list or not 1 <= len(imports) <= MAX_ORIGINALS:
        _fail()
    seen_jobs = set()
    for imported in imports:
        if type(imported) is not dict or set(imported) != import_fields or any(not _sha(imported[key]) for key in ('job_id','artifact_sha256','dataset_sha256','manifest_sha256')) or any(not _text(imported[key],256) for key in ('profile','extractor_version','mapping_revision')) or imported['source_type'] not in ('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER') or imported['job_id'] in seen_jobs:
            _fail()
        seen_jobs.add(imported['job_id'])
    if type(evidence) is not dict or set(evidence) != EVIDENCE_FIELDS or type(evidence['rows']) is not list or len(evidence['rows']) > MAX_ROWS or any(type(row) is not dict for row in evidence['rows']) or type(evidence['context']) is not dict:
        _fail()
    hashes = evidence['source_hashes']
    if type(hashes) is not list or not 1 <= len(hashes) <= MAX_ORIGINALS or not all(_sha(v) for v in hashes) or len(set(hashes)) != len(hashes) or case['source_hashes'] != hashes:
        _fail()
    if any(not _text(evidence[key],256) for key in ('profile','mapping_revision','importer_version')):
        _fail()
    try:
        Evidence(**{**evidence,'rows':tuple(evidence['rows']),'source_hashes':tuple(hashes)})
    except (ValueError,TypeError):
        _fail()
    if type(job) is not dict or set(job) != JOB_FIELDS or job['outcome'] not in OUTCOMES or type(job['metrics']) is not list or len(job['metrics']) > 10000 or type(job['findings']) is not list or len(job['findings']) > 10000 or type(job['coverage']) is not dict or type(job['configuration']) is not dict or type(job['unavailable']) is not list:
        _fail()
    if any(type(item) is not dict for item in job['metrics']+job['findings']) or any(not _text(item) for item in job['unavailable']):
        _fail()
    declaration = job['declaration']
    declaration_fields = set(Declaration.__dataclass_fields__)
    if type(declaration) is not dict or set(declaration) != declaration_fields or declaration.get('analyzer_id') != case['analyzer_id'] or declaration.get('version') != case['analyzer_version']:
        _fail()
    if type(declaration['supported_profiles']) is not list or not declaration['supported_profiles'] or any(not _text(v,256) for v in declaration['supported_profiles']):
        _fail()
    if type(declaration['baseline_required']) is not bool or not _finite(declaration['minimum_coverage']) or not 0 <= declaration['minimum_coverage'] <= 1 or not _text(declaration['eligible_windows'],4096) or type(declaration['grouping_keys']) is not list or len(declaration['grouping_keys']) > 64 or any(not _text(item,256) for item in declaration['grouping_keys']):
        _fail()
    for key in ('required','optional'):
        if type(declaration[key]) is not list or len(declaration[key]) > 128:
            _fail()
        for signal in declaration[key]:
            if type(signal) is not dict or set(signal) != {'path','unit','category'} or not _text(signal['path'],2048) or not _text(signal['category'],128) or signal['unit'] is not None and not _text(signal['unit'],256):
                _fail()
    for metric in job['metrics']:
        if not {'name','value','unit'} <= set(metric) or not _text(metric['name'],256) or not _text(metric['unit'],256) or metric['value'] is not None and not _finite(metric['value']):
            _fail()
    if any(not _text(finding.get('kind'),256) for finding in job['findings']):
        _fail()
    provenance = {key:item for key,item in evidence.items() if key != 'rows'}
    provenance['rows_digest'] = identity(evidence['rows'])
    if job['provenance'] != provenance or job['job_id'] != case['job_id'] or job['analyzer_id'] != case['analyzer_id'] or job['analyzer_version'] != case['analyzer_version'] or identity([case['framework_version'],declaration,provenance,job['configuration']]) != case['job_id']:
        _fail('regression_integrity')
    review = case['review']
    allowed = {'id','revision','reviewer','rationale','disposition','job_id','finding_index','maintenance_id','validation_job_id','finding_identity','action_scope'}
    if type(review) is not dict or set(review)-allowed or not {'id','revision','job_id','finding_index','finding_identity','action_scope','reviewer','rationale','disposition'} <= set(review) or not _id(review['id']) or type(review['revision']) is not int or not 1 <= review['revision'] <= 1000000 or review['job_id'] != case['job_id'] or not _integer(review['finding_index'],len(job['findings'])-1) or review['action_scope'] != 'review_record_only':
        _fail('regression_integrity')
    if case['reference_scope'] != 'incident_evidence_only' or case['review_links'] != _review_links(review):
        _fail('regression_integrity')
    from .review import DISPOSITIONS
    if review['disposition'] not in DISPOSITIONS or not _text(review['reviewer']) or not _text(review['rationale']) or review['finding_identity'] != identity([job['job_id'],review['finding_index'],job['findings'][review['finding_index']]]):
        _fail('regression_integrity')


def _review_links(review):
    links = []
    for key,relation,state in (('maintenance_id','maintenance','unpinned'),('validation_job_id','validation_job','not_included')):
        if review.get(key) not in (None,''):
            if not _id(review[key]):
                _fail()
            links.append(dict(relation=relation,id=review[key],state=state))
    return links


def export_case(root, *, review_id, review_revision, expectations, output):
    """Capture a selected saved review using read-only archive projection."""
    root = _local(root)
    output = _local(output,exists=False)
    repository = Path(__file__).resolve().parents[1]
    if any((ancestor/'.git').exists() or (ancestor/'.git').is_symlink() for ancestor in output.parents):
        _fail('unsafe_regression_output')
    if output.exists() or any(output==base or output.is_relative_to(base) or base.is_relative_to(output) for base in (root,repository)):
        _fail('unsafe_regression_output')
    if isinstance(expectations,(str,Path)):
        expectations = _parse(_read(expectations,MAX_EXPECTATIONS))
    expectations = _expectations(expectations)
    from .regression_evidence import capture
    captured = capture(root,review_id=review_id,review_revision=review_revision)
    evidence = _parse(_json_bytes(captured['evidence']))
    job = captured['job']
    case = dict(schema_version=1,format=FORMAT,framework_version=captured['framework_version'],review=captured['review'],job_id=job['job_id'],analyzer_id=job['analyzer_id'],analyzer_version=job['analyzer_version'],expectations=expectations,source_hashes=evidence['source_hashes'],reference_scope='incident_evidence_only',review_links=_review_links(captured['review']),imports=captured['imports'])
    _validate(case,evidence,job)
    originals = captured['originals']
    if type(originals) is not list or len(originals) != len(case['source_hashes']) or {item['sha256'] for item in originals} != set(case['source_hashes']):
        _fail('regression_integrity')
    if any(not _integer(item['size_bytes'],MAX_ORIGINAL) for item in originals) or sum(item['size_bytes'] for item in originals) > MAX_ORIGINAL_TOTAL:
        _fail('regression_resource_limit')
    encoded = {'case.json':_json_bytes(case),'evidence.json':_json_bytes(evidence),'saved-job.json':_json_bytes(job)}
    output.mkdir(mode=0o700)
    (output/'originals').mkdir()
    inventory = []
    for name,data in encoded.items():
        _write(output/name,data)
        inventory.append(dict(path=name,sha256=hashlib.sha256(data).hexdigest(),size_bytes=len(data)))
    for item in originals:
        name='originals/'+item['sha256']+'.wpilog'
        sha,size = _hash(item['path'],MAX_ORIGINAL,target=output/name,wpilog=True)
        if (sha,size) != (item['sha256'],item['size_bytes']):
            _fail('regression_integrity')
        inventory.append(dict(path=name,sha256=sha,size_bytes=size))
    manifest = dict(schema_version=1,format=FORMAT,inventory=sorted(inventory,key=lambda item:item['path']))
    _write(output/'manifest.json',_json_bytes(manifest,65536))
    return verify_case(output)


def _load(folder):
    folder = _local(folder)
    manifest = _parse(_read(folder/'manifest.json',65536))
    if type(manifest) is not dict or set(manifest) != {'schema_version','format','inventory'} or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or manifest['format'] != FORMAT or type(manifest['inventory']) is not list or not 4 <= len(manifest['inventory']) <= MAX_ORIGINALS+3:
        _fail()
    paths,originals,original_files,total = [],[],[],0
    data = {}
    for item in manifest['inventory']:
        if type(item) is not dict or set(item) != {'path','sha256','size_bytes'} or not _sha(item['sha256']) or not _integer(item['size_bytes'],MAX_ORIGINAL):
            _fail()
        name = item['path']
        if name not in ('case.json','evidence.json','saved-job.json') and not (type(name) is str and re.fullmatch(r'originals/[a-f0-9]{64}\.wpilog',name)):
            _fail('unsafe_regression_path')
        if name in paths:
            _fail()
        paths.append(name)
        original = name.startswith('originals/')
        if original:
            total += item['size_bytes']
            if total > MAX_ORIGINAL_TOTAL:
                _fail('regression_resource_limit')
            sha,size = _hash(folder/name,MAX_ORIGINAL,wpilog=True)
            if name != 'originals/'+sha+'.wpilog':
                _fail('regression_integrity')
            originals.append(sha)
            original_files.append(dict(path=folder/name,sha256=sha,size_bytes=size))
        else:
            encoded = _read(folder/name,MAX_METADATA)
            sha,size = hashlib.sha256(encoded).hexdigest(),len(encoded)
            data[name] = _parse(encoded)
        if (sha,size) != (item['sha256'],item['size_bytes']):
            _fail('regression_integrity')
    if paths != sorted(paths) or not {'case.json','evidence.json','saved-job.json'} <= set(paths):
        _fail()
    expected = set(paths)|{'manifest.json','originals'}
    actual = set()
    with os.scandir(folder) as entries:
        for entry in entries:
            path = _local(folder/entry.name)
            if entry.name not in ('case.json','evidence.json','saved-job.json','manifest.json','originals'):
                _fail('regression_integrity')
            actual.add(entry.name)
            if entry.name == 'originals':
                if not path.is_dir():
                    _fail()
                with os.scandir(path) as original_entries:
                    for original_entry in original_entries:
                        candidate = _local(path/original_entry.name)
                        relative = 'originals/'+original_entry.name
                        if relative not in expected or not candidate.is_file():
                            _fail('regression_integrity')
                        actual.add(relative)
    if actual != expected:
        _fail('regression_integrity')
    _validate(data['case.json'],data['evidence.json'],data['saved-job.json'])
    if set(originals) != set(data['case.json']['source_hashes']):
        _fail('regression_integrity')
    from .regression_evidence import verify_originals, EvidenceError
    try:
        verify_originals(original_files,data['case.json']['imports'],data['evidence.json'],data['saved-job.json'])
    except EvidenceError as exc:
        raise RegressionError(exc.code) from None
    return data['case.json'],data['evidence.json'],data['saved-job.json'],total


def verify_case(folder):
    case,evidence,job,total = _load(folder)
    from .analysis import identity
    return dict(schema_version=1,verified=True,case_id=identity(case),analyzer_id=case['analyzer_id'],stored_analyzer_version=case['analyzer_version'],source_type=evidence['source_type'],original_count=len(case['source_hashes']),original_bytes=total,robot_replay_executed=False,overall_health='not_assessed',reference_scope=case['reference_scope'],review_links=case['review_links'])


def _code_identity():
    import sys
    modules = ('analysis','reports','swerve','phoenix_status','analysis_plan','regression')
    hashes = {}
    for name in modules:
        from importlib import import_module
        module = import_module('.'+name,__package__)
        path = Path(module.__file__)
        hashes[name] = hashlib.sha256(_read(path,MAX_METADATA)).hexdigest()
    return dict(python_version=platform.python_version(),python_implementation=sys.implementation.name,evaluator_module_sha256=hashes)


def run_case(folder, *, allow_analyzer_version_change=False):
    if type(allow_analyzer_version_change) is not bool:
        _fail()
    case,evidence,saved,total = _load(folder)
    from .analysis import Evidence,Runner,VERSION,identity
    from .reports import analyzers
    report = dict(schema_version=1,state='unavailable',case_id=identity(case),analyzer_id=case['analyzer_id'],stored_analyzer_version=case['analyzer_version'],current_analyzer_version=None,source_type=evidence['source_type'],robot_replay_executed=False,overall_health='not_assessed',hardware_qualified=False,reference_scope=case['reference_scope'],review_links=case['review_links'],checks=[],code='regression_analyzer_unavailable',**_code_identity())
    matches = [analyzer for analyzer in analyzers() if analyzer.declaration.analyzer_id==case['analyzer_id']]
    if len(matches) != 1 or case['framework_version'] != VERSION:
        return report
    analyzer = matches[0]
    import marshal
    if not hasattr(analyzer.evaluate,'__code__'):
        return report
    report['loaded_evaluator_code_sha256'] = hashlib.sha256(marshal.dumps(analyzer.evaluate.__code__)).hexdigest()
    report['current_analyzer_version'] = analyzer.declaration.version
    report['analyzer_version_changed'] = analyzer.declaration.version != case['analyzer_version']
    if report['analyzer_version_changed'] and not allow_analyzer_version_change:
        report['code'] = 'regression_analyzer_version_changed'
        return report
    with closing(sqlite3.connect(':memory:')) as db:
        current = Runner(db).run(analyzer,Evidence(**{**evidence,'rows':tuple(evidence['rows']),'source_hashes':tuple(evidence['source_hashes'])}),saved['configuration'])
    if type(current.get('metrics')) is not list or type(current.get('findings')) is not list or len(current['metrics']) > 10000 or len(current['findings']) > 10000 or any(type(item) is not dict for item in current['metrics']+current['findings']):
        report['code']='regression_result_invalid'
        return report
    report.update(current_job_id=current['job_id'],current_outcome=current['outcome'],expected_outcome=case['expectations']['expected_outcome'])
    checks = []
    for check in case['expectations']['checks']:
        if check['kind'] == 'metric':
            matched = [metric for metric in current['metrics'] if all(type(metric.get(key)) is type(value) and metric.get(key)==value for key,value in check['selector'].items())]
            value = matched[0].get('value') if len(matched)==1 else None
            valid = _finite(value)
        else:
            value = sum(finding.get('kind')==check['finding_kind'] for finding in current['findings'])
            valid = True
        passed = valid and ('min' not in check or value>=check['min']) and ('max' not in check or value<=check['max'])
        checks.append(dict(expectation=check,state='passed' if passed else 'failed',actual=value if valid else None))
    passed = current['outcome']==case['expectations']['expected_outcome'] and all(check['state']=='passed' for check in checks)
    report.update(state='passed' if passed else 'failed',code='regression_expectations_passed' if passed else 'regression_expectations_failed',checks=checks)
    return report


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Private local analyzer regression cases; no robot replay or hardware health qualification.')
    commands = parser.add_subparsers(dest='action',required=True)
    export = commands.add_parser('export')
    export.add_argument('--data-dir',type=Path,required=True)
    export.add_argument('--review-id',required=True)
    export.add_argument('--review-revision',type=int,required=True)
    export.add_argument('--expectations',type=Path,required=True)
    export.add_argument('--output',type=Path,required=True)
    for action in ('verify','run'):
        command=commands.add_parser(action)
        command.add_argument('--case',type=Path,required=True)
        if action=='run':
            command.add_argument('--allow-analyzer-version-change',action='store_true')
            command.add_argument('--json',action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action=='export':
            report=export_case(args.data_dir,review_id=args.review_id,review_revision=args.review_revision,expectations=args.expectations,output=args.output)
        elif args.action=='verify':
            report=verify_case(args.case)
        else:
            report=run_case(args.case,allow_analyzer_version_change=args.allow_analyzer_version_change)
        print(json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False))
        return 0 if report.get('verified') is True or report.get('state')=='passed' else 1
    except Exception as exc:
        from .regression_evidence import EvidenceError
        known = {'invalid_regression_case','regression_resource_limit','unsafe_regression_path','regression_integrity',
                 'invalid_regression_expectations','unsafe_regression_output','invalid_regression_reference',
                 'regression_evidence_unsupported','regression_evidence_integrity','regression_evidence_unavailable',
                 'regression_evidence_resource_limit'}
        code = exc.code if isinstance(exc,(RegressionError,EvidenceError)) and exc.code in known else 'regression_operation_failed'
        guidance = ('Choose a fresh private local directory outside archives and Git.' if code=='unsafe_regression_output' else
                    'Review the explicit expected outcome, unique metric selectors and inclusive bounds.' if code=='invalid_regression_expectations' else
                    'Use a supported pinned importer and input selector; this case was not executed.' if code=='regression_evidence_unsupported' else
                    'This case exceeds the supported resource bounds; preserve its originals.' if code in ('regression_resource_limit','regression_evidence_resource_limit') else
                    'Check the exact saved review ID and revision.' if code=='invalid_regression_reference' else
                    'Preserve the case and source archive; check case integrity and private local storage.')
        print(json.dumps(dict(schema_version=1,state='unavailable',code=code,guidance=guidance,robot_replay_executed=False,overall_health='not_assessed'),sort_keys=True))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
