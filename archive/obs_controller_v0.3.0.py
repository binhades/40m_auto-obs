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
import asyncio

# --- CONFIGURATION ---
VERSION = "v0.3.0"
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
SCRIPT_DIR = USER_HOME / "scripts"
DRIVER_SCRIPT = SCRIPT_DIR / "run_driver.sh"

# Ensure directories exist
SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)

# --- STATE ---
active_process = None
active_tail_processes = [] # List to track SSH tail processes
log_elements = {} # Map: "a01_spec" -> ui.log element

# --- 1. VERIFICATION & FILE HANDLING (Standard) ---
# ... (Copy verify_schedule, handle_upload, get_file_list from previous versions) ...
# ... (These functions haven't changed) ...

# --- 2. LOGGING LOGIC (NEW) ---
async def tail_remote_log(host, log_type, ui_log):
    """
    SSHs into 'host' and tails '~/log/active_{log_type}.log'.
    """
    remote_path = f"~/log/active_{log_type}.log"
    cmd = ["ssh", host, "tail", "-F", remote_path]
    
    try:
        # Start the SSH tail process
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid
        )
        active_tail_processes.append(proc)
        
        ui_log.push(f"--- CONNECTED TO {host}:{remote_path} ---")

        # Non-blocking read loop
        while active_process is not None:
            line = proc.stdout.readline()
            if line:
                ui_log.push(line.strip())
            else:
                await asyncio.sleep(0.1)
                
        # Cleanup when main process stops
        proc.terminate()
        
    except Exception as e:
        ui_log.push(f"[ERROR] Connection failed: {str(e)}")

# --- 3. EXECUTION LOGIC ---
async def run_schedule():
    global active_process
    
    selected = [r for r in file_table.selected]
    if not selected: return ui.notify("Select a schedule!", type='warning')
    if active_process: return ui.notify("Running!", type='negative')

    filepath = selected[0]['path']
    filename = selected[0]['filename']
    
    # 1. Clear Logs
    for key, log in log_elements.items():
        log.clear()
        if key == 'main': log.push(f"--- STARTING: {filename} ---")

    # 2. Start Driver (Main Process)
    active_process = subprocess.Popen(
        [str(DRIVER_SCRIPT), str(filepath)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid 
    )
    
    # UI State
    btn_run.disable(); btn_stop.enable(); spinner.visible = True
    
    # 3. Start Remote Log Watchers
    # Note: We delay slightly to let the workers create the files
    await asyncio.sleep(2) 
    
    # Worker a01
    asyncio.create_task(tail_remote_log("a01", "spec", log_elements['a01_spec']))
    asyncio.create_task(tail_remote_log("a01", "psr",  log_elements['a01_psr']))
    asyncio.create_task(tail_remote_log("a01", "bb",   log_elements['a01_bb']))
    
    # Worker a02
    asyncio.create_task(tail_remote_log("a02", "spec", log_elements['a02_spec']))
    asyncio.create_task(tail_remote_log("a02", "psr",  log_elements['a02_psr']))
    asyncio.create_task(tail_remote_log("a02", "bb",   log_elements['a02_bb']))

    # 4. Stream Driver Output
    while True:
        line = active_process.stdout.readline()
        if not line and active_process.poll() is not None: break
        if line:
            log_elements['main'].push(line.strip())
            await asyncio.sleep(0.01)

    # 5. Cleanup
    rc = active_process.poll()
    log_elements['main'].push(f"--- FINISHED (Exit: {rc}) ---")
    active_process = None
    
    # Kill tail processes
    for p in active_tail_processes:
        try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except: pass
    active_tail_processes.clear()
    
    btn_run.enable(); btn_stop.disable(); spinner.visible = False

def stop_schedule():
    global active_process
    if active_process:
        log_elements['main'].push("--- ABORTING ---")
        os.killpg(os.getpgid(active_process.pid), signal.SIGINT)

# --- UI LAYOUT ---
# ... (Header, Upload, Table sections same as v0.2.0) ...

    # --- NEW LOG LAYOUT ---
    ui.separator().classes('my-4')
    ui.label("3. Live Logs").classes('font-bold text-gray-600')
    
    # Top Level Tabs: Driver | a01 | a02
    with ui.tabs().classes('w-full') as main_tabs:
        mt_driver = ui.tab('Driver')
        mt_a01 = ui.tab('Worker a01')
        mt_a02 = ui.tab('Worker a02')

    with ui.tab_panels(main_tabs, value=mt_driver).classes('w-full h-96 border bg-gray-900 rounded'):
        
        # --- DRIVER PANEL ---
        with ui.tab_panel(mt_driver).classes('p-0 h-full'):
            log_elements['main'] = ui.log().classes('w-full h-full text-green-400 font-mono text-sm p-2 overflow-y-auto')

        # --- A01 PANEL (Nested Tabs) ---
        with ui.tab_panel(mt_a01).classes('p-0 h-full'):
            with ui.tabs().classes('w-full bg-gray-800 text-white dense') as a01_tabs:
                t1 = ui.tab('Spec'); t2 = ui.tab('PSR'); t3 = ui.tab('Baseband')
            with ui.tab_panels(a01_tabs, value=t1).classes('w-full h-full bg-black'):
                with ui.tab_panel(t1).classes('p-0'): log_elements['a01_spec'] = ui.log().classes('w-full h-full text-cyan-300 font-mono text-xs p-2')
                with ui.tab_panel(t2).classes('p-0'): log_elements['a01_psr'] = ui.log().classes('w-full h-full text-yellow-300 font-mono text-xs p-2')
                with ui.tab_panel(t3).classes('p-0'): log_elements['a01_bb'] = ui.log().classes('w-full h-full text-pink-300 font-mono text-xs p-2')

        # --- A02 PANEL (Nested Tabs) ---
        with ui.tab_panel(mt_a02).classes('p-0 h-full'):
            with ui.tabs().classes('w-full bg-gray-800 text-white dense') as a02_tabs:
                t1 = ui.tab('Spec'); t2 = ui.tab('PSR'); t3 = ui.tab('Baseband')
            with ui.tab_panels(a02_tabs, value=t1).classes('w-full h-full bg-black'):
                with ui.tab_panel(t1).classes('p-0'): log_elements['a02_spec'] = ui.log().classes('w-full h-full text-cyan-300 font-mono text-xs p-2')
                with ui.tab_panel(t2).classes('p-0'): log_elements['a02_psr'] = ui.log().classes('w-full h-full text-yellow-300 font-mono text-xs p-2')
                with ui.tab_panel(t3).classes('p-0'): log_elements['a02_bb'] = ui.log().classes('w-full h-full text-pink-300 font-mono text-xs p-2')

ui.run(title='FAST Obs Controller', host='0.0.0.0', port=8081, reload=False)
