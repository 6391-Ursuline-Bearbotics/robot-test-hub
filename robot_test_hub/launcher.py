"""Private, explicit launch profiles for the foreground Windows hub.

Check is local only. It cannot qualify endpoint reachability or hardware.
"""
from __future__ import annotations

import argparse
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import webbrowser

from .config import Config

RELEASE_ROOT = Path(__file__).resolve().parent.parent
PATH_FIELDS = ('hub_config', 'source_config', 'wpilib_install', 'marker_config',
               'video_config', 'video_media_config', 'rewind_config')
LIVE_FIELDS = {'source_config', 'nt_host', 'nt_port', 'wpilib_install'}
FIELDS = {'schema_version', 'mode', 'nt_host', 'nt_port', *PATH_FIELDS}


class LaunchError(ValueError):
    """Fixed, redacted guidance safe for console and report export."""


def _json(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError()
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError()
    return value


def _local_path(value, base):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(c) < 32 for c in value) or value.startswith(('\\\\', '//'))):
        raise LaunchError('Use explicit local file paths; network shares are unsupported.')
    resolved = (base / value).resolve()
    if str(resolved).startswith(('\\\\', '//')):
        raise LaunchError('Use local paths; network shares are unsupported.')
    return resolved


def _private(path):
    if path == RELEASE_ROOT or path.is_relative_to(RELEASE_ROOT):
        raise LaunchError('Keep private profiles, configuration and data outside the code/release directory.')


def load_profile(path):
    """Load strict schema 1; resolve every profile reference against its file."""
    path = Path(path).resolve()
    _private(path)
    try:
        value = _json(path)
        if (value.keys() - FIELDS or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or value.get('mode') not in ('live', 'demo')
                or 'hub_config' not in value):
            raise ValueError()
        if value['mode'] == 'live':
            if not LIVE_FIELDS <= value.keys():
                raise ValueError()
            host, port = value['nt_host'], value['nt_port']
            if (not isinstance(host, str) or not 1 <= len(host) <= 253
                    or host.startswith('-') or any(c.isspace() or ord(c) < 32 or c in '/@\\' for c in host)
                    or type(port) is not int or not 1 <= port <= 65535):
                raise ValueError()
        elif value.keys() & (LIVE_FIELDS | {'rewind_config'}):
            raise ValueError()
        for field in PATH_FIELDS:
            if field in value:
                value[field] = _local_path(value[field], path.parent)
                if field != 'wpilib_install':
                    _private(value[field])
                if not (value[field].is_dir() if field == 'wpilib_install' else value[field].is_file()):
                    raise ValueError()
        return value
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise LaunchError('Invalid launch profile: check schema 1, explicit mode, required local paths and live endpoint; demo rejects live transfer/Rewind settings.') from None


def _inspect(profile):
    """Validate local settings only; never construct a service or open an archive."""
    raw = _json(profile['hub_config'])
    if 'data_dir' not in raw:
        raise LaunchError('Hub configuration must explicitly select a private data_dir.')
    data = _local_path(raw['data_dir'], profile['hub_config'].parent)
    _private(data)
    if data == profile['hub_config'].parent or profile['hub_config'].is_relative_to(data):
        raise LaunchError('Keep private configuration outside the active archive directory.')
    for field in ('backup_destination', 'log_export_destination'):
        if raw.get(field) is not None:
            _private(_local_path(raw[field], profile['hub_config'].parent))
    config = Config.load(profile['hub_config'], resolve_paths=True)
    if profile['mode'] == 'live':
        from .sftp_source import SFTPConfig
        source_raw = _json(profile['source_config'])
        for key in ('known_hosts', 'private_key'):
            _local_path(source_raw.get(key), profile['source_config'].parent)
        settings = SFTPConfig.load(profile['source_config'])
        for key in ('known_hosts', 'private_key'):
            _private(Path(getattr(settings, key)))
    from .marker_service import load_marker_config
    from .recorder_service import load_video_config
    from .video_media import load_media_config, sha
    from .rewind_service import load_rewind_config
    if 'marker_config' in profile:
        marker = load_marker_config(profile['marker_config'])
        if marker.install is not None:
            if not Path(marker.install).is_absolute():
                raise LaunchError('Marker installation path must be absolute.')
            _local_path(marker.install, profile['marker_config'].parent)
        if profile['mode'] == 'demo' and marker.runtime_mode != 'SIM':
            raise LaunchError('Demo profiles permit only SIM marker delivery.')
    if 'video_config' in profile:
        video = load_video_config(profile['video_config'])
        if profile['mode'] == 'demo' and video.source_type != 'SYNTHETIC':
            raise LaunchError('Demo profiles permit only synthetic recording inputs.')
        for executable in (video.executable, video.probe_executable):
            _local_path(executable, profile['video_config'].parent)
        if not all(Path(item).is_file() for item in (video.executable, video.probe_executable)):
            raise LaunchError('Configured recording tools are not available locally.')
    if 'video_media_config' in profile:
        media = load_media_config(profile['video_media_config'])
        for executable, digest in ((media.ffmpeg, media.ffmpeg_sha256), (media.ffprobe, media.ffprobe_sha256)):
            if sha(Path(executable)) != digest:
                raise LaunchError('Configured media tools do not match their local binary pins.')
    if 'rewind_config' in profile:
        rewind = load_rewind_config(profile['rewind_config'])
        if rewind.source_type != 'REAL' or rewind.robot_id != settings.robot_id:
            raise LaunchError('Live Rewind must select REAL camera provenance and the authoritative source robot identity.')
    return config, 'idle_delay' in raw


