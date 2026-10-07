"""Conditional saved-context association using pinned, pre-split imported clocks."""
from bisect import bisect_right
from decimal import Decimal, ROUND_CEILING
from fractions import Fraction
import copy
import json
import re

MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_ANCHORS = 100000
MAX_PIECES = 1000
MAX_CANDIDATES = 100
MAX_SELECTED_FRAMES = 10000
MAX_RUNS = 10000
MAX_RUN_WINDOWS = 100000


class ContextError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _id(value):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
        raise ContextError('invalid_context_identity')
    return value


def _digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        raise ContextError('invalid_catalog_revision')
    return value


def _ns(value, *, signed=False):
    pattern = '-?(?:0|[1-9][0-9]{0,18})' if signed else '0|[1-9][0-9]{0,18}'
    if type(value) is not str or not re.fullmatch(pattern,value) or value=='-0':
        raise ContextError('invalid_context_nanoseconds')
    number=int(value)
    if not (-(1<<63) if signed else 0)<=number<(1<<63):
        raise ContextError('context_interval_out_of_range')
    return number


def _bound(number):
    if not -(1<<63)<=number<(1<<63):
        raise ContextError('context_interval_out_of_range')
    return number


def _loads(value):
    def pairs(items):
        result={}
        for key,item in items:
            if key in result:raise ContextError('invalid_catalog_evidence',409)
            result[key]=item
        return result
    def constant(_):raise ContextError('invalid_catalog_evidence',409)
    try:return json.loads(value,object_pairs_hook=pairs,parse_constant=constant)
    except (ValueError,TypeError,RecursionError):
        raise ContextError('invalid_catalog_evidence',409) from None


def _domain(value):
    if type(value) is str and value.startswith('unknown-boot:'):
        _id(value[len('unknown-boot:'):])
        return value
    return _id(value)


