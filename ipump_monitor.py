import csv
import ipaddress
import json
import os
import sys
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

app = Flask(__name__)

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", BASE_DIR), "iPump Monitor")
os.makedirs(APP_DATA_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(APP_DATA_DIR, "config.json")
CATALOG_PATH = os.path.join(BASE_DIR, "pcmi-cat.xml")

DEFAULT_OUTPUT_FOLDER = os.path.join(os.path.expanduser("~"), "Documents")

config = {
    "receiver_ip": "0.0.0.0",
    "log_rows": 500,
    "poll_interval": 60,
    "output_folder": DEFAULT_OUTPUT_FOLDER,
    "time_display": "local"
}

catalog = {
    "groups": {},
    "members": {},
    "enums": {}
}

state = {
    "running": False,
    "last_update": None,
    "last_error": "",
    "rows": []
}

client_sessions = {}
client_sessions_lock = threading.Lock()
app_lifecycle = {
    "browser_connected_once": False,
    "shutdown_requested": False
}

CLIENT_HEARTBEAT_TIMEOUT = 15
CLIENT_WATCH_INTERVAL = 5
PROCESS_SHUTDOWN_DELAY = 0.75
APP_URL = "http://localhost:8080/"

###########################################################
# CONFIG
###########################################################

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config.update(json.load(f))

    config["receiver_ip"] = normalize_receiver_ip(config.get("receiver_ip", ""))


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def normalize_receiver_ip(value):

    ip_text = str(value or "").strip()

    if not ip_text:
        return ""

    parsed = urlsplit(ip_text)

    if parsed.hostname:
        return parsed.hostname.strip()

    ip_text = ip_text.split("/", 1)[0]
    ip_text = ip_text.split("\\", 1)[0]
    ip_text = ip_text.split("?", 1)[0]
    ip_text = ip_text.split("#", 1)[0]

    if ip_text.startswith("[") and "]" in ip_text:
        return ip_text[1:ip_text.index("]")].strip()

    if ip_text.count(":") == 1:
        host_text, port_text = ip_text.rsplit(":", 1)
        if port_text.isdigit():
            return host_text.strip()

    return ip_text.strip()


def has_valid_receiver_ip():

    ip_text = normalize_receiver_ip(config.get("receiver_ip", ""))

    if not ip_text or ip_text == "0.0.0.0":
        return False

    try:
        ipaddress.ip_address(ip_text)
    except ValueError:
        return False

    return True


def extract_client_id_from_request():

    json_payload = request.get_json(silent=True)

    if isinstance(json_payload, dict):
        client_id = str(json_payload.get("client_id", "")).strip()
        if client_id:
            return client_id

    client_id = request.form.get("client_id", "").strip()

    if client_id:
        return client_id

    raw_payload = request.get_data(as_text=True).strip()

    if not raw_payload:
        return ""

    try:
        parsed_payload = json.loads(raw_payload)
    except ValueError:
        return raw_payload

    return str(parsed_payload.get("client_id", "")).strip()


def mark_client_active(client_id):

    if not client_id:
        return

    with client_sessions_lock:
        client_sessions[client_id] = time.time()
        app_lifecycle["browser_connected_once"] = True


def prune_inactive_clients():

    cutoff = time.time() - CLIENT_HEARTBEAT_TIMEOUT

    with client_sessions_lock:
        stale_clients = [
            client_id
            for client_id, last_seen in client_sessions.items()
            if last_seen < cutoff
        ]

        for client_id in stale_clients:
            client_sessions.pop(client_id, None)

        return bool(client_sessions)


def schedule_application_shutdown():

    with client_sessions_lock:
        if app_lifecycle["shutdown_requested"]:
            return

        app_lifecycle["shutdown_requested"] = True

    state["running"] = False

    def delayed_shutdown():
        time.sleep(PROCESS_SHUTDOWN_DELAY)
        os._exit(0)

    threading.Thread(target=delayed_shutdown, daemon=True).start()


def browser_watchdog_loop():

    while True:
        time.sleep(CLIENT_WATCH_INTERVAL)

        with client_sessions_lock:
            if app_lifecycle["shutdown_requested"]:
                return

            browser_connected_once = app_lifecycle["browser_connected_once"]

        has_active_clients = prune_inactive_clients()

        if browser_connected_once and not has_active_clients:
            schedule_application_shutdown()
            return


def launch_browser_on_startup():

    time.sleep(1.0)

    try:
        webbrowser.open_new_tab(APP_URL)
    except Exception:
        pass


###########################################################
# CATALOG LOADER
###########################################################

def download_catalog():

    ip = config["receiver_ip"]
    url = f"http://{ip}/fw/resources/pcmi-data/pcmi-cat.xml"

    r = requests.get(url, timeout=10)
    r.raise_for_status()

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        f.write(r.text)


def load_catalog():

    if not os.path.exists(CATALOG_PATH):
        download_catalog()

    tree = ET.parse(CATALOG_PATH)
    root = tree.getroot()

    catalog["groups"].clear()
    catalog["members"].clear()
    catalog["enums"].clear()

    for group in root.findall(".//group"):

        group_tag = group.get("tag")
        group_name = group.findtext("name")

        if group_tag is None or not group_name:
            continue

        gtag = int(group_tag)
        gname = group_name.strip()

        catalog["groups"][gtag] = gname

        for member in group.findall("./member"):

            member_tag = member.get("tag")
            member_name = member.findtext("name")

            if member_tag is None or not member_name:
                continue

            mtag = int(member_tag)
            mname = member_name.strip()

            catalog["members"][(gtag, mtag)] = mname

            for enum in member.findall(".//enumerant"):

                enum_tag = enum.get("tag")
                enum_name = enum.get("name")

                if enum_tag is None or enum_name is None:
                    continue

                catalog["enums"][(gtag, mtag, int(enum_tag))] = enum_name


###########################################################
# DECODE HELPERS
###########################################################

def format_event_time(timestamp):

    if not timestamp:
        return ""

    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return str(timestamp).replace("T", " ", 1)

    if config.get("time_display", "local") == "local" and dt.tzinfo is not None:
        dt = dt.astimezone()

    time_text = dt.strftime("%Y-%m-%d %H:%M:%S")

    if dt.microsecond:
        return f"{time_text}.{dt.microsecond // 10000:02d}"

    return time_text


def decode_enum_value(group, member, value):

    enum_text = catalog["enums"].get((group, member, value))

    if enum_text is not None:
        return enum_text

    try:
        numeric_value = int(value)
    except (TypeError, ValueError):
        return value

    return catalog["enums"].get((group, member, numeric_value), value)


def decode_params(group, params):
    """
    Each param entry is [group_tag, member_tag, value].
    Look up the param name and resolve any enum values.
    Returns a string like "Current Relay State: On | Frequency: 94500"
    """
    parts = []

    for p in params:

        if len(p) < 3:
            continue

        param_group = p[0]
        param_member = p[1]
        param_value = p[2]

        param_name = catalog["members"].get((param_group, param_member), f"param {param_member}")
        param_text = decode_enum_value(param_group, param_member, param_value)

        parts.append(f"{param_name}: {param_text}")

    return " | ".join(parts)


def decode_index(group, index):

    try:
        index = int(index)
    except:
        return index

    # Relay group decoding (group 8)
    # The receiver reports a 1-based relay number offset by 128.
    # Example: raw 163 -> relay #35 -> Port 3, Relay 3.
    if group == 8:

        relay_number = index - 128

        if relay_number < 1:
            return index

        port = ((relay_number - 1) // 16) + 1
        relay = ((relay_number - 1) % 16) + 1

        return f"Port {port}, Relay {relay} (#{relay_number})"

    # All other groups: index is a 1-based port number
    return f"Port {index}"


###########################################################
# LOG FETCH
###########################################################

def fetch_logs():

    ip = config["receiver_ip"]
    url = f"http://{ip}/jsonrpc"

    payload = {
        "version": "1.1",
        "method": "custlog.list",
        "params": [2, 0, config["log_rows"]],
        "id": 1
    }

    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()

    response = r.json()

    if "result" not in response:
        raise Exception(f"Invalid JSON-RPC response: {response}")

    data = response["result"]

    rows = []

    for e in data:

        event_id = e.get("id", "")
        group = e.get("group")
        member = e.get("member")

        index_val = ""
        value_val = ""

        if e.get("indexes"):
            raw_index = e["indexes"][0][1]
            index_val = decode_index(group, raw_index)

        if e.get("params"):
            value_val = decode_params(group, e["params"])
        else:
            value_val = ""

        group_name = catalog["groups"].get(group, f"group {group}")
        member_name = catalog["members"].get((group, member), f"member {member}")

        rows.append({
            "event": event_id,
            "time": format_event_time(e.get("time")),
            "event_group": group_name,
            "index": index_val,
            "parameters": value_val,
            "event_name": member_name
        })

    return rows


###########################################################
# CSV EXPORT
###########################################################

def write_csv(rows):

    path = os.path.join(config["output_folder"], "ipump_log.csv")

    with open(path, "w", newline="", encoding="utf-8") as f:

        writer = csv.writer(f)

        writer.writerow(["Event", "Time", "Event Group", "Index", "Parameters"])

        for r in rows:
            writer.writerow([
                r["event"],
                r["time"],
                r["event_group"],
                r["index"],
                r["parameters"]
            ])


###########################################################
# MONITOR LOOP
###########################################################

def monitor_loop():

    while state["running"]:

        try:

            rows = fetch_logs()

            write_csv(rows)

            state["rows"] = rows
            state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["last_error"] = ""

        except Exception as e:

            state["last_error"] = str(e)

        time.sleep(config["poll_interval"])


def start_monitoring():

    if state["running"]:
        return

    if not has_valid_receiver_ip():
        state["last_error"] = "Enter a valid Receiver IP before starting monitoring."
        return

    try:
        load_catalog()
    except Exception as e:
        state["last_error"] = f"Failed to load receiver catalog: {e}"
        return

    state["running"] = True
    state["last_error"] = ""

    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()


###########################################################
# WEB UI
###########################################################

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <title>Operational Log - iPump Monitor</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #f5f6f8;
            color: #333;
            line-height: 1.6;
        }

        .container {
            display: flex;
            min-height: 100vh;
            gap: 20px;
            padding: 20px;
            max-width: 1600px;
            margin: 0 auto;
        }

        .sidebar {
            flex: 0 0 320px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            padding: 20px;
            position: sticky;
            top: 20px;
            align-self: flex-start;
            max-height: calc(100vh - 40px);
            overflow-y: auto;
        }

        .main {
            flex: 1;
            min-width: 0;
        }

        h1 {
            font-size: 28px;
            margin-bottom: 30px;
            color: #1a1a1a;
        }

        h2 {
            font-size: 18px;
            margin: 20px 0 15px;
            color: #1a1a1a;
            border-bottom: 2px solid #e0e0e0;
            padding-bottom: 8px;
        }

        details {
            width: 100%;
        }

        summary {
            list-style: none;
        }

        summary::-webkit-details-marker {
            display: none;
        }

        .accordion-summary {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 18px;
            font-weight: 600;
            color: #1a1a1a;
            border-bottom: 2px solid #e0e0e0;
            padding: 0 0 8px;
            cursor: pointer;
            user-select: none;
        }

        .accordion-summary::after {
            content: '+';
            font-size: 22px;
            line-height: 1;
            color: #0066cc;
        }

        details[open] .accordion-summary::after {
            content: '−';
        }

        .accordion-content {
            padding-top: 15px;
        }

        .config-section {
            margin-bottom: 25px;
        }

        .form-group {
            margin-bottom: 14px;
        }

        label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: #555;
            margin-bottom: 5px;
        }

        input[type="text"],
        input[type="number"],
        input[type="date"],
        select {
            width: 100%;
            padding: 8px 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 13px;
            background: white;
            transition: border-color 0.2s;
        }

        input[type="text"]:focus,
        input[type="number"]:focus,
        input[type="date"]:focus,
        select:focus {
            outline: none;
            border-color: #0066cc;
            box-shadow: 0 0 0 3px rgba(0, 102, 204, 0.1);
        }

        button {
            width: 100%;
            padding: 10px 16px;
            border: none;
            border-radius: 4px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            margin-bottom: 10px;
        }

        .btn-primary {
            background: #0066cc;
            color: white;
        }

        .btn-primary:hover {
            background: #0052a3;
            box-shadow: 0 2px 6px rgba(0, 102, 204, 0.3);
        }

        .btn-primary:active {
            background: #004a94;
            transform: translateY(1px);
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.18);
        }

        .btn-secondary {
            background: #6c757d;
            color: white;
        }

        .btn-secondary:hover {
            background: #5f676f;
            box-shadow: 0 2px 6px rgba(108, 117, 125, 0.3);
        }

        .btn-secondary:active {
            background: #545b62;
            transform: translateY(1px);
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.18);
        }

        .btn-success {
            background: #28a745;
            color: white;
        }

        .btn-success:hover {
            background: #218838;
            box-shadow: 0 2px 6px rgba(40, 167, 69, 0.3);
        }

        .btn-success:active {
            background: #1e7e34;
            transform: translateY(1px);
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.18);
        }

        .btn-danger {
            background: #dc3545;
            color: white;
        }

        .btn-danger:hover {
            background: #c82333;
            box-shadow: 0 2px 6px rgba(220, 53, 69, 0.3);
        }

        .btn-danger:active {
            background: #bd2130;
            transform: translateY(1px);
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.18);
        }

        .control-status {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 14px;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            background: #f8f9fa;
            margin-bottom: 12px;
        }

        .status-light {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            flex: 0 0 12px;
            box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.05);
        }

        .status-light-running {
            background: #28a745;
            box-shadow: 0 0 0 3px rgba(40, 167, 69, 0.18);
        }

        .status-light-stopped {
            background: #dc3545;
            box-shadow: 0 0 0 3px rgba(220, 53, 69, 0.18);
        }

        .control-status-text {
            font-size: 13px;
            font-weight: 600;
            color: #444;
        }

        .status-panel {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            padding: 20px;
            margin-bottom: 20px;
        }

        .status-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }

        .status-item:last-child {
            margin-bottom: 0;
        }

        .status-label {
            font-size: 13px;
            color: #666;
            font-weight: 600;
        }

        .status-value {
            font-size: 13px;
            color: #333;
        }

        .log-table-wrapper {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        thead {
            background: #f8f9fa;
            border-bottom: 2px solid #dee2e6;
        }

        th {
            padding: 14px;
            text-align: left;
            font-size: 13px;
            font-weight: 600;
            color: #333;
        }

        td {
            padding: 12px 14px;
            border-bottom: 1px solid #dee2e6;
            font-size: 13px;
        }

        tbody tr:hover {
            background: #f8f9fa;
        }

        tbody tr:nth-child(even) {
            background: #fafbfc;
        }

        tbody tr:nth-child(even):hover {
            background: #f0f1f3;
        }

        .log-table-wrapper th {
            position: sticky;
            top: 0;
            background: #f8f9fa;
            z-index: 10;
        }

        .error-message {
            color: #721c24;
            font-size: 13px;
            padding: 8px 0;
        }

        .timestamp {
            color: #666;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 12px;
        }

        .filter-info {
            background: #e7f3ff;
            border-left: 4px solid #0066cc;
            padding: 10px;
            border-radius: 4px;
            margin-top: 10px;
            font-size: 12px;
            color: #0052a3;
        }

        @media (max-width: 900px) {
            .container {
                flex-direction: column;
            }

            .sidebar {
                flex: 1;
                position: static;
            }
        }

        @media (max-width: 600px) {
            .container {
                padding: 10px;
                gap: 10px;
            }

            th, td {
                padding: 8px;
                font-size: 12px;
            }

            h1 {
                font-size: 22px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <aside class="sidebar" id="sidebar_panel">
            <h1 style="font-size: 20px; margin: 0 0 25px;">iPump Monitor</h1>

            <div class="config-section">
                <details id="configuration_panel">
                    <summary class="accordion-summary">Configuration</summary>
                    <div class="accordion-content">
                        <form method="post" action="/save" id="save_settings_form">
                            <div class="form-group">
                                <label for="receiver_ip">Receiver IP</label>
                                <input type="text" id="receiver_ip" name="receiver_ip" value="{{config.receiver_ip}}">
                            </div>
                            <div class="form-group">
                                <label for="log_rows">Log Rows</label>
                                <input type="number" id="log_rows" name="log_rows" value="{{config.log_rows}}">
                            </div>
                            <div class="form-group">
                                <label for="poll_interval">Polling Interval (sec)</label>
                                <input type="number" id="poll_interval" name="poll_interval" value="{{config.poll_interval}}">
                            </div>
                            <div class="form-group">
                                <label for="time_display">Time Zone Display</label>
                                <select id="time_display" name="time_display">
                                    <option value="local" {% if config.time_display == "local" %}selected{% endif %}>Local PC Time</option>
                                    <option value="receiver" {% if config.time_display == "receiver" %}selected{% endif %}>Receiver Time</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label for="output_folder">Output Folder</label>
                                <input type="text" id="output_folder" name="output_folder" value="{{config.output_folder}}">
                            </div>
                            <button type="submit" class="btn-primary">Save Settings</button>
                        </form>

                        <h2>Control</h2>
                        <div class="control-status">
                            <span class="status-light {% if state.running %}status-light-running{% else %}status-light-stopped{% endif %}"></span>
                            <span class="control-status-text">{% if state.running %}Monitoring is running{% else %}Monitoring is stopped{% endif %}</span>
                        </div>
                        <form method="post" action="/start">
                            <button type="submit" class="btn-success">Start Monitoring</button>
                        </form>
                        <form method="post" action="/stop">
                            <button type="submit" class="btn-danger">Stop Monitoring</button>
                        </form>
                        <button type="button" id="exit_application_button" class="btn-secondary">Exit Application</button>
                    </div>
                </details>
            </div>

            <div class="config-section">
                <h2>Filter</h2>
                <div class="form-group">
                    <label for="filter_port">Port (leave blank to show all)</label>
                    <input type="number" id="filter_port" min="1" max="4" placeholder="e.g., 1, 2, 3, 4">
                </div>
                <div class="form-group">
                    <label for="filter_relay">Relay (leave blank to show all)</label>
                    <input type="text" id="filter_relay" placeholder="e.g., 1,2 or 1-3">
                </div>
                <div class="form-group">
                    <label>Count range</label>
                    <div style="display: flex; gap: 10px;">
                        <div style="flex: 1; min-width: 0;">
                            <div style="font-size: 11px; font-weight: 600; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em;">Since</div>
                            <input type="date" id="count_since_date" style="width: 100%; margin-bottom: 6px;">
                            <div style="display: flex; gap: 3px; align-items: center;">
                                <select id="count_since_hour" style="flex: 1; min-width: 0; padding: 6px 2px; font-size: 12px;">
                                    <option value="00">00</option>
                                    <option value="01">01</option>
                                    <option value="02">02</option>
                                    <option value="03">03</option>
                                    <option value="04">04</option>
                                    <option value="05">05</option>
                                    <option value="06">06</option>
                                    <option value="07">07</option>
                                    <option value="08">08</option>
                                    <option value="09">09</option>
                                    <option value="10">10</option>
                                    <option value="11">11</option>
                                    <option value="12">12</option>
                                    <option value="13">13</option>
                                    <option value="14">14</option>
                                    <option value="15">15</option>
                                    <option value="16">16</option>
                                    <option value="17">17</option>
                                    <option value="18">18</option>
                                    <option value="19">19</option>
                                    <option value="20">20</option>
                                    <option value="21">21</option>
                                    <option value="22">22</option>
                                    <option value="23">23</option>
                                </select>
                                <span style="font-weight: 700; color: #555;">:</span>
                                <select id="count_since_minute" style="flex: 1; min-width: 0; padding: 6px 2px; font-size: 12px;">
                                    <option value="00">00</option>
                                    <option value="01">01</option>
                                    <option value="02">02</option>
                                    <option value="03">03</option>
                                    <option value="04">04</option>
                                    <option value="05">05</option>
                                    <option value="06">06</option>
                                    <option value="07">07</option>
                                    <option value="08">08</option>
                                    <option value="09">09</option>
                                    <option value="10">10</option>
                                    <option value="11">11</option>
                                    <option value="12">12</option>
                                    <option value="13">13</option>
                                    <option value="14">14</option>
                                    <option value="15">15</option>
                                    <option value="16">16</option>
                                    <option value="17">17</option>
                                    <option value="18">18</option>
                                    <option value="19">19</option>
                                    <option value="20">20</option>
                                    <option value="21">21</option>
                                    <option value="22">22</option>
                                    <option value="23">23</option>
                                    <option value="24">24</option>
                                    <option value="25">25</option>
                                    <option value="26">26</option>
                                    <option value="27">27</option>
                                    <option value="28">28</option>
                                    <option value="29">29</option>
                                    <option value="30">30</option>
                                    <option value="31">31</option>
                                    <option value="32">32</option>
                                    <option value="33">33</option>
                                    <option value="34">34</option>
                                    <option value="35">35</option>
                                    <option value="36">36</option>
                                    <option value="37">37</option>
                                    <option value="38">38</option>
                                    <option value="39">39</option>
                                    <option value="40">40</option>
                                    <option value="41">41</option>
                                    <option value="42">42</option>
                                    <option value="43">43</option>
                                    <option value="44">44</option>
                                    <option value="45">45</option>
                                    <option value="46">46</option>
                                    <option value="47">47</option>
                                    <option value="48">48</option>
                                    <option value="49">49</option>
                                    <option value="50">50</option>
                                    <option value="51">51</option>
                                    <option value="52">52</option>
                                    <option value="53">53</option>
                                    <option value="54">54</option>
                                    <option value="55">55</option>
                                    <option value="56">56</option>
                                    <option value="57">57</option>
                                    <option value="58">58</option>
                                    <option value="59">59</option>
                                </select>
                            </div>
                        </div>
                        <div style="flex: 1; min-width: 0;">
                            <div style="font-size: 11px; font-weight: 600; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em;">Until</div>
                            <input type="date" id="count_until_date" style="width: 100%; margin-bottom: 6px;">
                            <div style="display: flex; gap: 3px; align-items: center;">
                                <select id="count_until_hour" style="flex: 1; min-width: 0; padding: 6px 2px; font-size: 12px;">
                                    <option value="00">00</option>
                                    <option value="01">01</option>
                                    <option value="02">02</option>
                                    <option value="03">03</option>
                                    <option value="04">04</option>
                                    <option value="05">05</option>
                                    <option value="06">06</option>
                                    <option value="07">07</option>
                                    <option value="08">08</option>
                                    <option value="09">09</option>
                                    <option value="10">10</option>
                                    <option value="11">11</option>
                                    <option value="12">12</option>
                                    <option value="13">13</option>
                                    <option value="14">14</option>
                                    <option value="15">15</option>
                                    <option value="16">16</option>
                                    <option value="17">17</option>
                                    <option value="18">18</option>
                                    <option value="19">19</option>
                                    <option value="20">20</option>
                                    <option value="21">21</option>
                                    <option value="22">22</option>
                                    <option value="23">23</option>
                                </select>
                                <span style="font-weight: 700; color: #555;">:</span>
                                <select id="count_until_minute" style="flex: 1; min-width: 0; padding: 6px 2px; font-size: 12px;">
                                    <option value="00">00</option>
                                    <option value="01">01</option>
                                    <option value="02">02</option>
                                    <option value="03">03</option>
                                    <option value="04">04</option>
                                    <option value="05">05</option>
                                    <option value="06">06</option>
                                    <option value="07">07</option>
                                    <option value="08">08</option>
                                    <option value="09">09</option>
                                    <option value="10">10</option>
                                    <option value="11">11</option>
                                    <option value="12">12</option>
                                    <option value="13">13</option>
                                    <option value="14">14</option>
                                    <option value="15">15</option>
                                    <option value="16">16</option>
                                    <option value="17">17</option>
                                    <option value="18">18</option>
                                    <option value="19">19</option>
                                    <option value="20">20</option>
                                    <option value="21">21</option>
                                    <option value="22">22</option>
                                    <option value="23">23</option>
                                    <option value="24">24</option>
                                    <option value="25">25</option>
                                    <option value="26">26</option>
                                    <option value="27">27</option>
                                    <option value="28">28</option>
                                    <option value="29">29</option>
                                    <option value="30">30</option>
                                    <option value="31">31</option>
                                    <option value="32">32</option>
                                    <option value="33">33</option>
                                    <option value="34">34</option>
                                    <option value="35">35</option>
                                    <option value="36">36</option>
                                    <option value="37">37</option>
                                    <option value="38">38</option>
                                    <option value="39">39</option>
                                    <option value="40">40</option>
                                    <option value="41">41</option>
                                    <option value="42">42</option>
                                    <option value="43">43</option>
                                    <option value="44">44</option>
                                    <option value="45">45</option>
                                    <option value="46">46</option>
                                    <option value="47">47</option>
                                    <option value="48">48</option>
                                    <option value="49">49</option>
                                    <option value="50">50</option>
                                    <option value="51">51</option>
                                    <option value="52">52</option>
                                    <option value="53">53</option>
                                    <option value="54">54</option>
                                    <option value="55">55</option>
                                    <option value="56">56</option>
                                    <option value="57">57</option>
                                    <option value="58">58</option>
                                    <option value="59">59</option>
                                </select>
                            </div>
                        </div>
                    </div>
                </div>
                <button type="button" onclick="clearFilters()" class="btn-secondary">Clear Filters</button>
                <div class="filter-info" id="filter_info" style="display: none;">Showing <span id="match_count">0</span> of <span id="total_count">0</span> rows</div>
                <div class="filter-info" id="count_since_result" style="display: none;"><span id="since_count">0</span> matching event(s) in selected range</div>
            </div>

            <div class="config-section">
                <h2>Status</h2>
                <div class="status-item">
                    <span class="status-label">Last Update</span>
                    <span class="status-value timestamp">{{state.last_update or '—'}}</span>
                </div>
                {% if state.last_error %}
                <div class="status-item">
                    <span class="status-label">Last Error</span>
                </div>
                <div class="error-message">{{state.last_error}}</div>
                {% endif %}
            </div>
        </aside>

        <main class="main">
            <div class="log-table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 80px;">Event</th>
                            <th style="width: 165px;">Time</th>
                            <th style="width: 160px;">Event Group</th>
                            <th style="width: 200px;">Index</th>
                            <th>Parameters</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% if rows %}
                            {% for r in rows %}
                            <tr>
                                <td>{{r.event}}</td>
                                <td class="timestamp">{{r.time}}</td>
                                <td>{{r.event_group}}</td>
                                <td>{{r.index}}</td>
                                <td>{{r.parameters}}</td>
                            </tr>
                            {% endfor %}
                        {% else %}
                        <tr>
                            <td colspan="5" style="text-align: center; color: #999; padding: 40px;">No events yet</td>
                        </tr>
                        {% endif %}
                    </tbody>
                </table>
            </div>
        </main>
    </div>

    <script>
        function parseRelayFilter(filterText) {
            const text = filterText.trim();

            if (!text) {
                return { hasFilter: false, valid: true, values: new Set() };
            }

            const values = new Set();
            const parts = text.split(',');

            for (const rawPart of parts) {
                const part = rawPart.trim();
                if (!part) {
                    return { hasFilter: true, valid: false, values: new Set() };
                }

                const rangeMatch = part.match(/^(\d+)\s*-\s*(\d+)$/);
                if (rangeMatch) {
                    const start = parseInt(rangeMatch[1], 10);
                    const end = parseInt(rangeMatch[2], 10);
                    const low = Math.min(start, end);
                    const high = Math.max(start, end);

                    for (let i = low; i <= high; i++) {
                        values.add(i);
                    }
                    continue;
                }

                if (/^\d+$/.test(part)) {
                    values.add(parseInt(part, 10));
                    continue;
                }

                return { hasFilter: true, valid: false, values: new Set() };
            }

            return { hasFilter: true, valid: true, values };
        }

        function todayDateString() {
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            return `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
        }

        function ensureDateForTime(dateInputId, storageKey) {
            const dateInput = document.getElementById(dateInputId);
            if (!dateInput.value) {
                const today = todayDateString();
                dateInput.value = today;
                localStorage.setItem(storageKey, today);
            }
        }

        function filterTable() {
            const portFilter = document.getElementById('filter_port').value.trim();
            const relayFilter = document.getElementById('filter_relay').value.trim();
            const relaySelection = parseRelayFilter(relayFilter);
            const sinceDate = document.getElementById('count_since_date').value;
            const sinceHour = document.getElementById('count_since_hour').value;
            const sinceMinute = document.getElementById('count_since_minute').value;
            const sinceStr = sinceDate ? `${sinceDate} ${sinceHour}:${sinceMinute}:00` : '';
            const untilDate = document.getElementById('count_until_date').value;
            const untilHour = document.getElementById('count_until_hour').value;
            const untilMinute = document.getElementById('count_until_minute').value;
            const untilStr = untilDate ? `${untilDate} ${untilHour}:${untilMinute}:59` : '';
            const rows = document.querySelectorAll('tbody tr');
            let visibleCount = 0;
            let totalCount = 0;
            let sinceCount = 0;

            rows.forEach(row => {
                const indexCell = row.querySelector('td:nth-child(4)').textContent;
                const timeCell = row.querySelector('td:nth-child(2)').textContent.trim();
                let isVisible = true;

                if (portFilter || relayFilter) {
                    const portMatch = indexCell.match(/Port (\d+)/);
                    const relayMatch = indexCell.match(/Relay (\d+)/);

                    if (portFilter && (!portMatch || portMatch[1] !== portFilter)) {
                        isVisible = false;
                    }
                    if (relaySelection.hasFilter) {
                        if (!relaySelection.valid) {
                            isVisible = false;
                        } else {
                            const relayValue = relayMatch ? parseInt(relayMatch[1], 10) : NaN;
                            if (!relaySelection.values.has(relayValue)) {
                                isVisible = false;
                            }
                        }
                    }
                }

                if (isVisible && (sinceStr || untilStr)) {
                    const afterSince = !sinceStr || timeCell >= sinceStr;
                    const beforeUntil = !untilStr || timeCell <= untilStr;
                    if (!afterSince || !beforeUntil) {
                        isVisible = false;
                    } else {
                        sinceCount++;
                    }
                }

                row.style.display = isVisible ? '' : 'none';
                if (isVisible) visibleCount++;
                totalCount++;
            });

            const filterInfo = document.getElementById('filter_info');
            const matchCount = document.getElementById('match_count');
            const totalCount_el = document.getElementById('total_count');

            if (portFilter || relayFilter) {
                filterInfo.style.display = 'block';
                matchCount.textContent = visibleCount;
                totalCount_el.textContent = totalCount;
            } else {
                filterInfo.style.display = 'none';
            }

            const sinceResult = document.getElementById('count_since_result');
            if (sinceStr || untilStr) {
                sinceResult.style.display = 'block';
                document.getElementById('since_count').textContent = sinceCount;
            } else {
                sinceResult.style.display = 'none';
            }

            localStorage.setItem('filter_port', portFilter);
            localStorage.setItem('filter_relay', relayFilter);
            localStorage.setItem('count_since_date', sinceDate);
            localStorage.setItem('count_since_hour', sinceHour);
            localStorage.setItem('count_since_minute', sinceMinute);
            localStorage.setItem('count_until_date', untilDate);
            localStorage.setItem('count_until_hour', untilHour);
            localStorage.setItem('count_until_minute', untilMinute);
        }

        function clearFilters() {
            const pad = n => String(n).padStart(2, '0');
            const currentHourStr = pad(new Date().getHours());
            document.getElementById('filter_port').value = '';
            document.getElementById('filter_relay').value = '';
            document.getElementById('count_since_date').value = '';
            document.getElementById('count_since_hour').value = currentHourStr;
            document.getElementById('count_since_minute').value = '00';
            document.getElementById('count_until_date').value = '';
            document.getElementById('count_until_hour').value = currentHourStr;
            document.getElementById('count_until_minute').value = '59';
            localStorage.setItem('filter_port', '');
            localStorage.setItem('filter_relay', '');
            localStorage.setItem('count_since_date', '');
            localStorage.setItem('count_since_hour', currentHourStr);
            localStorage.setItem('count_since_minute', '00');
            localStorage.setItem('count_until_date', '');
            localStorage.setItem('count_until_hour', currentHourStr);
            localStorage.setItem('count_until_minute', '59');
            filterTable();
        }

        function restoreFilters() {
            const pad = n => String(n).padStart(2, '0');
            const now = new Date();
            const todayStr = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
            const currentHourStr = pad(now.getHours());

            const savedPort = localStorage.getItem('filter_port') || '';
            const savedRelay = localStorage.getItem('filter_relay') || '';
            const savedDate = localStorage.getItem('count_since_date');
            const savedHour = localStorage.getItem('count_since_hour');
            const savedMinute = localStorage.getItem('count_since_minute');
            const savedUntilDate = localStorage.getItem('count_until_date');
            const savedUntilHour = localStorage.getItem('count_until_hour');
            const savedUntilMinute = localStorage.getItem('count_until_minute');

            document.getElementById('filter_port').value = savedPort;
            document.getElementById('filter_relay').value = savedRelay;
            // Default to today only on first load; preserve explicit clears ('').
            document.getElementById('count_since_date').value = (savedDate === null) ? todayStr : savedDate;
            document.getElementById('count_since_hour').value = (savedHour === null) ? currentHourStr : savedHour;
            document.getElementById('count_since_minute').value = (savedMinute === null) ? '00' : savedMinute;
            document.getElementById('count_until_date').value = (savedUntilDate === null) ? todayStr : savedUntilDate;
            document.getElementById('count_until_hour').value = (savedUntilHour === null) ? currentHourStr : savedUntilHour;
            document.getElementById('count_until_minute').value = (savedUntilMinute === null) ? '59' : savedUntilMinute;
            filterTable();
        }

        function restoreAccordionState() {
            const configurationPanel = document.getElementById('configuration_panel');
            const savedState = localStorage.getItem('configuration_panel_open');

            if (configurationPanel && savedState === 'true') {
                configurationPanel.open = true;
            }
        }

        function storeSidebarScrollState() {
            const sidebar = document.getElementById('sidebar_panel');

            if (!sidebar) {
                return;
            }

            sessionStorage.setItem('ipump_monitor_sidebar_scroll_top', String(sidebar.scrollTop));
        }

        function restoreSidebarScrollState() {
            const sidebar = document.getElementById('sidebar_panel');
            const savedScrollTop = sessionStorage.getItem('ipump_monitor_sidebar_scroll_top');

            if (!sidebar || savedScrollTop === null) {
                return;
            }

            const scrollTop = parseInt(savedScrollTop, 10);

            if (Number.isNaN(scrollTop)) {
                return;
            }

            requestAnimationFrame(function () {
                sidebar.scrollTop = scrollTop;
            });
        }

        function closeConfigurationPanel() {
            const configurationPanel = document.getElementById('configuration_panel');

            if (!configurationPanel) {
                return;
            }

            configurationPanel.open = false;
            localStorage.setItem('configuration_panel_open', 'false');
        }

        document.getElementById('filter_port').addEventListener('input', filterTable);
        document.getElementById('filter_relay').addEventListener('input', filterTable);
        document.getElementById('count_since_date').addEventListener('change', filterTable);
        document.getElementById('count_until_date').addEventListener('change', filterTable);

        document.getElementById('count_since_hour').addEventListener('focus', function () {
            ensureDateForTime('count_since_date', 'count_since_date');
        });
        document.getElementById('count_since_minute').addEventListener('focus', function () {
            ensureDateForTime('count_since_date', 'count_since_date');
        });
        document.getElementById('count_until_hour').addEventListener('focus', function () {
            ensureDateForTime('count_until_date', 'count_until_date');
        });
        document.getElementById('count_until_minute').addEventListener('focus', function () {
            ensureDateForTime('count_until_date', 'count_until_date');
        });

        document.getElementById('count_since_hour').addEventListener('change', function () {
            ensureDateForTime('count_since_date', 'count_since_date');
            filterTable();
        });
        document.getElementById('count_since_minute').addEventListener('change', function () {
            ensureDateForTime('count_since_date', 'count_since_date');
            filterTable();
        });
        document.getElementById('count_until_hour').addEventListener('change', function () {
            ensureDateForTime('count_until_date', 'count_until_date');
            filterTable();
        });
        document.getElementById('count_until_minute').addEventListener('change', function () {
            ensureDateForTime('count_until_date', 'count_until_date');
            filterTable();
        });

        document.getElementById('configuration_panel').addEventListener('toggle', function () {
            localStorage.setItem('configuration_panel_open', this.open ? 'true' : 'false');
        });

        document.getElementById('sidebar_panel').addEventListener('scroll', storeSidebarScrollState, { passive: true });

        document.getElementById('save_settings_form').addEventListener('submit', function () {
            closeConfigurationPanel();
            storeSidebarScrollState();
        });

        window.addEventListener('load', restoreFilters);
        window.addEventListener('load', restoreAccordionState);
        window.addEventListener('load', restoreSidebarScrollState);
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', restoreFilters);
            document.addEventListener('DOMContentLoaded', restoreAccordionState);
            document.addEventListener('DOMContentLoaded', restoreSidebarScrollState);
        } else {
            restoreFilters();
            restoreAccordionState();
            restoreSidebarScrollState();
        }

        const CLIENT_ID_STORAGE_KEY = 'ipump_monitor_client_id';
        const CLIENT_HEARTBEAT_INTERVAL_MS = 5000;
        let _heartbeatTimer = null;
        let _shutdownInProgress = false;

        function getBrowserClientId() {
            let clientId = sessionStorage.getItem(CLIENT_ID_STORAGE_KEY);

            if (!clientId) {
                if (window.crypto && typeof window.crypto.randomUUID === 'function') {
                    clientId = window.crypto.randomUUID();
                } else {
                    clientId = `client-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                }

                sessionStorage.setItem(CLIENT_ID_STORAGE_KEY, clientId);
            }

            return clientId;
        }

        const browserClientId = getBrowserClientId();

        function postJson(url, payload) {
            return fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                keepalive: true
            }).catch(function () {});
        }

        function startClientHeartbeat() {
            postJson('/api/client-heartbeat', { client_id: browserClientId });

            if (_heartbeatTimer) clearInterval(_heartbeatTimer);

            _heartbeatTimer = setInterval(function () {
                postJson('/api/client-heartbeat', { client_id: browserClientId });
            }, CLIENT_HEARTBEAT_INTERVAL_MS);
        }

        function showShutdownMessage() {
            document.body.innerHTML = `
                <div style="max-width: 640px; margin: 60px auto; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; color: #333;">
                    <h1 style="font-size: 28px; margin-bottom: 12px;">iPump Monitor is closing</h1>
                    <p style="font-size: 16px; line-height: 1.5;">The application is shutting down. You can close this browser tab.</p>
                </div>
            `;
        }

        function exitApplication() {
            _shutdownInProgress = true;

            if (_refreshTimer) clearTimeout(_refreshTimer);
            if (_heartbeatTimer) clearInterval(_heartbeatTimer);

            showShutdownMessage();
            postJson('/shutdown', { client_id: browserClientId });
        }

        // Auto-refresh every 5s, but pause while any form field is focused
        let _refreshTimer = null;

        function startRefreshTimer() {
            if (_refreshTimer) clearTimeout(_refreshTimer);
            _refreshTimer = setTimeout(() => {
                storeSidebarScrollState();
                window.location.reload();
            }, 5000);
        }

        document.querySelectorAll('input, select').forEach(function(el) {
            el.addEventListener('focus', function() {
                if (_refreshTimer) clearTimeout(_refreshTimer);
            });
            el.addEventListener('blur', startRefreshTimer);
        });

        document.getElementById('exit_application_button').addEventListener('click', exitApplication);

        startClientHeartbeat();
        startRefreshTimer();
    </script>
