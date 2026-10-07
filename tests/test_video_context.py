"""Independent clock inversion cases and actual imported-catalog association."""
import copy
from decimal import Decimal
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.runs import RunCatalog
from robot_test_hub.server import create_http_server
from robot_test_hub.video_context import (ContextError,associate_context,invert_note,validate_catalog)
from robot_test_hub.video_investigation import InvestigationError
import test_video_investigation as fixtures
from test_notebook import annotation

R=9007199254740993
U=1790000000000000000
REVISION='a'*64


def piece(robot,utc,error=0,reason='first_anchor'):
    return dict(start_ns=str(robot[0]),end_ns=str(robot[-1]),reason=reason,
        anchors=[dict(robot_ns=str(r),utc_ns=str(u),uncertainty_ns=str(error)) for r,u in zip(robot,utc)])


def catalog(pieces):
    return dict(revision=REVISION,version='runs-1',max_gap_ns='250000000',runs=[],
        mappings=[dict(robot_id='robot6391',boot_id='boot1',revision=REVISION,pieces=pieces,rejected=[])])


class Video:
    def __init__(self):self.calls=[]
    def document(self):return dict(robot_id='robot6391',boot_id='boot1')
    def map_interval(self,robot,boot,start,end,**context):
        self.calls.append((start,end,context))
        return dict(state='mapped',spans=[],qualification='manual_unqualified',measured_camera_alignment=False)


def note(utc,error=0,**changes):
    result=dict(kind='note',annotation=dict(event_id='note1',event_utc_start_ns=str(utc),event_utc_end_ns=str(utc),
        uncertainty_ms=error,clock_quality='user_estimate'))
    result['annotation'].update(changes)
    return result


