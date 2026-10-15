/* Cached local processing knowledge, separate from live collection and ETA. */
(() => {
  'use strict';
  const node = id => document.getElementById(id), LIMIT = 20, SHA = /^[a-f0-9]{64}$/, ID = /^[A-Za-z0-9_-]{1,128}$/;
  const sections = {transfers: 'Original transfers', imports: 'Import attempts', runs: 'Searchable runs', reports: 'Saved reports'};
  const controllers = new Set(), previous = new Map();
  let generation = 0, ready = false, busy = false, selection = null, page = null, referencePage = null, lastLoad = 0, stopping = false;

  function count(value) { if (!Number.isSafeInteger(value) || value < 0) throw Error('Invalid recording progress count.'); return value; }
  function pageCursor(value) { if (value == null || value === '') return null; if (typeof value !== 'string' || value.length > 8192) throw Error('Invalid processing page. Refresh processing.'); return value; }
  function safeId(value) { return typeof value === 'string' && ID.test(value); }
  function readSelection() {
    if (!location.hash || location.hash === '#processing') return {view: 'all', sha: null, section: 'imports', cursor: null, referenceCursor: null};
    if (!location.hash.startsWith('#processing?')) throw Error('Invalid processing link. Refresh processing to begin.');
    const query = new URLSearchParams(location.hash.slice('#processing?'.length));
    const view = query.get('view') || 'all', sha = query.get('sha'), section = query.get('section') || 'imports';
    if (!['all', 'attention'].includes(view) || (sha !== null && !SHA.test(sha)) || !Object.hasOwn(sections, section)) throw Error('Invalid selected recording or reference section.');
    return {view, sha, section, cursor: pageCursor(query.get('cursor')), referenceCursor: sha ? pageCursor(query.get('reference_cursor')) : null};
  }
  function hashFor(value) {
    const query = new URLSearchParams({view: value.view});
    if (value.cursor) query.set('cursor', value.cursor);
    if (value.sha) { query.set('sha', value.sha); query.set('section', value.section); if (value.referenceCursor) query.set('reference_cursor', value.referenceCursor); }
    return '#processing?' + query.toString();
  }
  function navigate(value) { const hash = hashFor(value); if (hash !== location.hash) history.pushState(null, '', hash); load(); }
  function add(parent, tag, text) { const child = document.createElement(tag); if (text !== undefined) child.textContent = String(text); parent.append(child); return child; }
  function abort() { for (const controller of controllers) controller.abort(); controllers.clear(); }
  function clear() {
    for (const id of ['processing-recordings', 'processing-record-summary', 'processing-references']) node(id).replaceChildren();
    for (const id of ['processing-counts', 'processing-page-status', 'processing-reference-status', 'processing-record-title']) node(id).textContent = '';
    node('processing-selection').hidden = true; node('processing-guidance').hidden = true; page = null; referencePage = null;
  }
  function previousKey(which, value, cursor) { return JSON.stringify([which, value.view, which === 'references' ? value.sha : null, which === 'references' ? value.section : null, cursor]); }
  function controls() {
    node('processing-refresh').disabled = busy;
    node('processing-section').disabled = !ready || busy || !selection?.sha;
    node('processing-clear').disabled = !selection?.sha;
    for (const which of ['recordings', 'references']) {
      const references = which === 'references', current = references ? referencePage : page;
      const prefix = references ? 'processing-reference-' : 'processing-';
      const cursor = selection && (references ? selection.referenceCursor : selection.cursor);
      const active = ready && !busy && !!current;
      node(prefix + 'first').disabled = !active || !cursor;
      node(prefix + 'previous').disabled = !active || !cursor || !previous.has(previousKey(which, selection, cursor));
      node(prefix + 'next').disabled = !active || !current.next_cursor;
    }
  }
  function unavailable(message) {
    generation++; abort(); ready = false; busy = false; clear();
    node('processing-status').textContent = message + ' Recording progress is unavailable. Refresh processing to retry.';
    node('processing-worker').textContent = 'Indexer status unavailable; no current processing claim.';
    controls();
  }
  async function request(url, ticket) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000); controllers.add(controller);
    try {
      const response = await fetch(url, {signal: controller.signal, cache: 'no-store'});
      if (ticket !== generation) throw Object.assign(Error('Outdated processing response ignored.'), {stale: true});
      if (!response.ok) {
        if (response.status === 409) throw Error('The processing catalog changed. Refresh processing to restart paging.');
        if (response.status === 404) throw Error('The selected recording or saved reference is missing. No replacement was selected.');
        throw Error('The local hub could not provide recording progress (' + response.status + ').');
      }
      const data = await response.json();
      if (ticket !== generation) throw Object.assign(Error('Outdated processing response ignored.'), {stale: true});
      if (!data || data.schema_version !== 1 || data.read_only !== true) throw Error('Unsupported recording progress response.');
      return data;
    } finally { clearTimeout(timer); controllers.delete(controller); }
  }
  function validatePage(data, value, references = false) {
    if (!Array.isArray(data.items) || data.items.length > LIMIT || data.returned !== data.items.length || data.limit !== LIMIT) throw Error('Recording progress page exceeds supported bounds.');
    const reportsIncomplete = references && value.section === 'reports' && ['partial', 'unavailable'].includes(data.availability);
    if (reportsIncomplete) {
      if (data.total !== null) throw Error('Invalid incomplete report-reference page.');
      if (data.availability === 'partial') {
        if (count(data.known_total) < data.items.length) throw Error('Invalid known report-reference count.');
      } else if (data.items.length !== 0 || data.returned !== 0 || data.next_cursor !== null) throw Error('Invalid unavailable report-reference page.');
    } else count(data.total);
    pageCursor(data.next_cursor);
    if (references && (data.sha256 !== value.sha || data.section !== value.section)) throw Error('The hub returned references for a different recording.');
    return data;
  }
  function validateRecording(item, expectedSha = null) {
    if (!item || typeof item.sha256 !== 'string' || !SHA.test(item.sha256) || (expectedSha && item.sha256 !== expectedSha) ||
        !item.original || !item.imports || !item.indexing || !item.reports || !Array.isArray(item.source_types) || item.source_types.length > 32) throw Error('Invalid recording progress metadata.');
    count(item.indexing.run_count);
    if (item.reports.count !== null || !['partial', 'unavailable'].includes(item.reports.state)) count(item.reports.count);
    if (item.reports.state === 'partial') count(item.reports.known_count);
    return item;
  }
  function sourceLabel(item) {
    const names = {non_wpilog_transfer: 'Non-WPILOG transfer data', transfer_source_unqualified: 'Collected file; source type unqualified', unavailable: 'Source type unavailable', MANUAL_LOCAL: 'Manually imported local recording', VERIFIED_TRANSFER: 'Verified source transfer', TRANSFER_CHECKSUM_MISMATCH: 'Source transfer with a checksum mismatch', REAL: 'Recorded robot data', SIM: 'Simulation data', REPLAY: 'Replay data', SYNTHETIC: 'Synthetic test recording',
      synthetic_demo: 'Demo transfer data', synthetic: 'Synthetic test data', imported: 'Manually imported recording', live: 'Live-source recording'};
    return item.source_types.length ? item.source_types.map(value => names[value] || String(value).replaceAll('_', ' ')).join(' · ') : 'Source type unavailable';
  }
  function importLabel(item) {
    const states = {
      not_applicable: 'Not a WPILOG recording; import not applicable', unknown_format: 'Recording format not yet identified',
      pending: 'Waiting to import', running: 'Import in progress', succeeded: 'Supported data imported',
      succeeded_with_unsupported: 'Supported data imported; some signals unsupported',
      unsupported: 'Recording profile unsupported by current tools', invalid: 'Invalid recording; import needs attention',
      failed: 'Import failed; needs attention', checksum_mismatch: 'Original checksum mismatch; needs investigation', mixed: 'Import results differ; inspect the attempts'
    };
    return states[item.imports.state] || 'Import status unavailable';
  }
  function originalLabel(item) {
    return {verified: 'Original saved; hash verification recorded', unverified: 'Not yet verified locally', expected: 'Original expected; not verified locally'}[item.original.state] || 'Original state unavailable';
  }
  function searchableLabel(item) {
    if (item.indexing.state === 'unavailable') return 'Searchable-run availability unknown';
    if (item.indexing.state === 'not_applicable') return 'Not applicable to this data';
    if (item.indexing.run_count) return item.indexing.run_count + ' searchable run(s)' + (item.indexing.state === 'searchable_previous_import' ? ' from a previous import' : '');
    return item.indexing.state === 'no_runs' ? 'No runs found in imported data' : 'No searchable runs yet';
  }
  function reportLabel(item) {
    if (item.reports.state === 'partial') return item.reports.known_count + ' known saved report(s); ' + (item.reports.reason === 'report_lookup_failed' ? 'lookup needs attention' : 'lookup incomplete');
    if (item.reports.state === 'unavailable') return 'Report references unavailable; ' + (item.reports.reason === 'report_lookup_failed' ? 'lookup needs attention' : 'lookup incomplete');
    if (item.reports.state === 'not_applicable') return 'Not applicable to this data';
    return item.reports.count ? item.reports.count + ' saved report(s)' : 'No saved reports yet';
  }
  function workerStatus(worker) {
    const labels = {starting: 'Indexer starting; progress may be incomplete.', running: 'Indexer running. Stored progress may update in the background.',
      idle: 'Indexer ready. Stored evidence does not establish hardware health.', paused: 'Indexer paused; processing may be incomplete.',
      stopped: 'Indexer stopped. New imports, searchable runs and reports may be incomplete. Check service diagnostics before restarting the hub.',
      failed: 'Indexer failed. New imports, searchable runs and reports may be incomplete. Check service diagnostics before restarting the hub.',
      unknown: 'Indexer status unavailable; processing may be incomplete.', unavailable: 'Indexer status unavailable; processing may be incomplete.'};
    node('processing-worker').textContent = labels[worker?.state] || 'Indexer status unavailable; processing may be incomplete.';
  }
  function technical(parent, title, value) {
    const details = add(parent, 'details'); add(details, 'summary', title);
    const allowed = ['sha256', 'source_sha256', 'id', 'file_id', 'job_id', 'import_job_id', 'revision', 'state', 'status', 'source_type', 'source_types',
      'current_profile', 'reader_profile', 'profile', 'extractor_version', 'mapping_revision', 'size_bytes', 'created_utc_ns', 'finished_utc_ns',
      'run_id', 'report_id', 'catalog_revision', 'error_code', 'format', 'format_profile', 'verification_state', 'verified', 'supported_current_toolchain', 'current_toolchain'];
    const fields = Object.fromEntries(allowed.filter(key => value[key] !== undefined).map(key => [key, value[key]]));
    add(details, 'pre', JSON.stringify(fields, null, 2));
  }
  function renderList(data, value) {
    const body = node('processing-recordings'); body.replaceChildren();
    for (const candidate of data.items) {
      const item = validateRecording(candidate), row = add(body, 'tr'), identity = add(row, 'td');
      const link = add(identity, 'a', 'Recording ' + item.sha256.slice(0, 10)); link.className = 'processing-recording';
      const next = {...value, sha: item.sha256, section: 'imports', referenceCursor: null}; link.href = hashFor(next);
      link.addEventListener('click', event => {
        if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); navigate(next);
      });
      add(identity, 'p', sourceLabel(item)).className = 'foot';
      for (const text of [originalLabel(item), importLabel(item), searchableLabel(item), reportLabel(item)]) add(row, 'td', text);
    }
    if (!data.items.length) { const row = add(body, 'tr'); const cell = add(row, 'td', value.view === 'attention' ? 'No import-attention records in this page.' : 'No recordings in this page.'); cell.colSpan = 5; }
    node('processing-page-status').textContent = data.returned + ' shown of ' + data.total + (value.view === 'attention' ? ' recording(s) needing import attention.' : ' recording(s).') + ' This is a live catalog page; refresh to include new progress.';
    const counts = data.counts;
    if (counts && typeof counts === 'object') {
      const parts = [];
      for (const [key, label] of [['recordings', 'recordings'], ['verified', 'originals verified'], ['imported', 'supported imports'], ['searchable', 'with searchable runs'], ['reported', 'with saved reports'], ['attention', 'imports needing attention']]) {
        if (counts[key] !== undefined) parts.push(count(counts[key]) + ' ' + label);
      }
      node('processing-counts').textContent = parts.length ? 'Catalog totals: ' + parts.join(' · ') + '.' : '';
      node('processing-counts').className = counts.attention > 0 ? 'foot processing-attention' : 'foot';
      node('processing-guidance').hidden = !(counts.attention > 0 || value.view === 'attention');
    }
  }
  function renderRecording(recording) {
    node('processing-selection').hidden = false;
    node('processing-record-title').textContent = 'Selected recording ' + recording.sha256.slice(0, 10);
    const parent = node('processing-record-summary');
    add(parent, 'p', sourceLabel(recording));
    add(parent, 'p', [originalLabel(recording), importLabel(recording), searchableLabel(recording), reportLabel(recording)].join(' · '));
    technical(parent, 'Original identity and import versions', {sha256: recording.sha256, source_types: recording.source_types, ...recording.imports});
  }
  function renderReferences(data, value) {
    const parent = node('processing-references'); parent.replaceChildren();
    if (value.section === 'reports' && data.reason === 'report_lookup_failed') add(parent, 'p', 'Some saved report metadata could not be indexed. Lookup needs maintainer attention; this does not invalidate original recordings.');
    if (value.section === 'reports' && data.availability === 'unavailable') {
      add(parent, 'p', 'Report references unavailable for this recording. No report count is claimed. Original verification, import and searchable-run status remain separate.');
      add(parent, 'p', 'Use Run history to inspect retained analysis. Background catalog maintenance has not established complete report coverage. Check the indexer if this persists.');
      const link = add(parent, 'a', 'Open Run history'); link.href = '/runs';
      node('processing-reference-status').textContent = 'Saved report-reference availability is unknown for this original. Other reference sections remain available.';
      return;
    }
    if (value.section === 'reports' && data.availability === 'partial') add(parent, 'p', 'Report lookup is incomplete. These known references remain available; the full report count is unknown.');
    for (const item of data.items) {
      if (!item || typeof item !== 'object') throw Error('Invalid saved recording reference.');
      const entry = add(parent, 'article'); entry.className = 'processing-reference';
      if (value.section === 'runs') {
        if (!safeId(item.run_id)) throw Error('Invalid saved run identity.');
        add(entry, 'strong', 'Searchable run');
        const link = add(entry, 'a', 'Open current run details (JSON)'); link.href = '/api/v1/runs/' + encodeURIComponent(item.run_id); link.target = '_blank'; link.rel = 'noopener';
        if (item.current_toolchain === false) add(entry, 'p', 'This run was indexed from a previous import. Current supported-import status is separate.');
      } else if (value.section === 'reports') {
        if (!safeId(item.run_id) || !safeId(item.report_id)) throw Error('Invalid exact report identity.');
        add(entry, 'strong', 'Saved analysis report');
        const link = add(entry, 'a', 'Open this exact saved report (JSON)'); link.href = '/api/v1/runs/' + encodeURIComponent(item.run_id) + '/reports/' + encodeURIComponent(item.report_id); link.target = '_blank'; link.rel = 'noopener';
        add(entry, 'p', 'Saved analysis is separate from original verification and does not certify hardware health.');
      } else if (value.section === 'imports') {
        add(entry, 'strong', 'Import attempt');
        add(entry, 'p', importLabel({imports: item}));
        if (item.supported_current_toolchain === false) add(entry, 'p', 'This attempt is not a supported current-toolchain import. Older searchable results, if any, are separate.');
      } else {
        add(entry, 'strong', 'Original transfer');
        add(entry, 'p', ({complete: item.verified ? 'Original saved; hash verification recorded' : 'Transfer saved; hash verification unavailable', copying: 'Transfer in progress', queued: 'Waiting to download', error: 'Transfer needs attention', verifying: 'Awaiting local verification'})[item.state] || 'Stored transfer state; inspect identity details.');
      }
      technical(entry, 'Saved identity, version and state', item);
    }
    if (!data.items.length) add(parent, 'p', 'No saved references of this type in this page. This is not a successful processing or health result.');
    node('processing-reference-status').textContent = sections[value.section] + ': ' + data.returned + ' shown of ' + (data.availability === 'partial' ? data.known_total + ' known references; full total unknown' : data.total) + ' for the selected original.';
  }
  async function load() {
    if (stopping) { unavailable('The local hub is stopping.'); return; }
    const ticket = ++generation; abort(); ready = false; busy = true; lastLoad = Date.now(); clear(); controls();
    try {
      const value = readSelection(); selection = value; node('processing-view').value = value.view; node('processing-section').value = value.section;
      node('processing-status').textContent = 'Loading stored recording progress...';
      const listQuery = new URLSearchParams({limit: String(LIMIT), view: value.view}); if (value.cursor) listQuery.set('cursor', value.cursor);
      const detailPath = value.sha ? '/api/v1/processing/' + value.sha : null;
      const referenceQuery = new URLSearchParams({section: value.section, limit: String(LIMIT)}); if (value.referenceCursor) referenceQuery.set('cursor', value.referenceCursor);
      const [list, detail, references] = await Promise.all([
        request('/api/v1/processing?' + listQuery, ticket),
        detailPath ? request(detailPath, ticket) : null,
        detailPath ? request(detailPath + '?' + referenceQuery, ticket) : null
      ]);
      if (ticket !== generation) return;
      validatePage(list, value); if (detail) validateRecording(detail.recording, value.sha); if (references) validatePage(references, value, true);
      if (detail && references && detail.catalog_revision !== references.catalog_revision) throw Error('The selected recording catalog changed while loading references. Refresh processing before comparing them.');
      page = list; referencePage = references;
      renderList(list, value); workerStatus(list.indexer);
      if (detail) renderRecording(detail.recording); if (references) renderReferences(references, value);
      ready = true; busy = false;
      node('processing-status').textContent = 'Stored recording progress ready. Download, supported import, searchable runs and reports are separate. Refresh updates this cached view.';
      controls();
    } catch (error) { if (ticket === generation && !error.stale) unavailable(error.message || 'The local hub could not be reached.'); }
  }
  function move(which, direction) {
    if (!ready || busy || !selection) return;
    const references = which === 'references', current = references ? referencePage : page;
    const field = references ? 'referenceCursor' : 'cursor'; if (!current) return;
    let next;
    if (direction === 'first') next = null;
    else if (direction === 'next') {
      next = current.next_cursor; if (!next) return;
      const key = previousKey(which, selection, next); previous.delete(key); previous.set(key, selection[field]);
      while (previous.size > 64) previous.delete(previous.keys().next().value);
    } else { const key = previousKey(which, selection, selection[field]); if (!previous.has(key)) return; next = previous.get(key); }
    navigate({...selection, [field]: next});
  }
  for (const which of ['recordings', 'references']) for (const direction of ['first', 'previous', 'next']) {
    const prefix = which === 'references' ? 'processing-reference-' : 'processing-';
    node(prefix + direction).addEventListener('click', () => move(which, direction));
  }
  node('processing-refresh').addEventListener('click', () => {
    let value; try { value = readSelection(); } catch (_) { value = {view: 'all', sha: null, section: 'imports'}; }
    navigate({...value, cursor: null, referenceCursor: null});
  });
  node('processing-view').addEventListener('change', () => navigate({view: node('processing-view').value, sha: null, section: 'imports', cursor: null, referenceCursor: null}));
  node('processing-section').addEventListener('change', () => { if (selection?.sha) navigate({...selection, section: node('processing-section').value, referenceCursor: null}); });
  node('processing-clear').addEventListener('click', () => { if (selection) navigate({...selection, sha: null, referenceCursor: null}); });
  window.addEventListener('hashchange', load); window.addEventListener('popstate', load);
  window.addEventListener('offline', () => unavailable('Hub connection lost.'));
  window.addEventListener('hub-status-unavailable', () => unavailable('The local hub could not be reached.'));
  window.addEventListener('hub-status-available', event => {
    stopping = event.detail?.state === 'stopping';
    if (stopping) { unavailable('The local hub is stopping.'); return; }
    const indexer = event.detail?.workers?.indexer;
    // A generic worker-running status must not erase a processing API failure.
    if (ready && indexer && ['failed', 'stopped'].includes(indexer.state)) workerStatus(indexer);
    if (!busy && Date.now() - lastLoad >= 5000 && (!ready || !selection?.sha)) load();
  });
  load();
})();
