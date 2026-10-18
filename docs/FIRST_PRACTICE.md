# First practice with the hub

**Shortcut:** after step 2, double-click `scripts\windows\InstallHub.cmd`. It checks for Python and offers to install it with winget, runs setup and the self-test, creates `C:\RobotHubPrivate\hub.json` and an offline profile, and runs the readiness check (steps 3–5 below). It is safe to run again: existing settings are kept.

This gets a mentor from a clean Windows laptop to students reading "Today's practice". It covers importing logs from a USB drive or Google Drive; collecting straight from the robot is in [LOG_FLOW_COMMISSIONING.md](LOG_FLOW_COMMISSIONING.md). Plan on about 20 minutes the first time.

1. **Install Python 3.10 or newer** from python.org with the `py` launcher enabled. School IT may need to approve it.
2. **Get the hub** into a normal folder, for example `C:\RobotTestHub\app`: either `git clone https://github.com/6391-Ursuline-Bearbotics/robot-test-hub.git C:\RobotTestHub\app` or download and extract the ZIP from GitHub.
3. **Set it up**: in that folder run `scripts\windows\SetupHub.cmd`. It creates `.venv` and installs the hub.
4. **Self-test**: run `scripts\windows\TestHub.cmd`. It should finish with a passing summary. It uses only built-in sample data.
5. **Make a private data folder** outside the app folder, for example `C:\RobotHubPrivate`. Create `C:\RobotHubPrivate\hub.json` containing:

   ```json
   {"schema_version": 1, "data_dir": "C:\\RobotHubPrivate\\data"}
   ```

   Then save a launch profile with no robot connection:

   ```powershell
   .\.venv\Scripts\python.exe -m robot_test_hub.launcher configure --profile "$env:LOCALAPPDATA\RobotTestHub\config\launch.json" --mode offline --hub-config C:\RobotHubPrivate\hub.json
   ```

6. **Start the hub** with `scripts\windows\StartHub.cmd`, then open `http://127.0.0.1:6391` (or run `OpenHub.cmd`). The top line should say *Logs: no robot connection*.
7. **Add students**: on *Transfers (admin)*, add first names or nicknames under *Students*. Names stay on this laptop.
8. **Import logs**: on *Transfers (admin)* > *Import logs from a folder*, type the folder holding the `.wpilog` files (a USB drive such as `E:\`, or a Google Drive for desktop folder such as `G:\Shared drives\...`). Pick *2026 robot (roboRIO, last season)* or *2027 robot (SystemCore)* and choose **Import logs**. A 30–70 MB match log takes about 1 minute to import plus a minute of measurements.
9. **Read the results**: open *Today's practice*. Each run has a card with a status, plain sentences and *Details* > *Get the data* for AdvantageScope. Matches are grouped with their score estimate. *Trends*, *Practice time* and *Practice board* (for a TV) are in the top menu.
10. **Before the next practice**: pick the crew under *Who's on the controls?* before driving, and use **Mark event** when something happens. To collect logs over the network after practice instead of a USB stick, set up [Collect now](IMPORTER.md#collect-now-pull-after-practice-p02); for fully automatic collection use a `live` profile ([WINDOWS_SETUP.md](WINDOWS_SETUP.md)).

**Nothing is stuck on this laptop.** The original logs stay in Drive. The hub's data folder can be rebuilt on any laptop by repeating steps 3–8 and importing the same Drive folder. Only hand-entered things (notes, roster, crew choices, "not counted" marks) live only here; use [backups](BACKUP.md) for those. Do not put the data folder itself inside a Drive-synced folder.
