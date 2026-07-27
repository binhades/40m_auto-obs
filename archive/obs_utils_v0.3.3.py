import json
import re
from pathlib import Path
from datetime import datetime

# --- VERSION CONTROL ---
UTILS_VERSION = "v0.3.3"
DATA_VERSION = "v0.1.0"

# --- 1. GLOBAL PATHS ---
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
LOG_DIR = USER_HOME / "log"
SCRIPT_DIR = USER_HOME / "scripts"

# !!! CRITICAL CHANGE: Pointing to Python Driver !!!
DRIVER_SCRIPT = USER_HOME / "observe/run_auto_obs.py"
WORKER_SCRIPT = SCRIPT_DIR / "run_data_recorder.sh"
TRIGGER_SCRIPT = USER_HOME / "observe/roach_trigger.py"

# Log Files
DRIVER_LOG_FILE = LOG_DIR / "driver_master.log"

SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- 2. HARDWARE DEFINITIONS ---
ACTIVE_ANTENNAS = ["CA01", "CA02", "CA03"]
FUTURE_ANTENNAS = ["CA04", "CA05", "CA06", "CA07"]
BACKEND_TYPES = ["spec", "psr", "bb"]

# Map: Antenna -> Linux Hostname (for SSH)
ANTENNA_HOST_MAP = {
    "CA01": "a01",
    "CA02": "a01",
    "CA03": "a02",
    "CA04": "a02"
}

# Map: Antenna -> ROACH Hostname (for KATCP Triggering)
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
RA_PATTERN = re.compile(r'^\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')
DEC_PATTERN = re.compile(r'^[+-]?\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')

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
    try:
        if "version" not in data: return False, "Missing 'version' field."
        file_ver = data["version"]
        if get_major_minor(DATA_VERSION) != get_major_minor(file_ver):
            return False, f"Version Mismatch! Expected {get_major_minor(DATA_VERSION)}.x, got {file_ver}"

        if "schedule" not in data: return False, "Missing 'schedule'."
        
        validated_tasks = []
        
        for idx, task in enumerate(data["schedule"]):
            t_num = f"Task {idx+1}"

            for field in REQUIRED_FIELDS:
                if field not in task or str(task[field]).strip() == "":
                    return False, f"{t_num}: Missing '{field}'."

            if not task["antennas"]: return False, f"{t_num}: Antenna list empty."
            for ant in task["antennas"]:
                if ant not in ACTIVE_ANTENNAS: return False, f"{t_num}: Invalid Antenna {ant}."

            # Time Parsing
            try:
                start_dt = datetime.strptime(task["start_time_cst"], "%Y-%m-%dT%H:%M:%S")
            except ValueError: return False, f"{t_num}: Invalid Date Format."

            duration = parse_duration(task["duration"])
            end_ts = start_dt.timestamp() + duration
            start_ts = start_dt.timestamp()

            if start_ts < datetime.now().timestamp():
                return False, f"{t_num} starts in the past."

            validated_tasks.append({
                "id": idx,
                "start": start_ts,
                "end": end_ts,
                "ants": set(task["antennas"])
            })

        # Resource Collision Check
        for i in range(len(validated_tasks)):
            for j in range(i + 1, len(validated_tasks)):
                t1 = validated_tasks[i]
                t2 = validated_tasks[j]
                if (t1['start'] < t2['end']) and (t2['start'] < t1['end']):
                    intersection = t1['ants'].intersection(t2['ants'])
                    if intersection:
                        return False, f"Resource Conflict! Tasks {i+1} and {j+1} overlap on {intersection}."

        msg = "Valid"
        if warnings: msg += f" (Warnings: {'; '.join(warnings)})"
        return True, msg

    except Exception as e:
        return False, f"JSON Error: {str(e)}"
