# Public synthetic WPILOG fixtures

T04 provides four tiny genuine AdvantageKit recordings generated with the team's exact installed profile: WPILib **2027.0.0-alpha-7**, AdvantageKit **27.0.0-alpha-6**, and **JDK 25**, Windows x86-64. All values and identities are invented. There are no robot recordings, source connections, HAL robot initialization, deployment, or hub runtime dependencies. The public fixture directory is `tests/fixtures/synthetic/`; generated native libraries/classes stay under ignored `tools/fixtures/build/`.

From the hub repository root in PowerShell:

```powershell
python tools/fixtures/run.py generate
python tools/fixtures/run.py verify
python -m unittest discover -s tools/fixtures/tests -v
python tools/fixtures/run.py read --file tests/fixtures/synthetic/alpha7-main.wpilog
```

`generate` compiles two small Java programs using the installed JDK, writes the four synthetic logs with the actual `WPILOGWriter`, and verifies every file using a separate official reader executable and an independent Python literal oracle. It replaces only the known fixture names in the specified output directory. `verify` recompiles/verifies existing logs and compares their content and hashes with `expected.json`. `read` prints an official-reader JSON projection, including decoded poses, arrays, units, metadata controls, and AK replay cycles; it is a fixture inspection tool, not the production importer. Python's compatibility floor is 3.10.

No download or Gradle build is performed. The default installation is `C:\Users\Public\wpilib\2027_alpha7`, with AK taken from `%USERPROFILE%\.gradle\caches`. Override via `--install` and `--gradle-cache`. Missing dependencies, mismatched pinned hashes, a non-JDK25 installation, or an unverified native platform fail clearly. `dependencies.lock.json` pins SHA-256 for every binary Java/native input, including Quickbuf 1.4 and Gson 2.13.1. Build the version-pinned robot project separately if its AK cache is absent; this tool does not modify it. Portable fixture checks require only Python and the committed synthetic files.

`tests/test_fixture_assets.py` also includes the nine portable checks in the repository's standard `python -m unittest discover -s tests -v` suite, so CI needs no installed Java/native profile or separate download to validate the committed assets.

Reproducibility check:

```powershell
python tools/fixtures/run.py generate --output tools/fixtures/build/repro
```

The generated log bytes and `expected.json` in that directory must match the committed files byte for byte. This was verified locally in separate Java processes. AK's initial `/Timestamp` start control normally uses current time; the generator freezes `WPIUtilJNI` mock time at 1,000,000,000 ns so that control is deterministic too. The tested hash-map entry ordering is specific to the locked toolchain/JDK profile; other runtimes are not claimed to produce identical bytes.

## Known values and scenarios

`expected.json` and the separate literal oracle `validate.py` declare cycle timestamps, epoch values, validity, modes, boot identities, terminal-event availability, exact lengths/hashes, record counts, and complete short-tail sizes. The Java generator does not import these expected values.

| File | Bytes | Cycles | Records | Scenarios |
| --- | ---: | ---: | ---: | --- |
| `alpha7-main.wpilog` | 4530 | 7 | 143 | Invalid/valid epoch, +2 s UTC jump, two enabled intervals, auto to teleop without disable, 420 ms gap, disconnected/missing odometry sample, sub-microsecond payload precision |
| `alpha7-boot-b.wpilog` | 3364 | 3 | 81 | Different boot with overlapping monotonic values; enabled final sample without terminal event |
| `alpha7-dst-overlap.wpilog` | 3207 | 2 | 69 | 2026-11-01 06:30Z and 07:30Z correspond to 01:30 at UTC-05 and UTC-06 in America/Chicago |
| `alpha7-dst-gap.wpilog` | 3194 | 2 | 69 | 2026-03-08 07:59:59Z to 08:00:00Z spans the local 01:59:59 to 03:00:00 spring transition |

Every log includes synthetic build/robot/boot metadata, `Pose2d` with nested `Translation2d`/`Rotation2d` schemas, a two-element `Pose2d[]`, ragged `double[][]` encoded by AK as `/length` and child arrays (including an empty array), mode/enable flags, odometry timestamp/position arrays, and separate `RealOutputs`/`ReplayOutputs`. At cycle `i`, pose is `(1.25+i m, -2.5 m, 0.25 rad)` and the second pose-array element is `(-1 m, 2 m, -0.5 rad)`. Real speed is `1+i m/s`, replay speed is `10+i m/s`. Unit metadata changes from meters to centimeters; start and update controls are both verified.

