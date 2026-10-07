"""Offline readiness check for live transfer; never contacts a robot or opens an archive."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Config
from .live import validate_configuration
from .sftp_source import SFTPConfig


def check_live(config, source_path, nt_host, nt_port, install=None, *, idle_delay_explicit=False):
    """Check local prerequisites only. Results never include supplied values or exceptions.

    Native preparation verifies pinned bytes and compiles the reader locally.
    It does not launch a reader, perform DNS resolution, or connect to NT/SSH.
    """
    result = dict(schema_version=1, ready=False, network_contacted=False,
                  archive_opened=False, hardware_qualified=False, checks=[], settings=None)

    def record(name, passed, guidance):
        result['checks'].append(dict(name=name, passed=passed, guidance=guidance))

    settings = None
    try:
        settings = SFTPConfig.load(source_path)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        record('source_config', False,
               'Check source JSON schema, explicit endpoint/root/identity, and local key file references.')
    else:
        record('source_config', True, 'Source settings and local file references are valid.')

    paramiko = None
    try:
        import paramiko as transport
        if transport.__version__ != '4.0.0':
            raise ValueError()
        paramiko = transport
    except (ImportError, ValueError, AttributeError):
        record('sftp_dependency', False, 'Install the pinned dependency with pip install -e ".[sftp]".')
    else:
        record('sftp_dependency', True, 'Pinned Paramiko 4.0.0 is installed.')

    if settings is not None and paramiko is not None:
        try:
            keys = paramiko.HostKeys()
            keys.load(settings.known_hosts)
            name = settings.host if settings.port == 22 else f'[{settings.host}]:{settings.port}'
            if not keys.lookup(name):
                raise ValueError()
        except Exception:
            record('pinned_host_key', False,
                   'Known-hosts must contain the exact SSH host and port; verify its fingerprint through a trusted channel.')
        else:
            record('pinned_host_key', True, 'A pinned entry matches the configured SSH host and port.')
        try:
            paramiko.PKey.from_path(settings.private_key)
        except Exception:
            record('private_key', False,
                   'Use a supported local private key readable without an interactive password; no agent or ambient keys are used.')
        else:
            record('private_key', True, 'The explicitly referenced private key is locally readable.')

        try:
            effective, _ = validate_configuration(config, source_path, nt_host, nt_port, install,
                                                 idle_delay_explicit=idle_delay_explicit, settings=settings)
        except (ValueError, TypeError, OSError, OverflowError, RecursionError):
            record('transfer_settings', False,
                   'Set explicit NT host/port and keep hub chunk_size within the source max_read bound.')
        else:
            record('transfer_settings', True, 'NT identity and transfer bounds are valid locally.')
            result['settings'] = dict(idle_delay_seconds=effective.idle_delay,
                                      freshness_seconds=effective.freshness,
                                      chunk_bytes=effective.chunk_size,
                                      sftp_read_bytes=min(settings.max_read, 32768))

    try:
        from tools.status_bridge.run import prepare, DEFAULT_INSTALL
        prepare(Path(install) if install is not None else DEFAULT_INSTALL)
    except Exception:
        record('native_status_reader', False,
               'Use this source checkout and the pinned Windows x86-64 Alpha 7 installation with Java 25; verify dependency hashes.')
    else:
        record('native_status_reader', True, 'Pinned Alpha 7 dependencies verified; status reader compiled locally.')

    required = {'source_config', 'sftp_dependency', 'pinned_host_key', 'private_key',
                'transfer_settings', 'native_status_reader'}
    result['ready'] = (required == {check['name'] for check in result['checks']}
                       and all(check['passed'] for check in result['checks']))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Optional hub JSON, as used by the server')
    parser.add_argument('--source-config', type=Path, required=True)
    parser.add_argument('--nt-host', required=True)
    parser.add_argument('--nt-port', type=int, required=True)
    parser.add_argument('--wpilib-install', type=Path)
    parser.add_argument('--idle-delay', type=float)
    parser.add_argument('--json', action='store_true', help='Emit a redacted machine-readable report')
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config, idle_delay=args.idle_delay)
        explicit = args.idle_delay is not None
        if args.config is not None:
            explicit = explicit or 'idle_delay' in json.loads(args.config.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        report = dict(schema_version=1, ready=False, network_contacted=False, archive_opened=False,
                      hardware_qualified=False, settings=None,
                      checks=[dict(name='hub_config', passed=False,
                                   guidance='Check hub JSON schema and finite configuration bounds.')])
    else:
        report = check_live(config, args.source_config, args.nt_host, args.nt_port, args.wpilib_install,
                            idle_delay_explicit=explicit)
    if args.json:
        print(json.dumps(report, allow_nan=False))
    else:
        print('Live transfer local readiness: ' + ('READY' if report['ready'] else 'NEEDS ATTENTION'))
        for item in report['checks']:
            print(('PASS' if item['passed'] else 'FAIL') + ' ' + item['name'] + ': ' + item['guidance'])
        if report['settings'] is not None:
            values = report['settings']
            print(f"Idle delay {values['idle_delay_seconds']:g} s; freshness {values['freshness_seconds']:g} s; "
                  f"chunk {values['chunk_bytes']} bytes; SFTP read {values['sftp_read_bytes']} bytes")
        print('No robot connection or archive opened. Endpoint reachability, server authorization, receiver health, and hardware performance still require qualification.')
    return 0 if report['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
