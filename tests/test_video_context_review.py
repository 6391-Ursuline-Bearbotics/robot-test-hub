"""Independent saved-context golden math and actual local HTTP acceptance."""
import copy
from decimal import Decimal
import json
import unittest

from robot_test_hub.notebook import Notebook
from robot_test_hub.runs import RunCatalog
from robot_test_hub.video_context import invert_note
import test_video_investigation_review as fixture
from test_notebook import annotation

R = 9007199254740993
U = 1790000000000000000


def piece(robot, utc, errors):
    return dict(start_ns=str(robot[0]),end_ns=str(robot[-1]),reason='first_anchor',
        anchors=[dict(robot_ns=str(r),utc_ns=str(u),uncertainty_ns=str(e))
            for r,u,e in zip(robot,utc,errors)])


class IndependentInverseReviewTests(unittest.TestCase):
    def test_nonunit_scale_global_error_outward_rounding_and_contracted_edge(self):
        clock={'pieces':[piece([R,R+100],[U,U+1000],[0,100])]}
        candidates,gaps=invert_note(clock,U+500,U+500,0)
        self.assertEqual(candidates[0]['start_robot_ns'],str(R+39))
        self.assertEqual(candidates[0]['end_robot_ns'],str(R+61))
        self.assertEqual(candidates[0]['clock_uncertainty_utc_ns'],'101')
        self.assertEqual(candidates[0]['clock_coverage'],'complete')
        self.assertEqual(gaps,[])
        candidates,gaps=invert_note(clock,U+50,U+50,0)
        self.assertEqual(candidates[0]['start_robot_ns'],str(R))
        self.assertEqual(candidates[0]['end_robot_ns'],str(R+16))
        self.assertEqual(candidates[0]['clock_coverage'],'partial')
        self.assertEqual(gaps,[[str(U+50),str(U+50)]])

    def test_empty_guarantee_single_anchor_and_gap_points_are_not_full_coverage(self):
        narrow={'pieces':[piece([R,R+10],[U,U+10],[8,8])]}
        candidates,gaps=invert_note(narrow,U+5,U+5,0)
        self.assertEqual(candidates[0]['clock_coverage'],'partial')
        self.assertEqual(gaps,[[str(U+5),str(U+5)]])
        single={'pieces':[piece([R],[U],[5])]}
        candidates,gaps=invert_note(single,U+4,U+4,0)
        self.assertEqual((candidates[0]['start_robot_ns'],candidates[0]['end_robot_ns']),(str(R),str(R)))
        self.assertEqual(gaps,[[str(U+4),str(U+4)]])
        separate={'pieces':[piece([R,R+10],[U,U+10],[0,0]),piece([R+20,R+30],[U+20,U+30],[0,0])]}
        self.assertEqual(invert_note(separate,U+15,U+15,0),([],[[str(U+15),str(U+15)]]))

    def test_repeated_utc_keeps_distinct_boot_piece_candidates(self):
        clock={'pieces':[piece([R,R+100],[U,U+100],[0,0]),piece([R+200,R+300],[U,U+100],[0,0])]}
        candidates,gaps=invert_note(clock,U+50,U+50,0)
        self.assertEqual([(c['clock_piece'],c['start_robot_ns'],c['end_robot_ns']) for c in candidates],
            [(0,str(R+49),str(R+51)),(1,str(R+249),str(R+251))])
        self.assertEqual(gaps,[])


