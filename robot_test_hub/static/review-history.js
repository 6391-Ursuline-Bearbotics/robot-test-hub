/* Read-only, bounded review navigation. All mutable forms remain independent. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const labels = {
    assignment: 'Component assignment', maintenance: 'Maintenance record', baseline: 'Approved baseline',
    finding_review: 'Finding review', regression_bundle: 'Regression reference bundle', analysis_plan: 'Automatic analysis plan'
  };
  const ID = /^[A-Za-z0-9_-]{1,128}$/, LIMIT = 20, MAX_PREVIOUS = 64;
  const controllers = new Set(), previousPages = new Map();
  let generation = 0, ready = false, busy = false, state = null, idsPage = null, revisionsPage = null;

  function safeId(value) { return typeof value === 'string' && ID.test(value); }
  function safeRevision(value) { return Number.isSafeInteger(value) && value > 0; }
  function cursor(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value !== 'string' || value.length > 8192) throw Error('Invalid history page cursor. Refresh history.');
    return value;
  }
  function readState() {
    const hash = location.hash;
    if (!hash || hash === '#review-history') return {kind: 'assignment', id: null, revision: null, idsCursor: null, revisionsCursor: null};
    if (!hash.startsWith('#review-history?')) throw Error('Unsupported saved-history link. Choose a record type to begin.');
    const query = new URLSearchParams(hash.slice('#review-history?'.length));
    const kind = query.get('kind') || 'assignment', id = query.get('id'), revisionText = query.get('revision');
    if (!Object.hasOwn(labels, kind) || (id !== null && !safeId(id)) || (revisionText !== null && (!id || !/^[1-9][0-9]*$/.test(revisionText)))) throw Error('Invalid saved-history record identity. Choose a record type to begin.');
    const revision = revisionText === null ? null : Number(revisionText);
    if (revision !== null && !safeRevision(revision)) throw Error('Invalid saved-history revision.');
    return {kind, id, revision, idsCursor: cursor(query.get('ids_cursor')), revisionsCursor: id ? cursor(query.get('revisions_cursor')) : null};
  }
  function hashFor(value) {
    const query = new URLSearchParams({kind: value.kind});
    if (value.id) query.set('id', value.id);
    if (value.revision !== null) query.set('revision', String(value.revision));
    if (value.idsCursor) query.set('ids_cursor', value.idsCursor);
    if (value.revisionsCursor && value.id) query.set('revisions_cursor', value.revisionsCursor);
    return '#review-history?' + query.toString();
  }
  function navigate(next) {
    const hash = hashFor(next);
    if (location.hash !== hash) history.pushState(null, '', hash);
    load();
  }
  function add(parent, tag, text) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    parent.append(node);
    return node;
  }
  function abortRequests() { for (const controller of controllers) controller.abort(); controllers.clear(); }
  function clearClaims() {
    for (const id of ['history-identities', 'history-revisions', 'history-exact', 'history-chain']) byId(id).replaceChildren();
    idsPage = null; revisionsPage = null;
    byId('history-ids-status').textContent = '';
    byId('history-revisions-status').textContent = 'Select a record identity.';
  }
  function pageKey(which, value, pageCursor) {
    return JSON.stringify([which, value.kind, which === 'revisions' ? value.id : null, pageCursor]);
  }
  function rememberPrevious(which, value, nextCursor, oldCursor) {
    const key = pageKey(which, value, nextCursor);
    previousPages.delete(key); previousPages.set(key, oldCursor);
    while (previousPages.size > MAX_PREVIOUS) previousPages.delete(previousPages.keys().next().value);
  }
  function controls() {
    byId('history-refresh').disabled = busy;
    for (const which of ['ids', 'revisions']) {
      const page = which === 'ids' ? idsPage : revisionsPage;
      const currentCursor = state && (which === 'ids' ? state.idsCursor : state.revisionsCursor);
      const active = ready && !busy && !!page;
      byId('history-' + which + '-first').disabled = !active || !currentCursor;
      byId('history-' + which + '-previous').disabled = !active || !currentCursor || !previousPages.has(pageKey(which, state, currentCursor));
      byId('history-' + which + '-next').disabled = !active || !page.next_cursor;
    }
  }
  function unavailable(message) {
    generation++; abortRequests(); ready = false; busy = false; clearClaims();
    byId('history-status').textContent = message + ' Saved history is unavailable; refresh history to retry. The URL retains the exact selection.';
    controls();
  }
  async function request(url, ticket) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000);
    controllers.add(controller);
    try {
      const response = await fetch(url, {signal: controller.signal, cache: 'no-store'});
      if (ticket !== generation) throw Object.assign(Error('Outdated history response ignored.'), {stale: true});
      if (!response.ok) {
        if (response.status === 409) throw Error('The saved-history snapshot changed. Refresh history to start a new bounded page.');
        if (response.status === 404) throw Error('The exact saved revision or reference was not found. No latest revision was substituted.');
        throw Error('The hub rejected or could not provide saved history (' + response.status + ').');
      }
      const data = await response.json();
      if (ticket !== generation) throw Object.assign(Error('Outdated history response ignored.'), {stale: true});
      if (!data || data.schema_version !== 1 || data.read_only !== true) throw Error('Unsupported saved-history response.');
      return data;
    } finally { clearTimeout(timer); controllers.delete(controller); }
  }
  function queryFor(value, exact = false) {
    const query = new URLSearchParams({kind: value.kind});
    if (exact) { query.set('id', value.id); query.set('revision', String(value.revision)); }
    return query;
  }
  async function getPage(value, which, ticket) {
    const query = queryFor(value); query.set('limit', String(LIMIT));
    const currentCursor = which === 'ids' ? value.idsCursor : value.revisionsCursor;
    if (currentCursor) query.set('cursor', currentCursor);
    if (which === 'revisions') query.set('record_id', value.id);
    const page = await request('/api/v1/review/history?' + query, ticket);
    if (page.kind !== value.kind || page.record_id !== (which === 'ids' ? null : value.id) || !Array.isArray(page.records) || page.records.length > LIMIT) throw Error('Invalid bounded history page.');
    cursor(page.next_cursor);
    for (const row of page.records) {
      if (row.kind !== value.kind || !safeId(row.id) || !safeRevision(row.revision) || (which === 'revisions' && row.id !== value.id) || !row.record || typeof row.record !== 'object') throw Error('Invalid saved-history row.');
    }
    return page;
  }
  function recordLink(parent, pin, title) {
    const link = add(parent, 'a', title);
    const next = {...state, kind: pin.kind, id: pin.id, revision: pin.revision,
      idsCursor: pin.kind === state.kind ? state.idsCursor : null,
      revisionsCursor: pin.kind === state.kind && pin.id === state.id ? state.revisionsCursor : null};
    link.href = hashFor(next);
    link.addEventListener('click', event => {
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); navigate(next);
    });
    return link;
  }
  function renderPage(page, which, value) {
    const list = byId(which === 'ids' ? 'history-identities' : 'history-revisions');
    list.replaceChildren();
    for (const row of page.records) {
      const item = add(list, 'li');
      const link = recordLink(item, row, row.id + ' / revision ' + row.revision);
      if (row.id === value.id && row.revision === value.revision) link.setAttribute('aria-current', 'true');
      const label = row.record.label || row.record.disposition || row.record.location || row.record.kind;
      if (label) add(item, 'small', label);
      if (row.record.reviewer) add(item, 'small', 'Reviewer: ' + row.record.reviewer);
    }
    if (!page.records.length) add(list, 'li', 'No saved entries in this page.');
    byId('history-' + which + '-status').textContent = page.records.length + ' entries shown' +
      (which === 'revisions' ? ' for ' + value.id : '; latest revision of each identity in this snapshot') + '.';
  }
  function disclosure(parent, title, value) {
    const details = add(parent, 'details'); add(details, 'summary', title);
    add(details, 'pre', JSON.stringify(value, null, 2));
  }
  function renderReference(parent, reference) {
    if (!reference || !['pinned', 'unpinned', 'missing', 'embedded', 'unavailable'].includes(reference.state)) throw Error('Invalid saved reference state.');
    const item = add(parent, 'li');
    const label = (reference.relation || 'Reference').replaceAll('_', ' ');
    add(item, 'strong', label + ' — ' + reference.state);
    if (reference.state === 'unpinned') add(item, 'p', 'No exact revision was saved. The current latest record is not substituted.');
    if (reference.state === 'missing') add(item, 'p', 'Saved evidence is missing or unavailable; no successful repair or validation is implied.');
    if (reference.state === 'unavailable') add(item, 'p', 'Saved evidence cannot be inspected in this view' + (reference.reason ? ': ' + String(reference.reason).replaceAll('_', ' ') : '') + '. Review the source evidence; no repair effectiveness or hardware health is implied.');
    if (reference.kind && safeId(reference.id)) {
      if (reference.state === 'pinned' && Object.hasOwn(labels, reference.kind) && safeRevision(reference.revision)) {
        recordLink(item, reference, 'Inspect ' + labels[reference.kind].toLowerCase() + ' ' + reference.id + ' / revision ' + reference.revision);
      } else add(item, 'p', reference.kind + ' / ' + reference.id + (safeRevision(reference.revision) ? ' / saved revision ' + reference.revision : ' / revision unavailable'));
    }
    if (reference.job_id) add(item, 'p', 'Analysis job: ' + reference.job_id);
    if (reference.record) disclosure(item, reference.state === 'embedded' ? 'Preserved record embedded in this saved bundle' : 'Pinned stored record', reference.record);
    if (reference.job) disclosure(item, 'Saved analysis job evidence', reference.job);
    if (reference.report_links !== undefined) {
      if (!Array.isArray(reference.report_links) || reference.report_links.length > 20) throw Error('Analysis report references exceed display bounds.');
      for (const report of reference.report_links) {
        if (!safeId(report.run_id) || !safeId(report.report_id)) throw Error('Invalid exact analysis report identity.');
        const link = add(item, 'a', 'Open exact analysis report (JSON): ' + report.report_id);
        link.href = '/api/v1/runs/' + encodeURIComponent(report.run_id) + '/reports/' + encodeURIComponent(report.report_id);
        link.target = '_blank'; link.rel = 'noopener';
        add(item, 'small', 'Saved report for run ' + report.run_id + '; this does not select a newer report.');
      }
      if (reference.report_state === 'missing') add(item, 'p', 'No retained analysis report reference is available for this job.');
      if (reference.report_links_truncated) add(item, 'p', 'Only the first 20 retained report references are shown.');
    }
    if (reference.state === 'embedded') add(item, 'small', 'This embedded snapshot is preserved as saved; it does not follow later corrections.');
  }
  function renderExact(exact, chain, value) {
    const panel = byId('history-exact'), references = byId('history-chain');
    add(panel, 'h3', labels[value.kind] + ' ' + value.id + ' / revision ' + value.revision);
    add(panel, 'p', 'Exact saved revision. Refresh and newer corrections do not change this pin. This inspection does not populate or submit any editing form.');
    const permalink = add(panel, 'a', 'Link to this exact revision'); permalink.href = hashFor(value);
    disclosure(panel, 'All exact stored fields', exact.record);
    if (!Array.isArray(exact.links) || exact.links.length > 128) throw Error('Saved reference inventory exceeds display bounds.');
    if (exact.links.length) {
      add(panel, 'h4', 'Stored references'); const list = add(panel, 'ul');
      for (const reference of exact.links) renderReference(list, reference);
    }
    add(references, 'h3', 'Repair and validation chain');
    if (!chain) { add(references, 'p', 'Repair-chain inspection is available for finding reviews and regression reference bundles.'); return; }
    if (!Array.isArray(chain.nodes) || chain.nodes.length > 64 || !Array.isArray(chain.limitations) || chain.limitations.length > 32) throw Error('Saved repair-chain inventory exceeds display bounds.');
    const list = add(references, 'ul');
    for (const reference of chain.nodes) renderReference(list, reference);
    if (!chain.nodes.length) add(references, 'p', 'No repair or validation references were saved.');
    for (const limitation of chain.limitations) add(references, 'p', limitation);
  }
  function exactIdentity(data, value) {
    if (data.kind !== value.kind || data.id !== value.id || data.revision !== value.revision) throw Error('The hub returned a different saved revision; response discarded.');
  }
  async function load() {
    const ticket = ++generation; abortRequests(); ready = false; busy = true; clearClaims(); controls();
    try {
      const value = readState(); state = value; byId('history-kind').value = value.kind;
      byId('history-status').textContent = 'Loading a bounded saved-history page' + (value.id && value.revision ? ' and exact revision ' + value.revision : '') + '...';
      const exactQuery = value.id && value.revision ? queryFor(value, true) : null;
      const [identities, revisions, exact, chain] = await Promise.all([
        getPage(value, 'ids', ticket), value.id ? getPage(value, 'revisions', ticket) : null,
        exactQuery ? request('/api/v1/review/record?' + exactQuery, ticket) : null,
        exactQuery && ['finding_review', 'regression_bundle'].includes(value.kind) ? request('/api/v1/review/chain?' + exactQuery, ticket) : null
      ]);
      if (ticket !== generation) return;
      if (exact) { exactIdentity(exact, value); if (!exact.record || typeof exact.record !== 'object') throw Error('Exact saved record is unavailable.'); }
      if (chain) exactIdentity(chain, value);
      idsPage = identities; revisionsPage = revisions;
      renderPage(identities, 'ids', value);
      if (revisions) renderPage(revisions, 'revisions', value);
      if (exact) renderExact(exact, chain, value);
      else if (value.id) add(byId('history-exact'), 'p', 'Choose an exact revision from the revision list. No latest revision is assumed.');
      ready = true; busy = false;
      byId('history-status').textContent = 'Saved history ready. ' + (exact ? 'Pinned to ' + value.id + ' / revision ' + value.revision + '.' : 'Choose an identity and exact revision to inspect.') + ' Refresh explicitly to see newly saved records.';
      controls();
    } catch (error) { if (ticket === generation && !error.stale) unavailable(error.message || 'The local hub could not be reached.'); }
  }
  function pageNavigate(which, direction) {
    if (!ready || busy || !state) return;
    const field = which === 'ids' ? 'idsCursor' : 'revisionsCursor';
    const page = which === 'ids' ? idsPage : revisionsPage;
    if (!page) return;
    let nextCursor;
    if (direction === 'first') nextCursor = null;
    else if (direction === 'next') { nextCursor = page.next_cursor; if (!nextCursor) return; rememberPrevious(which, state, nextCursor, state[field]); }
    else { const key = pageKey(which, state, state[field]); if (!previousPages.has(key)) return; nextCursor = previousPages.get(key); }
    navigate({...state, [field]: nextCursor});
  }
  for (const which of ['ids', 'revisions']) for (const direction of ['first', 'previous', 'next']) {
    byId('history-' + which + '-' + direction).addEventListener('click', () => pageNavigate(which, direction));
  }
  byId('history-kind').addEventListener('change', () => navigate({kind: byId('history-kind').value, id: null, revision: null, idsCursor: null, revisionsCursor: null}));
  byId('history-refresh').addEventListener('click', () => {
    let value; try { value = readState(); } catch (_) { value = {kind: byId('history-kind').value, id: null, revision: null}; }
    navigate({...value, idsCursor: null, revisionsCursor: null});
  });
  window.addEventListener('hashchange', load); window.addEventListener('popstate', load);
  window.addEventListener('offline', () => unavailable('Hub connection lost.'));
  window.addEventListener('review-unavailable', () => unavailable('Current review evidence could not be refreshed.'));
  window.addEventListener('review-catalog-ready', () => { if (!ready && !busy) load(); });
  load();
})();
