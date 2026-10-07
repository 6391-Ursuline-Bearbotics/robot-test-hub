"""Explicit opt-in composition of authoritative NT status and read-only SFTP."""
from dataclasses import replace
from pathlib import Path
import subprocess
from .sftp_source import SFTPConfig, SFTPSource
from .status_bridge import StatusBridge


class LiveSource(SFTPSource):
    def start(self):
        self.status_provider.start()
        return self

    def cancel(self):
        super().cancel()
        # The service uses cancellation to shut down, not to pause/resume.
        self.status_provider.close()

    def close(self):
        self.cancel()

    def connection_status(self):
        details = {'adapter':'systemcore_sftp', 'status_channel':self.status_provider.error_code or 'advancing',
                   'ssh_connected':self.session is not None,'hardware_qualified':False}
        diagnostics = getattr(self.status_provider, 'diagnostics', None)
        if diagnostics is not None:
            details['status_reader'] = diagnostics()
        return details


def validate_configuration(config,source_path,nt_host,nt_port,install=None,*,idle_delay_explicit=False,
                           settings=None):
    """Validate local settings without preparing native code or starting a link."""
    if not nt_host or nt_port is None:
        raise ValueError('Live transfer requires explicit --nt-host and --nt-port')
    if settings is None:
        settings=SFTPConfig.load(source_path)
    elif not isinstance(settings,SFTPConfig):
        raise TypeError('Live settings must be an SFTPConfig')
    import paramiko
    if paramiko.__version__!='4.0.0':
        raise ValueError('Install the pinned sftp extra: pip install -e ".[sftp]"')
    if config.chunk_size>settings.max_read:
        raise ValueError('Hub chunk_size exceeds source max_read; choose matching bounded settings')
    if config.freshness>settings.freshness:
        config=replace(config,freshness=settings.freshness)
    if not idle_delay_explicit:
        config=replace(config,idle_delay=10)
    bridge=StatusBridge(nt_host,nt_port,settings.robot_id,install=install)
    return config,LiveSource(settings,bridge)


def configure(config,source_path,nt_host,nt_port,install=None,*,idle_delay_explicit=False):
    """Prepare the required native reader before the hub opens an archive or link."""
    config,source=validate_configuration(config,source_path,nt_host,nt_port,install,
        idle_delay_explicit=idle_delay_explicit)
    try:
        from tools.status_bridge.run import prepare,DEFAULT_INSTALL
        command,env=prepare(Path(install) if install is not None else DEFAULT_INSTALL)
    except (OSError,RuntimeError,ImportError,subprocess.SubprocessError,ValueError):
        raise RuntimeError('Native status reader preparation failed; check the pinned Alpha7 installation') from None
    source.status_provider.command=command
    source.status_provider.env=env
    return config,source
