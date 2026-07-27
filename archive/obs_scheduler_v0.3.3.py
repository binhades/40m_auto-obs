from nicegui import ui
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
import sys

# Import Shared Config
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path: sys.path.append(str(current_dir))
import obs_utils

SCRIPT_VERSION = "v0.3.3 (Strict Schema Compatible)"

# --- State Management ---
schedule = []

# --- Functions ---
def update_table():
    table.rows = schedule
    table.update()

def delete_row(row_id):
    global schedule
    schedule = [item for item in schedule if item['id'] != row_id]
    update_table()
    ui.notify("Task removed", type='info')

def handle_baseband_change():
    if baseband_check.value:
        spec_check.set_value(False); psr_check.set_value(False)
        spec_check.disable(); psr_check.disable()
        spec_time_input.disable(); spec_mode_input.disable()
        psr_time_input.disable(); psr_mode_input.disable()
    else:
        spec_check.enable(); psr_check.enable()

def handle_std_backend_change():
    if spec_check.value: 
        spec_time_input.enable(); spec_mode_input.enable()
    else: 
        spec_time_input.disable(); spec_mode_input.disable()
    
    if psr_check.value: 
        psr_time_input.enable(); psr_mode_input.enable()
    else: 
        psr_time_input.disable(); psr_mode_input.disable()

    if spec_check.value or psr_check.value:
        baseband_check.set_value(False); baseband_check.disable()
    else:
        baseband_check.enable()

def add_row():
    try:
        params = { "Project ID": proj_input.value, "Observer": obs_input.value, "Source": source_input.value, 
                   "RA": ra_input.value, "Dec": dec_input.value, "Receiver": recv_input.value }
        for name, value in params.items():
            if not value or value.strip() == "": return ui.notify(f"Error: '{name}' empty!", type='negative')

        if not (spec_check.value or psr_check.value or baseband_check.value):
            return ui.notify("Error: Select at least one Backend!", type='negative')

        selected_ants = [ant for ant, cb in ant_checkboxes.items() if cb.value]
        if not selected_ants: return ui.notify("Error: Select at least one Antenna!", type='negative')

        final_spec_mode = "W,N" if spec_check.value and spec_mode_input.value == "W & N" else "F"

        dt_str = f"{date_input.value} {time_input.value}:00"
        current_start_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        iso_time = current_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        dur_str = f"{int(duration_val.value)}{duration_unit.value}"

        unique_id = f"{datetime.now().timestamp()}_{source_input.value}"
        
        modes_desc = []
        if baseband_check.value: modes_desc.append("Baseband")
        else:
            if spec_check.value: modes_desc.append(f"Spec({spec_time_input.value}, {final_spec_mode})")
            if psr_check.value: modes_desc.append(f"PSR({psr_time_input.value}, {psr_mode_input.value})")
        
        new_obs = {
            "id": unique_id,
            "project_id": proj_input.value,
            "observer": obs_input.value,
            "source": source_input.value,
            "ra": ra_input.value.strip(),
            "dec": dec_input.value.strip(),
            "start_time_cst": iso_time,
            "duration": dur_str,
            "mode": mode_input.value,
            "receiver": recv_input.value,
            "antennas": selected_ants,
            "spec_enabled": spec_check.value,
            "spec_integ": spec_time_input.value if spec_check.value else None,
            "spec_mode": final_spec_mode if spec_check.value else None, 
            "psr_enabled": psr_check.value,
            "psr_integ": psr_time_input.value if psr_check.value else None,
            "psr_mode": psr_mode_input.value if psr_check.value else None,
            "baseband_enabled": baseband_check.value,
            "cal_on": round(float(cal_on_input.value), 1),
            "cal_off": round(float(cal_off_input.value), 1),
            "backend_display": " + ".join(modes_desc),
            "ant_display": ", ".join(selected_ants)
        }
        
        # --- RESOURCE VALIDATION ---
        test_schedule = {"version": obs_utils.DATA_VERSION, "schedule": []}
        for t in schedule:
            clean = t.copy()
            for k in ['id', 'backend_display', 'ant_display']: 
                if k in clean: del clean[k]
            test_schedule["schedule"].append(clean)
        
        clean_new = new_obs.copy()
        for k in ['id', 'backend_display', 'ant_display']: 
            if k in clean_new: del clean_new[k]
        test_schedule["schedule"].append(clean_new)
        
        # --- NEW DICTIONARY PARSING LOGIC ---
        is_valid, validation_result = obs_utils.verify_schedule(test_schedule)
        if not is_valid:
            error_lines = []
            if isinstance(validation_result, dict):
                if 'global' in validation_result:
                    error_lines.append("GLOBAL: " + " | ".join(validation_result['global']))
                
                for idx, msgs in validation_result.items():
                    if str(idx) == 'global': continue
                    error_lines.append(f"Task {int(idx) + 1}: " + " | ".join(msgs))
                    
                error_text = "\n".join(error_lines)
            else:
                error_text = str(validation_result)
            
            ui.notify(f"Validation Failed:\n{error_text}", type='negative', multi_line=True, position='top', timeout=8000)
            return
        # ---------------------------
        
        schedule.append(new_obs)
        update_table()
        ui.notify(f"Added {source_input.value}", type='positive')
        
    except Exception as e: ui.notify(f"Error: {str(e)}", type='negative')

