# Optional Windows sign-in startup

The hub can start once when the configured Windows user signs in. This is an explicit opt-in step after [Windows source setup](WINDOWS_SETUP.md) and private profile creation. Complete the manual [log-flow commissioning trial](LOG_FLOW_COMMISSIONING.md) before enabling startup for regular practice. Ordinary setup installs no startup task. The startup controller defaults to an XML preview and never starts a task during registration.

## Preview, then opt in

From the complete source root, after `SetupHub.cmd` and `CheckHub.cmd` succeed:

```powershell
.\scripts\windows\StartupHub.cmd
```

This prints the proposed Task Scheduler XML using UTF-8. The default saved profile is `%LOCALAPPDATA%\RobotTestHub\config\launch.json`. To select another existing private profile:

```powershell
.\scripts\windows\StartupHub.cmd -Action Plan -Profile "D:\Private Robot Hub\config\launch.json"
.\scripts\windows\StartupHub.cmd -Action Install -Profile "D:\Private Robot Hub\config\launch.json"
```

`Plan` validates the full-source setup prerequisites and existing private profile path. It does not verify the profile schema or contact Task Scheduler. `Install` checks local launcher readiness before registering the task; it does not connect to hardware, open the archive, install dependencies or start the hub. The launcher validates again at actual startup. Neither command creates a profile or infers an endpoint or mode. A configured live profile explicitly permits its live transports when the task starts at the next sign-in. There is no fallback to demo mode.

Use local drive paths for source and private profile, outside one another. UNC paths, linked profile files/ancestors, embedded quotes/control characters and literal percent signs are rejected. Task Scheduler expands environment-variable syntax in action fields; rejecting percent signs avoids changing the intended literal path. Spaces, accented characters, ampersands and literal PowerShell syntax in valid paths are passed as native `-File` arguments without shell evaluation.

## Task behavior

There is one root-level startup slot for the current Windows user, named `RobotTestHub-Startup-<SID>`. It uses that user's interactive token and least privilege, without a stored password, service account, SYSTEM identity or elevation. School IT policy may prevent current-user task registration; a failed registration is visible in the terminal and does not authorize an elevated workaround.

The only trigger is the current user's sign-in, delayed 30 seconds. The action invokes the system Windows PowerShell executable with a hidden window and the absolute `SetupHub.ps1 -Action Run -Profile ...` entry point. The source working directory is explicit. Keep the complete extracted source tree and its `.venv` in place.

The task allows operation on battery, does not stop when changing to battery, has unlimited execution time, ignores overlapping trigger launches and disables Task Scheduler hard termination. It has no registration trigger, process monitor or restart-on-failure policy. An intentional `StopHub.cmd` therefore stays stopped for this sign-in. After a crash or configuration failure, recovery requires manual `StartHub.cmd` or the next sign-in. Existing launcher/archive ownership still rejects duplicate manual or scheduled instances. Sleep, logoff, shutdown, storage contention and school policy remain Windows/platform conditions requiring laptop qualification; these settings do not guarantee graceful cleanup during power loss.

## Inspect or stop

```powershell
.\scripts\windows\StartupHub.cmd -Action Status
.\scripts\windows\CheckHub.cmd
.\scripts\windows\OpenHub.cmd
.\scripts\windows\StopHub.cmd
```

`Status` reports whether the managed task exists, its scheduler state, last-run time and last task result, with fixed guidance to run `CheckHub.cmd`. A scheduler state such as `Ready` or `Running` does not prove hub readiness or hardware operation. Hidden startup does not show a console error, so inspect status and run the ordinary readiness check when the browser is unavailable. `CheckHub` and `OpenHub` must receive the same `-Profile` when using a nondefault profile.

`StopHub` requests graceful cleanup from the actual matching local hub. The controller sends no stop or kill commands; removal and replacement refuse a task whose state is `Running` or `Queued`. To prevent future sign-in launches:

```powershell
.\scripts\windows\StartupHub.cmd -Action Remove
```

`Remove` refuses `Running` or `Queued` tasks. Use `StopHub.cmd` separately and wait for the scheduled task to exit, then retry removal. Once the task is no longer active, removal deletes its managed registration. `Status` and `Remove` validate stored task ownership/provenance and remain available when the old source directory or profile has been moved or deleted. They do not require the current default profile to exist.

## Upgrade, change profile, or roll back

Stop the running hub and wait for cleanup. Retain the previous source release, private configuration and a verified backup, following [the source upgrade procedure](WINDOWS_SETUP.md#upgrade-and-rollback). Prepare the new complete source folder and preview its startup XML. Updating an existing managed registration requires explicit replacement:

```powershell
.\scripts\windows\StartupHub.cmd -Action Plan -Profile "D:\Private Robot Hub\config\launch.json"
.\scripts\windows\StartupHub.cmd -Action Install -Replace -Profile "D:\Private Robot Hub\config\launch.json"
```

Replacement refuses `Running` or `Queued` tasks: use `StopHub.cmd`, wait for the task to exit, then retry. Replacement changes the future sign-in action and does not start the new source. No two source folders should run against the same archive. To restore a previous startup action, run the previous release's controller with its compatible private profile and `Install -Replace`; database/catalog rollback still requires the separate compatible-format/backup-restore procedure.

The controller refuses a same-name unrelated or modified task. Replacement and removal require the managed source marker, current SID in registration/principal/trigger, expected action and source/profile provenance, and the supported safety policy. An unexpected task must be inspected manually; `-Replace` is not permission to overwrite unrelated content. No password is collected or stored.

## Validation and limits

Author and independent Windows tests intercept scheduler cmdlets, using temporary source/configuration fixtures and Python startup interception. They exercise preview policy, readiness gating, task collisions, explicit replacement, ownership checks, inactive-only removal/replacement, status diagnostics and native UTF-8/literal path forwarding. They perform no actual task registration/removal, hardware connection or robot deployment. Native in-memory Task Scheduler XML validation can verify schema interpretation without registering a task. Actual school-account permissions, sign-in launch, hidden-window behavior and end-of-practice cleanup require an explicitly approved laptop commissioning trial.

The task format follows Microsoft's [Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema), [AllowHardTerminate setting](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-allowhardterminate-settingstype-element), [Register-ScheduledTask interface](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask), and [ExecAction environment expansion](https://learn.microsoft.com/en-us/windows/win32/taskschd/execaction).