def check_profile(path):
    """Return a redacted local-only report without opening data or network links."""
    report = dict(schema_version=1, ready=False, network_contacted=False,
                  archive_opened=False, hardware_qualified=False, settings=None, checks=[])
    try:
        profile = load_profile(path)
        config, explicit = _inspect(profile)
    except Exception:
        report['checks'].append(dict(name='launch_configuration', passed=False,
            guidance='Check private profile/hub/source and optional configuration schemas, paths, provenance and local binary pins.'))
        return report
    if profile['mode'] == 'live':
        from .live_check import check_live
        report = check_live(config, profile['source_config'], profile['nt_host'], profile['nt_port'],
                            profile['wpilib_install'], idle_delay_explicit=explicit)
    else:
        report['ready'] = True
    report['checks'].append(dict(name='launch_configuration', passed=True,
                                guidance='Explicit local paths and selected integration settings are valid.'))
    if 'marker_config' in profile:
        try:
            from .marker_service import load_marker_config
            from tools.status_bridge.run import prepare as prepare_status, DEFAULT_INSTALL
            marker = load_marker_config(profile['marker_config'])
            install = Path(marker.install) if marker.install is not None else DEFAULT_INSTALL
            prepare_status(install)
            marker_ready = True
        except Exception:
            marker_ready = False
        report['checks'].append(dict(name='marker_native_tools', passed=marker_ready,
            guidance='Marker delivery requires locally verified pinned status and marker native tools.'))
        report['ready'] = report['ready'] and marker_ready
    if config.analytics_enabled:
        try:
            valid = importlib.metadata.version('duckdb') == '1.5.6'
        except importlib.metadata.PackageNotFoundError:
            valid = False
        report['checks'].append(dict(name='analytics_dependency', passed=valid,
                                    guidance='Analytics requires the locally installed pinned duckdb 1.5.6 extra.'))
        report['ready'] = report['ready'] and valid
    return report


def runtime_path(path):
    return Path(path).resolve().with_suffix('.runtime.json')


def server_arguments(path, profile):
    config, _ = _inspect(profile)
    args = ['--resolve-config-paths', '--config', str(profile['hub_config']), '--data-dir', config.data_dir,
            '--launch-control', str(runtime_path(path))]
    if profile['mode'] == 'live':
        for field in ('source_config', 'nt_host', 'nt_port', 'wpilib_install'):
            args.extend(['--' + field.replace('_', '-'), str(profile[field])])
    for field in ('marker_config', 'video_config', 'video_media_config', 'rewind_config'):
        if field in profile:
            args.extend(['--' + field.replace('_', '-'), str(profile[field])])
    return args


