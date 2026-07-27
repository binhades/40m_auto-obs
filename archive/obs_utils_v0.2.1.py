import json
import re
from pathlib import Path
from datetime import datetime

# --- VERSION CONTROL ---
# UTILS_VERSION: The version of this Python script
# DATA_VERSION:  The schedule JSON format version this script validates
UTILS_VERSION = "v0.2.1"
DATA_VERSION = "v0.1.0"

# --- 1. GLOBAL PATHS ---
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
LOG_DIR = USER_HOME / "log"
SCRIPT_DIR = USER_HOME / "scripts"
DRIVER_SCRIPT = USER_HOME / "observe/run_auto_obs.sh"

SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- 2. HARDWARE DEFINITIONS ---
ACTIVE_ANTENNAS = ["CA01", "CA02", "CA03"]
BACKEND_TYPES = ["spec", "psr", "bb"]

# CA01/CA02 -> a01, CA03 -> a02
ANTENNA_HOST_MAP = {
    "CA01": "a01",
    "CA02": "a01",
    "CA03": "a02",
    "CA04": "a02"
}

# --- 3. VALIDATION RULES ---
VALID_MODES = ["Tracking", "Cross-Scan", "Drift-Scan", "OTF", "Position", "Test"]
VALID_RECEIVERS = ["A19", "L-Band", "W-Band"] 

VALID_SPEC_MODES = ["F", "W,N"]
VALID_SPEC_INTEG = ["0.1s", "1.0s"]

VALID_PSR_MODES = ["2-Pols", "Stokes"]
VALID_PSR_INTEG = ["50us", "100us", "200us"]

# --- 4. REGEX PATTERNS ---
RA_PATTERN = re.compile(r'^\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')
DEC_PATTERN = re.compile(r'^[+-]?\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')

REQUIRED_FIELDS = [
    "project_id", "observer", "source", "ra", "dec", 
    "receiver", "mode", "start_time_cst", "duration", "antennas"
]

# --- 5. SHARED LOGIC ---
def get_major_minor(version_str):
    """Extracts '0.1' from 'v0.1.0' or '0.1'."""
    clean = version_str.lstrip('v')
    parts = clean.split('.')
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return clean

def verify_schedule(data):
    warnings = []
    try:
        # --- VERSION CHECK ---
        if "version" not in data: 
            return False, "Missing 'version' field."
        
        file_ver = data["version"]
        expected_mm = get_major_minor(DATA_VERSION)
        file_mm = get_major_minor(file_ver)
        
        if expected_mm != file_mm:
            return False, f"Version Mismatch! Expected format {expected_mm}.x, got {file_ver}"

        # --- STRUCTURE CHECK ---
        if "schedule" not in data: return False, "Missing 'schedule'."
        if not isinstance(data["schedule"], list): return False, "'schedule' must be a list."
        if not data["schedule"]: return False, "Schedule list is empty."

        prev_end_time = None
        
        for idx, task in enumerate(data["schedule"]):
            t_num = f"Task {idx+1}"

            for field in REQUIRED_FIELDS:
                if field not in task or str(task[field]).strip() == "":
                    return False, f"{t_num}: Missing mandatory field '{field}'."

            if not RA_PATTERN.match(str(task["ra"]).strip()): return False, f"{t_num}: Invalid RA format."
            if not DEC_PATTERN.match(str(task["dec"]).strip()): return False, f"{t_num}: Invalid Dec format."
            
            if not task["antennas"]: return False, f"{t_num}: Antenna list empty."
            for ant in task["antennas"]:
                if ant not in ACTIVE_ANTENNAS: return False, f"{t_num}: Invalid Antenna {ant}."

            if task["mode"] not in VALID_MODES: return False, f"{t_num}: Invalid Mode."
            if task["receiver"] not in VALID_RECEIVERS: return False, f"{t_num}: Invalid Receiver."

            bb_en = task.get("baseband_enabled", False)
            spec_en = task.get("spec_enabled", False)
            psr_en = task.get("psr_enabled", False)

            if bb_en:
                if spec_en or psr_en: return False, f"{t_num}: Baseband is exclusive."
            else:
                if not spec_en and not psr_en: return False, f"{t_num}: No backend enabled."

            if spec_en:
                if "spec_mode" not in task: return False, f"{t_num}: Missing 'spec_mode'."
                if task["spec_mode"] not in VALID_SPEC_MODES: return False, f"{t_num}: Invalid Spec Mode."
                if task["spec_integ"] not in VALID_SPEC_INTEG: 
                    return False, f"{t_num}: Invalid Spec Integ '{task['spec_integ']}'."
            
            if psr_en:
                if "psr_mode" not in task: return False, f"{t_num}: Missing 'psr_mode'."
                if task["psr_mode"] not in VALID_PSR_MODES: return False, f"{t_num}: Invalid PSR Mode."
                if task["psr_integ"] not in VALID_PSR_INTEG:
                    return False, f"{t_num}: Invalid PSR Integ '{task['psr_integ']}'."

            try:
                start_dt = datetime.strptime(task["start_time_cst"], "%Y-%m-%dT%H:%M:%S")
            except ValueError: return False, f"{t_num}: Invalid Date Format."

            dur_str = str(task["duration"])
            dur_val = int(dur_str[:-1])
            if dur_str.endswith('m'): dur_val *= 60
            elif dur_str.endswith('h'): dur_val *= 3600
            
            current_end = start_dt.timestamp() + dur_val
            if prev_end_time and start_dt.timestamp() < prev_end_time:
                 return False, f"{t_num}: Tasks overlap."
            prev_end_time = current_end

            # --- STRICT TIME CHECK ---
            now_ts = datetime.now().timestamp()
            if start_dt.timestamp() < now_ts: 
                return False, f"{t_num} starts in the past ({task['start_time_cst']})."

        msg = "Valid"
        if warnings: msg += f" (Warnings: {'; '.join(warnings)})"
        return True, msg

    except Exception as e:
        return False, f"JSON Error: {str(e)}"
