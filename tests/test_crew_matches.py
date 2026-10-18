"""Match detection, crew selection/corrections and stick time on invented data."""
from contextlib import closing
import contextlib
import http.client
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from robot_test_hub import crew, import_folder, matches, practice
from robot_test_hub.config import Config
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE_AKIT26
from akit26_fixture import match_log
from test_processing_http import GeneratedRecordingSource

S = 1_000_000_000


def run(run_id, mode, start, end, boot='b1'):
    return {'run_id': run_id, 'boot_id': boot, 'phases': [{'mode': mode}],
            'start_monotonic_ns': str(start * S), 'end_monotonic_ns': str(end * S)}


class MatchTests(unittest.TestCase):
    def test_pairs_auto_and_teleop_and_labels_fms_matches(self):
        runs = [run('a', 'AUTONOMOUS', 100, 120), run('t', 'TELEOPERATED', 123, 263), run('free', 'TELEOPERATED', 400, 460)]
        contexts = {'a': {'match_time': 20.0, 'fms_attached': True, 'match_type': 2, 'match_number': 6, 'event_name': 'MOST1'},
                    't': {'match_time': 140.0}, 'free': {'match_time': 0.0}}
        found = matches.detect(runs, contexts)
        self.assertEqual(found['a']['label'], 'Qualification 6 · MOST1')
        self.assertIs(found['a'], found['t'])
        self.assertTrue(found['a']['complete'])
        self.assertNotIn('free', found)

    def test_auto_disabled_early_still_pairs_and_practice_mode_label(self):
        runs = [run('a', 'AUTONOMOUS', 100, 104), run('t', 'TELEOPERATED', 123, 263)]
        found = matches.detect(runs, {'a': {'match_time': 20.0, 'fms_attached': False}, 't': {'match_time': 140.0}})
        self.assertEqual(found['t']['label'], 'Practice match')

    def test_other_boot_or_late_teleop_is_not_paired(self):
        contexts = {'a': {'match_time': 20.0}, 't': {'match_time': 140.0}}
        self.assertFalse(matches.detect([run('a', 'AUTONOMOUS', 100, 120), run('t', 'TELEOPERATED', 123, 263, 'b2')], contexts)['a']['complete'])
        self.assertFalse(matches.detect([run('a', 'AUTONOMOUS', 100, 120), run('t', 'TELEOPERATED', 200, 340)], contexts)['a']['complete'])


class CrewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        (self.base / 'usb').mkdir()
        (self.base / 'usb' / 'invented.wpilog').write_bytes(match_log())
        self.data = self.base / 'data'
        with contextlib.redirect_stdout(io.StringIO()):
            import_folder.main([str(self.base / 'usb'), '--data-dir', str(self.data), '--profile', PROFILE_AKIT26, '--synthetic', '--json'])

    def test_selection_interval_correction_revision_and_stick_time(self):
        ava = crew.add_student(self.data, 'Ava')['id']
        ben = crew.add_student(self.data, ' Ben ')['id']
        self.assertEqual(crew.add_student(self.data, 'ava')['id'], ava)
        with self.assertRaises(crew.CrewError):
            crew.select_crew(self.data, {'driver': 'nobody'})
        cards = practice.day(self.data)['cards']
        self.assertEqual([c['crew'] for c in cards], [None, None])
        # The invented log is from 2026-09-10; a selection saved before it covers both runs.
        crew.select_crew(self.data, {'driver': ava, 'operator': ben}, now_ns=1)
        cards = practice.day(self.data)['cards']
        self.assertEqual(cards[0]['crew']['roles']['driver']['name'], 'Ava')
        self.assertEqual(cards[0]['crew']['source'], 'selected')
        first = crew.correct_run(self.data, cards[1]['run_id'], {'driver': ben}, 'swapped')
        second = crew.correct_run(self.data, cards[1]['run_id'], {'driver': ben, 'operator': ava})
        self.assertEqual((first['revision'], second['revision']), (1, 2))
        cards = practice.day(self.data)['cards']
        self.assertEqual(cards[1]['crew']['source'], 'corrected')
        self.assertEqual(cards[1]['crew']['roles']['operator']['name'], 'Ava')
        with closing(open_catalog(self.data)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM crew_corrections').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM crew_selections').fetchone()[0], 1)
        stats = {(s['name'], s['role']): s for s in crew.stick_time(self.data)['students']}
        self.assertEqual(stats[('Ava', 'driver')]['runs'], 1)
        self.assertEqual(stats[('Ben', 'driver')]['runs'], 1)
        self.assertEqual(stats[('Ava', 'driver')]['matches'], 0)  # the invented log has no match clock
        self.assertEqual(crew.stick_time(self.data)['unassigned']['runs'], 0)

    def test_unassigned_time_is_never_credited(self):
        crew.add_student(self.data, 'Ava')
        result = crew.stick_time(self.data)
        self.assertEqual(result['students'], [])
        self.assertEqual(result['unassigned']['runs'], 2)
        self.assertGreater(result['unassigned']['seconds'], 2.5)

    def test_http_roster_and_crew_routes(self):
        source = GeneratedRecordingSource()
        service = HubService(Config(data_dir=str(self.data)), source)
        self.addCleanup(service.close)
        server = create_http_server(service, source, 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(3), server.server_close()))

        def call(method, path, payload=None, hub='1'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request(method, path, body=json.dumps(payload).encode() if payload is not None else None,
                                   headers={'Content-Type': 'application/json', 'X-Hub-Request': hub})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        status, student = call('POST', '/api/v1/roster', {'name': 'Ava'})
        self.assertEqual(status, 201)
        self.assertEqual(call('POST', '/api/v1/roster', {'name': ''}), (400, {'error_code': 'invalid_name'}))
        self.assertEqual(call('POST', '/api/v1/roster', {'name': 'Ben'}, hub='0')[0], 403)
        self.assertEqual(call('POST', '/api/v1/crew', {'driver': student['id']})[0], 201)
        self.assertEqual(call('GET', '/api/v1/crew')[1]['current']['roles'], {'driver': student['id']})
        self.assertEqual(call('POST', '/api/v1/roster/' + student['id'], {'active': False}), (200, {'ok': True}))
        self.assertEqual(call('GET', '/api/v1/roster')[1]['students'][0]['active'], 0)
        self.assertEqual(call('GET', '/api/v1/practice-time?window=season')[0], 200)
        self.assertEqual(call('GET', '/api/v1/practice-time?window=forever')[0], 400)


if __name__ == '__main__':
    unittest.main()
