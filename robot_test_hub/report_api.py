"""Read-only immutable report receipts and bounded SQLite trace projections."""
import base64
import hashlib
import json
import re
import sqlite3

MAX_RESULT_BYTES=64*1024*1024
MAX_RESPONSE_BYTES=4*1024*1024
RUN_FILTER="CASE WHEN json_valid(result_json) THEN json_extract(result_json,'$.run_id') END=?"


def _id(value):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',value):
        raise ValueError('invalid_report_identity')
    return value


def _integer(value,minimum,maximum):
    if type(value) is not str or not re.fullmatch('0|[1-9][0-9]{0,18}',value):raise ValueError('invalid_report_query')
    number=int(value)
    if not minimum<=number<=maximum:raise ValueError('invalid_report_query')
    return number


def _json(value):
    try:return json.loads(value,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError,TypeError,RecursionError):raise ValueError('malformed_report') from None


def _bounded(value,maximum=MAX_RESPONSE_BYTES):
    try:encoded=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
    except (ValueError,TypeError,OverflowError):raise ValueError('malformed_report') from None
    if len(encoded)>maximum:raise ValueError('oversized_report_response')
    return value


def _receipt(db,run,report):
    _id(run);_id(report)
    row=db.execute('SELECT json_valid(result_json),length(CAST(result_json AS BLOB)),'
        "CASE WHEN json_valid(result_json) THEN json_extract(result_json,'$.run_id') END FROM analysis_reports WHERE report_id=?",(report,)).fetchone()
    if row is None:raise KeyError('report_not_found')
    if row[0]!=1:raise ValueError('malformed_report')
    if row[2]!=run:raise KeyError('report_not_found')
    if not 0<row[1]<=MAX_RESULT_BYTES:raise ValueError('oversized_report')
    # Hash original encoded bytes in bounded chunks without loading trace arrays.
    digest=hashlib.sha256()
    for offset in range(0,row[1],65536):
        block=db.execute('SELECT substr(CAST(result_json AS BLOB),?,65536) FROM analysis_reports WHERE report_id=?',(offset+1,report)).fetchone()[0]
        digest.update(block)
    return digest.hexdigest()


def _checks(db,report):
    valid=db.execute("SELECT json_type(result_json,'$.checks') FROM analysis_reports WHERE report_id=?",(report,)).fetchone()
    if valid is None or valid[0]!='array':raise ValueError('malformed_report')
    malformed=db.execute("SELECT 1 FROM analysis_reports r,json_each(r.result_json,'$.checks') c WHERE r.report_id=? AND (json_type(c.value,'$.coverage') IS NOT 'object' OR json_type(c.value,'$.provenance') IS NOT 'object' OR json_type(c.value,'$.findings') IS NOT 'array' OR json_type(c.value,'$.metrics') IS NOT 'array' OR json_type(c.value,'$.unavailable') IS NOT 'array') LIMIT 1",(report,)).fetchone()
    if malformed:raise ValueError('malformed_report')
    rows=db.execute("SELECT c.key,c.type,json_extract(c.value,'$.analyzer_id'),json_extract(c.value,'$.analyzer_version'),json_extract(c.value,'$.job_id'),json_extract(c.value,'$.outcome') FROM analysis_reports r,json_each(r.result_json,'$.checks') c WHERE r.report_id=? LIMIT 17",(report,)).fetchall()
    if not rows or len(rows)>16:raise ValueError('unsupported_report_checks')
    seen=set()
    for row in rows:
        if row[1]!='object' or type(row[0]) is not int:raise ValueError('malformed_report')
        _id(row[2]);_id(row[3]);_id(row[4])
        if row[2] in seen or row[5] not in ('finding','evaluated_no_finding','insufficient_data','unsupported','failed'):
            raise ValueError('malformed_report')
        seen.add(row[2])
    return rows


