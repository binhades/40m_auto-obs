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
from datetime import datetime

# --- VERSION CONTROL ---
CONTROLLER_VERSION = "v0.4.3 (Live Task Queue)"

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
        # Track task status by uid: {'20260907120000_B0950+08': 'RUNNING', ...}
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
        self.re_start = re.compile(r"--- Task (.+?): .* Started ---")
        self.re_finish = re.compile(r"Task (.+?): Finished")
        self.re_error = re.compile(r"Task (.+?):? (ERROR|ABORTED|CANCELLED)")
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
                            tid = m.group(1)
                            global_state.task_statuses[tid] = "RUNNING"
                            status_changed = True
                            continue

                        m = self.re_finish.search(line)
                        if m:
                            tid = m.group(1)
                            global_state.task_statuses[tid] = "DONE"
                            status_changed = True
                            continue

                        m = self.re_error.search(line)
                        if m:
                            tid = m.group(1)
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
        pending_scroll = False
        
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

                        # Throttle scroll commands (max 5/s) so high-speed logs
                        # (like Spec) don't flood the browser with updates.
                        now = time.time()
                        if now - last_scroll_time > 0.2:
                            self.ui_log.run_method('scrollToBottom')
                            last_scroll_time = now
                        pending_scroll = True
                    else:
                        break

                if self.should_run:
                    self.ui_log.push(f"[GUI] Connection lost to {self.host}. Retrying...")

                # Drain done (line == None) or EOF: force one final scroll so the
                # view always lands on the latest line even after a throttled burst.
                if pending_scroll:
                    self.ui_log.run_method('scrollToBottom')
                    pending_scroll = False

                if self.should_run:
                    await asyncio.sleep(2)
                
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

    active_readers = []
    # Staging: the one file currently under review. Replaced on each load.
    local_state = {'staging_name': None, 'staging_tasks': []}
    # Queue: the to-be-observed list (list of clean task dicts + '_uid').
    queue = []

    # Init UI refs
    schedule_table = None
    staging_table = None
    driver_log = None
    btn_run = None
    btn_stop = None
    status_label = None
    staging_status_label = None

    UI_KEYS = ['_uid', '_id', '_status', '_msg', '_class', '_locked', '_disabled',
               '_errors', 'ant_display', 'backend_display', 'dgain_display']

    def get_file_list():
        if not obs_utils.SCHEDULE_DIR.exists(): return []
        files = sorted(obs_utils.SCHEDULE_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)
        return [f.name for f in files]

    def write_queue_file():
        """GUI is the single queue-file writer. Drops finished tasks; atomic replace."""
        now_ts = datetime.now().timestamp()
        live = [t for t in queue if task_end_ts(t) >= now_ts]
        queue[:] = live
        data = {"version": obs_utils.DATA_VERSION,
                "schedule": [{k: v for k, v in t.items() if k not in UI_KEYS} for t in queue]}
        tmp = obs_utils.QUEUE_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f: json.dump(data, f, indent=4)
        os.replace(tmp, obs_utils.QUEUE_FILE)

    def task_end_ts(task):
        try:
            start = datetime.strptime(task['start_time_cst'], "%Y-%m-%dT%H:%M:%S").timestamp()
            return start + obs_utils.parse_duration(task['duration'])
        except (KeyError, ValueError):
            return 0.0

    def uid_set(tasks):
        return set(obs_utils.uid_map(tasks).keys())

    def task_locked(task, status):
        # Time-based lock: running/done, or starting within 60s. Never lock when idle.
        if not global_state.running: return False
        if status in ("RUNNING", "DONE", "ERROR"): return True
        try:
            start = datetime.strptime(task['start_time_cst'], "%Y-%m-%dT%H:%M:%S").timestamp()
            return (start - datetime.now().timestamp()) < 60
        except (KeyError, ValueError):
            return True

    def make_row(task, uid, status, locked, msg="Valid"):
        row = task.copy()
        row['_uid'] = uid
        row['_status'] = status
        row['_msg'] = msg
        row['_locked'] = locked
        row['_disabled'] = locked
        row['_class'] = ""
        if status == "RUNNING": row['_class'] = "bg-blue-100 text-blue-900 font-bold"
        elif status == "DONE": row['_class'] = "bg-green-100 text-green-900 opacity-60"
        elif status == "ERROR": row['_class'] = "bg-red-100 text-red-900 font-bold"
        elif locked: row['_class'] = "bg-gray-200 text-gray-500"

        row['ant_display'] = ", ".join(task.get('antennas', []))
        modes = []
        if task.get('baseband_enabled'): modes.append("Baseband")
        if task.get('spec_enabled'): modes.append(f"Spec({task.get('spec_integ')}, {task.get('spec_mode')})")
        if task.get('psr_enabled'): modes.append(f"PSR({task.get('psr_integ')}, {task.get('psr_mode')})")
        row['backend_display'] = " + ".join(modes) if modes else "-"
        dg = task.get('dgain')
        if dg is None: row['dgain_display'] = "-"
        else:
            try:
                d = int(dg, 16) if str(dg).lower().startswith("0x") else int(dg)
                row['dgain_display'] = f"0x{d:04X}"
            except (TypeError, ValueError): row['dgain_display'] = str(dg)
        return row

    def update_queue_table():
        if schedule_table is None: return
        rows = []
        for i, task in enumerate(sorted(queue, key=lambda t: t.get('start_time_cst') or '')):
            uid = task.get('_uid') or obs_utils.uid_for(task)
            task['_uid'] = uid
            status = global_state.task_statuses.get(uid, "PENDING")
            locked = task_locked(task, status)
            msg = {"RUNNING": "Running", "DONE": "Done", "ERROR": "Error"}.get(status, "")
            rows.append(make_row(task, uid, status, locked, msg))
        schedule_table.rows = rows
        schedule_table.update()

    def update_staging_table():
        if staging_table is None: return
        rows = []
        queued_uids = uid_set(queue)
        for i, task in enumerate(local_state['staging_tasks']):
            uid = task.get('_uid') or obs_utils.uid_for(task)
            task['_uid'] = uid
            already = uid in queued_uids
            err = task.get('_errors')
            status = "QUEUED" if already else ("ERROR" if err else "OK")
            msg = "Already in queue" if already else (err or "Valid")
            rows.append(make_row(task, uid, status, already or bool(err), msg))
            rows[-1]['_class'] = "bg-orange-100 text-orange-900" if already else \
                                 ("bg-red-100 text-red-900 font-bold" if err else "bg-green-50 text-green-900")
        staging_table.rows = rows
        staging_table.update()

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

        update_queue_table()
        update_staging_table()

    def load_staging(filename):
        try:
            path = obs_utils.SCHEDULE_DIR / filename
            with open(path, 'r') as f: data = json.load(f)

            is_valid, result = obs_utils.verify_schedule(data)
            errs = result if isinstance(result, dict) else ({'global': [str(result)]} if not is_valid else {})
            # Global errors (version mismatch, missing schedule, antenna
            # conflicts) belong to no single task; on version mismatch the
            # per-task checks never even run, so the rows must not read green.
            global_msgs = errs.get('global', [])

            tasks = []
            for i, task in enumerate(data.get('schedule', [])):
                t = dict(task)
                msgs = list(global_msgs) + list(errs.get(i, []))
                t['_errors'] = " | ".join(msgs) if msgs else None
                tasks.append(t)

            local_state['staging_name'] = filename
            local_state['staging_tasks'] = tasks
            if staging_status_label is not None:
                if global_msgs:
                    staging_status_label.set_text(f"FILE ERROR: {' | '.join(global_msgs)}")
                    staging_status_label.classes('text-red-400 font-bold', remove='text-green-400')
                else:
                    staging_status_label.set_text("")
                    staging_status_label.classes('', remove='text-red-400 font-bold')
            update_staging_table()
            ui.notify(f"Loaded {filename}: {len(tasks)} task(s)" +
                      ("" if is_valid else " - errors found, see review table"),
                      type='positive' if is_valid else 'warning')
        except json.JSONDecodeError:
            ui.notify(f"{filename}: invalid JSON syntax", type='negative')
        except Exception as e:
            ui.notify(f"Error: {e}", type='negative')

    def add_to_queue(uid):
        task = next((t for t in local_state['staging_tasks'] if t.get('_uid') == uid), None)
        if task is None: return
        if task.get('_errors'):
            ui.notify("Task has validation errors", type='negative'); return
        if uid in uid_set(queue):
            ui.notify("Task is already in the queue", type='warning'); return

        candidate = {k: v for k, v in task.items() if k not in UI_KEYS}
        merged = [{k: v for k, v in t.items() if k not in UI_KEYS} for t in queue] + [candidate]
        data = {"version": obs_utils.DATA_VERSION, "schedule": merged}
        is_valid, result = obs_utils.verify_schedule(data)
        if not is_valid:
            msgs = None
            if isinstance(result, dict):
                msgs = result.get('global') or result.get(len(merged) - 1)
            detail = " | ".join(msgs) if msgs else str(result)
            ui.notify(f"Cannot add - conflicts with queue:\n{detail}",
                      type='negative', multi_line=True, timeout=6000)
            return

        clean = {k: v for k, v in task.items() if k not in UI_KEYS}
        clean['_uid'] = uid
        queue.append(clean)
        queue.sort(key=lambda t: t.get('start_time_cst') or '')
        write_queue_file()
        update_queue_table()
        update_staging_table()
        ui.notify(f"Added {task.get('source')} to queue", type='positive')

    def add_all_valid():
        staged = [t for t in local_state['staging_tasks'] if not t.get('_errors')]
        added = skipped = 0
        for task in staged:
            uid = task.get('_uid') or obs_utils.uid_for(task)
            if uid in uid_set(queue):
                skipped += 1; continue
            candidate = {k: v for k, v in task.items() if k not in UI_KEYS}
            merged = [{k: v for k, v in t.items() if k not in UI_KEYS} for t in queue] + [candidate]
            is_valid, _ = obs_utils.verify_schedule({"version": obs_utils.DATA_VERSION, "schedule": merged})
            if not is_valid:
                skipped += 1; continue
            clean = dict(candidate); clean['_uid'] = uid
            queue.append(clean); added += 1
        if added or skipped:
            queue.sort(key=lambda t: t.get('start_time_cst') or '')
            write_queue_file()
            update_queue_table(); update_staging_table()
            ui.notify(f"Added {added} task(s)" + (f", skipped {skipped}" if skipped else ""),
                      type='positive' if added else 'warning')

    def delete_from_queue(uid):
        task = next((t for t in queue if t.get('_uid') == uid), None)
        if task is None: return
        status = global_state.task_statuses.get(uid, "PENDING")
        if task_locked(task, status):
            ui.notify("Task is locked (running or starts within 60 s)", type='warning'); return
        queue[:] = [t for t in queue if t.get('_uid') != uid]
        write_queue_file()  # engine picks up the removal while running
        update_queue_table()
        update_staging_table()
        ui.notify(f"Removed {task.get('source')} from queue", type='info')

    async def run_observation():
        check_existing_process()
        if global_state.running: return

        if not queue:
            ui.notify("Queue is empty - add tasks from a loaded file first", type='warning'); return

        try:
            for reader in active_readers: reader.clear()
            if driver_log: driver_log.clear()

            global_state.task_statuses = {}
            write_queue_file()
            update_queue_table()

            global_state.process = subprocess.Popen(
                ["python3", str(obs_utils.DRIVER_SCRIPT), str(obs_utils.QUEUE_FILE)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid
            )
            global_state.running = True
            update_status_ui()
            ui.notify("Observation Launched! Add tasks to the queue while it runs.", type='positive')

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

    with ui.header().classes('bg-slate-800 text-white min-h-0'):
        ui.label("FAST Core Array - Mission Control").classes('text-lg font-bold py-1 px-4')
        ui.label(CONTROLLER_VERSION).classes('text-xs text-gray-400 font-mono bg-gray-900 px-2 rounded self-center')
        ui.space()
        ui.label("Status: ").classes('font-bold')
        status_label = ui.label("IDLE").classes('mr-4 font-bold font-mono text-gray-400')
        ui.timer(1.0, update_status_ui)

    with ui.tabs().classes('w-full') as tabs:
        tab_sched = ui.tab('Schedule')
        tab_drv = ui.tab('Driver')
        ant_tabs = {ant: ui.tab(ant) for ant in obs_utils.ACTIVE_ANTENNAS}

    with ui.tab_panels(tabs, value=tab_sched).classes('w-full h-full'):
        with ui.tab_panel(tab_sched):
            # --- STAGING: loaded file under review ---
            with ui.row().classes('w-full items-center gap-4 mt-2'):
                ui.label("Loaded File Review").classes('font-bold text-gray-600')
                staging_status_label = ui.label("").classes('font-mono text-sm')
                file_select = ui.select(get_file_list(), label="Select Schedule File",
                                        on_change=lambda e: load_staging(e.value)).classes('w-72')
                ui.button('Reload', on_click=lambda: file_select.set_options(get_file_list()), icon='refresh').props('flat')
                ui.space()
                ui.button('Add All Valid', on_click=add_all_valid, icon='playlist_add').classes('bg-blue-600 text-white')

            staging_cols = [
                {'name': 'status', 'label': 'Check', 'field': '_msg', 'align': 'left', 'classes': 'font-bold'},
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start', 'label': 'Start (CST)', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'dur', 'label': 'Len', 'field': 'duration', 'align': 'left'},
                {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
                {'name': 'backends', 'label': 'Backends', 'field': 'backend_display', 'align': 'left'},
                {'name': 'rfgain', 'label': 'RF Gain', 'field': 'rfgain', 'align': 'left'},
                {'name': 'dgain_display', 'label': 'DGain', 'field': 'dgain_display', 'align': 'left'},
                {'name': 'ants', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
                {'name': 'add', 'label': 'Action', 'field': 'action', 'align': 'center'},
            ]
            staging_table = ui.table(columns=staging_cols, rows=[], row_key='_uid').classes('w-full')
            staging_table.add_slot('body', r'''
                <q-tr :props="props" :class="props.row._class">
                    <q-td key="status" :props="props">{{ props.row._msg }}</q-td>
                    <q-td key="source" :props="props">{{ props.row.source }}</q-td>
                    <q-td key="start" :props="props">{{ props.row.start_time_cst }}</q-td>
                    <q-td key="dur" :props="props">{{ props.row.duration }}</q-td>
                    <q-td key="mode" :props="props">{{ props.row.mode }}</q-td>
                    <q-td key="backends" :props="props">{{ props.row.backend_display }}</q-td>
                    <q-td key="rfgain" :props="props">{{ props.row.rfgain }}</q-td>
                    <q-td key="dgain_display" :props="props">{{ props.row.dgain_display }}</q-td>
                    <q-td key="ants" :props="props">{{ props.row.ant_display }}</q-td>
                    <q-td key="add" :props="props">
                        <q-btn icon="add_circle" color="positive" flat dense round
                            :disable="props.row._disabled"
                            @click="$parent.$emit('addtask', props.row._uid)">
                            <q-tooltip>Add to queue</q-tooltip>
                        </q-btn>
                    </q-td>
                </q-tr>
            ''')
            staging_table.on('addtask', lambda e: add_to_queue(e.args))

            ui.separator().classes('my-3')

            # --- QUEUE: to-be-observed, engine's task list ---
            with ui.row().classes('w-full items-center gap-4'):
                ui.label("To-Be-Observed Queue").classes('font-bold text-gray-600')
                ui.space()
                btn_run = ui.button('RUN OBSERVATION', on_click=run_observation, icon='play_arrow').classes('bg-green-600 text-white')
                btn_stop = ui.button('ABORT', on_click=stop_observation, icon='stop').classes('bg-red-600 text-white')

            queue_cols = [
                {'name': 'id', 'label': '#', 'field': '_id', 'align': 'left', 'sortable': True},
                {'name': 'status', 'label': 'Status', 'field': '_status', 'align': 'left'},
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start', 'label': 'Start (CST)', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'dur', 'label': 'Len', 'field': 'duration', 'align': 'left'},
                {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
                {'name': 'backends', 'label': 'Backends', 'field': 'backend_display', 'align': 'left'},
                {'name': 'rfgain', 'label': 'RF Gain', 'field': 'rfgain', 'align': 'left'},
                {'name': 'dgain_display', 'label': 'DGain', 'field': 'dgain_display', 'align': 'left'},
                {'name': 'ants', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
                {'name': 'del', 'label': 'Action', 'field': 'action', 'align': 'center'},
            ]
            schedule_table = ui.table(columns=queue_cols, rows=[], row_key='_uid').classes('w-full mb-2')

            schedule_table.add_slot('body', r'''
                <q-tr :props="props" :class="props.row._class">
                    <q-td key="id" :props="props">{{ props.row._uid }}</q-td>
                    <q-td key="status" :props="props">
                        <span v-if="props.row._status == 'RUNNING'" class="spinner-border spinner-border-sm">⏳</span>
                        {{ props.row._status }}
                    </q-td>
                    <q-td key="source" :props="props">{{ props.row.source }}</q-td>
                    <q-td key="start" :props="props">{{ props.row.start_time_cst }}</q-td>
                    <q-td key="dur" :props="props">{{ props.row.duration }}</q-td>
                    <q-td key="mode" :props="props">{{ props.row.mode }}</q-td>
                    <q-td key="backends" :props="props">{{ props.row.backend_display }}</q-td>
                    <q-td key="rfgain" :props="props">{{ props.row.rfgain }}</q-td>
                    <q-td key="dgain_display" :props="props">{{ props.row.dgain_display }}</q-td>
                    <q-td key="ants" :props="props">{{ props.row.ant_display }}</q-td>
                    <q-td key="del" :props="props">
                        <q-btn icon="delete" color="negative" flat dense round
                            :disable="props.row._disabled"
                            @click="$parent.$emit('deltask', props.row._uid)">
                            <q-tooltip>{{ props.row._locked ? 'Locked (running / starts <60 s)' : 'Remove from queue' }}</q-tooltip>
                        </q-btn>
                    </q-td>
                </q-tr>
            ''')
            schedule_table.on('deltask', lambda e: delete_from_queue(e.args))

            # Resume: if a queue file exists (engine aborted earlier / GUI restarted),
            # load it back into the queue table.
            if obs_utils.QUEUE_FILE.exists():
                try:
                    with open(obs_utils.QUEUE_FILE) as f: qdata = json.load(f)
                    now_ts = datetime.now().timestamp()
                    for t in qdata.get('schedule', []):
                        if task_end_ts(t) >= now_ts:
                            t['_uid'] = obs_utils.uid_for(t)
                            queue.append(t)
                    update_queue_table()
                except Exception:
                    pass

        with ui.tab_panel(tab_drv):
            ui.label("Driver Output (active_driver_session.log)").classes('font-bold text-gray-600')
            # Viewport-relative height: fills the space below header+tabs on a
            # 1920x1080 screen with no page scroll.
            driver_log = ui.log(max_lines=1000).classes('w-full h-[calc(100vh-24rem)] bg-black text-white font-mono rounded p-2 text-xs')

            # Attach to a driver already running (e.g. launched from a terminal): replay
            # the active session from the top; otherwise park at EOF so a stale log
            # from a past session is not replayed into a fresh GUI.
            local_reader = LogReader(obs_utils.ACTIVE_DRIVER_LOG, driver_log, parse_status=True,
                                     status_callback=update_queue_table, start_at_end=not driver_process_running())
            ui.timer(0.5, local_reader.read)

        for ant in obs_utils.ACTIVE_ANTENNAS:
            with ui.tab_panel(ant_tabs[ant]):
                with ui.column().classes('w-full gap-2'):
                    with ui.row().classes('w-full gap-4'):
                        with ui.column().classes('w-[49%]'):
                            ui.label(f"{ant} - SPEC").classes('font-bold text-center w-full')
                            log_spec = ui.log(max_lines=200).classes('w-full h-[38vh] bg-gray-900 text-green-400 font-mono rounded p-2 text-xs')
                        with ui.column().classes('w-[49%]'):
                            ui.label(f"{ant} - PSR").classes('font-bold text-center w-full')
                            log_psr = ui.log(max_lines=200).classes('w-full h-[38vh] bg-gray-900 text-green-400 font-mono rounded p-2 text-xs')
                    with ui.column().classes('w-full'):
                        ui.label(f"{ant} - BASEBAND").classes('font-bold text-center w-full')
                        log_bb = ui.log(max_lines=200).classes('w-full h-[33vh] bg-gray-900 text-green-400 font-mono rounded p-2 text-xs')

                    host = obs_utils.ANTENNA_HOST_MAP.get(ant, "localhost")
                    r1 = RemoteLogReader(host, f"active_spec_{ant}.log", log_spec)
                    r2 = RemoteLogReader(host, f"active_psr_{ant}.log", log_psr)
                    r3 = RemoteLogReader(host, f"active_bb_{ant}.log", log_bb)
                    active_readers.extend([r1, r2, r3])
                    asyncio.create_task(r1.start_loop())
                    asyncio.create_task(r2.start_loop())
                    asyncio.create_task(r3.start_loop())
    
    update_status_ui() 

# Loopback-only bind (v0.4.3): the network entry point is the nginx reverse
# proxy (https://atlas/controller/ -> 127.0.0.1:8082). This GUI drives the
# hardware — the localhost bind guarantees nobody bypasses the proxy.
# Cert auto-detect stays (TCP-passthrough fallback, docs/https_setup.md),
# but the cert pair must NOT exist behind the proxy: a backend serving
# HTTPS would 502 nginx, which owns TLS.
_cert_dir = Path(__file__).parent / "certs"
_cert_file = _cert_dir / "controller.crt"
_key_file = _cert_dir / "controller.key"
_ssl_kwargs = {}
if _cert_file.is_file() and _key_file.is_file():
    _ssl_kwargs = {"ssl_certfile": str(_cert_file), "ssl_keyfile": str(_key_file)}
    print(f"[CONTROLLER] HTTPS enabled ({_cert_file})")
else:
    print("[CONTROLLER] No cert pair in ./certs - serving plain HTTP")

ui.run(title='FAST Controller', host='127.0.0.1', port=8082, reload=False,
       **_ssl_kwargs)
