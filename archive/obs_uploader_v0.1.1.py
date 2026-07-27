#!/usr/bin/env python3
from nicegui import ui, app
import json
from datetime import datetime
from pathlib import Path

# Import your shared validation logic
import obs_utils

# --- CONFIGURATION ---
VERSION = "v0.1.1"

# --- STATE MANAGEMENT ---
current_data = None
current_filename = None

def reset_state():
    """Clears the current upload and resets the UI."""
    global current_data, current_filename
    current_data = None
    current_filename = None
    
    # Clear UI elements
    status_label.set_text("")
    status_card.classes(remove='bg-green-100 bg-orange-100 bg-red-100')
    status_icon.name = ''
    save_btn.disable()
    preview_table.rows = []
    upload_area.visible = True
    preview_container.visible = False

def handle_upload(e):
    """Parses the uploaded file and runs validation."""
    global current_data, current_filename
    
    try:
        # 1. Parse JSON
        content = e.content.read().decode('utf-8')
        data = json.loads(content)
        current_filename = e.name
        
        # 2. Run Strict Validation (Shared Logic)
        is_valid, message = obs_utils.verify_schedule(data)
        
        # 3. Update UI based on result
        upload_area.visible = False
        preview_container.visible = True
        
        if is_valid:
            # --- FIX: Pre-calculate the 'backend_type' string here ---
            # This avoids sending a lambda function to the UI, which caused the crash.
            for task in data["schedule"]:
                if task.get('baseband_enabled'):
                    task['backend_type'] = "BB"
                elif task.get('spec_enabled'):
                    task['backend_type'] = "Spec"
                elif task.get('psr_enabled'):
                    task['backend_type'] = "PSR"
                else:
                    task['backend_type'] = "-"

            current_data = data
            
            # Check for warnings
            if "Warnings" in message:
                status_card.classes('bg-orange-100', remove='bg-green-100 bg-red-100')
                status_icon.name = 'warning'
                status_icon.classes('text-orange-600')
                status_label.set_text(f"VALIDATED WITH WARNINGS: {message}")
            else:
                status_card.classes('bg-green-100', remove='bg-orange-100 bg-red-100')
                status_icon.name = 'check_circle'
                status_icon.classes('text-green-600')
                status_label.set_text("VALIDATION SUCCESSFUL: Ready to Submit.")
            
            # Populate Preview Table
            preview_table.rows = data["schedule"]
            save_btn.enable()
            
        else:
            # Invalid
            current_data = None 
            status_card.classes('bg-red-100', remove='bg-green-100 bg-orange-100')
            status_icon.name = 'error'
            status_icon.classes('text-red-600')
            status_label.set_text(f"VALIDATION FAILED: {message}")
            preview_table.rows = [] 
            save_btn.disable()
            
    except json.JSONDecodeError:
        ui.notify("Error: File is not valid JSON", type='negative')
        reset_state()
    except Exception as err:
        ui.notify(f"Unexpected Error: {str(err)}", type='negative')
        reset_state()

def save_to_server():
    """Renames and saves the valid data to the shared directory."""
    if not current_data: return
    
    try:
        # Generate standardized filename: schedule_YYYYMMDD_HHMMSS.json
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_filename = f"schedule_{timestamp}.json"
        save_path = obs_utils.SCHEDULE_DIR / new_filename
        
        # Write to disk (We remove our helper field 'backend_type' before saving to keep it clean)
        data_to_save = {"version": current_data.get("version", "v1"), "schedule": []}
        for task in current_data["schedule"]:
            clean_task = task.copy()
            if 'backend_type' in clean_task: del clean_task['backend_type']
            data_to_save["schedule"].append(clean_task)

        with open(save_path, 'w') as f:
            json.dump(data_to_save, f, indent=4)
            
        ui.notify(f"Success! Schedule queued as: {new_filename}", type='positive', close_button=True)
        reset_state()
        
    except Exception as e:
        ui.notify(f"Save Failed: {str(e)}", type='negative')

# --- UI LAYOUT ---
@ui.page('/')
def main_page():
    
    # Global containers
    global upload_area, preview_container, status_card, status_icon, status_label, preview_table, save_btn

    with ui.card().classes('w-full max-w-4xl mx-auto p-6 mt-10'):
        
        # Header
        with ui.row().classes('w-full items-center mb-6'):
            ui.icon('cloud_upload', size='lg').classes('text-blue-600 mr-2')
            ui.markdown(f"### FAST Schedule Uploader `{VERSION}`")
        
        ui.separator().classes('mb-6')

        # --- STATE A: Upload Area ---
        with ui.column().classes('w-full items-center py-10 border-2 border-dashed border-gray-300 rounded-lg bg-gray-50') as upload_area:
            ui.icon('upload_file', size='4rem').classes('text-gray-400 mb-2')
            ui.label("Drag & Drop your Schedule JSON here").classes('text-lg text-gray-500 font-medium')
            ui.upload(on_upload=handle_upload, auto_upload=True, multiple=False).props('accept=.json flat color=primary')

        # --- STATE B: Preview Container (Hidden initially) ---
        with ui.column().classes('w-full gap-4 hidden') as preview_container:
            
            # Status Banner
            with ui.row().classes('w-full p-4 rounded-lg items-center gap-4 border') as status_card:
                status_icon = ui.icon('', size='md')
                status_label = ui.label("").classes('font-bold text-lg')

            # Task Preview Table
            ui.label("Schedule Preview").classes('text-gray-500 font-bold mt-2')
            
            columns = [
                {'name': 'source', 'label': 'Source', 'field': 'source', 'align': 'left'},
                {'name': 'start_time_cst', 'label': 'Start (CST)', 'field': 'start_time_cst', 'align': 'left'},
                {'name': 'duration', 'label': 'Dur', 'field': 'duration', 'align': 'left'},
                {'name': 'mode', 'label': 'Mode', 'field': 'mode', 'align': 'left'},
                # --- FIX: Use the simple string key we created in handle_upload ---
                {'name': 'backend', 'label': 'Backend', 'field': 'backend_type', 'align': 'left'},
            ]
            preview_table = ui.table(columns=columns, rows=[], row_key='source').classes('w-full')

            # --- STATE C: Action Buttons ---
            with ui.row().classes('w-full justify-end gap-4 mt-4'):
                ui.button('Discard / Reset', on_click=reset_state, icon='refresh').props('outline color=grey')
                save_btn = ui.button('SUBMIT TO SERVER', on_click=save_to_server, icon='save').classes('bg-blue-600 text-white')
                save_btn.disable()

    # Footer
    with ui.row().classes('w-full justify-center mt-8 text-gray-400 text-sm'):
        ui.label("Note: Submitted schedules are queued for operator review.")

# Start the Public Server
ui.run(title='FAST Uploader', host='0.0.0.0', port=8081, reload=False)