def validate_catalog(document, revision):
    """Validate stored pieces as-is; never rebuild or merge clock policy splits."""
    _digest(revision)
    if (not isinstance(document,dict) or document.get('revision')!=revision
            or document.get('version')!='runs-1'):
        raise ContextError('invalid_catalog_evidence',409)
    runs=document.get('runs');mappings=document.get('mappings')
    if (not isinstance(runs,list) or len(runs)>MAX_RUNS or not isinstance(mappings,list)
            or len(mappings)>MAX_PIECES):
        raise ContextError('oversized_catalog',413)
    if _ns(document.get('max_gap_ns'))<=0:
        raise ContextError('invalid_catalog_evidence',409)
    anchors=pieces=run_windows=0
    domains=set();run_ids=set()
    for mapping in mappings:
        if not isinstance(mapping,dict) or mapping.get('revision')!=revision:
            raise ContextError('invalid_catalog_evidence',409)
        domain=(_id(mapping.get('robot_id')),_domain(mapping.get('boot_id')))
        if domain in domains:raise ContextError('duplicate_catalog_clock',409)
        domains.add(domain)
        raw_pieces=mapping.get('pieces')
        if not isinstance(raw_pieces,list):raise ContextError('invalid_catalog_evidence',409)
        pieces+=len(raw_pieces)
        if pieces>MAX_PIECES:raise ContextError('oversized_catalog',413)
        prior_end=-1
        for piece in raw_pieces:
            if (not isinstance(piece,dict) or piece.get('reason') not in
                    ('first_anchor','after_invalid_epoch','anchor_gap','utc_discontinuity')):
                raise ContextError('invalid_catalog_evidence',409)
            raw=piece.get('anchors')
            if not isinstance(raw,list) or not raw:raise ContextError('invalid_catalog_evidence',409)
            anchors+=len(raw)
            if anchors>MAX_ANCHORS:raise ContextError('oversized_catalog',413)
            previous=None
            for anchor in raw:
                if not isinstance(anchor,dict):raise ContextError('invalid_catalog_evidence',409)
                robot=_ns(anchor.get('robot_ns'));utc=_ns(anchor.get('utc_ns'));_ns(anchor.get('uncertainty_ns'))
                if utc<=0 or previous is not None and (robot<=previous[0] or utc<=previous[1]):
                    raise ContextError('invalid_catalog_evidence',409)
                previous=robot,utc
            first=_ns(piece.get('start_ns'));last=_ns(piece.get('end_ns'))
            if first!=_ns(raw[0]['robot_ns']) or last!=previous[0] or first<=prior_end:
                raise ContextError('invalid_catalog_evidence',409)
            prior_end=last
    for run in runs:
        if not isinstance(run,dict):raise ContextError('invalid_catalog_evidence',409)
        identity=_id(run.get('run_id'))
        if identity in run_ids:raise ContextError('duplicate_catalog_run',409)
        run_ids.add(identity)
        _id(run.get('robot_id'));_domain(run.get('boot_id'))
        if type(run.get('known_boot')) is not bool or run.get('mapping_revision')!=revision:
            raise ContextError('invalid_catalog_evidence',409)
        start=_ns(run.get('start_monotonic_ns'));end=_ns(run.get('end_monotonic_ns'))
        if end<start:raise ContextError('invalid_catalog_evidence',409)
        if (type(run.get('start_complete')) is not bool or type(run.get('end_complete')) is not bool
                or run.get('completeness') not in ('complete','incomplete')
                or (run['completeness']=='complete')!=(run['start_complete'] and run['end_complete'])):
            raise ContextError('invalid_catalog_evidence',409)
        phases=run.get('phases');intervals=run.get('utc_intervals')
        if not isinstance(phases,list) or not isinstance(intervals,list):
            raise ContextError('invalid_catalog_evidence',409)
        run_windows+=len(phases)+len(intervals)
        if run_windows>MAX_RUN_WINDOWS:raise ContextError('oversized_catalog',413)
        for phase in phases:
            if (not isinstance(phase,dict) or _ns(phase.get('start_monotonic_ns'))<start
                    or _ns(phase.get('end_monotonic_ns'))>end
                    or _ns(phase['end_monotonic_ns'])<_ns(phase['start_monotonic_ns'])):
                raise ContextError('invalid_catalog_evidence',409)
            mode=phase.get('mode')
            if type(mode) is not str or not re.fullmatch('[A-Za-z_]{1,32}',mode):
                raise ContextError('invalid_catalog_evidence',409)
        for interval in intervals:
            if not isinstance(interval,dict):raise ContextError('invalid_catalog_evidence',409)
            a=_ns(interval.get('robot_start_ns'));b=_ns(interval.get('robot_end_ns'))
            u=_ns(interval.get('utc_start_ns'));v=_ns(interval.get('utc_end_ns'))
            _ns(interval.get('uncertainty_ns'))
            if not start<=a<=b<=end or v<u:raise ContextError('invalid_catalog_evidence',409)
        if (run.get('runtime_mode') not in ('REAL','SIM','REPLAY','UNKNOWN','unknown')
                or run.get('end_reason') not in ('recording_gap','unknown_state','run_identity_change','disabled','end_of_recording')
                or run.get('wall_clock_quality') not in ('anchored','partial','unavailable')):
            raise ContextError('invalid_catalog_evidence',409)
        sources=run.get('source_types');segments=run.get('segment_ids')
        if (not isinstance(sources,list) or len(sources)>100 or any(type(value) is not str
                or value!='unknown' and not re.fullmatch('[A-Z_]{1,64}',value) for value in sources)
                or not isinstance(segments,list) or len(segments)>10000):
            raise ContextError('invalid_catalog_evidence',409)
        for segment in segments:_id(segment)
    return document