def _modules(db,report,index):
    rows=db.execute("SELECT m.key,m.type,json_extract(m.value,'$.module_id'),json_extract(m.value,'$.module_position'),json_type(m.value,'$.evidence_trace'),json_array_length(m.value,'$.evidence_trace') FROM analysis_reports r,json_each(r.result_json,?) m WHERE r.report_id=? LIMIT 17",(f'$.checks[{index}].coverage.modules',report)).fetchall()
    if len(rows)>16:raise ValueError('unsupported_report_modules')
    seen=set()
    for row in rows:
        if row[1]!='object' or type(row[0]) is not int or row[4]!='array':raise ValueError('malformed_report')
        _id(row[2]);_id(row[3])
        if row[2] in seen:raise ValueError('malformed_report')
        if not 0<=row[5]<=50000:raise ValueError('oversized_report_trace')
        seen.add(row[2])
    return rows


def _trace(db,report,check,module,offset,limit):
    path=f'$.checks[{check}].coverage.modules[{module}].evidence_trace'
    rows=db.execute('SELECT t.type,CASE WHEN length(CAST(t.value AS BLOB))<=16384 THEN t.value END FROM analysis_reports r,json_each(r.result_json,?) t WHERE r.report_id=? AND t.key>=? AND t.key<? ORDER BY t.key LIMIT ?',(path,report,offset,offset+limit,limit)).fetchall()
    output=[]
    for kind,value in rows:
        if kind!='object' or value is None:raise ValueError('malformed_report_trace')
        output.append(_json(value))
    return output


def detail(db,run,report):
    digest=_receipt(db,run,report)
    # Remove all checks before the top-level document reaches Python.
    raw=db.execute("SELECT json_remove(result_json,'$.checks') FROM analysis_reports WHERE report_id=?",(report,)).fetchone()[0]
    if len(raw.encode())>65536:raise ValueError('oversized_report_metadata')
    document=_json(raw)
    if not isinstance(document,dict) or document.get('report_id')!=report:raise ValueError('malformed_report')
    document['checks']=[]
    from .reports import public_check
    for check in _checks(db,report):
        index=check[0];modules=_modules(db,report,index)
        paths=[f'$.coverage.modules[{m[0]}].evidence_trace' for m in modules]
        paths+=['$.provenance.context.review_comparison_history','$.provenance.context.maintenance_references',
                '$.provenance.context.approved_baseline.comparison_history','$.provenance.context.approved_baseline.job_references']
        # Findings may contain one interval per trace row. Project those in SQL
        # too, then retain only their bounded display prefix.
        findings=db.execute("SELECT f.key,f.type FROM analysis_reports r,json_each(r.result_json,?) f WHERE r.report_id=? LIMIT 65",(f'$.checks[{index}].findings',report)).fetchall()
        if len(findings)>64 or any(f[1]!='object' or type(f[0]) is not int for f in findings):raise ValueError('oversized_report_findings')
        for f in findings:paths.extend([f'$.findings[{f[0]}].intervals_ns',f'$.findings[{f[0]}].source_references'])
        sql='SELECT json_remove(json_extract(result_json,?),'+','.join('?' for _ in paths)+') FROM analysis_reports WHERE report_id=?'
        raw=db.execute(sql,(f'$.checks[{index}]',*paths,report)).fetchone()[0]
        if len(raw.encode())>1024*1024:raise ValueError('oversized_report_metadata')
        value=_json(raw)
        for module in modules:
            target=value['coverage']['modules'][module[0]]
            target.update(evidence_trace=_trace(db,report,index,module[0],0,500),evidence_trace_total=module[5],evidence_trace_truncated=module[5]>500)
        for f in findings:
            for key in ('intervals_ns','source_references'):
                path=f'$.checks[{index}].findings[{f[0]}].{key}'
                count=db.execute('SELECT json_array_length(result_json,?) FROM analysis_reports WHERE report_id=?',(path,report)).fetchone()[0]
                if count is None:continue
                rows=db.execute('SELECT v.type,CASE WHEN length(CAST(v.value AS BLOB))<=16384 THEN v.value END FROM analysis_reports r,json_each(r.result_json,?) v WHERE r.report_id=? AND v.key<500 ORDER BY v.key LIMIT 500',(path,report)).fetchall()
                if any(v is None for _,v in rows):raise ValueError('oversized_report_finding')
                target=value['findings'][f[0]]
                target[key]=[_json(v) if t in ('object','array') else v for t,v in rows]
                target[key+'_total']=count;target[key+'_truncated']=count>500
        # Context projection is shared, while original SQL count receipts remain.
        original=[(m['evidence_trace_total'],m['evidence_trace_truncated']) for m in value.get('coverage',{}).get('modules',[])]
        finding_counts=[{k:f[k] for k in ('intervals_ns_total','intervals_ns_truncated','source_references_total','source_references_truncated') if k in f} for f in value.get('findings',[])]
        value=public_check(value)
        for module,(total,truncated) in zip(value.get('coverage',{}).get('modules',[]),original):
            module.update(evidence_trace_total=total,evidence_trace_truncated=truncated)
        for finding,counts in zip(value.get('findings',[]),finding_counts):finding.update(counts)
        document['checks'].append(value)
    return _bounded({'schema_version':1,'run_id':run,'report':document,'result_sha256':digest})