def configure_profile(path, values, *, force=False):
    """Validate before publishing; a revision backup is retained on replacement."""
    path = Path(path).resolve()
    _private(path)
    # Resolve configuration CLI paths against the profile, just like saved references.
    # A temporary sibling permits full validation without replacing the old profile.
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(values, indent=2, allow_nan=False) + '\n').encode('utf-8')
    fd, name = tempfile.mkstemp(prefix='.profile-', suffix='.json', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        profile = load_profile(temporary)
        _inspect(profile)
        if path.exists():
            if not force:
                raise LaunchError('Profile already exists; use --force to retain a revision and replace it.')
            backup = path.with_name(path.name + '.previous')
            # Refuse to silently overwrite an existing revision.
            with backup.open('xb') as stream:
                stream.write(path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        else:
            # Exclusive creation avoids overwriting a concurrently created profile.
            with path.open('xb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        temporary.unlink(missing_ok=True)


def _runtime(path):
    try:
        value = _json(runtime_path(path))
        if (value.keys() != {'schema_version', 'port', 'pid', 'token', 'instance_id'}
                or type(value['schema_version']) is not int or value['schema_version'] not in (1, 2)
                or type(value['port']) is not int or not 1 <= value['port'] <= 65535
                or type(value['pid']) is not int or not 0 < value['pid'] < 2**32
                or not isinstance(value['token'], str) or not re.fullmatch('[a-f0-9]{64}', value['token'])
                or not isinstance(value['instance_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['instance_id'])):
            raise ValueError()
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise LaunchError('Invalid private runtime record; no stop request was sent.') from None


def _control(record, method):
    # http.client never uses ambient proxies or follows redirects. The only host
    # is this literal loopback address; runtime JSON cannot supply an origin.
    connection = http.client.HTTPConnection('127.0.0.1', record['port'], timeout=2)
    try:
        connection.request(method, '/api/v1/application' + ('/stop' if method == 'POST' else ''),
            body=b'', headers={'Authorization': 'Bearer ' + record['token'],
                              'X-Hub-Request': '1', 'Content-Length': '0'})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status not in (200, 202) or len(raw) > 4096:
            raise ValueError()
        return json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException):
        raise LaunchError('Local hub control failed; the runtime record may be stale. No process was killed.') from None
    finally:
        connection.close()


def stop_profile(path):
    # Stop remains usable if configuration was edited while the instance ran.
    path = Path(path).resolve()
    _private(path)
    record = _runtime(path)
    if record is None:
        return False
    status = _control(record, 'GET')
    if (not isinstance(status, dict) or status.get('instance_id') != record['instance_id']
            or type(status.get('pid')) is not int or status['pid'] != record['pid']):
        raise LaunchError('Runtime identity did not match; no stop request was sent.')
    _control(record, 'POST')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('configure', aliases=['create-profile'], help='Create an explicit private launch profile')
    create.add_argument('--profile', type=Path, required=True)
    create.add_argument('--mode', choices=('live', 'demo'), required=True)
    create.add_argument('--hub-config', required=True)
    for field in PATH_FIELDS[1:]:
        create.add_argument('--' + field.replace('_', '-'))
    create.add_argument('--nt-host')
    create.add_argument('--nt-port', type=int)
    create.add_argument('--force', action='store_true')
    for command in ('check', 'run', 'stop', 'open'):
        child = sub.add_parser(command)
        child.add_argument('--profile', type=Path, required=True)
        if command == 'check':
            child.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command in ('configure', 'create-profile'):
            values = {key: value for key, value in vars(args).items() if key in FIELDS and value is not None}
            values['schema_version'] = 1
            configure_profile(args.profile, values, force=args.force)
            print('Private launch profile saved. Run check before starting the hub.')
        elif args.command == 'check':
            report = check_profile(args.profile)
            if args.json:
                print(json.dumps(report, allow_nan=False))
            else:
                print('Local readiness: ' + ('READY' if report['ready'] else 'NEEDS ATTENTION'))
                for item in report['checks']:
                    print(('PASS ' if item['passed'] else 'FAIL ') + item['name'] + ': ' + item['guidance'])
                print('No network or archive opened. Hardware and live endpoint operation remain unqualified.')
            return 0 if report['ready'] else 2
        elif args.command == 'stop':
            print('Graceful stop requested; the running hub retains ownership until workers close.'
                  if stop_profile(args.profile) else 'Hub is not running (no runtime record).')
        elif args.command == 'open':
            profile = load_profile(args.profile)
            config, _ = _inspect(profile)
            if not webbrowser.open('http://127.0.0.1:' + str(config.port)):
                raise LaunchError('Browser could not open the local hub URL.')
        else:
            profile = load_profile(args.profile)
            if not check_profile(args.profile)['ready']:
                raise LaunchError('Local readiness failed. Run check for redacted guidance before starting.')
            arguments = server_arguments(args.profile, profile)
            from . import server
            return server.main(argv=arguments)
        return 0
    except LaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print('Launcher failed: check local file permissions, configuration schemas and private paths.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
