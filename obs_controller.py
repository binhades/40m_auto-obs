#!/usr/bin/env python3
from nicegui import ui, app
import json
import asyncio
import signal
import os
import sys
import subprocess
import re
import time
from pathlib import Path

# --- VERSION CONTROL ---
CONTROLLER_VERSION = "v0.3.7 (Active Driver Log)"

# --- IMPORTS ---
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

import obs_utils

# --- GLOBAL STATE ---
class GlobalState:
    def __init__(self):
        self.process = None 
        self.running = False
        self.current_schedule = None
        # Track task status: { 1: 'RUNNING', 2: 'DONE', 3: 'ERROR' }
        self.task_statuses = {} 

global_state = GlobalState()

# --- HELPER 1: Log Parsing & Streaming (Local) ---
class LogReader:
    def __init__(self, filepath, ui_log_element, parse_status=False, status_callback=None, start_at_end=False):
        self.filepath = Path(filepath)
        self.ui_log = ui_log_element
        self.file = None
        self.inode = None
        self.parse_status = parse_status
        self.status_callback = status_callback
        self.re_start = re.compile(r"--- Task (\d+): .* Started ---")
        self.re_finish = re.compile(r"Task (\d+): Finished")
        self.re_error = re.compile(r"Task (\d+):? (ERROR|ABORTED|CANCELLED)")
        self.re_engine = re.compile(r"Loaded \d+ tasks\. Engine Start")
        self.start_at_end = start_at_end
        self.first_open = True

    def read(self):
        if not self.filepath.exists(): return
        try:
            current_inode = self.filepath.stat().st_ino
            if self.file is None or (self.inode and self.inode != current_inode):
                if self.file: self.file.close()
                self.file = open(self.filepath, 'r')
                self.inode = current_inode
                if self.first_open and self.start_at_end:
                    self.file.seek(0, 2)
                else:
                    self.file.seek(0)
                self.first_open = False

            lines = self.file.read()
            if lines: 
                self.ui_log.push(lines)
                self.ui_log.run_method('scrollToBottom') 
                
                if self.parse_status and self.status_callback:
                    status_changed = False
                    for line in lines.split('\n'):
                        if self.re_engine.search(line):
                            global_state.task_statuses = {}
                            status_changed = True
                            continue

                        m = self.re_start.search(line)
                        if m:
                            tid = int(m.group(1))
                            global_state.task_statuses[tid] = "RUNNING"
                            status_changed = True
                            continue
                        
                        m = self.re_finish.search(line)
                        if m:
                            tid = int(m.group(1))
                            global_state.task_statuses[tid] = "DONE"
                            status_changed = True
                            continue

                        m = self.re_error.search(line)
                        if m:
                            tid = int(m.group(1))
                            global_state.task_statuses[tid] = "ERROR"
                            status_changed = True

                    if status_changed:
                        self.status_callback()

        except Exception: pass 

# --- HELPER 2: REMOTE Log Streaming (Throttled) ---
class RemoteLogReader:
    def __init__(self, host, filename, ui_log_element):
        self.host = host
        self.filename = filename
        self.ui_log = ui_log_element
        self.process = None
        self.should_run = True 

    async def start_loop(self):
        self.should_run = True
        last_scroll_time = 0
        
        while self.should_run:
            try:
                cmd = [
                    "ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
                    self.host,
                    f"tail -n 50 -F {obs_utils.LOG_DIR}/{self.filename} 2>/dev/null"
                ]
                self.process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, preexec_fn=os.setsid
                )
                
                while self.process and self.process.returncode is None:
                    if self.process.stdout.at_eof(): break
                    line = await self.process.stdout.readline()
                    if line: 
                        # Push content immediately
                        self.ui_log.push(line.decode().strip())
                        
                        # CRITICAL FIX: Throttle Scroll commands (Max 5 per second)
                        # This prevents high-speed logs (like Spec) from freezing the browser
                        now = time.time()
                        if now - last_scroll_time > 0.2:
                            self.ui_log.run_method('scrollToBottom') 
                            last_scroll_time = now
                    else: break
                
                if self.should_run:
                    self.ui_log.push(f"[GUI] Connection lost to {self.host}. Retrying...")
                    await asyncio.sleep(2)
            except Exception as e:
                self.ui_log.push(f"[GUI Connect Error] {e}")
                await asyncio.sleep(5)

    def stop(self):
        self.should_run = False
        if self.process:
            try: os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except: pass
        
    def clear(self):
        self.ui_log.clear()