def traces(db,run,report,query):
    if set(query)-{'analyzer_id','module_id','offset','limit'} or not {'analyzer_id','module_id'}<=set(query):raise ValueError('invalid_report_query')
    analyzer,module=_id(query['analyzer_id']),_id(query['module_id'])
    offset=_integer(query.get('offset','0'),0,50000);limit=_integer(query.get('limit','500'),1,500)
    digest=_receipt(db,run,report)
    checks=[c for c in _checks(db,report) if c[2]==analyzer]
    if len(checks)!=1:raise KeyError('analyzer_not_found')
    check=checks[0][0];modules=[m for m in _modules(db,report,check) if m[2]==module]
    if len(modules)!=1:raise KeyError('module_not_found')
    found=modules[0];total=found[5]
    if offset>total:raise ValueError('invalid_trace_offset')
    items=_trace(db,report,check,found[0],offset,limit)
    return _bounded(dict(schema_version=1,run_id=run,report_id=report,analyzer_id=analyzer,module_id=module,
        module_position=found[3],offset=offset,limit=limit,total=total,returned=len(items),
        next_offset=offset+len(items) if offset+len(items)<total else None,evidence_trace=items,result_sha256=digest))


def _cursor(value):
    data=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    return base64.urlsafe_b64encode(data+hashlib.sha256(data).digest()).decode().rstrip('=')


def _decode_cursor(text,run):
    try:
        if type(text) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,1024}',text):raise ValueError()
        raw=base64.b64decode(text+'='*((-len(text))%4),altchars=b'-_',validate=True)
        data,checksum=raw[:-32],raw[-32:]
        if hashlib.sha256(data).digest()!=checksum:raise ValueError()
        value=_json(data)
        if set(value)!={'version','run','highwater','boundary','direction'} or type(value['version']) is not int or value['version']!=1 or value['run']!=run:raise ValueError()
        if type(value['highwater']) is not int or not 0<=value['highwater']<(1<<63):raise ValueError()
        if value['direction'] not in ('older','newer'):raise ValueError()
        boundary=value['boundary']
        if boundary is not None:
            if type(boundary) is not list or len(boundary)!=2 or type(boundary[0]) is not int or not 0<=boundary[0]<(1<<63):raise ValueError()
            _id(boundary[1])
        elif value['direction']!='older':raise ValueError()
        if _cursor(value)!=text:raise ValueError()
        return value
    except (ValueError,TypeError,KeyError):raise ValueError('invalid_report_cursor') from None


