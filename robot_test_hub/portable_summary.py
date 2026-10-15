"""Render a complete immutable summary as a self-contained offline explorer.

Only packaged JavaScript is executable. Summary text is serialized into a
non-executable JSON script and can never alter either code or its CSP hash.
"""
import base64
import hashlib
from importlib import resources
import json
import re

PLACEHOLDERS=('%%SUMMARY_DATA%%','%%SUMMARY_SCRIPT%%','%%SCRIPT_CSP%%')


class PresentationError(ValueError):
    pass


def render_report(summary):
    """Return standalone HTML containing every saved run, without runtime I/O."""
    static=resources.files('robot_test_hub').joinpath('static')
    template=static.joinpath('summary_explorer.html').read_text(encoding='utf-8')
    script=static.joinpath('summary_explorer.js').read_text(encoding='utf-8')
    if any(template.count(token)!=1 for token in PLACEHOLDERS) or re.search(r'</script',script,re.IGNORECASE):
        raise PresentationError('summary_explorer_assets_invalid')
    payload=json.dumps(summary,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    for character,escaped in (('<',r'\u003c'),('>',r'\u003e'),('&',r'\u0026'),('\u2028',r'\u2028'),('\u2029',r'\u2029')):
        payload=payload.replace(character,escaped)
    csp='sha256-'+base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode('ascii')
    values=dict(zip(PLACEHOLDERS,(payload,script,csp)))
    # Never rescan substituted data: a saved label may itself contain a token.
    return re.sub('|'.join(re.escape(token) for token in PLACEHOLDERS),lambda match:values[match.group()],template)
