"""Explicit opt-in composition of authoritative NT status and read-only SFTP."""
from dataclasses import replace
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


def configure(config,source_path,nt_host,nt_port,install=None,*,idle_delay_explicit=False):
    if not nt_host or nt_port is None:
        raise ValueError('Live transfer requires explicit --nt-host and --nt-port')
    settings=SFTPConfig.load(source_path)
    # Validate required native reader before opening the hub or any remote link.
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