def download_json():
    if not schedule: return ui.notify("Schedule is empty!", type='warning')
    clean_schedule = []
    for item in schedule:
        clean_item = item.copy()
        # Strip out the display-only variables so the saved JSON perfectly matches ALLOWED_FIELDS
        del clean_item['id'], clean_item['backend_display'], clean_item['ant_display']
        clean_schedule.append(clean_item)
    
    ui.download(json.dumps({"version": obs_utils.DATA_VERSION, "schedule": clean_schedule}, indent=4).encode('utf-8'), 'schedule.json')
    ui.notify("Download started")

# --- UI Layout ---
with ui.card().classes('w-full max-w-7xl mx-auto p-4'):
    with ui.row().classes('w-full items-baseline mb-2'):
        ui.markdown('### 📡 FAST Core Array Scheduler')
        ui.label(SCRIPT_VERSION).classes('text-xs text-gray-400 font-mono bg-gray-100 px-2 rounded')

    with ui.row().classes('w-full gap-4'):
        proj_input = ui.input('Project ID', value='P001').classes('w-24')
        obs_input = ui.input('Observer', value='Li').classes('w-24')
        recv_input = ui.input('Receiver', value='L-Band').classes('w-24')
        mode_input = ui.select(obs_utils.VALID_MODES, value='Tracking', label='Obs. Mode').classes('w-32')

    with ui.row().classes('w-full gap-4'):
        source_input = ui.input('Source Name', value='FRB2024').classes('w-1/3')
        ra_input = ui.input('RA (J2000)', placeholder='12:34:56.7').classes('w-1/4')
        dec_input = ui.input('Dec (J2000)', placeholder='+12:34:56').classes('w-1/4')

    with ui.row().classes('w-full gap-4 items-center'):
        with ui.input('Date') as date_input:
            with date_input.add_slot('append'):
                ui.icon('event').on('click', lambda: date_menu.open()).classes('cursor-pointer')
                with ui.menu() as date_menu: ui.date().bind_value(date_input)
        date_input.set_value(datetime.now().strftime("%Y-%m-%d"))

        with ui.input('Time (CST)') as time_input:
            with time_input.add_slot('append'):
                ui.icon('access_time').on('click', lambda: time_menu.open()).classes('cursor-pointer')
                with ui.menu() as time_menu: ui.time().bind_value(time_input)
        time_input.set_value(datetime.now().strftime("%H:%M"))

        duration_val = ui.number('Duration', value=300, min=1).classes('w-24')
        duration_unit = ui.toggle(['s', 'm', 'h'], value='s')

    ui.separator().classes('mt-4 mb-2')

    with ui.row().classes('w-full gap-6 items-start'):
        with ui.column().classes('gap-1'):
            ui.label("Backend Selection").classes('text-xs font-bold text-gray-500')
            with ui.row().classes('items-center gap-4 border border-black p-3 rounded bg-gray-50'):
                with ui.column().classes('gap-0'):
                    with ui.row().classes('items-center gap-1'):
                        spec_check = ui.checkbox('Spec', value=True, on_change=handle_std_backend_change)
                        spec_time_input = ui.select(obs_utils.VALID_SPEC_INTEG, value='0.1s', label='Integ').classes('w-20')
                    spec_mode_input = ui.radio(['W & N', 'Full'], value='Full').props('dense inline').classes('text-xs ml-8')
                
                ui.separator().props('vertical') 

                with ui.column().classes('gap-0'):
                    with ui.row().classes('items-center gap-1'):
                        psr_check = ui.checkbox('PSR', value=False, on_change=handle_std_backend_change)
                        psr_time_input = ui.select(obs_utils.VALID_PSR_INTEG, value='50us', label='Integ').classes('w-20')
                        psr_time_input.disable()
                    psr_mode_input = ui.radio(obs_utils.VALID_PSR_MODES, value='2-Pols').props('dense inline').classes('text-xs ml-8')
                    psr_mode_input.disable()

                ui.separator().props('vertical') 
                baseband_check = ui.checkbox('Baseband', value=False, on_change=handle_baseband_change)
                baseband_check.disable()

        with ui.column().classes('gap-1'):
            ui.label("Noise Cal").classes('text-xs font-bold text-gray-500')
            with ui.row().classes('gap-2 items-center border p-3 rounded bg-gray-50 h-full'):
                cal_on_input = ui.number(label='On', value=10.0, step=0.1, suffix='s').classes('w-24')
                cal_off_input = ui.number(label='Off', value=0.0, step=0.1, suffix='s').classes('w-24')

        with ui.column().classes('gap-1'):
            ui.label("Antenna Selection").classes('text-xs font-bold text-gray-500')
            with ui.row().classes('gap-4 items-center border p-4 rounded bg-gray-50 h-full'):
                ant_checkboxes = {}
                for ant in obs_utils.ACTIVE_ANTENNAS:
                    ant_checkboxes[ant] = ui.checkbox(ant, value=(ant == "CA01")).classes('text-sm')
                ui.separator().props('vertical')
                for ant in obs_utils.FUTURE_ANTENNAS:
                    ui.checkbox(ant, value=False).classes('text-sm text-gray-400').disable()

    with ui.row().classes('w-full justify-end mt-6'):
        ui.button('Add Task to Schedule', on_click=add_row, icon='add_task').classes('bg-green-600 w-full')

    ui.separator().classes('my-4')
    columns = [
        {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
        {'name': 'start_time_cst', 'label': 'Start', 'field': 'start_time_cst', 'align': 'left'},
        {'name': 'duration', 'label': 'Len', 'field': 'duration', 'align': 'left'},
        {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
        {'name': 'backend_display', 'label': 'Backends', 'field': 'backend_display', 'align': 'left'},
        {'name': 'ant_display', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
        {'name': 'actions', 'label': 'Action', 'field': 'actions', 'align': 'center'},
    ]
    table = ui.table(columns=columns, rows=schedule, row_key='id').classes('w-full')
    table.add_slot('body-cell-actions', r''' <q-td :props="props"> <q-btn icon="delete" color="negative" flat dense round @click="$parent.$emit('delete', props.row.id)" /> </q-td> ''')
    table.on('delete', lambda e: delete_row(e.args))

    with ui.row().classes('w-full mt-4'):
        ui.button('Clear All', on_click=lambda: (schedule.clear(), update_table()), icon='delete_sweep').props('flat color=red')
        ui.space()
        ui.button('Download JSON', on_click=download_json, icon='file_download').classes('bg-blue-600')

ui.run(title=f'FAST Scheduler {SCRIPT_VERSION}', host='0.0.0.0', port=8080, reload=False, show=False)
