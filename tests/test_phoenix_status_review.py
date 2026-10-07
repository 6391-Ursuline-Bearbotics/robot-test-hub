"""Independent recorded-SDK failure/coverage goldens; no physical or CAN qualification."""
from dataclasses import replace
import unittest
import test_phoenix_status as fixtures
from robot_test_hub.phoenix_status import phoenix_status


class PhoenixStatusReviewTests(unittest.TestCase):
    def check(self,rows,**updates):
        evidence=rows.evidence()
        return phoenix_status(replace(evidence,context={**evidence.context,**updates}),{})

    def test_missing_endpoint_and_incomplete_window_cannot_be_a_no_finding(self):
        rows=fixtures.Rows().snapshot(1,fixtures.LO).snapshot(2,fixtures.HI)
        good=self.check(rows);self.assertEqual(good['outcome'],'evaluated_no_finding')
        for update in ({'end_monotonic_ns':str(fixtures.HI+1)},{'completeness':'partial'}):
            result=self.check(rows,**update)
            self.assertEqual(result['outcome'],'insufficient_data');self.assertFalse(result['findings'])
            self.assertFalse(result['coverage']['physical_acquisition_time_qualified'])
        rows.rows=[r for r in rows.rows if not(r['kind']=='cycle' and r['timestamp_ns']==str(fixtures.HI))]
        result=self.check(rows);self.assertEqual(result['outcome'],'insufficient_data')
        self.assertIn('whole_run_cycle_endpoints_unavailable',result['unavailable'])

    def test_second_artifact_never_borrows_first_artifact_receipt_or_schema(self):
        missing='/Drive/Module0/PhoenixDriveVelocitySystemTimestampSeconds'
        rows=fixtures.Rows().snapshot(1,fixtures.LO,source=fixtures.A)
        rows.snapshot(2,fixtures.HI,source=fixtures.B,held=(missing,))
        result=self.check(rows);self.assertEqual(result['outcome'],'insufficient_data')
        module=result['coverage']['module_observations'][0]
        self.assertEqual(module['timing_cycles'],1)
        self.assertIsNone(module['unavailable_fields']['DriveVelocitySystemTimestampSeconds']['source_reference'])
        self.assertEqual(module['latest_observation']['source_sha256'],fixtures.A)
        other=result['coverage']['module_observations'][1]['latest_observation']
        self.assertEqual(other['source_sha256'],fixtures.B)
        refs=other['signals']['DriveVelocity']['references']
        self.assertTrue(all(r['source_sha256']==fixtures.B and r['cycle_source_sha256']==fixtures.B for r in refs.values()))

    def test_finish_removes_held_field_and_preserves_before_run_false_evidence(self):
        status='/Drive/Module0/PhoenixDriveVelocityStatusOk'
        rows=fixtures.Rows().snapshot(1,fixtures.LO-1,before=True,overrides={status:{'value':False}})
        rows.snapshot(2,fixtures.LO,held=(status,));rows.control(status)
        rows.snapshot(3,fixtures.HI,held=(status,))
        result=self.check(rows);self.assertEqual(result['outcome'],'finding')
        finding=next(f for f in result['findings'] if f['signal']=='DriveVelocity')
        self.assertEqual(finding['observed_false_status_records'],1)
        ref=finding['source_references'][0]['status_ok']
        self.assertTrue(ref['before_run']);self.assertFalse(ref['updated_in_cycle'])
        self.assertEqual(ref['record_cycle_timestamp_ns'],str(fixtures.LO-1))
        self.assertEqual(ref['current_cycle_timestamp_ns'],str(fixtures.LO))
        self.assertEqual(result['coverage']['module_observations'][0]['status_cycles'],1)
        self.assertIn('whole_run_all_four_modules_status_and_timing_coverage_unavailable',result['unavailable'])
        self.assertFalse(finding['physical_health_qualified'])

    def test_wrong_units_boolean_int64_and_unsupported_qualification_fail_closed(self):
        for field,override in [('/Drive/Module0/PhoenixDriveVelocityRawValue',{'unit':'radians per second'}),
            ('/Drive/Module0/PhoenixRobotObservationStartNs',{'value':True}),
            ('/Drive/Module0/PhoenixObservationSequence',{'type':'double','value':1.}),
            ('/Drive/Module0/PhoenixPhysicalAcquisitionTimeQualified',{'value':True})]:
            with self.subTest(field=field):
                rows=fixtures.Rows().snapshot(1,fixtures.LO,overrides={field:override}).snapshot(2,fixtures.HI,overrides={field:override})
                result=self.check(rows);self.assertEqual(result['outcome'],'insufficient_data')
                self.assertFalse(result['findings']);self.assertFalse(result['coverage']['physical_acquisition_time_qualified'])
                self.assertFalse(result['coverage']['native_timestamp_availability_qualified'])

    def test_many_real_status_records_have_bounded_references_and_no_age_fault_threshold(self):
        status='/Drive/Module0/PhoenixDriveVelocityStatusOk';age='/Drive/Module0/PhoenixDriveVelocityAgeAtObservationEndSeconds'
        rows=fixtures.Rows()
        for i in range(140):
            stamp=fixtures.LO+(fixtures.HI-fixtures.LO)*i//139
            rows.snapshot(i+1,stamp,overrides={status:{'value':False},age:{'value':999999.}})
        result=self.check(rows);self.assertEqual(result['outcome'],'finding')
        self.assertEqual(len(result['findings']),1)
        finding=result['findings'][0];self.assertEqual(finding['observed_false_status_records'],140)
        self.assertEqual(len(finding['source_references']),128);self.assertTrue(finding['supporting_references_limited'])
        self.assertEqual(finding['criterion'],'exact_recorded_status_ok_false')
        self.assertFalse(result['coverage']['sdk_age_fault_threshold_configured'])
        self.assertTrue(all(m['unit']=='seconds' for m in result['metrics']))
        self.assertFalse(result['coverage']['physical_acquisition_time_qualified'])


if __name__=='__main__':unittest.main()
