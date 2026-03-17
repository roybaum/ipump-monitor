# iPump Monitor

Flask-based monitor for iPump operational logs, packaged as a Windows executable and installer.

## What the app does

- Serves the web UI on http://localhost:8080/
- Opens your browser automatically on startup
- Polls receiver logs and exports CSV output
- Lets you configure receiver IP, polling interval, row count, output folder, and time display mode
- Stops when you click Exit Application or after browser disconnect timeout

## Quick install (Windows, no Python required)

1. Open the latest release page:

	https://github.com/roybaum/ipump-monitor/releases/latest

2. Download the installer asset:

	iPump-Monitor-Setup-v<version>.exe

3. Run the installer and complete setup.
4. Launch iPump Monitor from the Start Menu (or desktop shortcut if selected).
5. In the app UI:
	- Enter Receiver IP.
	- Click Save Settings.
	- Click Start Monitoring.

If you prefer not to install, download ipump_monitor.exe from the same release and run it directly.

## Local development

1. Create and activate a virtual environment.
2. Install dependencies.
3. Run the app.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python ipump_monitor.py
```

## Runtime data locations

On Windows, runtime config files are stored in:

%APPDATA%\iPump Monitor

- config.json: saved app settings
- pcmi-cat.xml: downloaded receiver catalog

CSV output is written to the configured Output Folder in the UI.

- Default output folder: %USERPROFILE%\Documents
- Output file name: ipump_log.csv

These runtime files are ignored by git.

## Build executable

Build the application executable with PyInstaller:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller ipump_monitor.spec --clean
```

Build output:

- dist\ipump_monitor.exe

## Build installer

Compile the Inno Setup installer script:

```powershell
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" ipump-monitor-installer.iss
```

Installer output:

- dist\iPump-Monitor-Setup-v<version>.exe

The installer version and output filename are controlled by AppVersion and OutputBaseFilename in ipump-monitor-installer.iss.

## Release flow

1. Rebuild the executable and installer.
2. Run a smoke test from dist\ipump_monitor.exe.
3. Push main and push a release tag.
4. Create the GitHub release for that tag.
5. Upload the dist artifacts:
	- dist\ipump_monitor.exe
	- dist\iPump-Monitor-Setup-v<version>.exe

## Git tracking notes

Generated build artifacts are intentionally ignored:

- build\
- dist\