"""Write small invented AdvantageKit 26-style WPILOGs for tests (no real robot data).

Shape copied from a real 2026 log's structure: /Timestamp int64 microseconds with
no unit metadata, DriverStation booleans logged on change, EpochTimeMicros int64,
SwerveStates as struct:SwerveModuleState[], and a final record cut off mid-write.
"""
import json
import struct

SCHEMAS = {'Rotation2d': 'double value', 'SwerveModuleState': 'double speed;Rotation2d angle'}


class Writer:
    def __init__(self):
        self.data = bytearray(b'WPILOG' + (0x100).to_bytes(2, 'little') + (12).to_bytes(4, 'little') + b'AdvantageKit')
        self.entries = {}

    def _record(self, entry, timestamp_us, payload):
        self.data += bytes([0x7F]) + entry.to_bytes(4, 'little') + len(payload).to_bytes(4, 'little')
        self.data += timestamp_us.to_bytes(8, 'little') + payload

    def _entry(self, name, kind, timestamp_us):
        if name not in self.entries:
            identity = len(self.entries) + 1
            self.entries[name] = identity
            parts = [name.encode(), kind.encode(), json.dumps({'source': 'AdvantageKit'}).encode()]
            payload = bytes([0]) + identity.to_bytes(4, 'little')
            for part in parts:
                payload += len(part).to_bytes(4, 'little') + part
            self._record(0, timestamp_us, payload)
        return self.entries[name]

    def value(self, name, kind, timestamp_us, value):
        if kind.startswith('struct:') and not any(n == '/.schema/' + kind for n in self.entries):
            for schema in SCHEMAS:
                self.value('/.schema/struct:' + schema, 'structschema', timestamp_us, SCHEMAS[schema])
        if kind == 'int64':
            payload = struct.pack('<q', value)
        elif kind == 'double':
            payload = struct.pack('<d', value)
        elif kind == 'boolean':
            payload = bytes([1 if value else 0])
        elif kind in ('string', 'structschema'):
            payload = value.encode()
        elif kind == 'struct:SwerveModuleState[]':
            payload = b''.join(struct.pack('<dd', speed, angle) for speed, angle in value)
        else:
            raise ValueError(kind)
        self._record(self._entry(name, kind, timestamp_us), timestamp_us, payload)

    def truncated_tail(self):
        self.data += bytes([0x7F]) + (1).to_bytes(4, 'little') + (100).to_bytes(4, 'little') + b'\x00\x00'


def match_log(*, broken_module=3, battery_low=6.5, epoch_start_us=1_789_000_000_000_000):
    """A 1 s auto run and a 2 s teleop run; one drive module gets no voltage."""
    w = Writer()
    t, ds = 1_000_000, {}

    def cycle(enabled, auto, moving):
        nonlocal t
        w.value('/Timestamp', 'int64', t, t)
        for name, state in (('Enabled', enabled), ('Autonomous', auto), ('Test', False)):
            if ds.get(name) != state:
                w.value('/DriverStation/' + name, 'boolean', t, state)
                ds[name] = state
        w.value('/SystemStats/EpochTimeMicros', 'int64', t, epoch_start_us + t)
        w.value('/SystemStats/BatteryVoltage', 'double', t, battery_low if moving and not auto else 12.4)
        w.value('/SystemStats/SerialNumber', 'string', t, 'invented-rio')
        w.value('/RealOutputs/LoggedRobot/FullCycleMS', 'double', t, 30.0 if moving and not auto else 15.0)
        speed = 3.0 if moving else 0.0
        w.value('/RealOutputs/SwerveStates/SetpointsOptimized', 'struct:SwerveModuleState[]', t, [(speed, 0.0)] * 4)
        w.value('/RealOutputs/SwerveStates/Measured', 'struct:SwerveModuleState[]', t,
                [(0.0 if i == broken_module else speed * 0.95, 0.0) for i in range(4)])
        for i in range(4):
            amps = 0.5 if (i == broken_module or not moving) else 20.0
            w.value(f'/Drive/Module{i}/DriveCurrentAmps', 'double', t, amps)
            w.value(f'/Drive/Module{i}/DriveTempCelsius', 'double', t, 25.0 + (t - 1_000_000) / 1e6)
        t += 20_000

    for _ in range(10):
        cycle(False, False, False)
    for _ in range(50):
        cycle(True, True, True)
    for _ in range(10):
        cycle(False, False, False)
    for _ in range(100):
        cycle(True, False, True)
    for _ in range(10):
        cycle(False, False, False)
    w.truncated_tail()
    return bytes(w.data)
