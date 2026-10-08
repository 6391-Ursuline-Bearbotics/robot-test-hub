# Offline device notebook (T08 portion)

The loopback notebook now commits each immutable request to IndexedDB before any hub POST. A note's UUID, revision, JSON body, original client action clock, retrospective event time, uncertainty, and text survive page reload and disconnected delivery. `Saved on this device` means a browser transaction completed; `Saved in hub` appears only after the matching historical/hub-only hub receipt is persisted locally with the cached annotation. A later increment adds [explicit robot marker delivery](NOTE_MARKERS.md) with separate contextual receipts; authenticated LAN pairing and anonymous LAN bind remain unavailable.

Use `/notebook` at the same loopback hostname and port each time. On the first connected visit, wait for `Notebook shell saved for offline reload`. The service worker caches only `/notebook` and `/notebook.js`; API responses, status, transfer pages, controls, and robot state are never cached by it. A later offline reload can reopen the notebook, mark new events, inspect pending device requests, and search cached hub notes. The cached list is explicitly labeled incomplete/stale when the hub cannot be reached. The full hub history remains authoritative and needs a live connection; device storage caches only encountered annotation pages and receipts.

`robot-test-hub-notebook-v1` contains an IndexedDB `jobs` outbox, latest confirmed `notes`, and a stable browser `device_id` setting. Browser clock domains remain distinct per page session; queued records retain the original domain across reload. IndexedDB writes request strict durability when supported and acknowledge transaction completion before sends. Storage failures, quota errors, or unavailable IndexedDB do not fall back to memory-only sending or claim that a note was saved. Browser storage can be cleared/evicted and is tied to its origin/browser profile; this is not a backup. Different ports, `localhost` versus `127.0.0.1`, or a different browser have separate stores. Reusing the same origin for a different hub archive retains old device history, which remains labeled as cached receipts rather than proof of the currently connected archive's contents.

## Delivery states and recovery

| Device state | Meaning and behavior |
| --- | --- |
| `queued` | Committed on this device; ready to send when available |
| `sending` | Committed request owns a temporary send lease; waiting for a hub receipt |
| `retry_wait` | Network/receipt timeout, transient hub error, or unverifiable response; same request retries |
| `saved_in_hub` | Exact payload acknowledged with historical/hub-only receipt, persisted locally |
| `conflict` | HTTP 409; device request retained, automatic retry stops |
| `rejected` | Other permanent HTTP 4xx; device request retained, automatic retry stops |

Sends have an eight-second deadline including reading the response body. A lost response after a hub commit retries the exact original body, relying on T07's idempotent event/revision contract. Two tabs use transactional claims with 15-second leases; an abandoned `sending` request becomes retryable after lease expiration. The hub's idempotency still protects against a response racing a reclaimed lease. Retry polling occurs every ten seconds while a page is open and also on focus/reconnection; closing all pages preserves requests but does not promise background delivery.

Local revisions depend on previous queued revisions being confirmed first. Conflicting/rejected predecessors block automatic descendants. `Review current hub note` loads the actual saved revision for human review; saving a correction creates a new revision with that explicit previous hub receipt, while retaining the conflicting device request. `Retry unchanged request after review` never rewrites an event/revision body or its time. A changed request under an existing local event/revision is refused and the original remains intact. No automatic last-write-wins conflict merge is used.

The UI renders imported notes/errors with DOM text nodes. Notes retain their historical/hub-only storage receipts and `run_id=null` under the current T07 backend. The optional marker bridge is a separate explicitly selected delivery; it never automatically retargets a pending note to a new boot. T08's authenticated LAN pairing remains separate work; the offline portion and locally qualified bridge do not complete physical acceptance criteria.

## Validation

```powershell
python -m unittest discover -s tests -p test_notebook_offline.py -v
python -m unittest discover -s tests -p test_notebook.py -v
```

Development-only JavaScript checks use an available Node runtime and no packages/downloads; the hub/browser runtime does not require Node. When Node is absent, those two development checks explicitly skip. Tests exercise frozen 30-seconds-ago requests retried after an injected 60-second clock advance and reload, a lost acknowledgment after remote commit, exact revision/time preservation, local conflicts/quota failure, permanent versus transient failures, revision ordering, concurrent tab send leases, cached interval filtering, and a service-worker harness proving only the notebook shell is intercepted. These orchestration tests use an in-memory test store; they do not establish actual browser IndexedDB/service-worker behavior or power-loss durability.

Browser qualification must additionally mark while connected, stop the server, reload through the installed service worker, save a retrospective event offline, reload again and confirm its stored IDs/body/times, restart the hub on the same origin, and verify one durable hub event with the unchanged intended time. Check offline cached search, editing, conflict presentation, and a second tab. Record the actual browser/version and result separately. No real robot/network commissioning is implied by these local checks.

Local browser evidence supplied by the parent task: with the server stopped, a 30-seconds-ago event was saved with displayed incident time **09:02:34** and original client action **09:03:04**. A complete offline reload through the service worker showed the pending request and cached notes from IndexedDB. Restarting the same-origin hub automatically delivered that request with displayed receipt **09:03:42**, preserving the original incident time. The result had one saved hub receipt and zero pending requests. Browser engine/version and clock calibration were not recorded; no measured synchronization accuracy, physical network trial, or robot delivery is claimed. Actual multi-tab/conflict browser checks remain additional qualification; their state transitions were tested in the JavaScript harness.
