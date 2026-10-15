"""Execute read-only history browser logic with an isolated DOM/fetch harness."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ''; this.disabled = false; this.attributes = {}; this._text = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
}
const source = fs.readFileSync(process.argv[1], 'utf8');
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
function browser(hash = '') {
  const elements = new Map(), handlers = {}, location = {hash}, calls = [];
  const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const state = {defer: false, pending: [], fail: null, latest: 2};
  const row = (kind, id, revision) => ({kind, id, revision, record: {id, revision, reviewer: '<img src=x onerror=attack()>', display_projection: true}});
  function reply(url) {
    const parsed = new URL(url, 'http://127.0.0.1'), q = parsed.searchParams, kind = q.get('kind'), id = q.get('id'), revision = Number(q.get('revision'));
    const common = {schema_version: 1, read_only: true, kind};
    if (parsed.pathname.endsWith('/history')) {
      const chosen = q.get('record_id'), page = q.get('cursor');
      return {...common, record_id: chosen, records: chosen ? [row(kind, chosen, 2), row(kind, chosen, 1)] : Array.from({length: 20}, (_, i) => row(kind, kind + '_' + (page ? i + 20 : i), state.latest)), next_cursor: chosen || page ? null : 'page-2', snapshot: {highwater: 100, schema_version: 6}};
    }
    const exact = {...common, id, revision};
    if (parsed.pathname.endsWith('/record')) return {...exact, record: {id, revision, rationale: '<img src=x onerror=attack()>'}, links: [{relation: 'maintenance', state: 'unpinned', kind: 'maintenance', id: 'repair-old'}]};
    return {...exact, nodes: [
      {relation: 'maintenance', state: 'unpinned', kind: 'maintenance', id: 'repair-old'},
      {relation: 'embedded_review', state: 'embedded', kind: 'finding_review', id: 'prior', revision: 1, record: {rationale: 'saved old review'}},
      {relation: 'incident_job', state: 'pinned', job_id: 'job-old', job: {outcome: 'finding'}, report_links: [{run_id: 'run-old', report_id: 'report-old', url: 'javascript:attack()'}]},
      {relation: 'validation_job', state: 'missing'},
      {relation: 'unreadable_job', state: 'unavailable', reason: 'invalid_or_oversized_analysis_job'}
    ], limitations: ['Historical references do not prove repair effectiveness.']};
  }
  const fetch = async url => {
    calls.push(url);
    if (state.defer) return new Promise(resolve => state.pending.push(() => resolve({ok: true, status: 200, json: async () => reply(url)})));
    if (state.fail) return {ok: false, status: state.fail, json: async () => ({error_code: 'review_history_changed'})};
    return {ok: true, status: 200, json: async () => reply(url)};
  };
  const context = {
    document: {getElementById: element, createElement: tag => new Element(tag)}, location,
    history: {pushState(_state, _unused, hash) { location.hash = hash; }},
    window: {addEventListener(name, action) { (handlers[name] ||= []).push(action); }},
    URLSearchParams, AbortController, setTimeout, clearTimeout, fetch, console
  };
  vm.runInNewContext(source, context);
  return {element, state, location, calls, emit: name => (handlers[name] || []).forEach(action => action()),
    click: id => element(id).listeners.click({button: 0, preventDefault() {}})};
}
(async () => {
  const b = browser(); await settle();
  assert.equal(b.element('history-identities').children.length, 20);
  assert.equal(b.element('history-ids-next').disabled, false);
  b.click('history-ids-next'); await settle();
  assert.equal(b.element('history-identities').children.length, 20, 'pages replace DOM');
  assert.match(b.element('history-identities').textContent, /assignment_20/);
  assert.equal(b.element('history-ids-previous').disabled, false);
  b.click('history-ids-previous'); await settle();
  assert.match(b.element('history-identities').textContent, /assignment_0/);
  assert.doesNotMatch(b.element('history-identities').textContent, /assignment_20/);

  const pinned = browser('#review-history?kind=finding_review&id=review-old&revision=1&ids_cursor=shared-page');
  await settle();
  assert.equal(pinned.element('history-ids-previous').disabled, true, 'direct cursor has no invented previous page');
  assert.equal(pinned.element('history-ids-first').disabled, false);
  assert.match(pinned.element('history-exact').textContent, /revision 1/);
  assert.match(pinned.element('history-exact').textContent, /<img src=x onerror=attack\(\)>/);
  assert.match(pinned.element('history-chain').textContent, /not substituted/);
  assert.match(pinned.element('history-chain').textContent, /missing/);
  assert.match(pinned.element('history-chain').textContent, /invalid or oversized analysis job/);
  assert.match(pinned.element('history-status').textContent, /Saved history ready/);
  const walk = node => [node, ...node.children.flatMap(walk)];
  assert.equal(walk(pinned.element('history-exact')).some(node => node.tagName === 'img'), false);
  const links = walk(pinned.element('history-chain')).filter(node => node.tagName === 'a');
  assert.equal(links.length, 1, 'unpinned/embedded references do not silently link latest');
  assert.equal(links[0].href, '/api/v1/runs/run-old/reports/report-old', 'URL rebuilt from validated pinned IDs');
  pinned.click('history-refresh'); await settle();
  assert.equal(new URLSearchParams(pinned.location.hash.split('?')[1]).get('revision'), '1', 'refresh retains exact pin');
  assert.equal(new URLSearchParams(pinned.location.hash.split('?')[1]).get('ids_cursor'), null);
  pinned.location.hash = '#review-history?kind=finding_review&id=review-old&revision=2'; pinned.emit('popstate'); await settle();
  assert.match(pinned.element('history-exact').textContent, /revision 2/);
  pinned.location.hash = '#review-history?kind=finding_review&id=review-old&revision=1'; pinned.emit('hashchange'); await settle();
  assert.match(pinned.element('history-exact').textContent, /revision 1/);

  const race = browser(); await settle(); race.state.defer = true;
  race.click('history-refresh');
  race.state.defer = false; race.element('history-kind').value = 'maintenance'; race.element('history-kind').listeners.change(); await settle();
  for (const respond of race.state.pending.splice(0)) respond(); await settle();
  assert.match(race.element('history-identities').textContent, /maintenance_0/);
  assert.doesNotMatch(race.element('history-identities').textContent, /assignment_0/, 'old fetch cannot retarget active kind');
  race.state.defer = true; race.click('history-refresh'); race.emit('review-unavailable');
  for (const respond of race.state.pending.splice(0)) respond(); await settle();
  assert.equal(race.element('history-identities').children.length, 0, 'unavailable clears stale evidence');
  assert.equal(race.element('history-exact').children.length, 0);
  assert.equal(race.element('history-ids-next').disabled, true);
  race.state.defer = false; race.state.fail = 409; race.click('history-refresh'); await settle();
  assert.match(race.element('history-status').textContent, /snapshot changed/);
  assert.equal(race.element('history-identities').children.length, 0);
  race.state.fail = null; race.click('history-refresh'); await settle();
  assert.match(race.element('history-identities').textContent, /maintenance_0/);
  race.emit('offline');
  assert.equal(race.element('history-identities').children.length, 0);
  assert.equal(race.element('history-ids-next').disabled, true);
  console.log('bounded pages, exact pins, safe text/links, back/forward, races, snapshot refresh and source-loss checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class ReviewHistoryUITests(unittest.TestCase):
    @unittest.skipUnless(NODE, "Node is optional for isolated browser-logic tests")
    def test_bounded_readonly_history_behavior(self):
        result = subprocess.run([NODE, "-e", HARNESS, str(ROOT / "robot_test_hub/static/review-history.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source-loss checks passed", result.stdout)

    def test_existing_forms_and_history_accessibility_are_present(self):
        source = (ROOT / "robot_test_hub/static/review.html").read_text(encoding="utf-8")
        for identity in ("analysis-plan", "baseline", "assignment", "maintenance", "finding", "bundle"):
            self.assertIn('<form id="' + identity + '">', source)
        self.assertIn('aria-labelledby="review-history-heading"', source)
        self.assertIn('aria-label="Selected record revision pages"', source)
        self.assertIn('<script src="/review-history.js"></script>', source)
        script = (ROOT / "robot_test_hub/static/review-history.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("method: 'POST'", script)