def history(db,run,query):
    _id(run)
    if set(query)-{'limit','cursor'}:raise ValueError('invalid_report_query')
    limit=_integer(query.get('limit','20'),1,50)
    highwater=db.execute('SELECT COALESCE(MAX(rowid),0) FROM analysis_reports').fetchone()[0]
    state=_decode_cursor(query['cursor'],run) if 'cursor' in query else dict(version=1,run=run,highwater=highwater,boundary=None,direction='older')
    if state['highwater']>highwater:raise ValueError('invalid_report_cursor')
    where=RUN_FILTER+' AND rowid<=?';parameters=[run,state['highwater']]
    total=db.execute('SELECT COUNT(*) FROM analysis_reports WHERE '+where,parameters).fetchone()[0]
    if not total:raise KeyError('report_history_not_found')
    direction=state['direction'];boundary=state['boundary']
    if boundary is not None:
        operator='<' if direction=='older' else '>'
        where+=f' AND (CAST(created_utc_ns AS INTEGER),report_id){operator}(?,?)';parameters+=boundary
    order='DESC' if direction=='older' else 'ASC'
    rows=db.execute('SELECT report_id,created_utc_ns,length(CAST(result_json AS BLOB)) '
        'FROM analysis_reports WHERE '+where+f' ORDER BY CAST(created_utc_ns AS INTEGER) {order},report_id {order} LIMIT ?',(*parameters,limit)).fetchall()
    if direction=='newer':rows.reverse()
    items=[]
    for report,created,size in rows:
        _id(report);stamp=_integer(created,0,(1<<63)-1)
        if not 0<size<=MAX_RESULT_BYTES:raise ValueError('oversized_report')
        raw=db.execute("SELECT json_object('source_type',json_extract(result_json,'$.source_type'),'evaluated_checks',json_extract(result_json,'$.evaluated_checks'),'unavailable_checks',json_extract(result_json,'$.unavailable_checks'),'finding_count',json_extract(result_json,'$.finding_count'),'overall_health',json_extract(result_json,'$.overall_health')) FROM analysis_reports WHERE report_id=?",(report,)).fetchone()[0]
        if len(raw.encode())>65536:raise ValueError('oversized_report_metadata')
        value=_json(raw)
        if value['source_type'] not in ('real','simulation','synthetic','historical') or value['overall_health']!='not_assessed':raise ValueError('malformed_report')
        for key in ('evaluated_checks','unavailable_checks','finding_count'):
            if type(value[key]) is not int or not 0<=value[key]<=100000:raise ValueError('malformed_report')
        value.update(report_id=report,created_utc_ns=str(stamp),checks=[dict(analyzer_id=c[2],analyzer_version=c[3],job_id=c[4],outcome=c[5]) for c in _checks(db,report)])
        items.append(value)
    def token(boundary=None,direction='older'):return _cursor({**state,'boundary':boundary,'direction':direction})
    first=token();previous=next_page=None
    if items:
        top=[int(items[0]['created_utc_ns']),items[0]['report_id']];bottom=[int(items[-1]['created_utc_ns']),items[-1]['report_id']]
        common=RUN_FILTER+' AND rowid<=? AND (CAST(created_utc_ns AS INTEGER),report_id)'
        if db.execute('SELECT 1 FROM analysis_reports WHERE '+common+'>(?,?) LIMIT 1',(run,state['highwater'],*top)).fetchone():previous=token(top,'newer')
        if db.execute('SELECT 1 FROM analysis_reports WHERE '+common+'<(?,?) LIMIT 1',(run,state['highwater'],*bottom)).fetchone():next_page=token(bottom)
    snapshot=hashlib.sha256(json.dumps([run,state['highwater']],separators=(',',':')).encode()).hexdigest()
    return _bounded(dict(schema_version=1,run_id=run,items=items,total=total,returned=len(items),limit=limit,
        next_cursor=next_page,previous_cursor=previous,first_cursor=first,snapshot_id=snapshot))


def _get(db,path,query):
    parts=path.split('/')
    if len(parts) not in (6,7,8) or parts[:4]!=['','api','v1','runs'] or parts[5]!='reports':raise KeyError('route_not_found')
    run=parts[4]
    if len(parts)==6:return history(db,run,query)
    if len(parts)==7 and not parts[6]:raise ValueError('invalid_report_identity')
    if len(parts)==7:
        if query:raise ValueError('invalid_report_query')
        return detail(db,run,parts[6])
    if len(parts)==8 and parts[7]=='traces':return traces(db,run,parts[6],query)
    raise KeyError('route_not_found')


def get(db,path,query):
    try:return _get(db,path,query)
    except sqlite3.Error:raise ValueError('malformed_report') from None