class IndependentContextHTTPReviewTests(unittest.TestCase):
    start_http=fixture.InvestigationHTTPReviewTests.start_http
    stop_http=fixture.InvestigationHTTPReviewTests.stop_http
    cleanup=fixture.InvestigationHTTPReviewTests.cleanup
    request=fixture.InvestigationHTTPReviewTests.request
    save=fixture.InvestigationHTTPReviewTests.save

    def setUp(self):
        fixture.InvestigationHTTPReviewTests.setUp(self)
        self.manifest['frames']=[dict(pts_ns=str(a),duration_ns='100000000') for a in (0,100000000,400000000,500000000)]
        self.manifest['end_pts_ns']='600000000'
        self.manifest_path.write_text(json.dumps(self.manifest),encoding='utf-8')
        self.service.video.recover()
        self.book=Notebook(self.root/'catalog.sqlite3')
        self.receipt=self.save()
        self.cycles=self.cycles_for('reviewboot')
        unknown=self.cycles_for(None)
        with self.book._connection() as db:
            self.catalog=RunCatalog(db).rebuild([('known-dataset',self.cycles),('unrelated-unknown',unknown)])
        self.note=annotation('context-review-note',seconds=0)
        self.note.update(submitted_utc_ns=str(U+450000000),event_utc_start_ns=str(U+450000000),
            event_utc_end_ns=str(U+450000000),when={'kind':'now'},uncertainty_ms=0)
        self.book.save(self.note)

    def calibration(self):
        value=fixture.InvestigationHTTPReviewTests.calibration(self)
        value['windows'][0]['end_robot_ns']=str(self.base+500000000)
        value['windows'][0]['anchors'][1]['robot_ns']=str(self.base+500000000)
        return value

    def cycles_for(self,boot):
        cycles=[]
        for offset in range(0,600000001,100000000):
            values=dict(status_robot_id='reviewrobot',state_known=True,status_enabled=200000000<=offset<500000000,
                status_mode='TELEOPERATED',runtime_mode='SIM',epoch_valid=True,
                epoch_us=str(Decimal(U+offset)/1000))
            if boot is not None:values['status_boot_id']=boot
            cycles.append(dict(kind='cycle',timestamp_ns=str(self.base+offset),source_type='SYNTHETIC',
                aliases={key:dict(value=value,updated_in_cycle=True) for key,value in values.items()}))
        return cycles

    def associate(self,context,catalog=None):
        return self.request('POST','/api/v1/video/associate',dict(alignment_id=self.receipt['alignment_id'],
            revision=self.receipt['revision'],sha256=self.receipt['sha256'],
            catalog_revision=(catalog or self.catalog)['revision'],context=context))

    def test_http_exact_note_and_old_catalog_pins_survive_rebuild_and_note_edit(self):
        spec=dict(kind='note',event_id=self.note['event_id'],note_revision=1)
        status,result=self.associate(spec)
        self.assertEqual(status,200)
        self.assertEqual(result['state'],'mapped')
        self.assertEqual(result['context']['annotation']['revision'],1)
        candidate=result['candidates'][0]
        self.assertEqual(candidate['start_robot_ns'],str(self.base+448999999))
        self.assertEqual(candidate['end_robot_ns'],str(self.base+451000001))
        self.assertEqual(candidate['clock_uncertainty_utc_ns'],'1000001')
        self.assertEqual(candidate['result']['spans'][0]['frame_indexes'],[2])
        self.assertFalse(result['measured_camera_alignment'])
        self.assertEqual(result['note_uncertainty_ns'],'0')
        second=dict(self.note,revision=2,text='newer revision must not replace the requested note')
        self.book.save(second,expected_previous_revision=1)
        revised=copy.deepcopy(self.cycles)
        for cycle in revised:
            cycle['aliases']['epoch_us']['value']=str(Decimal(cycle['aliases']['epoch_us']['value'])+10000000)
        with self.book._connection() as db:
            current=RunCatalog(db).rebuild([('new-dataset',revised)])
        self.assertNotEqual(current['revision'],self.catalog['revision'])
        self.assertEqual(self.associate(spec)[1],result)
        self.raw.write_bytes(b'X'*len(self.original))
        self.assertEqual(self.associate(spec)[0],409)

    def test_http_direct_run_and_unknown_note_domains_remain_honest(self):
        run=next(r for r in self.catalog['runs'] if r['known_boot'])
        status,result=self.associate(dict(kind='run',run_id=run['run_id']))
        self.assertEqual(status,200)
        self.assertEqual(result['time_basis'],'saved_run_robot_interval')
        candidate=result['candidates'][0]
        self.assertEqual(candidate['start_robot_ns'],str(self.base+200000000))
        self.assertEqual(candidate['end_robot_ns'],str(self.base+500000000))
        self.assertIsNone(candidate['clock_uncertainty_utc_ns'])
        self.assertEqual(result['state'],'partial')
        self.assertEqual(candidate['result']['windows'][0]['unavailable_pts_intervals'],[['200000000','400000000']])
        unknown=next(r for r in self.catalog['runs'] if not r['known_boot'])
        self.assertEqual(self.associate(dict(kind='run',run_id=unknown['run_id']))[1]['reason'],'run_boot_unknown')
        note=dict(self.note,event_id='unknown-review-note',clock_quality='unknown',uncertainty_ms=None)
        self.book.save(note)
        status,result=self.associate(dict(kind='note',event_id=note['event_id'],note_revision=1))
        self.assertEqual(status,200)
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['candidates'],[])


    def test_catalog_nested_extra_fields_cannot_escape_run_projection(self):
        altered=copy.deepcopy(self.catalog)
        run=next(r for r in altered['runs'] if r['known_boot'])
        run['phases'][0]['private_input']='secret-credential native endpoint'
        run['utc_intervals'][0]['provider_diagnostic']='secret-credential device path'
        with self.book._connection() as db:
            db.execute('UPDATE run_catalog_revisions SET document=? WHERE id=?',
                (json.dumps(altered),altered['revision']))
        status,result=self.associate(dict(kind='run',run_id=run['run_id']))
        self.assertIn(status,(200,409))
        if status==200:
            self.assertNotIn('private_input',result['context']['run']['phases'][0])
            self.assertNotIn('provider_diagnostic',result['context']['run']['utc_intervals'][0])

if __name__=='__main__':unittest.main()

