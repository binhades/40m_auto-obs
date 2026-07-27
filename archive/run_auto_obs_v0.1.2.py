#!/usr/bin/env python3
# ==================================================================
# FAST Core Array - Concurrent Python Driver
# Version: v0.1.2
# Updates: Thread-safe config, Zombie cleanup, Disk checks
# ==================================================================

import asyncio
import json
import os
import sys
import argparse
import signal
import subprocess
from datetime import datetime
from pathlib import Path

# --- IMPORT CONFIG ---
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

import obs_utils

# --- GLOBAL TRACKING (For Cleanup) ---
active_subprocesses = set()

# --- LOGGING SETUP ---
session_log_path = obs_utils.LOG_DIR / f"driver_session_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    with open(session_log_path, "a") as f:
        f.write(formatted + "\n")

# --- HELPER: REMOTE DISK CHECK ---
async def check_remote_disk(host, mount_point="/disk"):
    """Returns False if disk usage is > 95% or check fails."""
    try:
        # Run 'df --output=pcent /disk'
        cmd = ["ssh", "-o", "ConnectTimeout=3", f"{os.environ['USER']}@{host}", f"df --output=pcent {mount_point} | tail -1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        
        if proc.returncode != 0: return True # Assume OK if check fails to avoid blocking
        
        usage_str = stdout.decode().strip().replace('%', '')
        if int(usage_str) > 95:
            log(f"[CRITICAL] Disk full on {host}: {usage_str}%")
            return False
        return True
    except Exception as e:
        log(f"[WARNING] Could not check disk on {host}: {e}")
        return True

# --- ASYNC TASK RUNNER ---
class ObservationTask:
    def __init__(self, task_data, index, dry_run=False):
        self.data = task_data
        self.index = index
        self.dry_run = dry_run
        self.src = task_data['source']
        self.start_str = task_data['start_time_cst']
        self.ants = task_data['antennas']
        
        dt = datetime.strptime(self.start_str, "%Y-%m-%dT%H:%M:%S")
        self.start_ts = dt.timestamp()
        
        self.duration = obs_utils.parse_duration(task_data['duration'])
        self.end_ts = self.start_ts + self.duration

    async def run(self):
        try:
            log(f"--- Task {self.index}: {self.src} ({', '.join(self.ants)}) Started ---")
            
            # 1. Pre-Wait
            now = datetime.now().timestamp()
            wait_time = (self.start_ts - 20) - now
            if wait_time > 0:
                log(f"Task {self.index}: Waiting {wait_time:.1f}s for config window...")
                if not self.dry_run: await asyncio.sleep(wait_time)
            
            # 2. Disk Check & Config
            if not self.dry_run:
                # Check one host per task (simplification)
                target_host = obs_utils.ANTENNA_HOST_MAP.get(self.ants[0])
                if target_host and not await check_remote_disk(target_host):
                    log(f"Task {self.index} ABORTED: Disk Full on {target_host}")
                    return

                # CRITICAL: Run blocking config in a separate thread
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.configure_roach_sync)
            
            # 3. Launch Workers
            host_map = {}
            for ant in self.ants:
                h = obs_utils.ANTENNA_HOST_MAP.get(ant)
                if h: host_map.setdefault(h, []).append(ant)
            
            workers = []
            for host, ants_on_host in host_map.items():
                cmd = self.build_worker_cmd(host, " ".join(ants_on_host))
                if self.dry_run:
                    log(f"[DRY] Would launch on {host}: {cmd}")
                else:
                    # Launch and track process
                    proc = await asyncio.create_subprocess_exec(
                        "ssh", f"{os.environ['USER']}@{host}", cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    active_subprocesses.add(proc)
                    workers.append(proc)
                    log(f"Task {self.index}: Launched worker on {host} for {ants_on_host}")

            # 4. Wait for Trigger
            now = datetime.now().timestamp()
            wait_trigger = (self.start_ts - 5) - now
            if wait_trigger > 0:
                if not self.dry_run: await asyncio.sleep(wait_trigger)
            
            log(f"Task {self.index}: Firing Trigger...")
            if not self.dry_run: await self.fire_trigger()

            # 5. Wait for Completion
            now = datetime.now().timestamp()
            remaining = self.end_ts - now
            if remaining > 0:
                log(f"Task {self.index}: Observing... ({remaining:.1f}s remaining)")
                if not self.dry_run: await asyncio.sleep(remaining)
            
            # Cleanup tracked processes
            for p in workers:
                if p in active_subprocesses: active_subprocesses.remove(p)
            
            log(f"Task {self.index}: Finished.")

        except asyncio.CancelledError:
            log(f"Task {self.index}: CANCELLED.")
            raise
        except Exception as e:
            log(f"Task {self.index} ERROR: {e}")

    def configure_roach_sync(self):
        """Blocking configuration code goes here. Runs in thread pool."""
        # e.g., socket.connect(...)
        # time.sleep(1) # Simulating config time
        pass 

    async def fire_trigger(self):
        roach_list = [obs_utils.ROACH_HOST_MAP[a] for a in self.ants]
        cmd = [str(obs_utils.TRIGGER_SCRIPT), "-t", self.start_str, "-r"] + roach_list
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            # Add timeout to prevent hang if trigger script freezes
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if stdout: log(f"Trigger: {stdout.decode().strip()}")
            if stderr: log(f"Trigger Err: {stderr.decode().strip()}")
        except asyncio.TimeoutError:
            log(f"Task {self.index} TRIGGER TIMEOUT! Killing trigger script.")
            proc.kill()

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
async def main_loop(json_file, dry_run=False):
    with open(json_file) as f:
        data = json.load(f)
    
    tasks = [ObservationTask(t, i+1, dry_run) for i, t in enumerate(data['schedule'])]
    tasks.sort(key=lambda x: x.start_ts)
    
    active_coroutines = []
    log(f"Loaded {len(tasks)} tasks. Engine Start{' (DRY RUN)' if dry_run else ''}.")
    
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
    log("🛑 ABORT SIGNAL. Terminating child processes...")
    for proc in list(active_subprocesses):
        try:
            proc.terminate()
            log(f"Killed subprocess PID {proc.pid}")
        except Exception: pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    
    parser = argparse.ArgumentParser()
    parser.add_argument("schedule_file")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without executing")
    args = parser.parse_args()
    
    # Optional: Check System Clock
    try:
        subprocess.check_call(["timedatectl", "status"], stdout=subprocess.DEVNULL)
    except:
        log("WARNING: Could not verify system clock sync!")

    asyncio.run(main_loop(args.schedule_file, args.dry_run))