class ClockInversionTests(unittest.TestCase):
    def test_exact_rational_nonunit_scale_above_javascript_safe_integer(self):
        clock=catalog([piece([R,R+3],[U,U+10])])
        validate_catalog(clock,REVISION)
        candidates,gaps=invert_note(clock['mappings'][0],U+5,U+5,0)
        self.assertEqual(candidates,[dict(clock_piece=0,start_robot_ns=str(R+1),end_robot_ns=str(R+2),
            clock_uncertainty_utc_ns='1',clock_coverage='complete')])
        self.assertEqual(gaps,[])

    def test_decimal_note_error_clock_error_and_piecewise_inverse_round_outward(self):
        clock=catalog([piece([R,R+10,R+30],[U,U+20,U+30])])
        result=associate_context(Video(),clock,note(U+25,error=.0000015),lambda *args:1)
        self.assertEqual(result['note_uncertainty_ns'],'2')
        self.assertEqual(result['candidates'][0]['start_robot_ns'],str(R+14))
        self.assertEqual(result['candidates'][0]['end_robot_ns'],str(R+26))
        self.assertEqual(result['state'],'mapped')
        clock['mappings'][0]['pieces'][0]['anchors'][1]['uncertainty_ns']='2'
        result=associate_context(Video(),clock,note(U+25),lambda *args:1)
        self.assertEqual(result['candidates'][0]['clock_uncertainty_utc_ns'],'3')
        self.assertEqual(result['candidates'][0]['start_robot_ns'],str(R+14))

    def test_point_at_anchor_edge_is_partial_and_uncovered_gap_point_not_complete(self):
        clock=catalog([piece([R,R+10],[U,U+10])])
        result=associate_context(Video(),clock,note(U),lambda *args:1)
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['unmapped_utc_intervals'],[[str(U),str(U)]])
        self.assertEqual(result['candidates'][0]['start_robot_ns'],str(R))
        self.assertEqual(result['candidates'][0]['end_robot_ns'],str(R+1))
        clock=catalog([piece([R,R+10],[U,U+10]),piece([R+20,R+30],[U+20,U+30],reason='anchor_gap')])
        result=associate_context(Video(),clock,note(U+15),lambda *args:1)
        self.assertEqual(result['state'],'outside')
        self.assertEqual(result['unmapped_utc_intervals'],[[str(U+15),str(U+15)]])

    def test_utc_step_overlap_preserves_all_clock_piece_candidates(self):
        clock=catalog([piece([R,R+10],[U,U+10]),piece([R+20,R+30],[U+5,U+15],reason='utc_discontinuity')])
        validate_catalog(clock,REVISION)
        video=Video();result=associate_context(video,clock,note(U+7),lambda *args:1)
        self.assertEqual(result['state'],'ambiguous')
        self.assertEqual([item['clock_piece'] for item in result['candidates']],[0,1])
        self.assertEqual(len(video.calls),2)
        self.assertLess(int(result['candidates'][0]['end_robot_ns']),int(result['candidates'][1]['start_robot_ns']))

    def test_single_anchor_has_only_a_robot_point_and_explicit_uncertain_coverage(self):
        clock=catalog([piece([R],[U],error=5)])
        result=associate_context(Video(),clock,note(U+4),lambda *args:1)
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['candidates'][0]['start_robot_ns'],str(R))
        self.assertEqual(result['candidates'][0]['end_robot_ns'],str(R))
        self.assertEqual(result['unmapped_utc_intervals'],[[str(U+4),str(U+4)]])
        clock=catalog([piece([R],[U])])
        self.assertEqual(associate_context(Video(),clock,note(U),lambda *args:1)['state'],'mapped')
        self.assertEqual(associate_context(Video(),clock,note(U+1),lambda *args:1)['state'],'outside')

    def test_unknown_note_clock_missing_mapping_and_endpoint_overflow_are_explicit(self):
        clock=catalog([piece([R,R+10],[U,U+10])])
        for changes in ({'clock_quality':'unknown'},{'uncertainty_ms':None},{'event_utc_start_ns':None}):
            result=associate_context(Video(),clock,note(U+5,**changes),lambda *args:1)
            self.assertEqual(result['state'],'unavailable')
            self.assertIsNone(result['note_uncertainty_ns'])
        empty=catalog([])
        self.assertEqual(associate_context(Video(),empty,note(U+5),lambda *args:1)['reason'],'clock_mapping_unavailable')
        with self.assertRaises(ContextError):
            associate_context(Video(),clock,note((1<<63)-1,error=.000002),lambda *args:1)

    def test_catalog_anchor_piece_bounds_and_unrelated_unknown_boot_are_validated(self):
        valid=catalog([piece([R,R+10],[U,U+10])])
        for mutate in (lambda p:p['anchors'][0].update(robot_ns=R),
                       lambda p:p['anchors'][0].update(robot_ns='00'),
                       lambda p:p['anchors'][1].update(utc_ns=str(U-1)),
                       lambda p:p.update(end_ns=str(R+11)),
                       lambda p:p['anchors'][0].update(uncertainty_ns=True)):
            altered=copy.deepcopy(valid);mutate(altered['mappings'][0]['pieces'][0])
            with self.assertRaises(ContextError):validate_catalog(altered,REVISION)
        unrelated=dict(robot_id='unknown',boot_id='unknown-boot:dataset1',revision=REVISION,pieces=[],rejected=[])
        valid['mappings'].append(unrelated)
        self.assertIs(validate_catalog(valid,REVISION),valid)
        with patch('robot_test_hub.video_context.MAX_ANCHORS',1):
            with self.assertRaises(ContextError):validate_catalog(valid,REVISION)
        changed=copy.deepcopy(valid);changed['mappings'][0]['revision']='b'*64
        with self.assertRaises(ContextError):validate_catalog(changed,REVISION)

    def test_candidate_and_aggregate_frame_caps_precede_video_mapping(self):
        clock=catalog([piece([R,R+10],[U,U+10]),piece([R+20,R+30],[U+5,U+15],reason='utc_discontinuity')])
        with patch('robot_test_hub.video_context.MAX_CANDIDATES',1):
            with self.assertRaises(ContextError):associate_context(Video(),clock,note(U+7),lambda *args:1)
        video=Video()
        with self.assertRaises(ContextError) as raised:associate_context(video,clock,note(U+7),lambda *args:6000)
        self.assertEqual(raised.exception.code,'oversized_context_interval')
        self.assertEqual(video.calls,[])


