import json
import re
from pathlib import Path
from datetime import datetime

# --- 1. CONFIGURATION & CONSTANTS ---
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
SCRIPT_DIR = USER_HOME / "observe"

# Rule 3: Whitelists matched to obs_scheduler.py
VALID_MODES = [
    "Tracking", "Cross-Scan", "Drift-Scan", 
    "OTF", "Position", "Test"
]

VALID_RECEIVERS = ["A19", "L-Band", "W-Band"] 
VALID_SPEC_MODES = ["F", "W,N"]
VALID_PSR_MODES = ["2-Pols", "Stokes"]

# Hardware Limits
ACTIVE_ANTENNAS = ["CA01", "CA02", "CA03"]

# Regex Patterns for Coordinates
# RA:  00:00:00 to 23:59:59 (optional decimals)
# Dec: +00:00:00 to -90:00:00 (required +/- sign, optional decimals)
RA_PATTERN = re.compile(r'^\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')
DEC_PATTERN = re.compile(r'^[+-]?\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')

# Rule 2: Mandatory Fields
REQUIRED_FIELDS = [
    "project_id", "observer", "source", "ra", "dec", 
    "receiver", "mode", "start_time_cst", "duration", "antennas"
]

# Ensure directories exist
SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)

def get_file_list():
    """
    Returns a sorted list of validated JSON schedules from the shared directory.
    """
    files = sorted(SCHEDULE_DIR.glob("*.json"), reverse=True)
    rows = []
    for f in files:
        try:
            with open(f, 'r') as fp:
                data = json.load(fp)
                first = data["schedule"][0] if data["schedule"] else {}
                
            rows.append({
                "filename": f.name,
                "project": first.get("project_id", "N/A"),
                "tasks": len(data["schedule"]),
                "path": str(f)
            })
        except:
            continue
    return rows

def verify_schedule(data):
    """
    Validates a schedule JSON against strict operational rules.
    Returns: (is_valid, message_string)
    """
    warnings = []
    
    try:
        # --- Rule 1: File Structure ---
        if "version" not in data: return False, "Missing 'version' field."
        if "schedule" not in data: return False, "Missing 'schedule'."
        if not isinstance(data["schedule"], list): return False, "'schedule' must be a list."
        if not data["schedule"]: return False, "Schedule list is empty."

        prev_end_time = None
        
        for idx, task in enumerate(data["schedule"]):
            t_num = f"Task {idx+1}"

            # --- Rule 2: Mandatory Fields ---
            for field in REQUIRED_FIELDS:
                if field not in task or str(task[field]).strip() == "":
                    return False, f"{t_num}: Missing mandatory field '{field}'."

            # --- Rule 3: Hardware & Format Validation ---
            
            # Coordinate Checks
            if not RA_PATTERN.match(str(task["ra"]).strip()):
                return False, f"{t_num}: Invalid RA format '{task['ra']}'. Expected HH:MM:SS.s"
            
            if not DEC_PATTERN.match(str(task["dec"]).strip()):
                return False, f"{t_num}: Invalid Dec format '{task['dec']}'. Expected ±DD:MM:SS.s"

            # Antenna Checks
            task_ants = task["antennas"]
            if not isinstance(task_ants, list) or not task_ants:
                return False, f"{t_num}: Antenna list cannot be empty."
            
            for ant in task_ants:
                if ant not in ACTIVE_ANTENNAS:
                    return False, f"{t_num}: Invalid Antenna '{ant}'. Active: {ACTIVE_ANTENNAS}"

            # Mode & Receiver Checks
            if task["mode"] not in VALID_MODES:
                return False, f"{t_num}: Invalid Mode '{task['mode']}'."
            
            if task["receiver"] not in VALID_RECEIVERS:
                return False, f"{t_num}: Invalid Receiver '{task['receiver']}'."

            # --- Rule 4: Backend Exclusivity ---
            bb_en = task.get("baseband_enabled", False)
            spec_en = task.get("spec_enabled", False)
            psr_en = task.get("psr_enabled", False)

            if bb_en:
                if spec_en or psr_en:
                    return False, f"{t_num}: Exclusive Violation. Baseband cannot run with Spec or PSR."
            else:
                if not spec_en and not psr_en:
                    return False, f"{t_num}: No backend selected. Enable Spec, PSR, or Baseband."

            # Validate Backend Sub-Modes
            if spec_en:
                s_mode = task.get("spec_mode", "F")
                if s_mode not in VALID_SPEC_MODES:
                    return False, f"{t_num}: Invalid Spec Mode '{s_mode}'."
            
            if psr_en:
                p_mode = task.get("psr_mode", "2-Pols")
                if p_mode not in VALID_PSR_MODES:
                    return False, f"{t_num}: Invalid PSR Mode '{p_mode}'."

            # --- Rule 5: Chronology & Parsing ---
            try:
                start_dt = datetime.strptime(task["start_time_cst"], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return False, f"{t_num}: Invalid date format. Use YYYY-MM-DDTHH:MM:SS"

            dur_str = str(task["duration"])
            if not (dur_str.endswith('s') or dur_str.endswith('m') or dur_str.endswith('h')):
                 return False, f"{t_num}: Duration must end in s/m/h (e.g., '60s')."
            
            dur_val = int(dur_str[:-1])
            if dur_str.endswith('m'): dur_val *= 60
            if dur_str.endswith('h'): dur_val *= 3600
            
            current_end = start_dt.timestamp() + dur_val
            
            if prev_end_time and start_dt.timestamp() < prev_end_time:
                 return False, f"{t_num}: Start time overlaps with previous task end time."
            
            prev_end_time = current_end

            # --- Rule 6: Warning Checks ---
            time_diff = start_dt.timestamp() - datetime.now().timestamp()
            if time_diff < -300: 
                warnings.append(f"{t_num} starts in the past.")

        msg = "Valid"
        if warnings:
            msg += f" (Warnings: {'; '.join(warnings)})"
            
        return True, msg

    except Exception as e:
        return False, f"JSON Validation Error: {str(e)}"
