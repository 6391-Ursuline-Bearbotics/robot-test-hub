"""Versioned, bounded queue projections for the local HTTP host."""
import base64
import json
import math


def status_view(snapshot):
    return {key: value for key, value in snapshot.items() if key != 'files'}


def transfer_page(snapshot, *, limit=50, cursor=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Limit must be between 1 and 100')
    rows = snapshot.get('files', [])
    after = None
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor)>512:
            raise ValueError('Invalid cursor')
        try:
            after = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            if (not isinstance(after,list) or len(after)!=2 or type(after[0]) not in (int,float)
                    or not math.isfinite(after[0]) or not isinstance(after[1],str)):
                raise ValueError('Invalid cursor')
        except (ValueError, TypeError, OverflowError):
            raise ValueError('Invalid cursor') from None
    # This key order is stable when progress changes or newer files arrive.
    rows = sorted(rows,key=lambda r:(-r['created_at'],r['id']))
    if after is not None:
        rows=[r for r in rows if (-r['created_at'],r['id']) > (-after[0],after[1])]
    selected=rows[:limit]
    next_cursor=None
    if len(rows)>limit:
        last=selected[-1]
        next_cursor=base64.urlsafe_b64encode(json.dumps([last['created_at'],last['id']]).encode()).decode()
    return {'schema_version':1,'items':selected,'next_cursor':next_cursor,
            'discovery_complete':snapshot.get('discovery_complete',False),
            'manifest_revision':snapshot.get('manifest_revision'),
            'queue_is_last_known':snapshot.get('queue_is_last_known',True)}