def load_context(notebook, revision, specification):
    """One SQLite snapshot pins both the historical catalog and exact saved note."""
    _digest(revision)
    if not isinstance(specification,dict):raise ContextError('invalid_context_fields')
    kind=specification.get('kind')
    if kind=='note':
        if set(specification)!={'kind','event_id','note_revision'}:raise ContextError('invalid_context_fields')
        event=_id(specification['event_id']);note_revision=specification['note_revision']
        if type(note_revision) is not int or not 1<=note_revision<=1000000:
            raise ContextError('invalid_note_revision')
    elif kind=='run':
        if set(specification)!={'kind','run_id'}:raise ContextError('invalid_context_fields')
        _id(specification['run_id'])
    else:raise ContextError('invalid_context_kind')
    with notebook._connection() as db:
        db.execute('BEGIN')
        row=db.execute('SELECT CASE WHEN length(CAST(document AS BLOB))<=? THEN document ELSE NULL END AS document '
            'FROM run_catalog_revisions WHERE id=?',(MAX_CATALOG_BYTES,revision)).fetchone()
        if row is None:raise ContextError('catalog_revision_not_found',404)
        if row['document'] is None:raise ContextError('oversized_catalog',413)
        document=validate_catalog(_loads(row['document']),revision)
        if kind=='note':
            row=db.execute('SELECT * FROM annotation_revisions WHERE event_id=? AND revision=?',
                (event,note_revision)).fetchone()
            if row is None:raise ContextError('annotation_revision_not_found',404)
            context=dict(kind='note',annotation=notebook._annotation(row))
        else:
            run=next((run for run in document['runs'] if run['run_id']==specification['run_id']),None)
            if run is None:raise ContextError('run_not_found',404)
            keys=('run_id','robot_id','boot_id','known_boot','mapping_revision','runtime_mode',
                  'source_types','start_monotonic_ns','end_monotonic_ns','start_complete','end_complete',
                  'completeness','end_reason','phases','utc_intervals','wall_clock_quality','segment_ids')
            context=dict(kind='run',run=copy.deepcopy({key:run[key] for key in keys if key in run}))
            context['run']['phases']=[{key:phase[key] for key in
                ('mode','start_monotonic_ns','end_monotonic_ns')} for phase in run['phases']]
            context['run']['utc_intervals']=[{key:interval[key] for key in
                ('robot_start_ns','robot_end_ns','utc_start_ns','utc_end_ns','uncertainty_ns')}
                for interval in run['utc_intervals']]
    return document,context


