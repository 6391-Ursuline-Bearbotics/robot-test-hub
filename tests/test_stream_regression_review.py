"""Independent source-preservation and proof-boundary tests for streamed cases."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import importer
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE, PROFILE_AKIT26, ResourceLimit
import akit26_fixture


class GenerousGuard:
    def __init__(self):self.writes=[];self.rows=0
    def before_write(self,kind,size):self.writes.append((kind,size))
    def decoded_row(self,row):self.rows+=1


class ImporterGuardReviewTests(unittest.TestCase):
    # Captured from frozen3328dd1, before the optional importer guard existed.
    FROZEN={
        PROFILE:dict(raw='df79f80165f2a4577913d86a503e11069fa89037ae6ee2931d8dcbe7480cb3d5',
            dataset='cff025377906ea2d4e130c4cf18a517547996b2e47f9ed5d4044a424639306a6',
            manifest='1fb6f53be70770e25c48266fb1461d7a3afcb6f26a6e8fba6e5f62505f2df97b'),
        PROFILE_AKIT26:dict(raw='9244788f7e8cc484c9419f97767c8a031a448335c7a092991770ec08f0fa53e7',
            dataset='04a74c5719f1bc6e5a3123c4564d85d48364d9748572362f313bd384e024d128',
            manifest='b581d031a0ab2888b1bb6bfdc47ae0f9b59271ccd6b7826cef5f9096c891a93a')}

    def original(self,profile):
        return ((Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes()
                if profile==PROFILE else akit26_fixture.match_log())

    def test_generous_guard_and_default_keep_frozen_raw_dataset_manifest_bytes(self):
        for profile in (PROFILE,PROFILE_AKIT26):
            original=self.original(profile);outcomes=[]
            for guarded in (False,True):
                with self.subTest(profile=profile,guarded=guarded),tempfile.TemporaryDirectory(prefix='independent guarded import ') as folder:
                    root=Path(folder);source=root/'invented.wpilog';source.write_bytes(original)
                    guard=GenerousGuard() if guarded else None
                    with closing(open_catalog(root)) as db:
                        job=importer.Importer(root,db,resource_guard=guard).import_file(source,profile=profile,source_type='SYNTHETIC')
                        self.assertEqual(job['state'],'succeeded',job)
                        paths=dict(raw=f"raw/{job['artifact_sha256'][:2]}/{job['artifact_sha256']}.wpilog",dataset=job['dataset_path'],manifest=job['manifest_path'])
                        blobs={kind:(root/path).read_bytes() for kind,path in paths.items()}
                        self.assertEqual({kind:hashlib.sha256(value).hexdigest() for kind,value in blobs.items()},self.FROZEN[profile])
                        outcomes.append((job['id'],job['extractor_version'],job['mapping_revision'],blobs))
                        if guard:
                            self.assertGreater(guard.rows,0)
                            self.assertTrue({'raw','decoded','manifest'}<={kind for kind,_ in guard.writes})
                    self.assertEqual(source.read_bytes(),original)

                    self.assertFalse(list(root.rglob('.import-*')))
            self.assertEqual(outcomes[0],outcomes[1])

    def test_raw_copy_guard_rejects_before_rejected_bytes_and_cleans_partial(self):
        with tempfile.TemporaryDirectory(prefix='independent raw rejection ') as folder:
            root=Path(folder);source=root/'invented.wpilog';original=self.original(PROFILE);source.write_bytes(original)
            observed=[]
            class RejectRaw(GenerousGuard):
                def before_write(self,kind,size):
                    if kind=='raw':
                        observed.extend(path.stat().st_size for path in root.rglob('.import-*'))
                        raise ResourceLimit('independent raw allowance')
            with closing(open_catalog(root)) as db:
                with self.assertRaises(ResourceLimit):importer.Importer(root,db,resource_guard=RejectRaw()).import_file(source,source_type='SYNTHETIC')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM import_artifacts').fetchone()[0],0)
            self.assertTrue(observed);self.assertEqual(set(observed),{0})
            self.assertFalse(list(root.rglob('.import-*')))
            self.assertEqual(source.read_bytes(),original)

    def test_guarded_smaller_flush_batches_preserve_gzip_and_plain_bytes(self):
        import test_wpilog as wire
        text='invented diagnostic '*450
        for profile in (PROFILE,PROFILE_AKIT26):
            if profile==PROFILE:
                original=wire.log_bytes(wire.start(2,'/Drive/LargeDiagnostic','string'),
                    *(wire.wire_record(2,(str(index)+text).encode()) for index in range(300)))
            else:
                writer=akit26_fixture.Writer();writer.value('/Timestamp','int64',1000000,1000000)
                for index in range(300):writer.value('/Drive/LargeDiagnostic','string',1000000,str(index)+text)
                original=bytes(writer.data)
            outcomes=[]
            for guarded in (False,True):
                with self.subTest(profile=profile,guarded=guarded),tempfile.TemporaryDirectory(prefix='independent smaller batch ') as folder:
                    root=Path(folder);source=root/'invented.wpilog';source.write_bytes(original)
                    guard=GenerousGuard() if guarded else None
                    with closing(open_catalog(root)) as db:
                        job=importer.Importer(root,db,resource_guard=guard).import_file(source,profile=profile,source_type='SYNTHETIC')
                        self.assertEqual(job['state'],'succeeded',job)
                        outcomes.append((job['id'],(root/job['dataset_path']).read_bytes(),(root/job['manifest_path']).read_bytes()))
                    if guard:
                        batches=[size for kind,size in guard.writes if kind=='decoded']
                        self.assertGreater(len(batches),2)
                        self.assertLessEqual(max(batches),1024*1024)
                    self.assertEqual(source.read_bytes(),original)
            self.assertEqual(outcomes[0],outcomes[1])

    def test_metadata_rejection_keeps_original_and_closes_gzip_on_interruption(self):
        for profile in (PROFILE,PROFILE_AKIT26):
            with self.subTest(profile=profile),tempfile.TemporaryDirectory(prefix='independent metadata rejection ') as folder:
                root=Path(folder);source=root/'invented.wpilog';original=self.original(profile);source.write_bytes(original)
                if profile==PROFILE_AKIT26:
                    writer=akit26_fixture.Writer()
                    writer.value('/Timestamp','int64',1000000,1000000)
                    writer.value('/Metadata/Review','string',1000000,'invented metadata')
                    original=bytes(writer.data);source.write_bytes(original)
                seen=[];gzip_objects=[];real_gzip=importer.gzip.GzipFile
                class RejectMetadata(GenerousGuard):
                    def decoded_row(self,row):
                        if row.get('category')=='metadata':
                            seen.append(row['field'])
                            raise ResourceLimit('independent metadata allowance')
                def recording_gzip(*args,**kwargs):
                    result=real_gzip(*args,**kwargs);gzip_objects.append(result);return result
                with closing(open_catalog(root)) as db,patch.object(importer.gzip,'GzipFile',side_effect=recording_gzip):
                    job=importer.Importer(root,db,resource_guard=RejectMetadata()).import_file(source,profile=profile,source_type='SYNTHETIC')
                    self.assertTrue(seen)
                    self.assertEqual((job['state'],job['error_code']),('failed','resource_limit'))
                    self.assertIsNone(job['dataset_path']);self.assertIsNone(job['manifest_path'])
                    self.assertFalse(list(root.rglob('.import-*')))
                    for value in gzip_objects:self.assertTrue(value.closed,'gzip wrapper leaked after guard rejection')
                self.assertEqual(source.read_bytes(),original)

    def test_decoded_and_manifest_growth_reject_before_the_planned_write(self):
        for profile in (PROFILE,PROFILE_AKIT26):
            for rejected in ('decoded','manifest'):
                with self.subTest(profile=profile,rejected=rejected),tempfile.TemporaryDirectory(prefix='independent planned growth ') as folder:
                    root=Path(folder);source=root/'invented.wpilog';original=self.original(profile);source.write_bytes(original);sizes=[]
                    class RejectPlanned(GenerousGuard):
                        def before_write(self,kind,size):
                            if kind==rejected:
                                suffix='.jsonl.tmp' if rejected=='decoded' else '.manifest.tmp'
                                sizes.extend(path.stat().st_size for path in root.rglob('*'+suffix))
                                raise ResourceLimit('independent planned growth allowance')
                    with closing(open_catalog(root)) as db:
                        job=importer.Importer(root,db,resource_guard=RejectPlanned()).import_file(source,profile=profile,source_type='SYNTHETIC')
                        self.assertEqual((job['state'],job['error_code']),('failed','resource_limit'))
                        self.assertIsNone(job['manifest_path']);self.assertIsNone(job['dataset_path'])
                    self.assertTrue(sizes)
                    # A gzip constructor may already have emitted its fixed header.
                    self.assertTrue(all(size<=10 for size in sizes),sizes)
                    self.assertFalse(list(root.rglob('.import-*')))
                    self.assertEqual(source.read_bytes(),original)


class StreamProofReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_stream_regression as fixtures
        from robot_test_hub import stream_regression as stream
        cls.temporary=tempfile.TemporaryDirectory(prefix='independent stream proof seed ')
        cls.base=Path(cls.temporary.name).resolve()
        cls.archive,cls.review,cls.selected=fixtures.make_fixture(cls.base)
        cls.golden=cls.base/'golden'
        cls.receipt=stream.export_case(cls.archive,review_id=cls.review['id'],review_revision=1,
            expectations=fixtures.EXPECTATIONS,output=cls.golden,limits={'min_free_bytes':0})
    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='independent stream proof case ');self.addCleanup(temporary.cleanup)
        self.folder=Path(temporary.name)/'case';shutil.copytree(self.golden,self.folder)

    def rehash(self):
        """Deliberately coordinate all transport envelope hashes, not just one file."""
        from robot_test_hub.analysis import canonical
        path=self.folder/'manifest.json';manifest=json.loads(path.read_text())
        for item in manifest['files']:
            content=(self.folder/item['path']).read_bytes()
            item.update(size_bytes=len(content),sha256=hashlib.sha256(content).hexdigest())
        base=sum(item['size_bytes'] for item in manifest['files'])+sum(p.stat().st_size for p in (self.folder/'chunks').iterdir())
        for _ in range(20):
            encoded=(canonical(manifest)+'\n').encode();size=base+len(encoded)
            if size==manifest['total_bytes']:break
            manifest['total_bytes']=size
        else:self.fail('manifest length failed to converge')
        path.write_bytes(encoded)

    def forge_saved_metric(self):
        from robot_test_hub.analysis import canonical
        path=self.folder/'saved-job.json';value=json.loads(path.read_text())
        metric=next(m for m in value['metrics'] if isinstance(m.get('value'),(int,float)))
        metric['value']+=7
        path.write_text(canonical(value)+'\n',encoding='utf-8');self.rehash()

    def rebind_descriptor(self,change):
        from robot_test_hub.analysis import canonical,identity
        from robot_test_hub import regression as legacy
        descriptor=json.loads((self.folder/'descriptor.json').read_text())
        saved=json.loads((self.folder/'saved-job.json').read_text())
        case=json.loads((self.folder/'case.json').read_text())
        change(descriptor)
        descriptor['input_digest']=identity({k:v for k,v in descriptor.items() if k not in ('input_digest','result')})
        saved['provenance']={k:v for k,v in descriptor.items() if k!='result'}
        saved['job_id']=identity([descriptor['framework'],saved['declaration'],saved['provenance'],saved['configuration']])
        case['job_id']=saved['job_id'];case['review']['job_id']=saved['job_id']
        index=case['review']['finding_index']
        case['review']['finding_identity']=identity([saved['job_id'],index,saved['findings'][index]])
        case['review_links']=legacy._review_links(case['review'])
        for name,value in [('descriptor.json',descriptor),('saved-job.json',saved),('case.json',case)]:
            (self.folder/name).write_text(canonical(value)+'\n',encoding='utf-8')
        self.rehash()

    def test_rehashed_saved_result_is_independently_recomputed(self):
        from robot_test_hub import stream_regression as stream,regression as legacy
        self.forge_saved_metric()
        with self.assertRaisesRegex(legacy.RegressionError,'regression_integrity'):
            stream.verify_case(self.folder,limits={'min_free_bytes':0})

    def test_rewrite_after_proof_cannot_claim_final_bundle_verified(self):
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof,regression as legacy
        execute=proof.execute
        def rewrite(*args,**kwargs):
            result=execute(*args,**kwargs);self.forge_saved_metric();return result
        with patch.object(proof,'execute',side_effect=rewrite):
            with self.assertRaisesRegex(legacy.RegressionError,'regression_integrity'):
                stream.verify_case(self.folder,limits={'min_free_bytes':0})

    def test_trusted_changed_calculation_requires_optin_and_runs_fresh(self):
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof
        from robot_test_hub.analysis import canonical
        from robot_test_hub.stream_motion import MotionReducer
        first=next(m for m in self.selected['metrics'] if isinstance(m.get('value'),(int,float)))
        path=self.folder/'case.json';case=json.loads(path.read_text())
        case['expectations']={'schema_version':1,'expected_outcome':self.selected['outcome'],
            'checks':[{'kind':'metric','selector':{'name':first['name'],'unit':first['unit']},'min':first['value']+7,'max':first['value']+7}]}
        path.write_text(canonical(case)+'\n',encoding='utf-8');self.rehash()
        created=[]
        class Changed(MotionReducer):
            def __init__(self):self.consumed=0;created.append(self)
            def consume_chunk(self,*args):self.consumed+=1;return super().consume_chunk(*args)
            def finish(self,*args):
                self.assert_consumed=self.consumed>0
                output=super().finish(*args)
                metric=next(m for m in output['checks']['recording-quality']['metrics'] if m['name']==first['name'] and m['unit']==first['unit'])
                metric['value']+=7
                return output
        candidate=proof.Candidate('automatic-reports-11','independent-changed-calculation-1',proof.input_contract('automatic-reports-11'),Changed,analyzer_id='recording-quality')
        with patch.dict(proof.CANDIDATES,{'independent-changed':candidate}):
            blocked=stream.run_case(self.folder,candidate='independent-changed',limits={'min_free_bytes':0})
            self.assertEqual(blocked['code'],'regression_analyzer_version_changed');self.assertEqual(created,[])
            actual=stream.run_case(self.folder,candidate='independent-changed',allow_analyzer_version_change=True,limits={'min_free_bytes':0})
        self.assertEqual(actual['state'],'passed',actual)
        self.assertTrue(created);self.assertTrue(all(value.assert_consumed for value in created))
        self.assertEqual(actual['checks'][0]['actual'],first['value']+7)
        self.assertEqual(actual['stored_input_digest'],self.receipt['stored_input_digest'])
        self.assertNotEqual(actual['current_job_id'],self.selected['job_id'])

    def test_unknown_candidate_name_never_becomes_executable_code(self):
        from robot_test_hub import stream_regression as stream
        result=stream.run_case(self.folder,candidate='os.system:case_supplied_code',allow_analyzer_version_change=True,limits={'min_free_bytes':0})
        self.assertEqual(result['state'],'unavailable')
        self.assertEqual(result['code'],'regression_analyzer_unavailable')

    def test_coordinated_final_work_and_origin_forgery_are_replayed_against_originals(self):
        from robot_test_hub import stream_regression as stream,regression as legacy
        changes=[lambda d:d['stream_reduction'].update(finalized_work_digest='f'*64),
                 lambda d:d.update(source_type='simulation')]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                shutil.rmtree(self.folder);shutil.copytree(self.golden,self.folder)
                self.rebind_descriptor(change)
                with self.assertRaisesRegex(legacy.RegressionError,'regression_integrity'):
                    stream.verify_case(self.folder,limits={'min_free_bytes':0})

    def test_coordinated_chunk_rehash_does_not_substitute_original_selected_bytes(self):
        from robot_test_hub import stream_regression as stream,regression as legacy
        from robot_test_hub.analysis import canonical
        inventory=self.folder/'chunk-inventory.jsonl'
        rows=[json.loads(line) for line in inventory.read_text().splitlines()]
        first=rows[0];chunk=self.folder/first['transport_path'];content=b' '+chunk.read_bytes();chunk.write_bytes(content)
        first.update(sha256=hashlib.sha256(content).hexdigest(),size_bytes=len(content))
        inventory.write_text(''.join(canonical(row)+'\n' for row in rows),encoding='utf-8');self.rehash()
        with self.assertRaisesRegex(legacy.RegressionError,'regression_integrity'):
            stream.verify_case(self.folder,limits={'min_free_bytes':0})

    def test_original_change_between_proof_and_candidate_stops_candidate(self):
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof,regression as legacy
        execute=proof.execute;phases=[]
        def changed(*args,**kwargs):
            phases.append(kwargs.get('candidate'))
            result=execute(*args,**kwargs)
            if kwargs.get('candidate') is None:
                original=next((self.folder/'originals').iterdir());value=bytearray(original.read_bytes());value[-1]^=1;original.write_bytes(value)
            return result
        with patch.object(proof,'execute',side_effect=changed):
            with self.assertRaisesRegex(legacy.RegressionError,'regression_integrity'):
                stream.run_case(self.folder,limits={'min_free_bytes':0})
        self.assertEqual(phases,[None])

    def test_temp_accounting_includes_bundle_partial_database_and_wal(self):
        from robot_test_hub.stream_regression_resources import ResourceLimits,WorkBudget
        with tempfile.TemporaryDirectory(prefix='independent complete work accounting ') as folder:
            root=Path(folder)
            for name,size in [('partial.copy',10),('catalog.sqlite3',20),('catalog.sqlite3-wal',30),('catalog.sqlite3-shm',40)]:
                (root/name).write_bytes(b'x'*size)
            good=WorkBudget(root,ResourceLimits(max_temp_bytes=167,transaction_headroom=16,min_free_bytes=0));good.external_bytes=50
            self.assertEqual(good.check(extra=1),100)
            bad=WorkBudget(root,ResourceLimits(max_temp_bytes=166,transaction_headroom=16,min_free_bytes=0));bad.external_bytes=50
            with self.assertRaises(ResourceLimit):bad.check(extra=1)

    def test_incompatible_registered_input_policy_is_not_enabled_by_optin(self):
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof,regression as legacy
        from robot_test_hub.stream_motion import MotionReducer
        candidate=proof.Candidate('automatic-reports-11','independent-incompatible-1',proof.input_contract('automatic-reports-10'),MotionReducer,analyzer_id='recording-quality')
        with patch.dict(proof.CANDIDATES,{'independent-incompatible':candidate}):
            with self.assertRaisesRegex(legacy.RegressionError,'regression_evidence_unsupported'):
                stream.run_case(self.folder,candidate='independent-incompatible',allow_analyzer_version_change=True,limits={'min_free_bytes':0})

    def test_version_optin_never_relabels_quality_with_another_analyzer_version(self):
        from robot_test_hub import stream_regression as stream,regression as legacy
        with self.assertRaisesRegex(legacy.RegressionError,'regression_evidence_unsupported'):
            stream.run_case(self.folder,candidate='automatic-reports-11:swerve-tracking',allow_analyzer_version_change=True,limits={'min_free_bytes':0})

    def test_close_false_retains_actual_workspace_owner_and_live_handle_until_retry(self):
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof,regression as legacy
        from robot_test_hub.storage import DataRootOwner,OwnershipError
        original=proof.StreamRunner;held=[];allow_close=[False]
        class RetainingRunner(original):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                self.held=(self.root/'independent-live-handle').open('wb');held.append(self)
            def close(self):
                if not allow_close[0]:return False
                self.held.close();return super().close()
        try:
            with patch.object(proof,'StreamRunner',RetainingRunner):
                with self.assertRaisesRegex(legacy.RegressionError,'regression_cleanup_pending'):
                    stream.verify_case(self.folder,limits={'min_free_bytes':0})
            self.assertEqual(len(held),1)
            workspace=proof._RETAINED_WORK[held[0].root.name]
            owner=workspace.owner
            self.assertFalse(held[0].held.closed);self.assertFalse(owner.file.closed)
            self.assertTrue(workspace.root.exists())
            self.assertEqual(workspace.db.execute('SELECT 1').fetchone()[0],1)
            with self.assertRaises(OwnershipError):DataRootOwner(workspace.root)
            self.assertFalse(proof.cleanup_retained_work())
            self.assertFalse(owner.file.closed);self.assertTrue(workspace.root.exists())
            allow_close[0]=True
            self.assertTrue(proof.cleanup_retained_work())
            self.assertTrue(held[0].held.closed);self.assertTrue(owner.file.closed)
            self.assertFalse(workspace.root.exists())
        finally:
            allow_close[0]=True;proof.cleanup_retained_work()

    def test_interruption_cleans_only_owned_work_and_preserves_case(self):
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof
        before={p.relative_to(self.folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.rglob('*') if p.is_file()}
        created=[];real=proof._Workspace
        class RecordedWorkspace(real):
            def __init__(self,*args):super().__init__(*args);created.append(self)
        with patch.object(proof,'_Workspace',RecordedWorkspace),patch.object(proof.StreamRunner,'step',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):stream.verify_case(self.folder,limits={'min_free_bytes':0})
        self.assertTrue(created)
        self.assertTrue(all(not workspace.root.exists() for workspace in created))
        self.assertFalse(proof.retained_work_status()['cleanup_pending'])
        after={p.relative_to(self.folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.rglob('*') if p.is_file()}
        self.assertEqual(after,before)

    def test_capture_releases_source_wal_snapshot_before_bulk_copy(self):
        import sqlite3
        from robot_test_hub import stream_regression as stream
        temporary=tempfile.TemporaryDirectory(prefix='independent capture snapshot ');self.addCleanup(temporary.cleanup)
        base=Path(temporary.name);archive=base/'archive';shutil.copytree(self.archive,archive)
        copy_file=stream.copy_file;observed=[]
        def inspect(source,*args,**kwargs):
            if not observed and Path(source).is_relative_to(archive):
                with closing(sqlite3.connect(archive/'catalog.sqlite3',timeout=.1)) as writer:
                    with writer:writer.execute('CREATE TABLE independent_writer_probe(value INTEGER)')
                    observed.append(tuple(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()))
            return copy_file(source,*args,**kwargs)
        with patch.object(stream,'copy_file',side_effect=inspect):
            result=stream.export_case(archive,review_id=self.review['id'],review_revision=1,
                expectations=json.loads((self.folder/'case.json').read_text())['expectations'],output=base/'exported',limits={'min_free_bytes':0})
        self.assertTrue(result['verified']);self.assertTrue(observed)
        self.assertEqual(observed[0][0],0,'capture held its read snapshot during bulk copy')

    def test_generation_seven_multisegment_proof_preserves_unrelated_catalog_revision(self):
        import test_wpilog as wire
        from robot_test_hub import reports,runs,stream_analysis,stream_regression as stream
        from robot_test_hub.review import ReviewStore
        def segment(robot,boot,cycles):
            data=b'WPILOG\0\1'+struct.pack('<I',12)+b'AdvantageKit'
            data+=wire.start(1,'/Timestamp','int64','{"unit":"nanoseconds"}')
            fields=[('/RealOutputs/TestHub/RobotId','string',robot.encode()),('/RealOutputs/TestHub/BootId','string',boot.encode()),
                    ('/RealOutputs/TestHub/RuntimeMode','string',b'SIM'),('/DriverStation/Enabled','boolean',b'\0'),
                    ('/Drive/Module0/Connected','boolean',b'\0')]
            for entry,(field,kind,value) in enumerate(fields,2):data+=wire.start(entry,field,kind)+wire.wire_record(entry,value)
            for stamp,enabled in cycles:
                data+=wire.wire_record(1,struct.pack('<q',stamp),stamp//1000)
                data+=wire.wire_record(5,bytes([enabled]),stamp//1000)
                data+=wire.wire_record(6,b'\0',stamp//1000)
            return data
        temporary=tempfile.TemporaryDirectory(prefix='independent multisegment generation ');self.addCleanup(temporary.cleanup)
        base=Path(temporary.name).resolve();root=base/'archive';root.mkdir()
        originals=[segment('review-robot','review-boot',[(1000000000,False),(1100000000,True),(1200000000,True)]),
                   segment('review-robot','review-boot',[(1300000000,True),(1400000000,True),(1500000000,False)]),
                   segment('unrelated-robot','unrelated-boot',[(3000000000,False),(3100000000,False)])]
        with closing(open_catalog(root)) as db:
            for index,value in enumerate(originals):
                source=base/('invented-'+str(index)+'.wpilog');source.write_bytes(value)
                job=importer.Importer(root,db).import_file(source,source_type='SYNTHETIC');self.assertEqual(job['state'],'succeeded',job)
            document=runs.rebuild_from_imports(root,db)
            self.assertEqual(len(document['runs']),1,document)
            self.assertEqual(len(document['runs'][0]['segment_ids']),2)
            runner=reports.StreamReportRunner(root,db,limits={'min_free_bytes':0,'chunk_bytes':4096})
            try:
                scheduled=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-11')
                sid=scheduled[0]['stream_id']
                with db:
                    for table in ('analysis_stream_jobs','analysis_stream_sources','analysis_stream_checkpoints'):
                        db.execute('UPDATE '+table+' SET generation=7 WHERE stream_id=?',(sid,))
                for _ in range(10000):
                    with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=reports.advance_streams(root,db,runner,max_rows=20,max_bytes=4096)
                    self.assertNotIn(progress['state'],('failed','canceled'),progress)
                    if progress.get('report'):break
                else:self.fail('no segmented report')
            finally:runner.close()
            selected=next(c for c in progress['report']['checks'] if c['analyzer_id']=='recording-quality')
            index=next(i for i,f in enumerate(selected['findings']) if f['kind']=='disconnected_observations')
            review=ReviewStore(db).review_finding(dict(id='segmented-review',job_id=selected['job_id'],finding_index=index,
                reviewer='Independent invented test',rationale='Multisegment retained-context proof',disposition='unresolved'))
            self.assertEqual(selected['provenance']['generation'],7)
            self.assertEqual(selected['provenance']['context']['run_catalog_revision'],document['revision'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0],3)
        output=base/'case'
        expectations={'schema_version':1,'expected_outcome':'finding','checks':[{'kind':'finding_count','finding_kind':'disconnected_observations','min':1,'max':1}]}
        with patch.object(runs,'derive_runs',side_effect=AssertionError('proof must retain declared historical context')):
            exported=stream.export_case(root,review_id=review['id'],review_revision=1,expectations=expectations,output=output,limits={'min_free_bytes':0})
            verified=stream.verify_case(output,limits={'min_free_bytes':0})
            candidate=stream.run_case(output,limits={'min_free_bytes':0})
        self.assertEqual(exported['original_count'],2);self.assertEqual(verified['original_count'],2)
        self.assertEqual(len(list((output/'originals').iterdir())),2)
        self.assertEqual(candidate['state'],'passed',candidate)
        self.assertEqual(candidate['current_job_id'],selected['job_id'])
        descriptor=json.loads((output/'descriptor.json').read_text())
        self.assertEqual(descriptor['generation'],7)
        self.assertEqual(descriptor['context']['run_catalog_revision'],document['revision'])
        for index,value in enumerate(originals):self.assertEqual((base/('invented-'+str(index)+'.wpilog')).read_bytes(),value)


    def test_report_budget_rejection_happens_before_sql_publication(self):
        from dataclasses import replace
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof,regression as legacy,reports
        from robot_test_hub.stream_regression_resources import WorkBudget
        meters=[];planned=[]
        class TightPublicationBudget(WorkBudget):
            def __init__(self,*args):super().__init__(*args);meters.append(self)
            def before_write(self,stage,size):
                if stage=='report':
                    planned.append(size)
                    current=self.check()
                    self.limits=replace(self.limits,max_temp_bytes=current+self.external_bytes+self.limits.transaction_headroom+size-1)
                return super().before_write(stage,size)
        source_before={p.relative_to(self.folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.rglob('*') if p.is_file()}
        with patch.object(proof,'WorkBudget',TightPublicationBudget),patch.object(reports,'publish_stream_report') as publisher:
            with self.assertRaisesRegex(legacy.RegressionError,'regression_resource_limit'):
                stream.verify_case(self.folder,limits={'min_free_bytes':0})
        publisher.assert_not_called()
        self.assertTrue(planned);self.assertGreater(planned[0],0);self.assertEqual(planned[0]%48,0)
        self.assertTrue(all(not meter.root.exists() for meter in meters))
        self.assertFalse(proof.retained_work_status()['cleanup_pending'])
        source_after={p.relative_to(self.folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.rglob('*') if p.is_file()}
        self.assertEqual(source_after,source_before)

    def test_postpublication_actual_budget_rejection_cannot_verify_or_publish_case(self):
        from dataclasses import replace
        from robot_test_hub import stream_regression as stream,stream_regression_proof as proof,regression as legacy,reports
        from robot_test_hub.stream_regression_resources import WorkBudget
        real_publish=reports.publish_stream_report
        for action in ('verify','export'):
            with self.subTest(action=action):
                meters=[];observed=[]
                class RecordedBudget(WorkBudget):
                    def __init__(self,*args):super().__init__(*args);meters.append(self)
                def publish_then_tighten(*args):
                    meter=meters[-1];before=meter.check()
                    result=real_publish(*args)
                    after=meter.check()
                    observed.append((before,after))
                    # Make the measured report boundary exceed admission by one
                    # byte; the real WorkBudget.check must reject immediately.
                    meter.limits=replace(meter.limits,max_temp_bytes=after+meter.external_bytes+meter.limits.transaction_headroom-1)
                    return result
                output=self.folder.parent/'partial-export'
                with patch.object(proof,'WorkBudget',RecordedBudget),patch.object(reports,'publish_stream_report',side_effect=publish_then_tighten):
                    with self.assertRaisesRegex(legacy.RegressionError,'regression_resource_limit'):
                        if action=='verify':stream.verify_case(self.folder,limits={'min_free_bytes':0})
                        else:stream.export_case(self.archive,review_id=self.review['id'],review_revision=1,
                            expectations=json.loads((self.folder/'case.json').read_text())['expectations'],output=output,limits={'min_free_bytes':0})
                self.assertEqual(len(observed),1);self.assertGreater(observed[0][1],observed[0][0])
                self.assertTrue(all(not meter.root.exists() for meter in meters))
                self.assertFalse(proof.retained_work_status()['cleanup_pending'])
                if action=='export':self.assertFalse((output/'manifest.json').exists())
                self.assertTrue((self.folder/'manifest.json').is_file())


class ResourcePreflightReviewTests(unittest.TestCase):
    def test_schema_and_active_held_aggregates_fail_before_decoder_materialization(self):
        import test_wpilog as wire
        from robot_test_hub.stream_regression_resources import ResourceLimits,preflight
        schema=wire.log_bytes(wire.start(2,'/.schema/struct:ReviewA','structschema'),wire.wire_record(2,b'double value;'+b' '*400),
            wire.start(3,'/.schema/struct:ReviewB','structschema'),wire.wire_record(3,b'double value;'+b' '*400))
        held=wire.log_bytes(wire.start(2,'/Drive/ReviewA','string'),wire.wire_record(2,b'a'*300),
            wire.start(3,'/Drive/ReviewB','string'),wire.wire_record(3,b'b'*300))
        released=wire.log_bytes(wire.start(2,'/Drive/ReviewA','string'),wire.wire_record(2,b'a'*300),
            wire.wire_record(0,b'\1'+struct.pack('<I',2)),wire.start(3,'/Drive/ReviewB','string'),wire.wire_record(3,b'b'*300))
        with tempfile.TemporaryDirectory(prefix='independent raw allocation preflight ') as folder:
            source=Path(folder)/'invented.wpilog'
            source.write_bytes(schema)
            with self.assertRaisesRegex(ResourceLimit,'schema_resource_limit'):preflight(source,ResourceLimits(schema_bytes=600,held_payload_bytes=4096))
            self.assertEqual(source.read_bytes(),schema)
            source.write_bytes(held)
            with self.assertRaisesRegex(ResourceLimit,'held_resource_limit'):preflight(source,ResourceLimits(held_payload_bytes=500))
            self.assertEqual(source.read_bytes(),held)
            source.write_bytes(released);preflight(source,ResourceLimits(held_payload_bytes=500))
            self.assertEqual(source.read_bytes(),released)

    def test_decoded_held_and_metadata_budgets_reset_between_selected_originals(self):
        from robot_test_hub.stream_regression_resources import ResourceLimits,WorkBudget
        with tempfile.TemporaryDirectory(prefix='independent decoded allocation ') as folder:
            guard=WorkBudget(Path(folder),ResourceLimits(held_row_bytes=500,metadata_values_bytes=500,max_metadata_fields=1,min_free_bytes=0))
            row=lambda field:dict(kind='observation',category='input',field=field,value='x'*200)
            guard.decoded_row(row('/Drive/ReviewA'))
            with self.assertRaisesRegex(ResourceLimit,'held_resource_limit'):guard.decoded_row(row('/Drive/ReviewB'))
            guard.begin_decode();guard.decoded_row(row('/Drive/ReviewB'))
            guard.begin_decode()
            guard.decoded_row(dict(kind='observation',category='metadata',field='/Metadata/ReviewA',value='invented'))
            with self.assertRaisesRegex(ResourceLimit,'metadata_resource_limit'):
                guard.decoded_row(dict(kind='observation',category='metadata',field='/Metadata/ReviewB',value='invented'))
            guard.begin_decode();guard.decoded_row(dict(kind='observation',category='metadata',field='/Metadata/ReviewB',value='invented'))


if __name__=='__main__':unittest.main()
