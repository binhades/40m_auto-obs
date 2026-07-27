#!/usr/bin/env python3
from nicegui import ui, app
import json
from datetime import datetime
from pathlib import Path
import sys

# Import shared logic
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path: sys.path.append(str(current_dir))
import obs_utils

VERSION = "v0.1.8 (Scheduler Match)"

@ui.page('/')
def main_page():
    
    state = {'data': None, 'filename': None}

    # UI Refs
    upload_area, preview_container, status_card = None, None, None
    status_icon, status_label, preview_table, save_btn = None, None, None, None

    def reset_state():
        state['data'] = None; state['filename'] = None
        status_label.set_text("")
        status_card.classes(remove='bg-green-100 bg-orange-100 bg-red-100')
        status_icon.name = ''
        save_btn.disable()
        preview_table.rows = []
        upload_area.visible = True
        preview_container.visible = False

    def delete_task(row_id):
        """Removes a task from the preview list."""
        if not state['data']: return
        # Filter out the deleted task
        state['data']["schedule"] = [t for t in state['data']["schedule"] if t.get('source') != row_id] # Using source as ID for now
        
        # Update Table
        preview_table.rows = state['data']["schedule"]
        ui.notify("Task removed", type='info')
        
        # Disable save if empty
        if not state['data']["schedule"]:
            save_btn.disable()
            status_label.set_text("Schedule is empty.")
            status_card.classes('bg-orange-100', remove='bg-green-100')

    async def handle_upload(e):
        try:
            # File Reading Logic (Same as before)
            content = ""
            filename = "unknown.json"
            if hasattr(e, 'file'):
                f = e.file
                filename = getattr(f, 'name', 'unknown.json')
                if hasattr(f, 'read'):
                    file_content = f.read()
                    file_bytes = await file_content if hasattr(file_content, '__await__') else file_content
                    content = file_bytes.decode('utf-8')
                elif hasattr(f, '_data'): content = f._data.decode('utf-8')
            elif hasattr(e, 'content'):
                filename = getattr(e, 'name', 'unknown.json')
                content = e.content.read().decode('utf-8')
            
            state['filename'] = filename
            data = json.loads(content)
            
            is_valid, message = obs_utils.verify_schedule(data)
            
            upload_area.visible = False
            preview_container.visible = True
            
            if is_valid:
                # Format Data for Table to Match Scheduler
                for task in data["schedule"]:
                    # Antennas
                    task['ant_display'] = ", ".join(task.get('antennas', []))
                    
                    # Backends (Exact Format: "Spec(0.1s, W,N) + PSR(...)")
                    modes = []
                    if task.get('baseband_enabled'): modes.append("Baseband")
                    if task.get('spec_enabled'):
                        modes.append(f"Spec({task.get('spec_integ')}, {task.get('spec_mode')})")
                    if task.get('psr_enabled'):
                        modes.append(f"PSR({task.get('psr_integ')}, {task.get('psr_mode')})")
                    
                    task['backend_display'] = " + ".join(modes) if modes else "-"

                state['data'] = data
                
                if "Warnings" in message:
                    status_card.classes('bg-orange-100', remove='bg-green-100 bg-red-100')
                    status_icon.name = 'warning'; status_icon.classes('text-orange-600')
                    status_label.set_text(f"WARNING: {message}")
                else:
                    status_card.classes('bg-green-100', remove='bg-orange-100 bg-red-100')
                    status_icon.name = 'check_circle'; status_icon.classes('text-green-600')
                    status_label.set_text("Valid Schedule")
                
                preview_table.rows = data["schedule"]
                save_btn.enable()
            else:
                state['data'] = None
                status_card.classes('bg-red-100', remove='bg-green-100 bg-orange-100')
                status_icon.name = 'error'; status_icon.classes('text-red-600')
                status_label.set_text(f"INVALID: {message}")
                preview_table.rows = []
                save_btn.disable()
                
        except Exception as err:
            ui.notify(f"Error: {str(err)}", type='negative'); reset_state()

    def save_to_server():
        if not state['data'] or not state['data']["schedule"]: return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_filename = f"schedule_{timestamp}.json"
            save_path = obs_utils.SCHEDULE_DIR / new_filename
            
            # Clean display fields
            data_to_save = {"version": state['data'].get("version", "v1"), "schedule": []}
            for task in state['data']["schedule"]:
                clean = task.copy()
                for k in ['ant_display', 'backend_display']: 
                    if k in clean: del clean[k]
                data_to_save["schedule"].append(clean)

            with open(save_path, 'w') as f: json.dump(data_to_save, f, indent=4)
            ui.notify(f"Saved: {new_filename}", type='positive'); reset_state()
        except Exception as e: ui.notify(f"Save Failed: {str(e)}", type='negative')

    # --- LAYOUT ---
    with ui.card().classes('w-full max-w-7xl mx-auto p-6 mt-10'):
        with ui.row().classes('w-full items-center mb-6'):
            ui.icon('cloud_upload', size='lg').classes('text-blue-600 mr-2')
            ui.markdown(f"### FAST Schedule Uploader `{VERSION}`")
        ui.separator().classes('mb-6')

        with ui.column().classes('w-full items-center py-10 border-2 border-dashed border-gray-300 rounded-lg bg-gray-50') as upload_area:
            ui.icon('upload_file', size='4rem').classes('text-gray-400 mb-2')
            ui.label("Drag & Drop JSON Schedule").classes('text-lg text-gray-500 font-medium')
            ui.upload(on_upload=handle_upload, auto_upload=True, multiple=False).props('accept=.json flat color=primary')

        with ui.column().classes('w-full gap-4') as preview_container:
            preview_container.visible = False
            
            with ui.row().classes('w-full p-4 rounded-lg items-center gap-4 border') as status_card:
                status_icon = ui.icon('', size='md')
                status_label = ui.label("").classes('font-bold text-lg')

            # --- TABLE DEFINITION (Matches Scheduler) ---
            columns = [
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start_time_cst', 'label': 'Start (CST)', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'duration', 'label': 'Len', 'field': 'duration', 'align': 'left'},
                {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
                {'name': 'backends', 'label': 'Backends', 'field': 'backend_display', 'align': 'left', 'style': 'min-width: 250px'},
                {'name': 'antennas', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
                {'name': 'action', 'label': 'Action', 'field': 'action', 'align': 'center'},
            ]
            preview_table = ui.table(columns=columns, rows=[], row_key='source').classes('w-full')
            
            # Add Delete Button Slot
            preview_table.add_slot('body-cell-action', r'''
                <q-td :props="props">
                    <q-btn icon="delete" color="negative" flat dense round @click="$parent.$emit('delete', props.row.source)" />
                </q-td>
            ''')
            # Bind delete event
            preview_table.on('delete', lambda e: delete_task(e.args))

            with ui.row().classes('w-full justify-end gap-4 mt-4'):
                ui.button('Reset', on_click=reset_state, icon='refresh').props('outline color=grey')
                save_btn = ui.button('SUBMIT TO SERVER', on_click=save_to_server, icon='save').classes('bg-blue-600 text-white')
                save_btn.disable()

    with ui.row().classes('w-full justify-center mt-8 text-gray-400 text-sm'):
        ui.label("Note: Submitted schedules are queued for operator review.")

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title='FAST Uploader', host='0.0.0.0', port=8081, reload=False)
