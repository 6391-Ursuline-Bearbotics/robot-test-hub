from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.service import HubService
from robot_test_hub.storage import DataRootOwner


class MarkerStartupTests(unittest.TestCase):
    def test_marker_schema_failure_releases_archive_owner_for_repaired_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(data_dir=directory)
            with patch('robot_test_hub.marker_service.MarkerStore',
                       side_effect=sqlite3.OperationalError('invented private path')):
                with self.assertRaises(sqlite3.OperationalError):
                    HubService(config, DemoSource())
            owner = DataRootOwner(Path(directory))
            owner.close()
            repaired = HubService(config, DemoSource())
            repaired.close()