def _floor(value):return value.numerator//value.denominator
def _ceil(value):return -((-value.numerator)//value.denominator)


def _inverse(anchors,utc):
    index=bisect_right(anchors,utc,key=lambda anchor:int(anchor['utc_ns']))-1
    if index==len(anchors)-1:return Fraction(int(anchors[index]['robot_ns']))
    left,right=anchors[index:index+2]
    a,b=int(left['utc_ns']),int(right['utc_ns'])
    return Fraction(int(left['robot_ns']))+Fraction(utc-a,b-a)*(int(right['robot_ns'])-int(left['robot_ns']))


def _uncovered(start,end,coverage):
    if start==end:
        return [] if any(a<=start<=b for a,b in coverage) else [[str(start),str(end)]]
    cursor=start;gaps=[]
    for a,b in sorted(coverage):
        a,b=max(start,a),min(end,b)
        if a>b:continue
        if a>cursor:gaps.append([str(cursor),str(a)])
        cursor=max(cursor,b)
    if cursor<end:gaps.append([str(cursor),str(end)])
    return gaps


def invert_note(mapping,start,end,uncertainty):
    """Exact conservative inversion, keeping original piece boundaries separate."""
    a,b=_bound(start-uncertainty),_bound(end+uncertainty)
    candidates=[];guaranteed=[]
    for index,piece in enumerate(mapping['pieces']):
        anchors=piece['anchors']
        error=max(int(anchor['uncertainty_ns']) for anchor in anchors)+(1 if len(anchors)>1 else 0)
        u,v=int(anchors[0]['utc_ns']),int(anchors[-1]['utc_ns'])
        low,high=_bound(a-error),_bound(b+error)
        if len(anchors)==1:
            if high<u or low>u:continue
            first=last=int(anchors[0]['robot_ns'])
            complete=a==b==u and error==0
            if error==0:guaranteed.append((u,u))
        else:
            low,high=max(u,low),min(v,high)
            if low>high:continue
            first=max(int(anchors[0]['robot_ns']),_floor(_inverse(anchors,low)))
            last=min(int(anchors[-1]['robot_ns']),_ceil(_inverse(anchors,high)))
            covered_start,covered_end=_bound(u+error),_bound(v-error)
            complete=covered_start<=a<=b<=covered_end
            if covered_start<=covered_end:guaranteed.append((covered_start,covered_end))
        candidates.append(dict(clock_piece=index,start_robot_ns=str(first),end_robot_ns=str(last),
            clock_uncertainty_utc_ns=str(error),clock_coverage='complete' if complete else 'partial'))
        if len(candidates)>MAX_CANDIDATES:raise ContextError('oversized_context_candidates',413)
    return candidates,_uncovered(a,b,guaranteed)


def associate_context(video,catalog,context,selection_bound):
    domain=video.document();robot,boot=domain['robot_id'],domain['boot_id']
    kind=context['kind']
    result=dict(time_basis='saved_note_utc_via_imported_epoch_anchors' if kind=='note' else 'saved_run_robot_interval',
        context=context,state='unavailable',reason='clock_mapping_unavailable',note_uncertainty_ns=None,
        unmapped_utc_intervals=[],candidates=[],qualification='candidate_context_association',measured_camera_alignment=False)
    if kind=='run':
        run=context['run']
        if run['known_boot'] is not True:
            result['reason']='run_boot_unknown';return result
        if (run['robot_id'],run['boot_id'])!=(robot,boot):
            result['reason']='run_clock_domain_mismatch';return result
        complete=run['completeness']=='complete'
        candidates=[dict(clock_piece=None,start_robot_ns=run['start_monotonic_ns'],end_robot_ns=run['end_monotonic_ns'],
            clock_uncertainty_utc_ns=None,clock_coverage='complete' if complete else 'partial')]
        event=None;run_id=run['run_id']
    else:
        note=context['annotation']
        estimate=note.get('uncertainty_ms')
        if (note.get('clock_quality') not in ('unverified_client','user_estimate') or estimate is None
                or note.get('event_utc_start_ns') is None or note.get('event_utc_end_ns') is None):
            result['reason']='note_clock_unavailable';return result
        if type(estimate) not in (int,float) or not 0<=estimate<=86400000:
            raise ContextError('invalid_note_uncertainty',409)
        decimal=Decimal(str(estimate))*1000000
        if not decimal.is_finite():raise ContextError('invalid_note_uncertainty',409)
        uncertainty=int(decimal.to_integral_value(rounding=ROUND_CEILING))
        start=_ns(note['event_utc_start_ns'],signed=True);end=_ns(note['event_utc_end_ns'],signed=True)
        if end<start:raise ContextError('invalid_note_interval',409)
        result['note_uncertainty_ns']=str(uncertainty)
        clock=next((item for item in catalog['mappings'] if (item['robot_id'],item['boot_id'])==(robot,boot)),None)
        if clock is None or not clock['pieces']:return result
        candidates,gaps=invert_note(clock,start,end,uncertainty)
        result['unmapped_utc_intervals']=gaps
        if not candidates:
            result.update(state='outside',reason='outside_imported_clock_coverage');return result
        event=note['event_id'];run_id=None
    total=0
    for candidate in candidates:
        total+=selection_bound(video,robot,boot,int(candidate['start_robot_ns']),int(candidate['end_robot_ns']))
        if total>MAX_SELECTED_FRAMES:raise ContextError('oversized_context_interval',413)
    for candidate in candidates:
        candidate['result']=video.map_interval(robot,boot,candidate['start_robot_ns'],candidate['end_robot_ns'],
                                             event_id=event,run_id=run_id)
    result['candidates']=candidates
    if len(candidates)>1:
        result.update(state='ambiguous',reason='multiple_imported_clock_pieces');return result
    candidate=candidates[0];state=candidate['result']['state']
    if state=='mapped' and candidate['clock_coverage']=='partial':state='partial'
    result.update(state=state,reason={'mapped':'candidate_frames_selected','partial':'clock_or_video_coverage_partial',
        'gap':'no_available_frame_span','outside':'outside_video_calibration','unavailable':'video_clock_unavailable'}[state])
    return result
