"""Offline fixture build/generation. No dependency downloads or hub runtime imports."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WPILIB_VERSION = "2027.0.0-alpha-7"
AK_VERSION = "27.0.0-alpha-6"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependencies(install, gradle_cache):
    maven = install / "maven"
    jars = []
    for component in ("datalog", "wpiutil", "wpimath", "wpiunits", "wpilibj", "drivers", "hal", "ntcore", "fields", "telemetry", "tunables"):
        name = component + "-java"
        jars.append(maven / "org/wpilib" / component / name / WPILIB_VERSION / f"{name}-{WPILIB_VERSION}.jar")
    jars += [maven / "us/hebi/quickbuf/quickbuf-runtime/1.4/quickbuf-runtime-1.4.jar",
             maven / "com/google/code/gson/gson/2.13.1/gson-2.13.1.jar"]
    matches = sorted(gradle_cache.glob(f"modules-2/files-2.1/org.littletonrobotics.akit/akit-java/{AK_VERSION}/*/akit-java-{AK_VERSION}.jar"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one cached AK {AK_VERSION} binary; found {len(matches)}. Build the pinned robot project first.")
    jars += matches
    natives = []
    for component in ("wpiutil", "datalog"):
        name = component + "-cpp"
        natives.append(maven / "org/wpilib" / component / name / WPILIB_VERSION / f"{name}-{WPILIB_VERSION}-windowsx86-64.zip")
    for path in jars + natives:
        if not path.is_file():
            raise RuntimeError(f"Missing pinned local dependency: {path}")
    return jars, natives


def prepare(install, cache):
    if os.name != "nt":
        raise RuntimeError("Generation currently requires the verified Windows x86-64 Alpha7 native profile. Committed fixtures/tests are portable.")
    java = install / "jdk/bin/java.exe"
    javac = install / "jdk/bin/javac.exe"
    jars, zips = dependencies(install, cache)
    locks = json.loads((HERE / "dependencies.lock.json").read_text())
    actual = {p.name: sha256(p) for p in jars + zips}
    if actual != locks["sha256"]:
        raise RuntimeError("Local dependency bytes differ from dependencies.lock.json; review the exact version/profile before regenerating.")
    version = subprocess.run([str(java), "-version"], capture_output=True, text=True, check=True)
    if 'version "25' not in version.stderr:
        raise RuntimeError(f"Fixture compiler requires JDK25; got {version.stderr}")
    build = HERE / "build"
    classes, native_dir = build / "classes", build / "native"
    classes.mkdir(parents=True, exist_ok=True)
    native_dir.mkdir(parents=True, exist_ok=True)
    for path in zips:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.filename.endswith(".dll"):
                    (native_dir / Path(entry.filename).name).write_bytes(archive.read(entry))
    classpath = os.pathsep.join(str(p) for p in jars)
    subprocess.run([str(javac), "-cp", classpath, "-d", str(classes), *map(str, sorted((HERE / "src").glob("*.java")))], check=True)
    env = os.environ.copy()
    env["PATH"] = str(native_dir) + os.pathsep + env.get("PATH", "")
    return [str(java), "--enable-native-access=ALL-UNNAMED", f"-Djava.library.path={native_dir}", "-cp", str(classes) + os.pathsep + classpath], env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "verify", "read"))
    parser.add_argument("--install", type=Path, default=Path(r"C:\Users\Public\wpilib\2027_alpha7"))
    parser.add_argument("--gradle-cache", type=Path, default=Path.home() / ".gradle/caches")
    parser.add_argument("--output", type=Path, default=ROOT / "tests/fixtures/synthetic")
    parser.add_argument("--file", type=Path, help="WPILOG to inspect for the read action")
    args = parser.parse_args()
    command, env = prepare(args.install, args.gradle_cache)
    if args.action == "read":
        if args.file is None:
            parser.error("read requires --file")
        subprocess.run(command + ["OfficialReader", str(args.file)], check=True, env=env)
        return
    args.output.mkdir(parents=True, exist_ok=True)
    if args.action == "generate":
        subprocess.run(command + ["GenerateFixtures", str(args.output)], check=True, env=env)
    from validate import CASES, validate_bytes, validate_official
    files = []
    for name, case in CASES.items():
        path = args.output / (name + ".wpilog")
        validate_bytes(path.read_bytes(), case)
        result = subprocess.run(command + ["OfficialReader", str(path)], check=True, env=env, text=True, capture_output=True)
        report = json.loads(result.stdout)
        validate_official(report, case)
        files.append({"file": path.name, "size_bytes": path.stat().st_size, "sha256": sha256(path), **case})
        print(f"Verified {path.name}: {path.stat().st_size} bytes, {len(case['timestamp_ns'])} cycles; official WPILib + AK readers")
    manifest = {"schema_version": 1, "source_type": "SYNTHETIC", "profile": "wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6",
                "generator": "tools/fixtures/src/GenerateFixtures.java", "record_header_unit_on_disk": "microseconds",
                "reader_timestamp_unit": "nanoseconds", "timestamp_payload_unit": "nanoseconds", "epoch_unit": "microseconds",
                "odometry_timestamp_unit": "seconds", "older_profile": {"status": "unsupported", "reason": "No reviewed version-matched 2026 fixture/reader has been verified."},
                "files": files}
    manifest_path = args.output / "expected.json"
    if args.action == "generate":
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    elif json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise AssertionError("Committed expected manifest differs from validated fixture content")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, AssertionError, subprocess.CalledProcessError) as error:
        print(f"Fixture tool failed: {error}", file=sys.stderr)
        raise SystemExit(1)
