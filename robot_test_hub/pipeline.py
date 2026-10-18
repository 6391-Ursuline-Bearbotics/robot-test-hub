"""Local-only processing of verified closed recordings, independent of transfers."""
import json

from .importer import Importer, EXTRACTOR_VERSION, MAPPING_REVISION
from .runs import rebuild_from_imports
from .reports import generate
from .analysis_plan import review_snapshot


class Pipeline:
    def __init__(self, root, db, *, stream_limits=None):
        self.root,self.db=root,db
        self.importer=Importer(root,db)
        self.last_signature=None
        self.stream_runner=None
        self.stream_limits=stream_limits
        self.closed=False

    def tick(self):
        if self.closed:raise RuntimeError('Pipeline is closed')
        from .report_lookup import has_schema, step
        if has_schema(self.db):
            step(self.db)
        from .run_lookup import has_schema as has_run_lookup, tick as run_lookup_tick
        if has_run_lookup(self.db):
            run_lookup_tick(self.db)
        from .stream_analysis import has_schema as has_streams
        if has_streams(self.db):
            if self.stream_runner is None:
                from .reports import StreamReportRunner
                self.stream_runner=StreamReportRunner(self.root,self.db,limits=self.stream_limits)
            from .reports import advance_streams
            advance_streams(self.root,self.db,self.stream_runner)
        from .import_retry import step as retry_step
        retry_processed=retry_step(self.root,self.db)
        candidates=self.db.execute("""SELECT f.id,f.sha256,m.metadata_json
            FROM files f JOIN verification_jobs v ON v.file_id=f.id
            JOIN manifest_items m ON m.file_id=f.id
            WHERE f.state='complete' AND v.state='complete'
            AND m.snapshot_id=(SELECT MAX(m2.snapshot_id) FROM manifest_items m2 WHERE m2.file_id=f.id)
            ORDER BY f.created_at DESC""").fetchall()
        for candidate in ([] if retry_processed else candidates):
            metadata=json.loads(candidate['metadata_json'])
            if metadata.get('format')!='wpilog':
                continue
            profile=metadata.get('format_profile')
            existing=self.db.execute('''SELECT state FROM import_jobs WHERE artifact_sha256=? AND
                extractor_version=? AND profile=? AND mapping_revision=?''',
                (candidate['sha256'],EXTRACTOR_VERSION,profile or 'unspecified',MAPPING_REVISION)).fetchone()
            if existing is not None and existing['state'] not in ('pending','running'):
                continue
            job=self.importer.import_file(self.root/'archive'/(candidate['id']+'.logdata'),
                profile=profile or 'unspecified',expected_sha256=candidate['sha256'],source_type='VERIFIED_TRANSFER')
            format_state={'succeeded':'valid','succeeded_with_unsupported':'valid_with_unsupported',
                          'invalid':'invalid','unsupported':'unsupported'}.get(job['state'],'not_checked')
            with self.db:
                self.db.execute('UPDATE transfer_meta SET format_status=? WHERE file_id=?',(format_state,candidate['id']))
                self.db.execute('UPDATE verification_jobs SET format_status=? WHERE file_id=?',(format_state,candidate['id']))
            break  # One recording per worker tick; status/collection remain independent.
        imports=tuple((r['id'],r['dataset_sha256']) for r in self.db.execute(
            "SELECT id,dataset_sha256 FROM import_jobs WHERE state IN ('succeeded','succeeded_with_unsupported') ORDER BY id"))
        signature=(imports,review_snapshot(self.db)['revision'])
        if signature!=self.last_signature:
            document=rebuild_from_imports(self.root,self.db)
            reports=generate(self.root,self.db,document)
            self.last_signature=signature
            self.metrics_document=document
            return {'state':'indexed','imports':len(imports),'runs':len(document['runs']),'reports':len(reports),'revision':document['revision']}
        # Per-run measurements, one dataset per tick so status and collection stay responsive.
        from . import run_metrics
        document=getattr(self,'metrics_document',None)
        if document is not None:
            computed=run_metrics.step(self.root,self.db,document)
            if computed:
                # Indexing is idle; measurements continue on following ticks.
                return {'state':'idle','imports':len(imports),'measured_runs':computed}
            self.metrics_document=None
        return {'state':'idle','imports':len(imports)}

    def close(self):
        """Keep the worker owner alive until stream resources actually close."""
        if self.stream_runner is not None and self.stream_runner.close() is not True:return False
        self.closed=True
        return True
