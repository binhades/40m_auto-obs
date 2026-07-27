#!/usr/bin/env python3
from nicegui import ui, app
import json
from datetime import datetime
from pathlib import Path
import sys

# --- 1. ROBUST IMPORT ---
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

import obs_utils

# --- CONFIGURATION ---
VERSION = "v0.1.7 (Wide & Detailed)"

# --- UI ENTRY POINT ---
@ui.page('/')
def main_page():
    
    # Isolated State
    state = {
        'data': None,
        'filename': None
    }

    # References
    upload_area = None
    preview_container = None
    status_card = None
    status_icon = None
    status_label = None
    preview_table = None
    save_btn = None

    # --- HANDLERS ---
    def reset_state():
        state['data'] = None
        state['filename'] = None
        
        status_label.set_text("")
        status_card.classes(remove='bg-green-100 bg-orange-100 bg-red-100')
        status_icon.name = ''
        save_btn.disable()
        preview_table.rows = []
        
        # Visibility Reset
        upload_area.visible = True
        preview_container.visible = False

    async def handle_upload(e):
        try:
            content = ""
            filename = "unknown.json"

            # --- FILE READING LOGIC ---
            if hasattr(e, 'file'):
                f = e.file
                filename = getattr(f, 'name', 'unknown.json')
                if hasattr(f, 'read'):
                    file_content = f.read()
                    if hasattr(file_content, '__await__'):
                        file_bytes = await file_content
                    else:
                        file_bytes = file_content
                    content = file_bytes.decode('utf-8')
                elif hasattr(f, '_data'):
                    content = f._data.decode('utf-8')
            elif hasattr(e, 'content'):
                filename = getattr(e, 'name', 'unknown.json')
                content = e.content.read().decode('utf-8')
            else:
                raise ValueError("Could not locate file content.")

            state['filename'] = filename
            data = json.loads(content)
            
            # --- VALIDATION ---
            is_valid, message = obs_utils.verify_schedule(data)
            
            # --- UPDATE UI ---
            upload_area.visible = False
            preview_container.visible = True 
            
            if is_valid:
                # --- DATA ENRICHMENT FOR DISPLAY ---
                # We create formatted strings for the table columns
                for task in data["schedule"]:
                    
                    # 1. Format Antennas (List -> String)
                    if 'antennas' in task and isinstance(task['antennas'], list):
                        task['ant_display'] = ", ".join(task['antennas'])
                    else:
                        task['ant_display'] = "None"

                    # 2. Format Backend Details
                    details = []
                    if task.get('baseband_enabled'):
                        details.append("BB")
                    
                    if task.get('spec_enabled'):
                        # e.g., "Spec(F, 1.0s)"
                        mode = task.get('spec_mode', '?')
                        integ = task.get('spec_integ', '?')
                        details.append(f"Spec({mode}, {integ})")
                    
                    if task.get('psr_enabled'):
                        # e.g., "PSR(Stokes, 50us)"
                        mode = task.get('psr_mode', '?')
                        integ = task.get('psr_integ', '?')
                        details.append(f"PSR({mode}, {integ})")
                    
                    if not details:
                        task['backend_display'] = "-"
                    else:
                        task['backend_display'] = " + ".join(details)

                state['data'] = data
                
                # Status Banner
                if "Warnings" in message:
                    status_card.classes('bg-orange-100', remove='bg-green-100 bg-red-100')
                    status_icon.name = 'warning'
                    status_icon.classes('text-orange-600')
                    status_label.set_text(f"WARNING: {message}")
                else:
                    status_card.classes('bg-green-100', remove='bg-orange-100 bg-red-100')
                    status_icon.name = 'check_circle'
                    status_icon.classes('text-green-600')
                    status_label.set_text("VALID: Ready to Submit.")
                
                preview_table.rows = data["schedule"]
                save_btn.enable()
            else:
                state['data'] = None
                status_card.classes('bg-red-100', remove='bg-green-100 bg-orange-100')
                status_icon.name = 'error'
                status_icon.classes('text-red-600')
                status_label.set_text(f"INVALID: {message}")
                preview_table.rows = [] 
                save_btn.disable()
                
        except json.JSONDecodeError:
            ui.notify("Error: File is not valid JSON", type='negative')
            reset_state()
        except Exception as err:
            ui.notify(f"System Error: {str(err)}", type='negative')
            print(f"ERROR: {err}")
            reset_state()

    def save_to_server():
        if not state['data']: return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_filename = f"schedule_{timestamp}.json"
            save_path = obs_utils.SCHEDULE_DIR / new_filename
            
            # Clean data before saving (remove display helper fields)
            data_to_save = {"version": state['data'].get("version", "v1"), "schedule": []}
            for task in state['data']["schedule"]:
                clean_task = task.copy()
                # Remove the temporary display fields we added
                for field in ['ant_display', 'backend_display']:
                    if field in clean_task: del clean_task[field]
                data_to_save["schedule"].append(clean_task)

            with open(save_path, 'w') as f:
                json.dump(data_to_save, f, indent=4)
                
            ui.notify(f"Saved as: {new_filename}", type='positive')
            reset_state()
        except Exception as e:
            ui.notify(f"Save Failed: {str(e)}", type='negative')

    # --- LAYOUT ---
    # CHANGED: max-w-4xl -> max-w-7xl for wider view
    with ui.card().classes('w-full max-w-7xl mx-auto p-6 mt-10'):
        
        with ui.row().classes('w-full items-center mb-6'):
            ui.icon('cloud_upload', size='lg').classes('text-blue-600 mr-2')
            ui.markdown(f"### FAST Schedule Uploader `{VERSION}`")
        
        ui.separator().classes('mb-6')

        # Drop Zone
        with ui.column().classes('w-full items-center py-10 border-2 border-dashed border-gray-300 rounded-lg bg-gray-50') as upload_area:
            ui.icon('upload_file', size='4rem').classes('text-gray-400 mb-2')
            ui.label("Drag & Drop JSON Schedule").classes('text-lg text-gray-500 font-medium')
            ui.upload(on_upload=handle_upload, auto_upload=True, multiple=False).props('accept=.json flat color=primary')

        # Preview Zone
        with ui.column().classes('w-full gap-4') as preview_container:
            preview_container.visible = False
            
            with ui.row().classes('w-full p-4 rounded-lg items-center gap-4 border') as status_card:
                status_icon = ui.icon('', size='md')
                status_label = ui.label("").classes('font-bold text-lg')

            # CHANGED: Added Antennas and Backend details
            columns = [
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start_time_cst', 'label': 'Start', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'duration', 'label': 'Dur', 'field': 'duration', 'align': 'left'},
                {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
                {'name': 'antennas', 'label': 'Antennas', 'field': 'ant_display', 'align': 'left'},
                {'name': 'backend', 'label': 'Backend Config', 'field': 'backend_display', 'align': 'left', 'style': 'min-width: 200px'},
            ]
            preview_table = ui.table(columns=columns, rows=[], row_key='source').classes('w-full')

            with ui.row().classes('w-full justify-end gap-4 mt-4'):
                ui.button('Reset', on_click=reset_state, icon='refresh').props('outline color=grey')
                save_btn = ui.button('SUBMIT TO SERVER', on_click=save_to_server, icon='save').classes('bg-blue-600 text-white')
                save_btn.disable()

    with ui.row().classes('w-full justify-center mt-8 text-gray-400 text-sm'):
        ui.label("Note: Submitted schedules are queued for operator review.")

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title='FAST Uploader', host='0.0.0.0', port=8081, reload=False)
