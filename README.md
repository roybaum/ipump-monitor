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

The app starts monitoring automatically on launch and serves the UI on port 8080.

## Runtime files

- `config.json` is created locally when settings are saved.
- `pcmi-cat.xml` is downloaded from the receiver if it does not exist.
- `ipump_log.csv` is generated during monitoring.

These runtime files are ignored by git.