</body>
</html>
"""


@app.route("/")
def index():

    return render_template_string(
        HTML,
        config=config,
        state=state,
        rows=state["rows"]
    )


@app.route("/save", methods=["POST"])
def save():

    config["receiver_ip"] = normalize_receiver_ip(request.form["receiver_ip"])
    config["log_rows"] = int(request.form["log_rows"])
    config["poll_interval"] = int(request.form["poll_interval"])
    config["time_display"] = request.form.get("time_display", "local")
    config["output_folder"] = request.form["output_folder"]

    save_config()

    return redirect(url_for("index"))


@app.route("/start", methods=["POST"])
def start():

    start_monitoring()

    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():

    state["running"] = False

    return redirect(url_for("index"))


@app.route("/shutdown", methods=["POST"])
def shutdown():

    schedule_application_shutdown()

    return ("", 204)


@app.route("/api/logs")
def api_logs():

    return jsonify(state["rows"])


@app.route("/api/client-heartbeat", methods=["POST"])
def api_client_heartbeat():

    mark_client_active(extract_client_id_from_request())

    return ("", 204)


###########################################################
# START
###########################################################

if __name__ == "__main__":

    load_config()
    threading.Thread(target=launch_browser_on_startup, daemon=True).start()
    threading.Thread(target=browser_watchdog_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=8080)