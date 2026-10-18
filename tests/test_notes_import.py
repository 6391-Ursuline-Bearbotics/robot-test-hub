"""CSV (Google Form) note import: event time from the timestamp, idempotent re-import."""
from pathlib import Path
import tempfile
import unittest

from robot_test_hub.notebook import Notebook
from robot_test_hub import notes_import

FORM = ('Timestamp,Your name,Subsystem,What happened?\n'
        '10/18/2026 15:04:05,Ava,Drive,Back right wheel stopped driving\n'
        '10/18/2026 15:06:00,Ben,,\n'
        'yesterday,Ben,Intake,Intake jammed\n')


class NotesImportTests(unittest.TestCase):
    def test_import_maps_columns_times_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            book = Notebook(Path(folder) / 'catalog.sqlite3')
            first = notes_import.import_csv(book, FORM, timezone_name='America/Chicago')
            self.assertEqual(first['saved'], 1)
            self.assertEqual([s['row'] for s in first['skipped']], [3, 4])
            self.assertEqual(first['columns'], {'time': 'Timestamp', 'text': 'What happened?', 'author': 'Your name', 'tag': 'Subsystem'})
            [note] = book.list()['annotations']
            self.assertEqual(note['event_utc_start_ns'], str(1792353845 * 10**9))  # 15:04:05 CDT = 20:04:05 UTC
            self.assertEqual(note['when'], {'kind': 'exact_interval', 'start': '2026-10-18T15:04:05-05:00', 'end': '2026-10-18T15:04:05-05:00'})
            self.assertEqual((note['author'], note['tags'], note['clock_quality']), ('Ava', ['drive'], 'user_estimate'))
            again = notes_import.import_csv(book, FORM, timezone_name='America/Chicago')
            self.assertEqual((again['saved'], again['already_imported']), (0, 1))

    def test_requires_time_and_note_columns_and_valid_zone(self):
        with self.assertRaises(notes_import.ImportError_):
            notes_import.payloads('Name,Color\nAva,red\n', timezone_name='UTC')
        with self.assertRaises(notes_import.ImportError_):
            notes_import.payloads(FORM, timezone_name='Mars/Base')

    def test_iso_and_offset_times(self):
        items, _, _ = notes_import.payloads('Timestamp,Note\n2026-10-18T20:04:05Z,ok\n2026-10-18 15:04,local\n',
                                            timezone_name='America/Chicago')
        self.assertEqual(items[0]['event_utc_start_ns'], str(1792353845 * 10**9))
        self.assertEqual(items[1]['when']['start'], '2026-10-18T15:04:00-05:00')


if __name__ == '__main__':
    unittest.main()
