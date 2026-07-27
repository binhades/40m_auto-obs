#!/usr/bin/env python3
from nicegui import ui, app
import json
import shutil
from pathlib import Path
from datetime import datetime
import subprocess
import os
import signal
import sys

# --- CONFIGURATION ---
VERSION = "v0.1.0"
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
SCRIPT_DIR = USER_HOME / "observe"
DRIVER_SCRIPT = SCRIPT_DIR / "run_auto_obs.sh"

# Ensure directories exist
SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)

# --- STATE ---
active_process = None
log_element = None

# --- 1. VERIFICATION LOGIC ---
def verify_schedule(data):
    """
    Performs automatic verification on the uploaded JSON.
    Returns: (is_valid: bool, message: str)
    """
    try:
        # Structure Check
        if "version" not in data: return False, "Missing 'version' field."
        if "schedule" not in data or not isinstance(data["schedule"], list):
            return False, "Missing or invalid 'schedule' list."
        
        if not data["schedule"]: return False, "Schedule is empty."

        # Logic Check
        prev_end_time = None
        
        for idx, task in enumerate(data["schedule"]):
            # Required Fields
            required = ["project_id", "source", "start_time_cst", "duration", "mode", "antennas"]
            for field in required:
                if field not in task or not task[field]:
                    return False, f"Task {idx+1}: Missing '{field}'."

            # Timing Check
            try:
                start_dt = datetime.strptime(task["start_time_cst"], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return False, f"Task {idx+1}: Invalid date format (use YYYY-MM-DDTHH:MM:SS)."

            # Duration Check
            dur_str = task["duration"]
            if dur_str.endswith('s'): dur_sec = int(dur_str[:-1])
            elif dur_str.endswith('m'): dur_sec = int(dur_str[:-1]) * 60
            elif dur_str.endswith('h'): dur_sec = int(dur_str[:-1]) * 3600
            else: return False, f"Task {idx+1}: Invalid duration format."

            # Gap Check
            current_end = start_dt.timestamp() + dur_sec
            if prev_end_time and start_dt.timestamp() < prev_end_time:
                 return False, f"Task {idx+1}: Overlaps with previous task!"
            
            # 10s Lead Time Check (Simple check against "now")
            if (start_dt.timestamp() - datetime.now().timestamp()) < 10:
                return False, f"Task {idx+1}: Start time is too close (<10s) or in the past."

            prev_end_time = current_end

            # Backend Check
            if not (task.get("spec_enabled") or task.get("psr_enabled") or task.get("baseband_enabled")):
                return False, f"Task {idx+1}: No backend selected."

        return True, "Valid"

    except Exception as e:
        return False, f"JSON Parsing Error: {str(e)}"

# --- 2. FILE HANDLING ---
def handle_upload(e):
    try:
        content = e.content.read().decode('utf-8')
        data = json.loads(content)
        
        # Step A: Verify
        valid, msg = verify_schedule(data)
        if not valid:
            ui.notify(f"Verification Failed: {msg}", type='negative', close_button=True)
            return
        
        # Step B: Standardize Name
        # Format: YYYYMMDD_HHMM_ProjectID.json (Based on FIRST task)
        first_task = data["schedule"][0]
        start_str = first_task["start_time_cst"] # 2026-01-29T08:00:00
        dt_obj = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S")
        timestamp = dt_obj.strftime("%Y%m%d_%H%M")
        proj = first_task["project_id"]
        
        filename = f"{timestamp}_{proj}.json"
        save_path = SCHEDULE_DIR / filename
        
        # Step C: Save
        with open(save_path, 'w') as f:
            json.dump(data, f, indent=4)
            
        ui.notify(f"Schedule verified & saved: {filename}", type='positive')
        refresh_file_list()
        
    except Exception as err:
        ui.notify(f"Upload Error: {str(err)}", type='negative')

def get_file_list():
    files = sorted(SCHEDULE_DIR.glob("*.json"), reverse=True)
    rows = []
    for f in files:
        try:
            with open(f, 'r') as fp:
                data = json.load(fp)
                count = len(data.get("schedule", []))
                first = data["schedule"][0] if count > 0 else {}
                source = first.get("source", "N/A")
                
            rows.append({
                "filename": f.name,
                "project": first.get("project_id", "N/A"),
                "start": first.get("start_time_cst", "N/A"),
                "tasks": count,
                "path": str(f)
            })
        except:
            continue
    return rows

# --- 3. EXECUTION LOGIC ---
async def run_schedule():
    global active_process
    
    selected = [r for r in file_table.selected]
    if not selected:
        ui.notify("Please select a schedule to run.", type='warning')
        return

    if active_process is not None:
        ui.notify("A schedule is already running!", type='negative')
        return

    filepath = selected[0]['path']
    filename = selected[0]['filename']
    
    log_element.push(f"--- STARTING SCHEDULE: {filename} ---")
    log_element.push(f"Command: {DRIVER_SCRIPT} {filepath}")
    
    # Run Driver Script
    # process_group=True allows us to send signal to the whole group (Driver + SSH)
    active_process = subprocess.Popen(
        [str(DRIVER_SCRIPT), str(filepath)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid 
    )
    
    # Update UI State
    btn_run.disable()
    btn_stop.enable()
    spinner.visible = True
    
    # Stream Output
    while True:
        line = active_process.stdout.readline()
        if not line and active_process.poll() is not None:
            break
        if line:
            log_element.push(line.strip())
            # Auto-scroll hack could go here
            await app.sleep(0.01) # Yield to UI loop

    # Cleanup State
    rc = active_process.poll()
    log_element.push(f"--- FINISHED (Exit Code: {rc}) ---")
    active_process = None
    btn_run.enable()
    btn_stop.disable()
    spinner.visible = False

def stop_schedule():
    global active_process
    if active_process:
        log_element.push("--- SENDING STOP SIGNAL (SIGINT) ---")
        # Send Ctrl+C (SIGINT) to the process group to trigger the Driver's 'trap'
        os.killpg(os.getpgid(active_process.pid), signal.SIGINT)
    else:
        ui.notify("No process running.", type='warning')

# --- UI LAYOUT ---
with ui.card().classes('w-full max-w-6xl mx-auto p-4'):
    # Header
    with ui.row().classes('w-full items-center justify-between'):
        ui.markdown(f"### 🔭 FAST Observation Controller `{VERSION}`")
        ui.label(f"Server: {USER_HOME}").classes('text-gray-400 text-xs')

    ui.separator()

    # Section 1: Upload
    ui.label("1. Upload New Schedule").classes('font-bold text-gray-600')
    with ui.row().classes('w-full items-start gap-4'):
        ui.upload(on_upload=handle_upload, label="Drop schedule.json here", auto_upload=True).props('accept=.json').classes('w-full')

    ui.separator().classes('my-4')

    # Section 2: Select & Run
    ui.label("2. Select Schedule to Run").classes('font-bold text-gray-600')
    
    columns = [
        {'name': 'filename', 'label': 'File Name', 'field': 'filename', 'align': 'left'},
        {'name': 'project', 'label': 'Project', 'field': 'project', 'align': 'left'},
        {'name': 'start', 'label': 'First Task Start', 'field': 'start', 'align': 'left'},
        {'name': 'tasks', 'label': 'Tasks', 'field': 'tasks', 'align': 'center'},
    ]
    
    file_table = ui.table(columns=columns, rows=get_file_list(), selection='single', row_key='filename').classes('w-full h-64')
    
    def refresh_file_list():
        file_table.rows = get_file_list()
        file_table.update()

    ui.button('Refresh List', on_click=refresh_file_list, icon='refresh').props('flat dense')

    # Control Bar
    with ui.row().classes('w-full items-center gap-4 mt-4 border-t pt-4 bg-gray-50 p-2 rounded'):
        btn_run = ui.button('RUN SCHEDULE', on_click=run_schedule, icon='play_arrow').classes('bg-green-600 text-white font-bold')
        btn_stop = ui.button('STOP / ABORT', on_click=stop_schedule, icon='stop').classes('bg-red-600 text-white font-bold').disable()
        spinner = ui.spinner(size='lg').classes('text-green-600')
        spinner.visible = False
        ui.label('Status:').classes('font-bold ml-4')
        ui.label().bind_text_from(spinner, 'visible', lambda v: 'RUNNING' if v else 'IDLE')

    # Section 3: Live Log
    ui.separator().classes('my-4')
    ui.label("3. Live Driver Log").classes('font-bold text-gray-600')
    
    log_container = ui.scroll_area().classes('w-full h-96 border bg-black rounded p-2')
    with log_container:
        log_element = ui.log(max_lines=1000).classes('text-green-400 font-mono text-sm')

ui.run(title='FAST Obs Controller', host='0.0.0.0', port=8080, reload=False)
