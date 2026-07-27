#!/usr/bin/env python3
from nicegui import ui, app
import json
import re
from datetime import datetime
from pathlib import Path
import sys
import uuid

# Import shared logic
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path: sys.path.append(str(current_dir))
import obs_utils

VERSION = "v0.2.3 (Async Fix)"

@ui.page('/')
def main_page():
    
    # Global state for the session
    state = {
        'data': None, 
        'filename': None,
        'is_valid': False
    }

    # UI Refs
    upload_area, preview_container, status_card = None, None, None
    status_icon, status_label, preview_table, save_btn = None, None, None, None

    def reset_view_only():
        """Resets the visual indicators but keeps the table data visible."""
        status_label.set_text("")
        status_card.classes(remove='bg-green-100 bg-orange-100 bg-red-100 bg-blue-100')
        status_icon.name = ''
        status_icon.classes(remove='text-green-500 text-orange-600 text-red-600 text-blue-600')

    def delete_task(row_uuid):
        """Removes a task using its temporary Unique ID."""
        if not state['data']: return
        
        # Filter based on the hidden UUID
        state['data']["schedule"] = [t for t in state['data']["schedule"] if t.get('_ui_id') != row_uuid]
        
        # Re-verify after deletion to see if it fixes the schedule
        validate_and_update_ui(state['data'], state['filename'])

    def validate_and_update_ui(data, filename):
        """
        Core logic: Validates data, assigns row colors/errors, and updates table.
        Does NOT hide the table on error.
        """
        # 1. Run Verification
        is_valid, message = obs_utils.verify_schedule(data)
        state['is_valid'] = is_valid
        state['data'] = data
        state['filename'] = filename

        # 2. Pre-process Rows for Display
        rows = []
        error_task_index = -1
        
        # If validation failed, try to find which task index caused it
        if not is_valid:
            match = re.search(r"Task (\d+):", message)
            if match:
                error_task_index = int(match.group(1)) - 1

        for idx, task in enumerate(data.get("schedule", [])):
            if '_ui_id' not in task: task['_ui_id'] = str(uuid.uuid4())

            task['ant_display'] = ", ".join(task.get('antennas', []))
            modes = []
            if task.get('baseband_enabled'): modes.append("Baseband")
            if task.get('spec_enabled'): modes.append(f"Spec")
            if task.get('psr_enabled'): modes.append(f"PSR")
            task['backend_display'] = " + ".join(modes) if modes else "-"

            # --- ROW STATUS LOGIC ---
            if is_valid:
                task['_status'] = "OK"
                task['_msg'] = "Valid"
                task['_class'] = "bg-green-50 text-green-900"
            else:
                if idx == error_task_index:
                    task['_status'] = "ERROR"
                    clean_msg = re.sub(r"^Task \d+: ", "", message)
                    task['_msg'] = clean_msg
                    task['_class'] = "bg-red-100 text-red-900 font-bold"
                else:
                    task['_status'] = "PENDING"
                    task['_msg'] = "-"
                    task['_class'] = "text-gray-500"
            
            rows.append(task)

        # 3. Update UI Elements
        upload_area.visible = False
        preview_container.visible = True
        preview_table.rows = rows

        reset_view_only()
        if is_valid:
            status_card.classes('bg-green-100')
            status_icon.name = 'check_circle'
            status_icon.classes('text-green-500')
            status_label.set_text("Validation Passed: Ready to Submit")
            save_btn.enable()
        else:
            status_card.classes('bg-red-100')
            status_icon.name = 'error'
            status_icon.classes('text-red-600')
            status_label.set_text(f"VALIDATION FAILED: {message}")
            save_btn.disable()

    # --- CRITICAL FIX: ASYNC HANDLER ---
    async def handle_upload(e):
        try:
            content_str = ""
            filename = getattr(e, 'name', 'unknown.json')

            # NiceGUI 1.4+ / 3.x usually provides 'content'
            if hasattr(e, 'content'):
                # Force async read
                content_bytes = e.content.read()
                if hasattr(content_bytes, '__await__'):
                    content_bytes = await content_bytes
                content_str = content_bytes.decode('utf-8')
            
            # Fallback for alternative event structures
            elif hasattr(e, 'file'):
                f = e.file
                if hasattr(f, 'read'):
                    content_bytes = f.read()
                    if hasattr(content_bytes, '__await__'):
                        content_bytes = await content_bytes
                    content_str = content_bytes.decode('utf-8')
                else:
                    content_str = str(f)
            else:
                raise ValueError("No file content found in event")

            if not content_str: raise ValueError("File is empty")
            
            data = json.loads(content_str)
            validate_and_update_ui(data, filename)
                
        except json.JSONDecodeError:
            ui.notify("Invalid JSON Syntax", type='negative')
        except Exception as err:
            ui.notify(f"Upload Error: {str(err)}", type='negative')

    def save_to_server():
        if not state['data'] or not state['is_valid']: return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_suffix = str(uuid.uuid4())[:6]  # Short random string
            new_filename = f"schedule_{timestamp}_{unique_suffix}.json"

            save_path = obs_utils.SCHEDULE_DIR / new_filename
            
            data_to_save = {"version": state['data'].get("version", "v1"), "schedule": []}
            for task in state['data']["schedule"]:
                clean = task.copy()
                for k in ['ant_display', 'backend_display', '_ui_id', '_status', '_msg', '_class']: 
                    if k in clean: del clean[k]
                data_to_save["schedule"].append(clean)

            with open(save_path, 'w') as f: json.dump(data_to_save, f, indent=4)
            
            ui.notify(f"Saved: {new_filename}", type='positive')
            status_card.classes('bg-blue-100', remove='bg-green-100')
            status_icon.name = 'cloud_done'
            status_icon.classes('text-blue-600', remove='text-green-500')
            status_label.set_text(f"SUBMITTED: {new_filename}")
            save_btn.disable()

        except Exception as e: ui.notify(f"Save Failed: {str(e)}", type='negative')

    def full_reset():
        state['data'] = None
        preview_table.rows = []
        upload_area.visible = True
        preview_container.visible = False

    # --- LAYOUT ---
    with ui.card().classes('w-full max-w-7xl mx-auto p-6 mt-10'):
        with ui.row().classes('w-full items-center mb-6'):
            ui.icon('cloud_upload', size='lg').classes('text-blue-600 mr-2')
            ui.markdown(f"### FAST Schedule Uploader `{VERSION}`")
        ui.separator().classes('mb-6')

        # 1. Upload Area
        with ui.column().classes('w-full items-center py-10 border-2 border-dashed border-gray-300 rounded-lg bg-gray-50') as upload_area:
            ui.icon('upload_file', size='4rem').classes('text-gray-400 mb-2')
            ui.label("Drag & Drop JSON Schedule").classes('text-lg text-gray-500 font-medium')
            ui.upload(on_upload=handle_upload, auto_upload=True, multiple=False).props('accept=.json flat color=primary')

        # 2. Preview Container
        with ui.column().classes('w-full gap-4') as preview_container:
            preview_container.visible = False
            
            with ui.row().classes('w-full p-4 rounded-lg items-center gap-4 border') as status_card:
                status_icon = ui.icon('', size='md')
                status_label = ui.label("").classes('font-bold text-lg')

            # Table
            columns = [
                {'name': 'status', 'label': 'Status / Error', 'field': '_msg', 'align': 'left', 'classes': 'font-bold'},
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start_time_cst', 'label': 'Start (CST)', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'ra', 'label': 'RA', 'field': 'ra', 'align': 'left'},
                {'name': 'dec', 'label': 'DEC', 'field': 'dec', 'align': 'left'},
                {'name': 'backends', 'label': 'Backends', 'field': 'backend_display', 'align': 'left'},
                {'name': 'antennas', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
                {'name': 'action', 'label': 'Action', 'field': 'action', 'align': 'center'},
            ]
            
            preview_table = ui.table(columns=columns, rows=[], row_key='_ui_id').classes('w-full')
            
            preview_table.add_slot('body', r'''
                <q-tr :props="props" :class="props.row._class">
                    <q-td key="status" :props="props">
                        <q-icon v-if="props.row._status == 'ERROR'" name="warning" color="negative" size="sm" class="mr-2" />
                        <q-icon v-if="props.row._status == 'OK'" name="check" color="positive" size="sm" class="mr-2" />
                        {{ props.row._msg }}
                    </q-td>
                    <q-td key="source" :props="props">{{ props.row.source }}</q-td>
                    <q-td key="start_time_cst" :props="props">{{ props.row.start_time_cst }}</q-td>
                    <q-td key="ra" :props="props">{{ props.row.ra }}</q-td>
                    <q-td key="dec" :props="props">{{ props.row.dec }}</q-td>
                    <q-td key="backends" :props="props">{{ props.row.backend_display }}</q-td>
                    <q-td key="antennas" :props="props">{{ props.row.ant_display }}</q-td>
                    <q-td key="action" :props="props">
                        <q-btn icon="delete" color="negative" flat dense round @click="$parent.$emit('delete', props.row._ui_id)" />
                    </q-td>
                </q-tr>
            ''')
            preview_table.on('delete', lambda e: delete_task(e.args))

            with ui.row().classes('w-full justify-end gap-4 mt-4'):
                ui.button('Upload New File', on_click=full_reset, icon='upload').props('outline color=grey')
                save_btn = ui.button('SUBMIT TO SERVER', on_click=save_to_server, icon='save').classes('bg-blue-600 text-white')
                save_btn.disable()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title='FAST Uploader', host='0.0.0.0', port=8081, reload=False)
