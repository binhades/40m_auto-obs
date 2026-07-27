import json
import re
from pathlib import Path
from datetime import datetime

# --- VERSION CONTROL ---
UTILS_VERSION = "v0.3.7"
DATA_VERSION = "v0.1.0"

# --- 1. GLOBAL PATHS ---
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
LOG_DIR = USER_HOME / "log"
SCRIPT_DIR = USER_HOME / "scripts"

# Script Locations
DRIVER_SCRIPT = USER_HOME / "observe/run_auto_obs.py"
WORKER_SCRIPT = SCRIPT_DIR / "run_data_recorder.sh"

# Log Files
DRIVER_LOG_FILE = LOG_DIR / "driver_master.log"

SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- 2. HARDWARE DEFINITIONS ---
ACTIVE_ANTENNAS = ["CA01", "CA02", "CA03"]
FUTURE_ANTENNAS = ["CA04", "CA05", "CA06", "CA07"]
BACKEND_TYPES = ["spec", "psr", "bb"]

# Map: Antenna -> Linux Hostname
ANTENNA_HOST_MAP = {
    "CA01": "a01",
    "CA02": "a01",
    "CA03": "a02",
    "CA04": "a02"
}

# Map: Antenna -> ROACH Hostname
ROACH_HOST_MAP = {
    "CA01": "r2170",
    "CA02": "r2171",
    "CA03": "r2172",
    "CA04": "r2173"
}

FPGA_CLK = 250000000 

# --- 3. VALIDATION RULES ---
VALID_MODES = ["Tracking", "Cross-Scan", "Drift-Scan", "OTF", "Position", "Test"]
VALID_RECEIVERS = ["A19", "L-Band", "W-Band"] 
VALID_SPEC_MODES = ["F", "W,N"]
VALID_SPEC_INTEG = ["0.1s", "1.0s"]
VALID_PSR_MODES = ["2-Pols", "Stokes"]
VALID_PSR_INTEG = ["50us", "100us", "200us"]

# --- 4. REGEX PATTERNS ---
RA_PATTERN = re.compile(r'^\d{1,2}:\d{1,2}:\d{1,2}(\.\d{1,3})?$')
DEC_PATTERN = re.compile(r'^[+-]?\d{1,2}:\d{1,2}:\d{1,2}(\.\d{1,3})?$')

REQUIRED_FIELDS = [
    "project_id", "observer", "source", "ra", "dec", 
    "receiver", "mode", "start_time_cst", "duration", "antennas"
]

# --- 5. SHARED LOGIC ---
def get_major_minor(version_str):
    clean = version_str.lstrip('v')
    parts = clean.split('.')
    if len(parts) >= 2: return f"{parts[0]}.{parts[1]}"
    return clean

def parse_duration(dur_str):
    dur_str = str(dur_str)
    val = int(dur_str[:-1])
    if dur_str.endswith('m'): val *= 60
    elif dur_str.endswith('h'): val *= 3600
    return val

def verify_schedule(data):
    warnings = []
    # NEW: Dictionary to aggregate all errors across the whole file
    errors = {} 

    def add_error(idx, msg):
        if idx not in errors: errors[idx] = []
        errors[idx].append(msg)

    try:
        if "version" not in data: 
            add_error('global', "Missing 'version' field.")
            return False, errors
            
        file_ver = data["version"]
        if get_major_minor(DATA_VERSION) != get_major_minor(file_ver):
            add_error('global', f"Version Mismatch! Expected {get_major_minor(DATA_VERSION)}.x, got {file_ver}")
            return False, errors

        if "schedule" not in data:
            add_error('global', "Missing 'schedule' array.")
            return False, errors
        
        validated_tasks = []
        
        # Check every single task, do not break early
        for idx, task in enumerate(data["schedule"]):
            
            for field in REQUIRED_FIELDS:
                if field not in task or str(task[field]).strip() == "":
                    add_error(idx, f"Missing '{field}'.")
            
            if idx in errors: continue # Skip deep logic if fields are entirely missing

            # NEW: Strip whitespace to prevent accidental trailing space errors
            ra_val = str(task["ra"]).strip()
            dec_val = str(task["dec"]).strip()

            if not RA_PATTERN.match(ra_val):
                add_error(idx, f"Invalid RA format '{ra_val}'. Max 3 decimal digits.")
            
            if not DEC_PATTERN.match(dec_val):
                add_error(idx, f"Invalid DEC format '{dec_val}'. Max 3 decimal digits.")

            if not task["antennas"]: 
                add_error(idx, "Antenna list empty.")
            else:
                for ant in task["antennas"]:
                    if ant not in ACTIVE_ANTENNAS: add_error(idx, f"Invalid Antenna {ant}.")

            # Backend Validation
            bb_en = task.get("baseband_enabled", False)
            spec_en = task.get("spec_enabled", False)
            psr_en = task.get("psr_enabled", False)

            if bb_en:
                if spec_en or psr_en: add_error(idx, "Baseband is exclusive.")
            else:
                if not spec_en and not psr_en: add_error(idx, "No backend enabled.")

            if spec_en:
                if "spec_mode" not in task: add_error(idx, "Missing 'spec_mode'.")
                elif task["spec_mode"] not in VALID_SPEC_MODES: add_error(idx, "Invalid Spec Mode.")
                if "spec_integ" not in task: add_error(idx, "Missing 'spec_integ'.")
                elif task["spec_integ"] not in VALID_SPEC_INTEG: add_error(idx, "Invalid Spec Integ.")
            
            if psr_en:
                if "psr_mode" not in task: add_error(idx, "Missing 'psr_mode'.")
                elif task["psr_mode"] not in VALID_PSR_MODES: add_error(idx, "Invalid PSR Mode.")
                if "psr_integ" not in task: add_error(idx, "Missing 'psr_integ'.")
                elif task["psr_integ"] not in VALID_PSR_INTEG: add_error(idx, "Invalid PSR Integ.")

            # Time Parsing
            try:
                start_dt = datetime.strptime(task["start_time_cst"], "%Y-%m-%dT%H:%M:%S")
                duration = parse_duration(task["duration"])
                end_ts = start_dt.timestamp() + duration
                start_ts = start_dt.timestamp()

                if start_ts < datetime.now().timestamp():
                    add_error(idx, "Starts in the past.")

                validated_tasks.append({
                    "id": idx,
                    "start": start_ts,
                    "end": end_ts,
                    "ants": set(task["antennas"])
                })
            except ValueError: 
                add_error(idx, "Invalid Date Format.")

        # Collision Check (Only run if the schedule forms are valid so far)
        if not errors: 
            for i in range(len(validated_tasks)):
                for j in range(i + 1, len(validated_tasks)):
                    t1 = validated_tasks[i]
                    t2 = validated_tasks[j]
                    if (t1['start'] < t2['end']) and (t2['start'] < t1['end']):
                        intersection = t1['ants'].intersection(t2['ants'])
                        if intersection:
                            add_error('global', f"Conflict! Tasks {t1['id']+1} and {t2['id']+1} overlap on {intersection}.")

        # Final Return Check
        if errors:
            return False, errors

        msg = "Valid"
        if warnings: msg += f" (Warnings: {'; '.join(warnings)})"
        return True, msg

    except Exception as e:
        return False, {"global": [f"JSON Error: {str(e)}"]}
