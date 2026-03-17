# iPump Monitor

Flask-based monitor for iPump operational logs.

## Setup

1. Create a Python environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Run the app:

```powershell
python ipump_monitor.py
```

The app serves the UI on port 8080 and opens http://localhost:8080/ in your default browser when it starts. Enter a valid Receiver IP, save settings, and click Start to begin monitoring. Use Exit Application in the web UI to close the app, or close the browser page and the app will stop after a short timeout.

## Runtime files

- `config.json` is created locally when settings are saved.
- `pcmi-cat.xml` is downloaded from the receiver if it does not exist.
- `ipump_log.csv` is generated during monitoring.

These runtime files are ignored by git.