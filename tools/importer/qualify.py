"""Compare the portable extractor with the locked official Alpha7 reader."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from robot_test_hub.wpilog import extract


def official_value(row):
    value = row["value"]
    def pose(item):
        return {"translation": {"x": item["x_m"], "y": item["y_m"]}, "rotation": {"value": item["heading_rad"]}}
    if row["type"] == "struct:Pose2d":
        return pose(value)
    if row["type"] == "struct:Pose2d[]":
        return [pose(v) for v in value]
    return value


def main():
    spec = importlib.util.spec_from_file_location("fixture_builder", ROOT / "tools/fixtures/run.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    command, env = builder.prepare(Path(r"C:\Users\Public\wpilib\2027_alpha7"), Path.home() / ".gradle/caches")
    total = 0
    for path in sorted((ROOT / "tests/fixtures/synthetic").glob("*.wpilog")):
        output = subprocess.run(command + ["OfficialReader", str(path)], env=env, check=True, capture_output=True, text=True)
        expected = json.loads(output.stdout)["records"]
        actual = [r for r in extract(path) if r["kind"] in ("observation", "control")]
        assert len(actual) == len(expected), f"{path.name}: record count differs"
        for index, (left, right) in enumerate(zip(actual, expected)):
            label = f"{path.name}: record {index}"
            assert left["record_index"] == index, label
            assert left["record_timestamp_ns"] == str(right["timestamp_ns"]), label
            assert left["field"] == right["name"], label
            if left["kind"] == "observation":
                assert left["type"] == right["type"], label
                value = int(left["value"]) if left["field"] == "/Timestamp" else left["value"]
                assert value == official_value(right), label
            else:
                assert left["control"] == right["control"], label
                assert left["metadata_raw"] == right["metadata"], label
        total += len(actual)
        print(f"{path.name}: {len(actual)} physical records agree, including complete short tail")
    print(f"Qualified all {total} physical records in four synthetic recordings")


if __name__ == "__main__":
    main()
