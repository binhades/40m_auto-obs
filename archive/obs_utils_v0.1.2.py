import json
import re
from pathlib import Path
from datetime import datetime

# --- CONFIGURATION ---
USER_HOME = Path.home()
SCHEDULE_DIR = USER_HOME / "schedule"
SCRIPT_DIR = USER_HOME / "observe"

# Valid values
VALID_MODES = ["Tracking", "Cross-Scan", "Drift-Scan", "OTF", "Position", "Test"]
VALID_RECEIVERS = ["A19", "L-Band", "W-Band"] 
VALID_SPEC_MODES = ["F", "W,N"]
VALID_PSR_MODES = ["2-Pols", "Stokes"]
ACTIVE_ANTENNAS = ["CA01", "CA02", "CA03"]

# Regex
RA_PATTERN = re.compile(r'^\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')
DEC_PATTERN = re.compile(r'^[+-]?\d{1,2}:\d{1,2}:\d{1,2}(\.\d+)?$')

# Mandatory Top-Level Fields
REQUIRED_FIELDS = [
    "project_id", "observer", "source", "ra", "dec", 
    "receiver", "mode", "start_time_cst", "duration", "antennas"
]

SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)

def verify_schedule(data):
    warnings = []
    try:
        if "version" not in data: return False, "Missing 'version' field."
        if "schedule" not in data: return False, "Missing 'schedule'."
        if not isinstance(data["schedule"], list): return False, "'schedule' must be a list."
        if not data["schedule"]: return False, "Schedule list is empty."

        prev_end_time = None
        
        for idx, task in enumerate(data["schedule"]):
            t_num = f"Task {idx+1}"

            # 1. Check Mandatory Fields
            for field in REQUIRED_FIELDS:
                if field not in task or str(task[field]).strip() == "":
                    return False, f"{t_num}: Missing mandatory field '{field}'."

            # 2. Validate Regex & Lists
            if not RA_PATTERN.match(str(task["ra"]).strip()): return False, f"{t_num}: Invalid RA."
            if not DEC_PATTERN.match(str(task["dec"]).strip()): return False, f"{t_num}: Invalid Dec."
            
            if not task["antennas"]: return False, f"{t_num}: Antenna list empty."
            for ant in task["antennas"]:
                if ant not in ACTIVE_ANTENNAS: return False, f"{t_num}: Invalid Antenna {ant}."

            if task["mode"] not in VALID_MODES: return False, f"{t_num}: Invalid Mode."
            if task["receiver"] not in VALID_RECEIVERS: return False, f"{t_num}: Invalid Receiver."

            # 3. Backend Logic (STRICTER NOW)
            bb_en = task.get("baseband_enabled", False)
            spec_en = task.get("spec_enabled", False)
            psr_en = task.get("psr_enabled", False)

            if bb_en:
                if spec_en or psr_en: return False, f"{t_num}: Baseband is exclusive."
            else:
                if not spec_en and not psr_en: return False, f"{t_num}: No backend enabled."

            # Strict Sub-Mode Checking (No defaults allowed!)
            if spec_en:
                if "spec_mode" not in task: return False, f"{t_num}: Missing 'spec_mode'."
                if "spec_integ" not in task: return False, f"{t_num}: Missing 'spec_integ'."
                if task["spec_mode"] not in VALID_SPEC_MODES: return False, f"{t_num}: Invalid Spec Mode."
            
            if psr_en:
                if "psr_mode" not in task: return False, f"{t_num}: Missing 'psr_mode'."
                if "psr_integ" not in task: return False, f"{t_num}: Missing 'psr_integ'."
                if task["psr_mode"] not in VALID_PSR_MODES: return False, f"{t_num}: Invalid PSR Mode."

            # 4. Chronology
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

            # 5. Warnings
            if (start_dt.timestamp() - datetime.now().timestamp()) < -300: 
                warnings.append(f"{t_num} starts in the past.")

        msg = "Valid"
        if warnings: msg += f" (Warnings: {'; '.join(warnings)})"
        return True, msg

    except Exception as e:
        return False, f"JSON Error: {str(e)}"
