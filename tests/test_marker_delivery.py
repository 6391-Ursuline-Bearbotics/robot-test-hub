import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from robot_test_hub.notebook import Notebook,NotebookError
from robot_test_hub.marker_delivery import MarkerStore,MarkerError,strict_json,validate_ack,canonical
from tests.test_notebook import annotation

class MarkerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'catalog.sqlite3';self.book=Notebook(self.path)
        self.payload=annotation();self.book.save(self.payload);self.store=MarkerStore(self.path)
        self.request={'delivery_id':'delivery-a','event_id':'event-a','note_revision':1,
            'annotation_sha256':self.book.exact_revision('event-a',1)['sha256'],
            'destination_robot_id':'robot-a','destination_boot_id':'boot-a'}
    def schedule(self):return self.store.schedule(self.request,robot_id='robot-a')
    def test_exact_committed_revision_restart_and_privacy_projection(self):
        result=self.schedule();row=self.store.get('delivery-a',private=True);wire=strict_json(row['wire_json']);payload=strict_json(wire['payload_json'])
        self.assertNotIn('author',payload);self.assertNotIn('device_id',payload);self.assertNotIn('run_id',payload)
        self.assertEqual(payload['event_utc_start_ns'],self.payload['event_utc_start_ns'])
        self.assertEqual(wire['payload_sha256'],hashlib.sha256(wire['payload_json'].encode()).hexdigest())
        self.assertEqual(wire['annotation_sha256'],self.request['annotation_sha256'])
        self.book.save(dict(self.payload,revision=2,text='new content'),expected_previous_revision=1)
        restarted=MarkerStore(self.path)
        self.assertEqual(restarted.get('delivery-a',private=True)['wire_json'],row['wire_json'])
        self.assertTrue(restarted.schedule(self.request,robot_id='robot-a')['idempotent'])
        self.assertEqual(self.book.history('event-a')['revisions'][0]['delivery_state'],'historical_hub_only')
        self.assertNotIn('Steering',canonical(result));self.assertFalse(result['delivery']['annotation_source_digest_verified_by_robot'])
    def test_missing_uncommitted_revision_and_changed_pins_rejected(self):
        with self.assertRaises(NotebookError):self.store.schedule(dict(self.request,note_revision=2),robot_id='robot-a')
        with self.assertRaises(MarkerError):self.store.schedule(dict(self.request,annotation_sha256='0'*64),robot_id='robot-a')
        self.schedule()
        for changed in (dict(self.request,destination_boot_id='boot-b'),dict(self.request,event_id='other'),dict(self.request,delivery_id='another')):
            with self.assertRaises((MarkerError,NotebookError)):self.store.schedule(changed,robot_id='robot-a')
        self.assertEqual(len(self.store.page()['items']),1)
    def test_oversized_unavailable_job_retries_all_exact_pins_without_retarget(self):
        self.payload['text']='\\n'*2048
        # Locally valid note; the escaped outer payload exceeds its independent cap.
        self.book.save(dict(self.payload,revision=2),expected_previous_revision=1)
        self.request.update(note_revision=2,annotation_sha256=self.book.exact_revision('event-a',2)['sha256'])
        with patch('robot_test_hub.marker_delivery.MAX_WIRE',100):
            first=self.schedule();self.assertEqual(first['delivery']['state'],'unavailable')
            self.assertTrue(self.schedule()['idempotent'])
            for key,value in (('destination_boot_id','boot-b'),('destination_robot_id','robot-b'),('note_revision',1)):
                changed=dict(self.request,**{key:value})
                with self.assertRaises(MarkerError):self.store.schedule(changed,robot_id=changed['destination_robot_id'])
        self.assertEqual(self.store.get('delivery-a',private=True)['wire_json'],'')
    def test_unknown_client_time_never_becomes_robot_time(self):
        p=annotation('unknown');p.update(submitted_utc_ns=None,client_monotonic_ns=None,event_utc_start_ns=None,event_utc_end_ns=None,when={'kind':'unknown'},uncertainty_ms=None,clock_quality='unknown')
        self.book.save(p);request=dict(self.request,delivery_id='unknown-delivery',event_id='unknown',annotation_sha256=self.book.exact_revision('unknown',1)['sha256'])
        self.store.schedule(request,robot_id='robot-a');row=self.store.get('unknown-delivery',private=True)
        payload=strict_json(strict_json(row['wire_json'])['payload_json'])
        self.assertIsNone(payload['event_utc_start_ns']);self.assertIsNone(payload['submitted_utc_ns'])
    def test_pending_bound_paging_and_failed_commit_leave_no_job(self):
        self.store.maximum_pending=1;self.schedule()
        p=annotation('event-b');self.book.save(p)
        q=dict(self.request,delivery_id='delivery-b',event_id='event-b',annotation_sha256=self.book.exact_revision('event-b',1)['sha256'])
        with self.assertRaises(MarkerError):self.store.schedule(q,robot_id='robot-a')
        self.store.update('delivery-a','rejected',error='capacity');self.store.schedule(q,robot_id='robot-a')
        first=self.store.page(limit=1);self.assertEqual(first['items'][0]['delivery_id'],'delivery-a')
        self.assertEqual(self.store.page(limit=1,cursor=first['next_cursor'])['items'][0]['delivery_id'],'delivery-b')
        with self.assertRaises(MarkerError):self.store.page(cursor='01')
    def test_strict_bounded_json_duplicate_depth_nonfinite_and_utf8(self):
        for value in ('{"x":1,"x":2}','['*9+'0'+']'*9,'{"x":NaN}',b'\xff','"'+('x'*16384)+'"','"\ud800"'):
            with self.subTest(value=str(value)[:30]):
                with self.assertRaises((MarkerError,UnicodeError)):strict_json(value)
    def test_ack_pins_original_receipt_and_fixed_rejection_codes(self):
        self.schedule();job=self.store.get('delivery-a',private=True);ack=accepted_ack(job)
        self.assertEqual(validate_ack(ack,job,'SIM')['receipt_robot_ns'],'1000000000')
        for key,value in [('revision',True),('destination_boot_id','boot-b'),('payload_sha256','1'*64),('receipt_robot_ns','01'),('runtime_mode','REAL')]:
            with self.assertRaises(MarkerError):validate_ack(dict(ack,**{key:value}),job,'SIM')
        with self.assertRaises(MarkerError):validate_ack(ack,dict(job,receipt_robot_ns='1000000001'),'SIM')
        rejected=dict(ack,state='rejected',receipt_robot_ns=None,reason='capacity')
        self.assertEqual(validate_ack(rejected,job,'SIM')['reason'],'capacity')
        with self.assertRaises(MarkerError):validate_ack(dict(rejected,duplicate=True),job,'SIM')
        with self.assertRaises(MarkerError):validate_ack(dict(rejected,reason='private endpoint failed'),job,'SIM')

def accepted_ack(job):
    return {'schema_version':1,'profile':'testhub-note-marker-1',
        **{key:job[key] for key in ('delivery_id','event_id','destination_robot_id','destination_boot_id','annotation_sha256','payload_sha256')},
        'revision':job['note_revision'],'state':'accepted_into_log_input','duplicate':False,'receipt_robot_ns':'1000000000',
        'reason':None,'ack_scope':'contextual_receipt_only','usb_durability':'unavailable','logger_queue_fault':False,'runtime_mode':'SIM'}