The main fixture's cycle 3 marks the module disconnected and emits no new odometry sample; its retained values are the previous observation, not fresh zeros. AK suppresses unchanged fields, so an importer must keep physical sample availability distinct from replay-held state. Odometry drive positions at source cycle `i` are `[i+0.25, i+0.5]` radians. All files close cleanly at the byte level. `boot-b` deliberately lacks a logged terminal event while enabled, representing incomplete run coverage; it does not pretend to be an actual killed process or power-loss recording. Every partial final-record cut, bad magic/version/extra-header length, reserved header flag, and appended garbage are tested in memory and must fail strict framing. Well-framed changes to boot identity, mode, timestamp payload precision, pose/odometry values, and unit metadata fail the separate literal oracle; an unregistered data entry fails fixture decoding. These test mutations leave the public recordings unchanged.

## Four timestamp domains

| Domain | Unit | Exact source evidence |
| --- | --- | --- |
| WPILOG binary record header | microseconds | `datalog-java` Alpha7 `DataLogReader.getRecord()` reads the encoded value and multiplies by 1000 |
| Alpha7 Java `DataLogRecord.getTimestamp()` | nanoseconds | Same conversion; the reader result JSON uses `timestamp_ns` |
| AK `/Timestamp` integer payload | nanoseconds | AK Alpha6 `WPILOGWriter.start()` supplies `unit=nanoseconds`; `putTable()` appends `LogTable.getTimestamp()` |
| `SystemStats/EpochTime` double payload | microseconds since Unix epoch | AK Alpha6 `LoggedSystemStats.saveToLog()` divides conduit nanoseconds by 1000 and labels `microseconds`; validity is independent |

High-rate odometry timestamps are seconds, following the sibling robot's array convention. They are sample timestamps, distinct from the enclosing table timestamp. The main second `/Timestamp` payload is **1,020,000,123 ns**, while its binary header is **1,020,000 us** and the official reader exposes **1,020,000,000 ns**. The 123 ns payload remainder is preserved; inventing header precision would be incorrect.

Installed source locations used for verification:

- `C:\Users\Public\wpilib\2027_alpha7\maven\org\wpilib\datalog\datalog-java\2027.0.0-alpha-7\datalog-java-2027.0.0-alpha-7-sources.jar`: `org/wpilib/datalog/DataLogReader.java`, `DataLogIterator.java`, `DataLogWriter.java`, `DataLog.java`.
- `C:\Users\Public\wpilib\2027_alpha7\maven\org\wpilib\wpimath\wpimath-java\2027.0.0-alpha-7\wpimath-java-2027.0.0-alpha-7-sources.jar`: geometry struct implementations.
- `%USERPROFILE%\.gradle\caches\modules-2\files-2.1\org.littletonrobotics.akit\akit-java\27.0.0-alpha-6\b5a855e40c53e2fe647d30d42566ae1cd859d1f0\akit-java-27.0.0-alpha-6-sources.jar`: `LogTable.java`, `LoggedSystemStats.java`, `wpilog/WPILOGWriter.java`, `WPILOGReader.java`, `WPILOGConstants.java`.

## Verified reader limitation and future importer regression

Alpha7 `DataLogIterator.hasNext()` requires at least **16 bytes** remaining. The logs end with valid complete records of 14 bytes (15 for DST overlap). Enhanced-for traversal skips that last record. Official `DataLogReader.forEach()` reads it. This verifier deliberately preserves the edge case and checks both record counts; it never hides it by padding. Strict independent framing consumes all bytes and rejects truncation rather than trusting `DataLogReader.isValid()` (which checks only the header) or silent iterator exhaustion.

AK `WPILOGReader` uses that iterator. Its replay cycle times, sequences, real speed values, and exclusion of `ReplayOutputs` pass, but it omits the main fixture's final `/Fixture/Mode=DISABLED` (14-byte record) and retains `TELEOP`. The other omitted tails are final `/Fixture/NestedArray/2` updates. The verifier explicitly expects and reports the version-specific omission. T05 must preserve complete short tails and detect truncated tails; do not copy the iterator's incomplete traversal into an importer. This is an installed alpha library behavior, not evidence that those source bytes are invalid.

Local evidence: four logs validated by the version-matched official WPILib reader; AK replay checked with its documented tail limitation; nine portable tests passed; regeneration produced identical log/manifest bytes. AdvantageScope GUI opening has **not** been checked. No hardware or robot simulation was started. Older 2026 profiles remain **unsupported** until exact dependencies or reviewed data and a matching reader are independently verified. These fixtures establish the current profile; they do not complete V14 cross-year ingestion/time mapping or T10 rotation/reassembly.
