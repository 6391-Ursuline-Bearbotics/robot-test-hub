# Windows source setup and everyday launch

These scripts run the hub natively on a school Windows laptop from a full source checkout or extracted source release. They create a separate `.venv` in that installation and install the package with its pinned SFTP and analytics extras. Python and WPILib are prerequisites installed separately; this source package does not redistribute WPILib. Setup installs no Windows service, scheduled task, startup entry, robot deployment or hardware connection.

## Install once

1. Install Python 3.10 or newer. Enable the Python `py` launcher, or locate the installed `python.exe`. School IT may need to approve the installation. Install the team's supported WPILib Alpha 7 separately when preparing live NT collection; preserve the installed Java tools.
2. Extract the complete source tree to a writable local folder, outside the private archive/configuration folder. Avoid running directly inside a ZIP file. Keep the SSD archive outside this source tree and outside any actively synchronized folder; publish completed originals using the separate opt-in Drive workflow.
3. From PowerShell in the source root, run:

   ```powershell
   .\scripts\windows\SetupHub.cmd
   ```

   When the launcher is unavailable or selects an older Python, supply the explicit interpreter:

   ```powershell
   .\scripts\windows\SetupHub.cmd -PythonPath "C:\Program Files\Python313\python.exe"
   ```

Setup uses `py -3` discovery when no interpreter is supplied. It checks Python 3.10 or newer, creates `.venv`, and runs `.venv\Scripts\python.exe -m pip install ".[sftp,analytics]"` from the source root. Initial dependency installation requires access to the configured Python package index, or a school-approved local package cache. There is no automatic interpreter download. A later setup run reuses this installation's existing environment and reinstalls the local package; `-PythonPath` selects the interpreter only when creating the environment. It does not replace an existing environment. Failures return a nonzero exit code and display the failing Python exit code.

The `.cmd` files invoke Windows PowerShell with an execution-policy override for that process only. They do not alter the machine policy. A centrally enforced school policy can still block them; ask IT to approve the scripts. An already-approved PowerShell session can call `SetupHub.ps1` directly.

## Save one private live profile

The default profile is `%LOCALAPPDATA%\RobotTestHub\config\launch.json`. Setup does not create or overwrite it. First create the private hub/source settings using the [log commissioning guide](LOG_FLOW_COMMISSIONING.md), including the actual pinned SFTP host identity and credential references. Keep passwords out of commands and this repository.

Then save a profile once, substituting the laptop's real private paths and NT endpoint:

```powershell
$profile = Join-Path $env:LOCALAPPDATA 'RobotTestHub\config\launch.json'
.\.venv\Scripts\python.exe -m robot_test_hub.launcher configure `
  --profile "$profile" `
  --mode live `
  --hub-config "C:\RobotHubPrivate\hub.json" `
  --source-config "C:\RobotHubPrivate\source.json" `
  --nt-host "SYSTEMCORE_HOST" `
  --nt-port 5810 `
  --wpilib-install "C:\Users\Public\wpilib\2027_alpha7"
```

All six live configuration options shown after `--profile` are required: mode, hub config, source config, NT host, NT port and WPILib installation. `SYSTEMCORE_HOST` is a placeholder, not a detected endpoint; port 5810 is an example to confirm against the actual installation. `configure` saves local settings and does not connect to the robot. Existing profiles are preserved unless the launcher is explicitly asked to replace one; review its revision behavior before using `--force`. Profile creation does not qualify the endpoint or hardware.

For a different profile location, use an absolute path outside the checkout and pass the same `-Profile` to every wrapper. Relative paths resolve against the calling directory, so absolute paths are easier to reuse. The launcher also validates private path placement; do not route a private profile back into the checkout through a junction or symbolic link.

## Daily operation

From the source root:

```powershell
.\scripts\windows\CheckHub.cmd
.\scripts\windows\StartHub.cmd
```

`CheckHub` performs local readiness checks. It does not connect to the robot or open the archive. It may compile the pinned local Java bridge after verifying installed dependencies; readiness does not establish endpoint reachability or physical performance. `StartHub` runs the configured foreground hub in that console; it is the explicit action that enables configured live transport. Leave the console open during collection. Use another console or Explorer for:

```powershell
.\scripts\windows\OpenHub.cmd
.\scripts\windows\StopHub.cmd
```

`OpenHub` opens the configured loopback browser address; it does not start the hub or prove it is running. `StopHub` asks the matching local instance to stop gracefully; it does not kill processes. Wait for the running console to finish its worker cleanup before upgrading or moving the archive. A missing runtime record reports that the hub is not running; stale or mismatched identity reports an error.

Every wrapper accepts a private profile, including paths containing spaces:

```powershell
.\scripts\windows\CheckHub.cmd -Profile "D:\Private Robot Hub\config\launch.json"
.\scripts\windows\StartHub.cmd -Profile "D:\Private Robot Hub\config\launch.json"
.\scripts\windows\StopHub.cmd -Profile "D:\Private Robot Hub\config\launch.json"
```

The wrappers pass paths as arguments without evaluating their contents as PowerShell code. Python/launcher exit codes propagate to the caller, including a failed readiness check. An Explorer double-click uses the default profile; to retain diagnostic output, run the wrapper from a terminal. Every action anchors its Python working directory to the complete source root, including when invoked from Explorer or another directory. This keeps `tools/status_bridge` and the source release root available for live preparation; retain the full extracted tree. Installing a wheel alone is not a supported live setup. No long live command is needed after configuration.

## Runtime record and interrupted shutdown

The launcher creates a private launch.runtime.json beside the profile only after the server binds and its workers start. It contains a random local control credential; do not share it, sync it to Drive, or put it in Git. Keep its parent directory private to the laptop account on a local filesystem supporting hard links (normally NTFS). Ordinary server launches without launcher control expose no application stop endpoint.

Stop verifies both the recorded instance identity and PID before requesting shutdown. The server removes only its own unchanged record after all service workers close. It retains archive ownership and the record while adapter cleanup remains outstanding. An unreachable port is not proof that the old hub has exited, so the launcher does not automatically delete a stale record or kill its recorded PID.

After a crash or forced shutdown, the record may remain and a new start will refuse to overwrite it. First establish that the prior hub process has exited; rebooting the laptop is an unambiguous way to establish this. Preserve/rename that runtime file privately before restarting. Do not remove the archive's .owner.lock file. This conservative manual crash-recovery step and autostart are remaining packaging work.

## Upgrade and rollback

Stop the current hub and wait for shutdown before replacing source or running setup again. Retain the previous source release and private configuration, and take a verified backup using [BACKUP.md](BACKUP.md). Extract the next release into a separate folder and run its setup to create its own environment. Check it against the same private profile before starting. Never run both installations against the same archive. Rollback requires compatible catalog/configuration formats or the documented clean-directory backup restore; changing source folders alone does not undo a database migration. No automatic upgrade, database rollback or autostart is supplied by these scripts.

## Validation boundaries

`tests/test_windows_setup.py` runs real Windows PowerShell and a real `cmd.exe` wrapper with paths containing spaces, ampersands and literal PowerShell syntax. A temporary Python startup hook records intended venv/pip/launcher arguments, creates pip-free temporary environments, and intercepts dependency installation and launcher execution. Tests cover per-install placement, exact extras, reuse/profile preservation, the default private profile, all four actions from outside the checkout with source-root/tooling-path preservation, checkout rejection, old-Python rejection, visible setup failure and wrapper exit propagation. They neither install dependencies nor contact hardware. They skip on non-Windows hosts. Real school IT policy, package-index installation, laptop resource contention and physical robot/storage commissioning remain separate checks.
