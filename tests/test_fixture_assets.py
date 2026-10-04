"""Include portable T04 asset checks in the hub's standard unittest discovery."""
from pathlib import Path
import sys

FIXTURE_TESTS = Path(__file__).resolve().parents[1] / "tools/fixtures/tests"
sys.path.insert(0, str(FIXTURE_TESTS))

from test_synthetic_fixtures import SyntheticFixtureTests  # noqa: E402,F401
