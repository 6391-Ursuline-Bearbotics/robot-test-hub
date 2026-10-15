"""Read-only run/time HTTP projections; no source or filesystem mutation."""
from contextlib import closing
import sqlite3
from pathlib import Path
from . import run_notes
from .runs import RunCatalog
from .timebase import local_candidates
from .reports import public_for_run


def get(root, path, query, notebook=None):
    if path=='/api/v1/time/resolve':
        if query.keys() != {'local','timezone'}:
            raise ValueError('Specify local and timezone')
        return {'schema_version':1,'candidates':local_candidates(query['local'],query['timezone'])}
    if path.startswith('/api/v1/runs/') and path.endswith('/annotations'):
        return run_notes.get(root,path,query)
    with closing(sqlite3.connect((Path(root).resolve()/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        catalog=RunCatalog(db)
        if path.startswith('/api/v1/runs/') and '/reports' in path:
            from . import report_api
            return report_api.get(db,path,query)
        if path=='/api/v1/runs':
            if query.keys()-{'from','to','robot','include_unknown','offset','limit'}:
                raise ValueError('Invalid run query')
            unknown=query.get('include_unknown','false')
            if unknown not in ('true','false'):
                raise ValueError('Invalid include_unknown')
            return {'schema_version':1,**catalog.search(
                utc_from_ns=int(query['from']) if 'from' in query else None,
                utc_to_ns=int(query['to']) if 'to' in query else None,
                robot=query.get('robot'),include_unknown=unknown=='true',
                offset=int(query.get('offset','0')),limit=int(query.get('limit','50')))}
        prefix='/api/v1/runs/'
        if path.startswith(prefix) and not query:
            identity=path[len(prefix):]
            run,revision=run_notes.select_run(db,identity)
            try:
                annotations=run_notes.page(db,identity,{},revision=revision)
            except run_notes.RunNotesError as exc:
                if exc.status!=503:raise
                annotations=dict(schema_version=1,run_id=identity,run_catalog_revision=revision,
                    annotation_watermark=None,annotations=[],total=None,returned=0,limit=20,next_cursor=None,
                    candidate_basis=run_notes.BASIS,clock_uncertainty_applied=False,availability='unavailable',
                    reason='run_notes_unavailable')
            reports,display=public_for_run(db,identity)
            return {'schema_version':1,'run':run,'run_catalog_revision':annotations['run_catalog_revision'],
                    'reports':reports,'reports_display':display,'annotation_candidates':annotations['annotations'],
                    'annotations_display':{key:value for key,value in annotations.items() if key!='annotations'},
                    'annotation_basis':'UTC interval overlap; candidate association, not robot acknowledgment'}
    raise KeyError('route_not_found')
