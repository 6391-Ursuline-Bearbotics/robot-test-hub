"""Setup wizard (P3.2) for "Collect now": find the robot, pin its host key, test, save.

Steps the mentor sees on /admin/setup:
1. Robot address: the team default (10.TE.AM.2) or a manual override.
2. Host key: the hub reads the robot's SSH host key WITHOUT logging in and shows its
   SHA-256 fingerprint. It is pinned only after the mentor confirms it.
3. Login: a username and the path of a private key file. No password is stored.
4. Read-only test: logs in with the pinned key and lists the robot's log folder.
5. Save: writes known_hosts + source.json next to hub.json and sets
   pull_source_config in hub.json (the previous hub.json is kept as hub.json.previous).
   The hub must be restarted to use it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat

# The 27_AK_Skeleton robot code writes REAL logs with WPILOGWriter("/U/logs").
DEFAULT_LOG_ROOT = '/U/logs'
HOST = re.compile(r'[A-Za-z0-9.-]{1,253}')
USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}')


class SetupError(ValueError):
    pass


def default_host(team=6391):
    return f'10.{team // 100}.{team % 100}.2'


def _check_host(host, port):
    if not isinstance(host, str) or not HOST.fullmatch(host) or host.startswith(('-', '.')):
        raise SetupError('Enter a robot address such as 10.63.91.2')
    if type(port) is not int or not 1 <= port <= 65535:
        raise SetupError('Port must be 1-65535')


def fingerprint(key_blob):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key_blob).digest()).decode().rstrip('=')


def fetch_host_key(host, port=22, *, timeout=3.0):
    """Read the server's host key without authenticating."""
    import paramiko
    _check_host(host, port)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise SetupError(f'Could not reach {host}:{port}. Is the robot on and is this laptop on its network?') from exc
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return {'host': host, 'port': port, 'key_type': key.get_name(), 'key_base64': key.get_base64(),
                'fingerprint': fingerprint(key.asbytes())}
    except Exception as exc:
        raise SetupError('Reached the robot, but it did not offer an SSH host key') from exc
    finally:
        transport.close()


def known_hosts_line(host, port, key_type, key_base64):
    name = host if port == 22 else f'[{host}]:{port}'
    return f'{name} {key_type} {key_base64}\n'


def _validate(values):
    host, port = values.get('host'), values.get('port', 22)
    _check_host(host, port)
    if not isinstance(values.get('username'), str) or not USER.fullmatch(values['username']):
        raise SetupError('Enter the robot login name (for example systemcore)')
    key = values.get('private_key')
    if not isinstance(key, str) or not Path(key).is_absolute() or not Path(key).is_file():
        raise SetupError('Private key must be the full path of an existing key file on this laptop')
    root = values.get('log_root') or DEFAULT_LOG_ROOT
    if not isinstance(root, str) or not root.startswith('/') or '..' in root.split('/') or root == '/':
        raise SetupError('Log folder must be a specific absolute folder on the robot, like /U/logs')
    for field in ('key_type', 'key_base64', 'fingerprint'):
        if not isinstance(values.get(field), str) or not values[field]:
            raise SetupError('Fetch and confirm the robot host key first')
    if values.get('confirmed') is not True:
        raise SetupError('Confirm that the fingerprint matches the robot before saving')
    blob = base64.b64decode(values['key_base64'].encode(), validate=True)
    if fingerprint(blob) != values['fingerprint']:
        raise SetupError('The host key changed since it was shown; fetch it again')
    return dict(host=host, port=port, username=values['username'], private_key=key, log_root=root,
                key_type=values['key_type'], key_base64=values['key_base64'])


def test_connection(values, folder):
    """Read-only: log in with the pinned key and count .wpilog files in the log folder."""
    from .pull import connect, NAME
    from .sftp_source import SFTPConfig
    clean = _validate(values)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    hosts = folder / 'known_hosts.test'
    hosts.write_text(known_hosts_line(clean['host'], clean['port'], clean['key_type'], clean['key_base64']))
    try:
        config = SFTPConfig(clean['host'], clean['username'], str(hosts), clean['private_key'], clean['log_root'],
                            'robot-6391', port=clean['port'])
        try:
            client, sftp = connect(config)
        except Exception as exc:
            raise SetupError('Login failed: check the username and that this key is authorized on the robot') from exc
        try:
            # Explicit setup action reads directory metadata only, never log bytes.
            # It cannot issue a bulk-transfer permission or call collect().
            entries = (sftp.listdir_iter(clean['log_root'], read_aheads=1)
                       if hasattr(sftp, 'listdir_iter') else iter(sftp.listdir_attr(clean['log_root'])))
            items = []
            for index, entry in enumerate(entries):
                if index >= 4096:
                    raise SetupError('Log folder is too large for this setup test')
                if NAME.fullmatch(entry.filename) and stat.S_ISREG(entry.st_mode or 0):
                    items.append((entry.filename, int(entry.st_size), int(entry.st_mtime or 0)))
            items.sort(key=lambda item: (item[2], item[0]))
        except OSError as exc:
            raise SetupError(f"Logged in, but could not read {clean['log_root']} on the robot") from exc
        finally:
            sftp.close()
            client.close()
        return {'logs': len(items), 'bytes': sum(i[1] for i in items), 'newest': items[-1][0] if items else None}
    finally:
        hosts.unlink(missing_ok=True)


def save(values, hub_config_path, robot_id='robot-6391'):
    clean = _validate(values)
    hub_config_path = Path(hub_config_path)
    folder = hub_config_path.parent
    hosts, source = folder / 'robot_known_hosts', folder / 'robot_source.json'
    hosts.write_text(known_hosts_line(clean['host'], clean['port'], clean['key_type'], clean['key_base64']))
    source.write_text(json.dumps({'schema_version': 1, 'host': clean['host'], 'port': clean['port'],
                                  'username': clean['username'], 'known_hosts': hosts.name,
                                  'private_key': clean['private_key'], 'log_root': clean['log_root'],
                                  'robot_id': robot_id}, indent=2) + '\n')
    hub = json.loads(hub_config_path.read_text(encoding='utf-8'))
    previous = hub_config_path.with_name(hub_config_path.name + '.previous')
    previous.write_text(json.dumps(hub, indent=2) + '\n')
    hub['pull_source_config'] = source.name
    temporary = hub_config_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(hub, indent=2) + '\n')
    os.replace(temporary, hub_config_path)
    from .config import Config
    Config.load(hub_config_path, resolve_paths=True)  # validates the result
    from .sftp_source import SFTPConfig
    SFTPConfig.load(source)
    return {'source_config': str(source), 'known_hosts': str(hosts), 'restart_required': True}
