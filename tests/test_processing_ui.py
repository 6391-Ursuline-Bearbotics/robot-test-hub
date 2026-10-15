"""Isolated processing UI behavior; no network, archive writes or hardware."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ''; this.disabled = false; this.hidden = false; this._text = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(' '); }
  addEventListener(name, action) { this.listeners[name] = action; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = value; }
}
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
const sha = number => number.toString(16).padStart(64, '0');
function browser(hash = '') {
  const elements = new Map(), listeners = {}, location = {hash}, calls = [];
  const control = {pending: [], defer: false, fail: null, mismatch: false, reportsUnavailable: false, reportsPartial: false, badRunCount: false, retryMode: false, postBehavior: 'success', audit: {}, posts: [], pendingPosts: [], enqueued: 0, attempts: 3, stamp: '9007199254740993123', workerState: 'running', now: 10000};
  const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const recording = number => ({sha256: sha(number), label: 'ignored private filename', source_types: number === 0 ? ['synthetic_demo'] : number === 1 ? ['MANUAL_LOCAL', 'TRANSFER_CHECKSUM_MISMATCH'] : number === 2 ? ['VERIFIED_TRANSFER'] : ['<img src=x onerror=attack()>'],
    original: {state: number % 3 ? 'verified' : 'expected', transfer_count: 1, artifact_count: 0},
    imports: {state: number === 0 ? 'not_applicable' : number === 1 ? 'failed' : number === 2 ? 'checksum_mismatch' : 'succeeded', current_profile: 'alpha7', extractor_version: 2, mapping_revision: 'm1', current_jobs: 1, total_jobs: 2},
    indexing: {state: number === 0 ? 'not_applicable' : number === 1 ? 'searchable_previous_import' : 'searchable', run_count: control.badRunCount ? null : number ? 1 : 0},
    reports: control.reportsPartial ? {state: 'partial', count: null, known_count: 25} : {state: number === 0 ? 'not_applicable' : number === 1 ? 'unavailable' : 'available', count: number === 1 ? null : number > 1 ? 1 : 0}});
  function reply(url) {
    const parsed = new URL(url, 'http://127.0.0.1'), q = parsed.searchParams, section = q.get('section');
    const common = {schema_version: 1, read_only: true, hardware_qualified: false, catalog_revision: 'catalog1', indexer: {state: control.workerState, error_code: null}};
    if (parsed.pathname === '/api/v1/processing') {
      const offset = q.get('cursor') ? Number(q.get('cursor').slice(1)) : 0, attention = q.get('view') === 'attention';
      const total = attention ? 3 : 125, numbers = attention ? [1, 101, 124] : Array.from({length: Math.min(20, total - offset)}, (_, i) => i + offset);
      return {...common, items: numbers.map(recording), next_cursor: offset + 20 < total ? 'c' + (offset + 20) : null,
        limit: 20, returned: numbers.length, total, counts: {recordings: 125, attention: 3}, membership: 'live_sha_keyset'};
    }
    const selected = parsed.pathname.split('/').at(-1), number = Number.parseInt(selected.slice(-4), 16);
    if (!section) return {...common, recording: recording(number), sections: Object.fromEntries(['transfers', 'imports', 'runs', 'reports'].map(name => [name, {total: name === 'reports' && control.reportsUnavailable ? null : 25, availability: name === 'reports' && control.reportsUnavailable ? 'unavailable' : 'available', url: 'javascript:attack()'}]))};
    if (section === 'reports' && control.reportsUnavailable) return {...common, sha256: selected, section, availability: 'unavailable', reason: 'report_lookup_budget', items: [], total: null, returned: 0, limit: 20, next_cursor: null};
    const offset = q.get('cursor') ? Number(q.get('cursor').slice(1)) : 0;
    const items = Array.from({length: Math.min(20, 25 - offset)}, (_, i) => {
      const n = i + offset;
      if (section === 'runs') return {run_id: 'run-' + n, url: 'javascript:attack()', current_toolchain: false, catalog_revision: 'catalog1'};
      if (section === 'reports') return {run_id: 'run-' + n, report_id: 'report-' + n, url: 'javascript:attack()', sha256: sha(n)};
      if (section === 'imports') {
        if (!control.retryMode) return {id: 'attempt-' + n, profile: 'alpha7', state: 'failed', error_code: 'invalid_recording', extractor_version: 2, mapping_revision: 'm1', supported_current_toolchain: false, private_path: 'DO_NOT_RENDER_PRIVATE_PATH'};
        const states = ['failed', 'failed', 'invalid', 'unsupported', 'checksum_mismatch', 'succeeded', 'running', 'pending', 'failed', 'failed'];
        const state = states[n] || 'unsupported', oldVersion = n === 8;
        const error = n === 0 ? 'import_io_or_integrity' : n === 1 ? 'extractor_failure' : n === 9 ? 'resource_limit' : 'invalid_format';
        const pins = {job_id: 'attempt-' + n, source_sha256: selected, profile: 'alpha7', extractor_version: oldVersion ? 'old-reader' : 'extractor-1', mapping_revision: 'm1', expected_attempts: control.attempts, expected_updated_utc_ns: control.stamp};
        return {id: pins.job_id, source_sha256: selected, profile: pins.profile, extractor_version: pins.extractor_version, mapping_revision: pins.mapping_revision, attempts: control.attempts, updated_utc_ns: control.stamp, state, error_code: error, supported_current_toolchain: !oldVersion,
          retry: {eligible: n < 2, reason: n < 2 ? 'eligible' : oldVersion ? 'unsupported_version' : state !== 'failed' ? 'state_not_retryable' : 'error_not_retryable', pins}, latest_retry_request: control.audit[pins.job_id] || null};
      }
      return {id: 'transfer-' + n, state: 'complete', verification_state: 'verified', verified: true, format: 'not_wpilog'};
    });
    return {...common, catalog_revision: control.mismatch ? 'changed-catalog' : common.catalog_revision, sha256: selected, section, items, availability: section === 'reports' && control.reportsPartial ? 'partial' : 'available', known_total: 25, total: section === 'reports' && control.reportsPartial ? null : 25, returned: items.length, limit: 20, next_cursor: offset ? null : 'c20'};
  }
  const fetch = async (url, options) => {
    calls.push({url, options});
    if (options?.method === 'POST') {
      const payload = JSON.parse(options.body); control.posts.push(payload);
      if (control.postBehavior === 'conflict') return {ok: false, status: 409};
      let receipt = control.audit[payload.job_id];
      if (!receipt) {
        control.enqueued++;
        const {request_id, ...pins} = payload;
        receipt = {id: control.postBehavior === 'dedup' ? 'a'.repeat(32) : request_id, ...pins, state: 'pending', error_code: null,
          created_utc_ns: '9007199254740993999', updated_utc_ns: '9007199254740993999', attempt_updated_utc_ns: null,
          result_state: null, result_attempts: null, result_updated_utc_ns: null}; control.audit[payload.job_id] = receipt;
      }
      const respond = () => ({ok: true, status: 202, json: async () => ({schema_version: 1, request: receipt, idempotent: control.posts.length > 1, deduplicated: control.postBehavior === 'dedup'})});
      if (control.postBehavior === 'lost') throw Error('Simulated lost acknowledgement');
      if (control.postBehavior === 'defer') return new Promise(resolve => control.pendingPosts.push(() => resolve(respond())));
      return respond();
    }
    if (control.defer) return new Promise(resolve => control.pending.push(() => resolve({ok: true, status: 200, json: async () => reply(url)})));
    return control.fail ? {ok: false, status: control.fail} : {ok: true, status: 200, json: async () => reply(url)};
  };
  const context = {document: {getElementById: element, createElement: tag => new Element(tag)}, location,
    history: {pushState(_value, _unused, hash) { location.hash = hash; }},
    window: {addEventListener(name, action) { (listeners[name] ||= []).push(action); }},
    Date: class extends Date { static now() { return control.now; } }, URLSearchParams, AbortController, setTimeout, clearTimeout, fetch, console, crypto: require('node:crypto')};
  vm.runInNewContext(source, context);
  return {element, location, control, calls, emit(name, detail) { for (const action of listeners[name] || []) action({detail}); },
    click(id) { element(id).listeners.click({button: 0, preventDefault() {}}); },
    change(id, value) { element(id).value = value; element(id).listeners.change(); }};
}
const allNodes = node => [node, ...node.children.flatMap(allNodes)];
(async () => {
  const b = browser(); await settle();
  assert.equal(b.element('processing-recordings').children.length, 20);
  assert.match(b.element('processing-counts').textContent, /125 recordings.*3 imports needing attention/);
  assert.match(b.element('processing-recordings').textContent, /Not a WPILOG/);
  assert.match(b.element('processing-recordings').textContent, /Import failed/);
  assert.match(b.element('processing-recordings').textContent, /Manually imported local recording/);
  assert.match(b.element('processing-recordings').textContent, /Verified source transfer/);
  assert.match(b.element('processing-recordings').textContent, /Original checksum mismatch/);
  assert.match(b.element('processing-recordings').textContent, /from a previous import/);
  assert.match(b.element('processing-recordings').textContent, /Report references unavailable/);
  assert.equal(allNodes(b.element('processing-recordings')).some(node => node.tagName === 'img'), false);
  for (let i = 0; i < 6; i++) { b.click('processing-next'); await settle(); }
  assert.equal(b.element('processing-recordings').children.length, 5, '>100 recordings remain reachable');
  const lastPageLinks = allNodes(b.element('processing-recordings')).filter(node => node.tagName === 'a');
  assert.equal(new URLSearchParams(lastPageLinks.at(-1).href.split('?')[1]).get('sha'), sha(124));
  assert.equal(b.element('processing-next').disabled, true);
  b.click('processing-previous'); await settle();
  assert.equal(b.element('processing-recordings').children.length, 20, 'bounded replacement');
  b.change('processing-view', 'attention'); await settle();
  assert.equal(b.element('processing-recordings').children.length, 3);
  assert.match(b.element('processing-page-status').textContent, /needing import attention/);
  b.emit('hub-status-available', {state: 'waiting', workers: {indexer: {state: 'failed'}}});
  assert.match(b.element('processing-worker').textContent, /Indexer failed/);
  b.emit('hub-status-available', {state: 'waiting', workers: {indexer: {state: 'running'}}});
  assert.match(b.element('processing-worker').textContent, /Indexer failed/, 'generic worker running does not erase cached processing failure');
  assert.equal(b.element('processing-recordings').children.length, 3, 'failed indexer does not erase historical facts');

  const selected = browser('#processing?view=all&sha=' + sha(1) + '&section=reports&cursor=c100'); await settle();
  assert.equal(selected.element('processing-first').disabled, false);
  assert.equal(selected.element('processing-previous').disabled, true);
  assert.equal(selected.element('processing-references').children.length, 20);
  const links = allNodes(selected.element('processing-references')).filter(node => node.tagName === 'a');
  assert.equal(links[0].href, '/api/v1/runs/run-0/reports/report-0', 'exact URL rebuilt from validated IDs');
  assert.equal(links.some(link => link.href.startsWith('javascript:')), false);
  selected.click('processing-reference-next'); await settle();
  assert.equal(selected.element('processing-references').children.length, 5);
  selected.click('processing-reference-previous'); await settle();
  assert.equal(selected.element('processing-references').children.length, 20);
  selected.change('processing-section', 'imports'); await settle();
  assert.match(selected.element('processing-references').textContent, /not a supported current-toolchain import/);
  assert.doesNotMatch(selected.element('processing-references').textContent, /DO_NOT_RENDER_PRIVATE_PATH/);
  selected.click('processing-refresh'); await settle();
  assert.equal(new URLSearchParams(selected.location.hash.split('?')[1]).get('sha'), sha(1));
  selected.location.hash = '#processing?view=all&sha=' + sha(2) + '&section=runs'; selected.emit('popstate'); await settle();
  assert.equal(allNodes(selected.element('processing-references')).find(node => node.tagName === 'a').href, '/api/v1/runs/run-0');
  assert.match(selected.element('processing-references').textContent, /Open current run details/);
  selected.control.mismatch = true; selected.click('processing-refresh'); await settle();
  assert.match(selected.element('processing-status').textContent, /changed while loading references/);
  assert.equal(selected.element('processing-references').children.length, 0, 'mixed catalog revisions never publish selected claims');

  const partial = browser('#processing?view=all&sha=' + sha(1) + '&section=reports'); partial.control.reportsUnavailable = true; await settle();
  assert.equal(partial.element('processing-recordings').children.length, 20, 'unknown report counts do not erase other stages');
  assert.match(partial.element('processing-record-summary').textContent, /hash verification recorded/);
  assert.match(partial.element('processing-record-summary').textContent, /Import failed/);
  assert.match(partial.element('processing-record-summary').textContent, /from a previous import/);
  assert.match(partial.element('processing-record-summary').textContent, /Report references unavailable/);
  assert.match(partial.element('processing-references').textContent, /No report count is claimed/);
  assert.match(partial.element('processing-references').textContent, /Run history/);
  assert.equal(partial.element('processing-section').disabled, false);
  assert.equal(partial.element('processing-reference-next').disabled, true);
  partial.change('processing-section', 'runs'); await settle();
  assert.equal(partial.element('processing-references').children.length, 20, 'other reference sections remain usable');
  const known = browser('#processing?view=all&sha=' + sha(1) + '&section=reports'); known.control.reportsPartial = true; await settle();
  assert.match(known.element('processing-record-summary').textContent, /25 known saved report/);
  assert.match(known.element('processing-references').textContent, /full report count is unknown/);
  assert.equal(allNodes(known.element('processing-references')).filter(node => node.tagName === 'a').length, 20);
  assert.match(known.element('processing-reference-status').textContent, /known references; full total unknown/);
  assert.equal(known.element('processing-reference-next').disabled, false);
  known.click('processing-reference-next'); await settle();
  assert.equal(allNodes(known.element('processing-references')).filter(node => node.tagName === 'a').length, 5);
  const invalid = browser(); invalid.control.badRunCount = true; await settle();
  assert.equal(invalid.element('processing-recordings').children.length, 0, 'null counts remain invalid for other stages');
  assert.match(invalid.element('processing-status').textContent, /Invalid recording progress count/);

  const race = browser(); await settle(); race.control.defer = true; race.click('processing-refresh');
  const pendingCount = race.calls.length; race.control.now += 6000;
  race.emit('hub-status-available', {state: 'waiting', workers: {indexer: {state: 'running'}}});
  assert.equal(race.calls.length, pendingCount, 'status polling does not overlap requests');
  race.control.defer = false; race.change('processing-view', 'attention'); await settle();
  for (const respond of race.control.pending.splice(0)) respond(); await settle();
  assert.equal(race.element('processing-recordings').children.length, 3, 'stale earlier page cannot retarget new view');
  race.control.defer = true; race.click('processing-refresh'); race.emit('hub-status-unavailable');
  for (const respond of race.control.pending.splice(0)) respond(); await settle();
  assert.equal(race.element('processing-recordings').children.length, 0, 'source loss clears claims');
  assert.equal(race.element('processing-next').disabled, true);
  assert.equal(race.element('processing-refresh').disabled, false);
  race.control.defer = false; race.control.fail = 409; race.click('processing-refresh'); await settle();
  assert.match(race.element('processing-status').textContent, /catalog changed/);
  race.control.fail = null; race.click('processing-refresh'); await settle();
  assert.equal(race.element('processing-recordings').children.length, 3);
  race.emit('hub-status-available', {state: 'stopping'});
  assert.equal(race.element('processing-recordings').children.length, 0);
  const callsWhileStopping = race.calls.length; race.click('processing-refresh'); await settle();
  assert.equal(race.calls.length, callsWhileStopping, 'stopping hub cannot publish fresh processing claims');
  race.emit('offline');
  assert.equal(race.element('processing-recordings').children.length, 0);
  assert.equal(race.element('processing-refresh').disabled, false);
  for (const app of [b, selected, race]) assert.equal(app.calls.some(call => call.options?.method && call.options.method !== 'GET'), false, 'read-only UI never posts');
  const retryPage = () => {
    const app = browser('#processing?view=all&sha=' + sha(1) + '&section=imports'); app.control.retryMode = true; return app;
  };
  const buttons = app => allNodes(app.element('processing-references')).filter(node => node.tagName === 'button');
  const press = button => button.listeners.click({button: 0, preventDefault() {}});
  const eligible = retryPage(); await settle();
  assert.equal(buttons(eligible).length, 2, 'only two eligible failures expose actions');
  assert.equal(buttons(eligible)[0].disabled, false);
  assert.match(eligible.element('processing-references').textContent, /matching supported reader/);
  assert.match(eligible.element('processing-references').textContent, /does not repair the format/);
  assert.match(eligible.element('processing-references').textContent, /fixed processing limit/);
  eligible.emit('hub-status-available', {state: 'waiting', workers: {indexer: {state: 'failed'}}});
  assert.equal(buttons(eligible)[0].disabled, true);
  eligible.control.workerState = 'running'; eligible.click('processing-refresh'); await settle();
  eligible.control.postBehavior = 'lost'; await press(buttons(eligible)[0]); await settle();
  assert.equal(eligible.control.posts.length, 1);
  assert.match(eligible.element('processing-references').textContent, /result is unknown/);
  assert.equal(buttons(eligible)[0].disabled, false);
  const exactPayload = JSON.stringify(eligible.control.posts[0]);
  assert.equal(eligible.control.posts[0].expected_updated_utc_ns, '9007199254740993123');
  eligible.control.postBehavior = 'success'; await press(buttons(eligible)[0]); await settle();
  assert.equal(JSON.stringify(eligible.control.posts[1]), exactPayload, 'lost ack retry reuses exact UUID and pins');
  assert.equal(eligible.control.enqueued, 1, 'one accepted retry despite lost acknowledgement');
  assert.match(eligible.element('processing-references').textContent, /Queued local retry request/);
  assert.match(eligible.element('processing-references').textContent, /Import success has not been confirmed/);
  assert.equal(buttons(eligible)[0].disabled, true);

  const duplicate = retryPage(); await settle(); duplicate.control.postBehavior = 'defer';
  const duplicateButton = buttons(duplicate)[0]; press(duplicateButton); press(duplicateButton); await settle();
  assert.equal(duplicate.control.posts.length, 1, 'duplicate clicks cannot submit twice');
  assert.equal(buttons(duplicate)[1].disabled, true, 'all retry actions lock while submitting');
  duplicate.location.hash = '#processing?view=all&sha=' + sha(2) + '&section=runs'; duplicate.emit('popstate'); await settle();
  const afterNavigation = duplicate.calls.length;
  duplicate.control.pendingPosts.splice(0).forEach(resolve => resolve()); await settle();
  assert.equal(duplicate.calls.length, afterNavigation, 'stale POST acknowledgement cannot reload another selection');
  assert.match(duplicate.element('processing-reference-status').textContent, /Searchable runs/);
  assert.equal(duplicate.element('processing-record-title').textContent, 'Selected recording ' + sha(2).slice(0, 10));

  const stale = retryPage(); await settle(); stale.control.postBehavior = 'conflict';
  await press(buttons(stale)[0]); await settle();
  assert.equal(buttons(stale)[0].disabled, true);
  assert.match(stale.element('processing-references').textContent, /attempt changed/);
  await press(buttons(stale)[0]); await settle(); assert.equal(stale.control.posts.length, 1);
  stale.control.attempts = 4; stale.control.stamp = '9007199254740993124'; stale.click('processing-refresh'); await settle();
  assert.equal(buttons(stale)[0].disabled, false);
  assert.equal(stale.control.posts.length, 1, 'read-only refresh never retries automatically');

  const dedup = retryPage(); await settle(); dedup.control.postBehavior = 'dedup';
  await press(buttons(dedup)[0]); await settle();
  assert.match(dedup.element('processing-references').textContent, /Queued local retry request/, 'canonical different request ID accepted only with matching pins');
  const disconnected = retryPage(); await settle(); disconnected.control.postBehavior = 'defer'; press(buttons(disconnected)[0]);
  disconnected.emit('hub-status-unavailable'); disconnected.control.pendingPosts.splice(0).forEach(resolve => resolve()); await settle();
  assert.equal(disconnected.element('processing-references').children.length, 0, 'offline clears pending action claims');
  assert.equal(disconnected.element('processing-refresh').disabled, false);
  const stopped = retryPage(); stopped.control.workerState = 'stopped'; await settle();
  assert.equal(buttons(stopped)[0].disabled, true);
  stopped.emit('hub-status-available', {state: 'stopping'});
  assert.equal(stopped.element('processing-references').children.length, 0);
  console.log('125-record paging, attention, separate stages, exact safe links, races, no-overlap, stopped and source-loss checks passed; explicit retry pins, lost acknowledgements, duplicates and stale attempts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class ProcessingUITests(unittest.TestCase):
    @unittest.skipUnless(NODE, "Node is optional for isolated UI behavior tests")
    def test_processing_behavior_without_network_or_hardware(self):
        result = subprocess.run([NODE, "-e", HARNESS, str(ROOT / "robot_test_hub/static/processing.js")],
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source-loss checks passed", result.stdout)

    def test_panel_preserves_collection_controls_and_exposes_accessible_stages(self):
        html = (ROOT / "robot_test_hub/static/index.html").read_text(encoding="utf-8")
        for identity in ("pause", "retry", "eta", "basis", "files", "next", "verification"):
            self.assertIn('id="' + identity + '"', html)
        self.assertIn('aria-labelledby="processing-heading"', html)
        self.assertIn('aria-label="Selected recording reference pages"', html)
        self.assertIn('src="/processing.js"', html)
        script = (ROOT / "robot_test_hub/static/processing.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertEqual(script.count("method: 'POST'"), 1)
        self.assertIn("'/api/v1/imports/' + encodeURIComponent(item.id) + '/retry'", script)
