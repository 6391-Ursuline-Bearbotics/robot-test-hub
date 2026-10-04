import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.notebook import Notebook, NotebookError, install_schema
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService


ACTION = 1791071520000000000


def annotation(event_id="event-a", *, seconds=30):
    return {"schema_version": 1, "event_id": event_id, "revision": 1,
            "submitted_utc_ns": str(ACTION), "client_monotonic_ns": "50000000000",
            "event_utc_start_ns": str(ACTION - seconds * 1000000000),
            "event_utc_end_ns": str(ACTION - seconds * 1000000000),
            "when": {"kind": "seconds_ago", "seconds": seconds}, "uncertainty_ms": 1000,
            "clock_domain": "browser-session-example", "clock_quality": "unverified_client",
            "text": "Steering felt wrong", "tags": ["drive"], "source": "practice-notebook",
            "author": "test-observer", "device_id": "test-device", "run_id": None}


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.sqlite3"
        self.now = ACTION + 60000000000
        self.book = Notebook(self.path, clock_ns=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_delayed_submission_and_retry_keep_original_event_time(self):
        payload = annotation()
        result = self.book.save(payload)
        self.assertEqual(result["annotation"]["event_utc_start_ns"], str(ACTION - 30000000000))
        self.assertEqual(result["annotation"]["submitted_utc_ns"], str(ACTION))
        self.assertEqual(result["annotation"]["hub_received_utc_ns"], str(self.now))
        self.now += 90000000000
        again = self.book.save(copy.deepcopy(payload))
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["annotation"], result["annotation"])
        self.assertEqual(len(self.book.history("event-a")["revisions"]), 1)

    def test_conflicting_retry_and_concurrent_edit_are_rejected(self):
        original = annotation()
        self.book.save(original)
        conflict = dict(original, text="different")
        with self.assertRaises(NotebookError) as caught:
            self.book.save(conflict)
        self.assertEqual(caught.exception.status, 409)
        edited = dict(original, revision=2, text="Loose connector found")
        self.book.save(edited, expected_previous_revision=1)
        with self.assertRaises(NotebookError) as caught:
            self.book.save(dict(edited, text="Competing update"), expected_previous_revision=1)
        self.assertEqual(caught.exception.code, "revision_conflict")
        with self.assertRaises(NotebookError):
            self.book.save(dict(original, revision=4), expected_previous_revision=3)
        self.assertEqual(self.book.history("event-a")["annotation"]["text"], edited["text"])

    def test_edits_append_history_and_older_retries_stay_idempotent(self):
        first = annotation()
        self.book.save(first)
        self.now += 1
        second = dict(first, revision=2, text="Correction")
        self.book.save(second, expected_previous_revision=1)
        self.now += 1
        self.book.save(dict(second, revision=3, text="Repair confirmed"), expected_previous_revision=2)
        self.assertTrue(self.book.save(first)["idempotent"])
        self.assertTrue(self.book.save(second, expected_previous_revision=1)["idempotent"])
        history = self.book.history("event-a", limit=2)
        self.assertEqual([item["revision"] for item in history["revisions"]], [1, 2])
        self.assertEqual(history["next_revision_cursor"], 2)
        self.assertEqual(history["annotation"]["revision"], 3)
        self.assertEqual(self.book.history("event-a", after_revision=2)["revisions"][0]["revision"], 3)
        self.assertEqual(history["revisions"][0]["text"], first["text"])
        self.assertEqual(history["revisions"][1]["event_utc_start_ns"], first["event_utc_start_ns"])

    def test_restart_retains_saved_note_and_no_robot_ack(self):
        self.book.save(annotation())
        reopened = Notebook(self.path)
        saved = reopened.history("event-a")["annotation"]
        self.assertEqual(saved["storage_state"], "saved_in_hub")
        self.assertEqual(saved["delivery_state"], "historical_hub_only")
        self.assertNotIn("robot_acknowledged", saved)
        self.assertIsNone(saved["run_id"])

    def test_unknown_clock_and_empty_marker_remain_accessible(self):
        payload = annotation()
        payload.update(submitted_utc_ns=None, client_monotonic_ns=None,
                       event_utc_start_ns=None, event_utc_end_ns=None, when={"kind": "unknown"},
                       clock_quality="unknown", uncertainty_ms=None, text="", tags=[])
        self.book.save(payload)
        saved = self.book.list()["annotations"][0]
        self.assertIsNone(saved["event_utc_start_ns"])
        self.assertIsNone(saved["uncertainty_ms"])
        self.assertEqual(saved["text"], "")
        self.assertEqual(self.book.list(start_ns="1", end_ns="2", include_unknown=True)["annotations"][0]["event_id"], "event-a")
        self.assertEqual(self.book.list(start_ns="1", end_ns="2", include_unknown=False)["annotations"], [])

    def test_interval_overlap_latest_revision_and_bounded_pagination(self):
        payload = annotation("event-b")
        payload.update(when={"kind": "exact_interval", "start": "1970-01-01T00:00:00.000000100Z", "end": "1970-01-01T00:00:00.000000200Z"},
                       event_utc_start_ns="100", event_utc_end_ns="200")
        self.book.save(payload)
        self.book.save(annotation("event-a"))
        self.book.save(annotation("event-c"))
        self.assertEqual(self.book.list(start_ns="200", end_ns="300")["annotations"][0]["event_id"], "event-b")
        self.assertEqual(self.book.list(start_ns="201", end_ns="300")["annotations"], [])
        first = self.book.list(limit=2)
        self.assertEqual([note["event_id"] for note in first["annotations"]], ["event-a", "event-b"])
        self.assertEqual(first["next_cursor"], "event-b")
        self.assertEqual(self.book.list(limit=2, cursor=first["next_cursor"])["annotations"][0]["event_id"], "event-c")
        self.book.save(dict(payload, revision=2, event_utc_start_ns="400", event_utc_end_ns="500",
                            when={"kind": "exact_interval", "start": "1970-01-01T00:00:00.000000400Z",
                                  "end": "1970-01-01T00:00:00.000000500Z"}), expected_previous_revision=1)
        self.assertEqual(self.book.list(start_ns="100", end_ns="200")["annotations"], [])
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"start_ns": "1"},
                       {"start_ns": "2", "end_ns": "1"}, {"cursor": "../"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(NotebookError):
                self.book.list(**kwargs)

    def test_invalid_fields_cannot_store_or_fabricate_time_precision(self):
        cases = [dict(annotation(), submitted_utc_ns=ACTION), dict(annotation(), client_monotonic_ns="01"),
                 dict(annotation(), submitted_utc_ns=str(2**63)), dict(annotation(), event_utc_start_ns="0"),
                 dict(annotation(), clock_domain=""), dict(annotation(), uncertainty_ms=float("nan")),
                 dict(annotation(), uncertainty_ms=10**400), dict(annotation(), uncertainty_ms=True),
                 dict(annotation(), clock_quality="trusted_robot"), dict(annotation(), text="a"*4097),
                 dict(annotation(), text="é"*2049), dict(annotation(), text="\ud800"),
                 dict(annotation(), tags=["drive"]*21), dict(annotation(), revision=True),
                 dict(annotation(), run_id="unverified-run"), dict(annotation(), robot_acknowledged=True),
                 dict(annotation(), schema_version=True)]
        for payload in cases:
            with self.subTest(fields=str(payload)[:100]), self.assertRaises(NotebookError):
                self.book.save(payload)
        self.assertEqual(self.book.list()["annotations"], [])

    def test_exact_times_preserve_offset_and_reject_invalid_calendar_input(self):
        payload = annotation()
        payload.update(when={"kind": "exact_interval", "start": "2026-11-01T01:30:00-05:00", "end": "2026-11-01T01:30:00-06:00"},
                       event_utc_start_ns="1793514600000000000", event_utc_end_ns="1793518200000000000")
        self.book.save(payload)
        self.assertEqual(self.book.history("event-a")["annotation"]["when"], payload["when"])
        for start in ("2026-02-30T01:00:00Z", "2026-11-01T01:30:00", "2026-11-01T25:30:00Z"):
            invalid = dict(payload, event_id="invalid", when={"kind": "exact_interval", "start": start, "end": start})
            with self.subTest(start=start), self.assertRaises(NotebookError):
                self.book.save(invalid)
        with self.assertRaises(NotebookError):
            self.book.save(dict(payload, event_id="mismatch", event_utc_start_ns="1"))

    def test_concurrent_idempotent_creates_make_one_revision(self):
        results, errors = [], []
        barrier = threading.Barrier(3)
        def save():
            barrier.wait()
            try:
                results.append(self.book.save(annotation()))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(sorted(result["idempotent"] for result in results), [False, True])
        self.assertEqual(len(self.book.history("event-a")["revisions"]), 1)

    def test_schema_helper_obeys_callers_transaction(self):
        path = Path(self.temp.name) / "rolled-back.sqlite3"
        db = sqlite3.connect(path)
        db.execute("BEGIN IMMEDIATE")
        install_schema(db)
        db.rollback()
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='annotation_revisions'").fetchone())
        db.close()


class NotebookHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = DemoSource()
        self.service = HubService(Config(data_dir=self.temp.name), self.source)
        self.httpd = create_http_server(self.service, self.source, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(2)
        self.httpd.server_close()
        self.service.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body, headers or {"Content-Type": "application/json", "X-Hub-Request": "1"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_page_and_api_save_edit_history_and_search(self):
        status, page = self.request("GET", "/notebook")
        self.assertEqual(status, 200)
        self.assertIn(b'Mark event', page)
        self.assertIn(b'textContent', page)
        self.assertNotIn(b'innerHTML', page)
        payload = annotation()
        status, body = self.request("POST", "/api/v1/annotations", payload)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["annotation"]["delivery_state"], "historical_hub_only")
        self.assertEqual(self.request("POST", "/api/v1/annotations", payload)[0], 200)
        edit = dict(payload, revision=2, text='<img src=x onerror="alert(1)">')
        envelope = {"annotation": edit, "expected_previous_revision": 1}
        self.assertEqual(self.request("POST", "/api/v1/annotations/event-a/revisions", envelope)[0], 201)
        status, body = self.request("GET", "/api/v1/annotations/event-a")
        self.assertEqual(len(json.loads(body)["revisions"]), 2)
        self.assertEqual(json.loads(body)["annotation"]["text"], edit["text"])
        self.assertEqual(self.request("POST", "/api/v1/annotations/event-a/revisions", envelope)[0], 200)
        status, body = self.request("GET", "/api/v1/annotations?from="+payload["event_utc_start_ns"]+"&to="+payload["event_utc_end_ns"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["annotations"][0]["revision"], 2)

    def test_security_and_bounded_requests_and_errors(self):
        self.assertEqual(self.request("POST", "/api/v1/annotations", annotation(), {"Content-Type":"application/json"})[0], 403)
        self.assertEqual(self.request("POST", "/api/v1/annotations", annotation(),
            {"Content-Type":"application/json", "X-Hub-Request":"1", "Origin":"http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/v1/annotations", dict(annotation(), text="x"*20000))[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/annotations?limit=101")[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/annotations?limit=1&limit=2")[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/annotations/missing")[0], 404)
        self.assertEqual(self.request("POST", "/api/v1/annotations/new-event/revisions",
            {"annotation": annotation("new-event"), "expected_previous_revision": 0})[0], 400)
        self.assertEqual(self.request("POST", "/api/v1/annotations", annotation())[0], 201)
        status, body = self.request("POST", "/api/v1/annotations", dict(annotation(), text="Conflict"))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error_code"], "revision_conflict")

    def test_service_shutdown_refuses_new_notes(self):
        self.service.stop.set()
        self.assertEqual(self.request("POST", "/api/v1/annotations", annotation())[0], 503)


if __name__ == "__main__":
    unittest.main()