class SavedContextTests(unittest.TestCase):
    setUp=fixtures.VideoInvestigationTests.setUp
    segment=fixtures.VideoInvestigationTests.segment
    payload=fixtures.VideoInvestigationTests.payload

    def setup_context(self):
        self.manifest,self.path=self.segment(capture='contextcapture',
            frames=((0,100000000),(100000000,100000000),(400000000,100000000),(500000000,100000000)))
        payload=self.payload();payload['windows'][0]['end_robot_ns']=str(self.base+500000000)
        payload['windows'][0]['anchors'][1]['robot_ns']=str(self.base+500000000)
        receipt=self.backend.create(payload)
        cycles=[]
        for offset in range(0,600000001,100000000):
            aliases={key:dict(value=value,updated_in_cycle=True) for key,value in dict(
                status_robot_id='robot6391',status_boot_id='boot1',state_known=True,status_enabled=200000000<=offset<500000000,
                status_mode='teleop',runtime_mode='SIM',epoch_valid=True,
                epoch_us=str(Decimal(U+offset)/1000)).items()}
            cycles.append(dict(kind='cycle',timestamp_ns=str(self.base+offset),aliases=aliases,source_type='SYNTHETIC'))
        with self.book._connection() as db:document=RunCatalog(db).rebuild([('dataset1',cycles)])
        note_payload=annotation('context-note',seconds=0)
        note_payload.update(submitted_utc_ns=str(U+450000000),event_utc_start_ns=str(U+450000000),
            event_utc_end_ns=str(U+450000000),when={'kind':'now'},uncertainty_ms=0)
        saved=self.book.save(note_payload)['annotation']
        return receipt,document,cycles,note_payload,saved

    def request(self,receipt,document,context):
        return dict(alignment_id=receipt['alignment_id'],revision=receipt['revision'],sha256=receipt['sha256'],
                    catalog_revision=document['revision'],context=context)

    def test_saved_note_pin_old_catalog_and_changed_current_are_not_substituted(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        request=self.request(receipt,document,dict(kind='note',event_id=saved['event_id'],note_revision=1))
        result=self.backend.associate(request)
        self.assertEqual(result['state'],'mapped')
        self.assertEqual(result['context']['annotation'],saved)
        self.assertEqual(result['candidates'][0]['result']['spans'][0]['frame_indexes'],[2])
        self.assertEqual(result['catalog_revision'],document['revision'])
        self.assertEqual(result['note_uncertainty_ns'],'0')
        changed=copy.deepcopy(note_payload);changed['revision']=2;changed['text']='later note edit'
        self.book.save(changed,expected_previous_revision=1)
        changed_cycles=copy.deepcopy(cycles)
        for cycle in changed_cycles:
            cycle['aliases']['epoch_us']['value']=str(Decimal(cycle['aliases']['epoch_us']['value'])+5000)
        with self.book._connection() as db:current=RunCatalog(db).rebuild([('dataset2',changed_cycles)])
        self.assertNotEqual(current['revision'],document['revision'])
        self.assertEqual(self.backend.associate(request),result)
        self.assertFalse(result['measured_camera_alignment'])
        self.assertIsNone(result['context']['annotation']['run_id'])

    def test_runs_use_direct_robot_interval_and_preserve_incomplete_domain_metadata(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        run=document['runs'][0]
        request=self.request(receipt,document,dict(kind='run',run_id=run['run_id']))
        result=self.backend.associate(request)
        self.assertEqual(result['time_basis'],'saved_run_robot_interval')
        candidate=result['candidates'][0]
        self.assertIsNone(candidate['clock_piece']);self.assertIsNone(candidate['clock_uncertainty_utc_ns'])
        self.assertEqual(candidate['start_robot_ns'],str(self.base+200000000))
        self.assertEqual(candidate['end_robot_ns'],str(self.base+500000000))
        self.assertEqual(result['context']['run']['runtime_mode'],'SIM')
        self.assertEqual(result['context']['run']['source_types'],['SYNTHETIC'])
        self.assertEqual(result['state'],'partial')
        incomplete=copy.deepcopy(cycles[2:5])
        with self.book._connection() as db:partial=RunCatalog(db).rebuild([('partial',incomplete)])
        result=self.backend.associate(self.request(receipt,partial,dict(kind='run',run_id=partial['runs'][0]['run_id'])))
        self.assertEqual(result['candidates'][0]['clock_coverage'],'partial')
        self.assertEqual(result['context']['run']['completeness'],'incomplete')
        other=copy.deepcopy(cycles)
        for row in other:row['aliases']['status_boot_id']['value']='otherboot'
        with self.book._connection() as db:wrong=RunCatalog(db).rebuild([('other',other)])
        result=self.backend.associate(self.request(receipt,wrong,dict(kind='run',run_id=wrong['runs'][0]['run_id'])))
        self.assertEqual(result['state'],'unavailable');self.assertEqual(result['candidates'],[])

    def test_unknown_boot_rows_do_not_poison_note_matching_and_unknown_run_is_unavailable(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        unknown=copy.deepcopy(cycles)
        for row in unknown:row['aliases'].pop('status_boot_id')
        with self.book._connection() as db:mixed=RunCatalog(db).rebuild([('known',cycles),('unknown',unknown)])
        result=self.backend.associate(self.request(receipt,mixed,dict(kind='note',event_id=saved['event_id'],note_revision=1)))
        self.assertEqual(result['state'],'mapped')
        run=next(item for item in mixed['runs'] if not item['known_boot'])
        result=self.backend.associate(self.request(receipt,mixed,dict(kind='run',run_id=run['run_id'])))
        self.assertEqual(result['reason'],'run_boot_unknown')

    def test_public_run_nested_metadata_is_projected_without_mutating_catalog(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        altered=copy.deepcopy(document);run=altered['runs'][0]
        run['phases'][0]['private_input']='secret-camera-password/private-path'
        run['utc_intervals'][0]['private_observation']='secret-local-device/private-path'
        with self.book._connection() as db:
            with db:
                db.execute('UPDATE run_catalog_revisions SET document=? WHERE id=?',
                           (json.dumps(altered),document['revision']))
        result=self.backend.associate(self.request(receipt,document,dict(kind='run',run_id=run['run_id'])))
        exposed=result['context']['run']
        self.assertEqual(set(exposed['phases'][0]),{'mode','start_monotonic_ns','end_monotonic_ns'})
        self.assertEqual(set(exposed['utc_intervals'][0]),
            {'robot_start_ns','robot_end_ns','utc_start_ns','utc_end_ns','uncertainty_ns'})
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(exposed['runtime_mode'],run['runtime_mode'])
        self.assertEqual(exposed['source_types'],run['source_types'])
        with self.book._connection() as db:
            stored=json.loads(db.execute('SELECT document FROM run_catalog_revisions WHERE id=?',
                                        (document['revision'],)).fetchone()[0])
        self.assertEqual(stored,altered)

    def test_missing_note_revision_catalog_and_changed_evidence_have_fixed_failures(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        query=self.request(receipt,document,dict(kind='note',event_id=saved['event_id'],note_revision=1))
        for update,code in ((dict(catalog_revision='0'*64),'catalog_revision_not_found'),
                            (dict(context=dict(kind='note',event_id=saved['event_id'],note_revision=99)),'annotation_revision_not_found'),
                            (dict(context=dict(kind='run',run_id='missing')),'run_not_found')):
            request={**query,**update}
            with self.assertRaises(InvestigationError) as raised:self.backend.associate(request)
            self.assertEqual(raised.exception.code,code)
        metadata=self.path.with_name(self.path.name+'.json');original=metadata.read_bytes()
        changed=copy.deepcopy(self.manifest)
        changed['frames'][1].update(pts_ns='101000000',duration_ns='99000000')
        metadata.write_text(json.dumps(changed))
        with self.assertRaises(InvestigationError):self.backend.associate(query)
        metadata.write_bytes(original)
        mismatched={**query,'sha256':'0'*64}
        with self.assertRaises(InvestigationError):self.backend.associate(mismatched)
        self.path.write_bytes(self.path.read_bytes()+b'altered')
        with self.assertRaises(InvestigationError):self.backend.associate(query)

    def test_unknown_note_estimate_is_unavailable_and_response_caps_apply(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        changed=copy.deepcopy(note_payload);changed.update(revision=2,clock_quality='unknown',uncertainty_ms=None)
        self.book.save(changed,expected_previous_revision=1)
        request=self.request(receipt,document,dict(kind='note',event_id=saved['event_id'],note_revision=2))
        result=self.backend.associate(request)
        self.assertEqual(result['state'],'unavailable');self.assertIsNone(result['note_uncertainty_ns'])
        with patch('robot_test_hub.video_context.MAX_CATALOG_BYTES',10):
            with self.assertRaises(InvestigationError) as raised:self.backend.associate(request)
            self.assertEqual(raised.exception.code,'oversized_catalog')
        with patch('robot_test_hub.video_investigation.MAX_RESPONSE',10):
            with self.assertRaises(InvestigationError):self.backend.associate(request)

    def test_actual_http_association_security_stopping_and_path_redaction(self):
        receipt,document,cycles,note_payload,saved=self.setup_context()
        query=self.request(receipt,document,dict(kind='note',event_id=saved['event_id'],note_revision=1))
        server=create_http_server(self.service,fixtures.Source(),0)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        def request(body,headers=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            try:
                client.request('POST','/api/v1/video/associate',body,
                    headers=headers or {'Content-Type':'application/json','X-Hub-Request':'1'})
                response=client.getresponse();return response.status,json.loads(response.read())
            finally:client.close()
        try:
            status,result=request(json.dumps(query));self.assertEqual(status,200)
            self.assertEqual(result['state'],'mapped')
            self.assertNotIn(str(self.root),json.dumps(result));self.assertNotIn('relative_path',json.dumps(result))
            self.assertEqual(request(json.dumps(query),headers={'Content-Type':'application/json'})[0],403)
            self.assertEqual(request('{"context":{},"context":{}}')[0],400)
            self.assertEqual(request(json.dumps({**query,'secret-path':'private'}))[0],400)
            self.service.stop.set()
            self.assertEqual(request(json.dumps(query))[0],503)
        finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
