"""Read-only run/time HTTP projections; no source or filesystem mutation."""
from contextlib import closing
import sqlite3
from .runs import RunCatalog
from .timebase import local_candidates
from .reports import public_for_run


def get(root, path, query, notebook):
    if path=='/api/v1/time/resolve':
        if query.keys() != {'local','timezone'}:
            raise ValueError('Specify local and timezone')
        return {'schema_version':1,'candidates':local_candidates(query['local'],query['timezone'])}
    with closing(sqlite3.connect(root/'catalog.sqlite3',timeout=2)) as db:
        catalog=RunCatalog(db)
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
            for run in catalog.current()['runs']:
                if run['run_id']==identity:
                    candidates={}
                    for interval in run['utc_intervals']:
                        notes=notebook.list(start_ns=interval['utc_start_ns'],end_ns=interval['utc_end_ns'],include_unknown=False)
                        for note in notes['annotations']:
                            candidates[note['event_id']]=note
                    reports,display=public_for_run(db,identity)
                    return {'schema_version':1,'run':run,'reports':reports,'reports_display':display,'annotation_candidates':list(candidates.values()),
                            'annotation_basis':'UTC interval overlap; candidate association, not robot acknowledgment'}
            raise KeyError('run_not_found')
    raise KeyError('route_not_found')
