#!/usr/bin/env python3
# ==================================================================
# FAST Core Array - Concurrent Python Driver
# Version: v0.1.1
# Updates: Unified Configuration via obs_utils
# ==================================================================

import asyncio
import json
import os
import sys
import argparse
import signal
from datetime import datetime
from pathlib import Path

# --- IMPORT CONFIG ---
# Dynamically find obs_utils
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

import obs_utils

# --- LOGGING SETUP ---
session_log_path = obs_utils.LOG_DIR / f"driver_session_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

def log(msg):
    """Prints to stdout (for Controller) and file (for History)."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)  # <--- FORCE FLUSH
    with open(session_log_path, "a") as f:
        f.write(formatted + "\n")

# --- ASYNC TASK RUNNER ---
class ObservationTask:
    def __init__(self, task_data, index):
        self.data = task_data
        self.index = index
        self.src = task_data['source']
        self.start_str = task_data['start_time_cst']
        self.ants = task_data['antennas']
        
        # Time Calculations
        dt = datetime.strptime(self.start_str, "%Y-%m-%dT%H:%M:%S")
        self.start_ts = dt.timestamp()
        
        self.duration = obs_utils.parse_duration(task_data['duration'])
        self.end_ts = self.start_ts + self.duration

    async def run(self):
        try:
            log(f"--- Task {self.index}: {self.src} ({', '.join(self.ants)}) Started Logic ---")
            
            # 1. Pre-Wait
            now = datetime.now().timestamp()
            wait_time = (self.start_ts - 20) - now
            if wait_time > 0:
                log(f"Task {self.index}: Waiting {wait_time:.1f}s for config window...")
                await asyncio.sleep(wait_time)
            
            # 2. Configure Hardware
            self.configure_roach()
            
            # 3. Launch Workers
            host_map = {}
            for ant in self.ants:
                h = obs_utils.ANTENNA_HOST_MAP.get(ant)
                if h: host_map.setdefault(h, []).append(ant)
            
            for host, ants_on_host in host_map.items():
                cmd = self.build_worker_cmd(host, " ".join(ants_on_host))
                await asyncio.create_subprocess_exec(
                    "ssh", f"{os.environ['USER']}@{host}", cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                log(f"Task {self.index}: Launched worker on {host} for {ants_on_host}")

            # 4. Wait for Trigger
            now = datetime.now().timestamp()
            wait_trigger = (self.start_ts - 5) - now
            if wait_trigger > 0:
                await asyncio.sleep(wait_trigger)
            
            log(f"Task {self.index}: Firing Trigger...")
            await self.fire_trigger()

            # 5. Wait for Completion
            now = datetime.now().timestamp()
            remaining = self.end_ts - now
            if remaining > 0:
                log(f"Task {self.index}: Observing... ({remaining:.1f}s remaining)")
                await asyncio.sleep(remaining)
            
            log(f"Task {self.index}: Finished.")

        except asyncio.CancelledError:
            log(f"Task {self.index}: ABORTED.")
            raise
        except Exception as e:
            log(f"Task {self.index} ERROR: {e}")

    def configure_roach(self):
        # Configuration logic here using obs_utils.ROACH_HOST_MAP
        pass 

    async def fire_trigger(self):
        roach_list = [obs_utils.ROACH_HOST_MAP[a] for a in self.ants]
        cmd = [str(obs_utils.TRIGGER_SCRIPT), "-t", self.start_str, "-r"] + roach_list
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if stdout: log(f"Trigger: {stdout.decode().strip()}")
        if stderr: log(f"Trigger Err: {stderr.decode().strip()}")

    def build_worker_cmd(self, host, ant_str):
        d = self.data
        backends = []
        if d.get('spec_enabled'): backends.append("Spec")
        if d.get('psr_enabled'): backends.append("PSR")
        if d.get('baseband_enabled'): backends.append("Baseband")
        
        cmd = f"{obs_utils.WORKER_SCRIPT} -s \"{d['source']}\" -t \"{self.start_str}\" -d {self.duration} -a \"{ant_str}\" -B \"{','.join(backends)},\""
        cmd += f" -p \"{d['project_id']}\" -o \"{d['observer']}\" -R \"{d['ra']}\" -D \"{d['dec']}\" -r \"{d['receiver']}\" -M \"{d['mode']}\""
        cmd += f" --psr_mode \"{d.get('psr_mode')}\" --spec_mode \"{d.get('spec_mode')}\""
        
        if d.get('spec_enabled'):
            integ = d.get('spec_integ')
            val = "4 20 48 48" if integ == "0.1s" else "4 20 480 80"
            cmd += f" --spec_params \"{val}\""
        
        if d.get('psr_enabled'):
            integ = d.get('psr_integ')
            val = {"50us": 5, "100us": 11, "200us": 23}.get(integ, 5)
            cmd += f" --psr_params \"{val}\""
            
        return cmd

# --- MAIN ENGINE ---
async def main_loop(json_file):
    with open(json_file) as f:
        data = json.load(f)
    
    tasks = [ObservationTask(t, i+1) for i, t in enumerate(data['schedule'])]
    tasks.sort(key=lambda x: x.start_ts)
    
    active_coroutines = []
    log(f"Loaded {len(tasks)} tasks. Engine Start.")
    
    while tasks or active_coroutines:
        now = datetime.now().timestamp()
        
        while tasks and (tasks[0].start_ts - now < 30):
            task = tasks.pop(0)
            if task.end_ts < now:
                log(f"Task {task.index} is in the past. Skipping.")
                continue
            co = asyncio.create_task(task.run())
            active_coroutines.append(co)
        
        active_coroutines = [c for c in active_coroutines if not c.done()]
        await asyncio.sleep(1)

def cleanup(sig, frame):
    log("🛑 ABORT SIGNAL. Killing all tasks.")
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    
    parser = argparse.ArgumentParser()
    parser.add_argument("schedule_file")
    args = parser.parse_args()
    
    asyncio.run(main_loop(args.schedule_file))