# --- SYSTEM CHECKS ---
def driver_process_running():
    try:
        cmd = ["pgrep", "-a", "-f", "run_auto_obs.py"]
        output = subprocess.check_output(cmd).decode().strip()

        if output:
            for line in output.split('\n'):
                if any(x in line for x in ["vi ", "vim ", "nano ", "tail ", "grep "]): continue
                if "python" in line and "run_auto_obs.py" in line:
                    return True
    except Exception:
        pass
    return False

def check_existing_process():
    if driver_process_running():
        if not global_state.running: global_state.running = True
    else:
        if global_state.running: global_state.running = False

# --- UI ENTRY POINT ---
@ui.page('/')
def main_page():
    
    local_state = {'schedule_data': global_state.current_schedule}
    active_readers = []
    
    # Init UI refs
    schedule_table = None
    driver_log = None
    btn_run = None
    btn_stop = None
    status_label = None

    def get_file_list():
        if not obs_utils.SCHEDULE_DIR.exists(): return []
        files = sorted(obs_utils.SCHEDULE_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)
        return [f.name for f in files]

    def update_table():
        if schedule_table is None: return 
        
        if not local_state['schedule_data']:
            schedule_table.rows = []
            return

        rows = []
        for i, task in enumerate(local_state['schedule_data']['schedule']):
            task_id = i + 1
            
            status = global_state.task_statuses.get(task_id, "PENDING")
            row_class = ""
            if status == "RUNNING":
                row_class = "bg-blue-100 text-blue-900 font-bold"
            elif status == "DONE":
                row_class = "bg-green-100 text-green-900 opacity-60"
            elif status == "ERROR":
                row_class = "bg-red-100 text-red-900 font-bold"
            elif task.get('_skipped'):
                row_class = "bg-gray-200 text-gray-400"

            row = task.copy()
            row['_id'] = task_id
            row['_status'] = status
            row['_class'] = row_class
            row['_disabled'] = global_state.running or status in ["DONE", "RUNNING"] 

            row['ant_display'] = ", ".join(task.get('antennas', []))
            modes = []
            if task.get('baseband_enabled'): modes.append("Baseband")
            if task.get('spec_enabled'): modes.append(f"Spec")
            if task.get('psr_enabled'): modes.append(f"PSR")
            row['backend_display'] = " + ".join(modes) if modes else "-"

            rows.append(row)
        
        schedule_table.rows = rows
        schedule_table.update()

    def update_status_ui():
        check_existing_process()
        
        if status_label:
            if global_state.running:
                status_label.set_text("RUNNING")
                status_label.classes('text-green-400', remove='text-gray-400')
                if btn_run: btn_run.disable()
                if btn_stop: btn_stop.enable()
            else:
                status_label.set_text("IDLE")
                status_label.classes('text-gray-400', remove='text-green-400')
                if btn_run: btn_run.enable()
                if btn_stop: btn_stop.disable()
            
        update_table()

    def load_schedule(filename):
        try:
            path = obs_utils.SCHEDULE_DIR / filename
            with open(path, 'r') as f: data = json.load(f)
            
            global_state.task_statuses = {} 
            for task in data['schedule']:
                task['_skipped'] = False

            local_state['schedule_data'] = data
            global_state.current_schedule = data 
            update_table()
            ui.notify(f"Loaded {filename}", type='positive')
        except Exception as e:
            ui.notify(f"Error: {e}", type='negative')

    def toggle_skip(source_name):
        if global_state.running:
            ui.notify("Cannot edit while observation is running!", type='warning')
            return

        if not local_state['schedule_data']: return
        for task in local_state['schedule_data']['schedule']:
            if task.get('source') == source_name: 
                task['_skipped'] = not task['_skipped']
                break
        update_table()

    async def run_observation():
        check_existing_process()
        if global_state.running: return

        if not local_state['schedule_data']: 
            ui.notify("Load a schedule first", type='warning'); return

        valid_tasks = [t for t in local_state['schedule_data']['schedule'] if not t.get('_skipped')]
        if not valid_tasks:
            ui.notify("No tasks enabled", type='warning'); return

        try:
            for reader in active_readers: reader.clear()
            if driver_log: driver_log.clear()
            
            global_state.task_statuses = {}
            update_table()

            temp_file = obs_utils.SCHEDULE_DIR / ".active_run.json"
            clean_data = {"version": obs_utils.DATA_VERSION, "schedule": []}
            for t in valid_tasks:
                copy_t = t.copy()
                for k in ['_skipped', 'ant_display', 'backend_display', '_id', '_status', '_class', '_disabled']: 
                    if k in copy_t: del copy_t[k]
                clean_data['schedule'].append(copy_t)

            with open(temp_file, 'w') as f: json.dump(clean_data, f, indent=4)

            global_state.process = subprocess.Popen(
                ["python3", str(obs_utils.DRIVER_SCRIPT), str(temp_file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid
            )
            global_state.running = True
            update_status_ui()
            ui.notify("Observation Launched!", type='positive')
            
        except Exception as e:
            ui.notify(f"Launch Failed: {e}", type='negative')

    def stop_observation():
        try:
            subprocess.run(["pkill", "-f", "run_auto_obs.py"])
            with open(obs_utils.ACTIVE_DRIVER_LOG, 'a') as f: f.write("\n>>> ABORT SIGNAL SENT (pkill) <<<\n")
        except Exception as e: ui.notify(f"Stop Error: {e}")

    app.on_disconnect(lambda: [r.stop() for r in active_readers])

    # --- LAYOUT ---
    ui.query('.q-page').classes('flex-center w-full max-w-full px-4')

    with ui.header().classes('bg-slate-800 text-white'):
        ui.label("FAST Core Array - Mission Control").classes('text-xl font-bold p-4')
        ui.label(CONTROLLER_VERSION).classes('text-xs text-gray-400 font-mono bg-gray-900 px-2 rounded self-center')
        ui.space()
        ui.label("Status: ").classes('font-bold')
        status_label = ui.label("IDLE").classes('mr-4 font-bold font-mono text-gray-400')
        ui.timer(1.0, update_status_ui)

    with ui.tabs().classes('w-full') as tabs:
        tab_sched = ui.tab('Schedule')
        ant_tabs = {ant: ui.tab(ant) for ant in obs_utils.ACTIVE_ANTENNAS}

    with ui.tab_panels(tabs, value=tab_sched).classes('w-full h-full'):
        with ui.tab_panel(tab_sched):
            with ui.row().classes('w-full items-center gap-4 mb-4'):
                file_select = ui.select(get_file_list(), label="Select Schedule", on_change=lambda e: load_schedule(e.value)).classes('w-64')
                ui.button('Reload', on_click=lambda: file_select.set_options(get_file_list()), icon='refresh').props('flat')
                ui.space()
                btn_run = ui.button('RUN OBSERVATION', on_click=run_observation, icon='play_arrow').classes('bg-green-600 text-white')
                btn_stop = ui.button('ABORT', on_click=stop_observation, icon='stop').classes('bg-red-600 text-white')

            # --- TABLE ---
            cols = [
                {'name': 'id', 'label': '#', 'field': '_id', 'align': 'left', 'sortable': True},
                {'name': 'status', 'label': 'Status', 'field': '_status', 'align': 'left'},
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start', 'label': 'Start', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'dur', 'label': 'Len', 'field': 'duration', 'align': 'left'},
                {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
                {'name': 'backends', 'label': 'Backends', 'field': 'backend_display', 'align': 'left'},
                {'name': 'ants', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
                {'name': 'skip', 'label': 'Action', 'field': 'action', 'align': 'center'},
            ]
            schedule_table = ui.table(columns=cols, rows=[], row_key='source').classes('w-full mb-6')
            
            schedule_table.add_slot('body', r'''
                <q-tr :props="props" :class="props.row._class">
                    <q-td key="id" :props="props">{{ props.row._id }}</q-td>
                    <q-td key="status" :props="props">
                        <span v-if="props.row._status == 'RUNNING'" class="spinner-border spinner-border-sm">⏳</span>
                        {{ props.row._status }}
                    </q-td>
                    <q-td key="source" :props="props">{{ props.row.source }}</q-td>
                    <q-td key="start" :props="props">{{ props.row.start_time_cst }}</q-td>
                    <q-td key="dur" :props="props">{{ props.row.duration }}</q-td>
                    <q-td key="mode" :props="props">{{ props.row.mode }}</q-td>
                    <q-td key="backends" :props="props">{{ props.row.backend_display }}</q-td>
                    <q-td key="ants" :props="props">{{ props.row.ant_display }}</q-td>
                    <q-td key="skip" :props="props">
                        <q-btn 
                            :icon="props.row._skipped ? 'undo' : 'delete'" 
                            :color="props.row._skipped ? 'grey' : 'negative'" 
                            :disable="props.row._disabled"
                            flat dense round 
                            @click="$parent.$emit('toggle', props.row.source)"
                        >
                            <q-tooltip>{{ props.row._skipped ? 'Restore' : 'Skip' }}</q-tooltip>
                        </q-btn>
                    </q-td>
                </q-tr>
            ''')
            schedule_table.on('toggle', lambda e: toggle_skip(e.args))

            ui.label("Driver Output").classes('font-bold text-gray-600')
            driver_log = ui.log(max_lines=1000).classes('w-full h-[600px] bg-black text-white font-mono rounded p-2 text-xs')
            
            # Attach to a driver already running (e.g. launched from a terminal): replay
            # the active session from the top; otherwise park at EOF so a stale log
            # from a past session is not replayed into a fresh GUI.
            local_reader = LogReader(obs_utils.ACTIVE_DRIVER_LOG, driver_log, parse_status=True,
                                     status_callback=update_table, start_at_end=not driver_process_running())
            ui.timer(0.5, local_reader.read)

        for ant in obs_utils.ACTIVE_ANTENNAS:
            with ui.tab_panel(ant_tabs[ant]):
                with ui.column().classes('w-full h-full gap-4'):
                    with ui.row().classes('w-full gap-4'):
                        with ui.column().classes('w-[49%]'):
                            ui.label(f"{ant} - SPEC").classes('font-bold text-center w-full')
                            log_spec = ui.log(max_lines=200).classes('w-full h-[500px] bg-gray-900 text-green-400 font-mono rounded p-2 text-xs')
                        with ui.column().classes('w-[49%]'):
                            ui.label(f"{ant} - PSR").classes('font-bold text-center w-full')
                            log_psr = ui.log(max_lines=200).classes('w-full h-[500px] bg-gray-900 text-green-400 font-mono rounded p-2 text-xs')
                    with ui.column().classes('w-full'):
                        ui.label(f"{ant} - BASEBAND").classes('font-bold text-center w-full')
                        log_bb = ui.log(max_lines=200).classes('w-full h-[500px] bg-gray-900 text-green-400 font-mono rounded p-2 text-xs')

                    host = obs_utils.ANTENNA_HOST_MAP.get(ant, "localhost")
                    r1 = RemoteLogReader(host, f"active_spec_{ant}.log", log_spec)
                    r2 = RemoteLogReader(host, f"active_psr_{ant}.log", log_psr)
                    r3 = RemoteLogReader(host, f"active_bb_{ant}.log", log_bb)
                    active_readers.extend([r1, r2, r3])
                    asyncio.create_task(r1.start_loop())
                    asyncio.create_task(r2.start_loop())
                    asyncio.create_task(r3.start_loop())
    
    update_status_ui() 

ui.run(title='FAST Controller', host='127.0.0.1', port=8082, reload=False)
