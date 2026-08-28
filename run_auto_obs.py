#!/usr/bin/env python3
# ==================================================================
# FAST Core Array - Concurrent Python Driver
# Version: v0.1.7
# Updates: Session log at ~/log/active_driver_session.log (old rotated to driver_session_<session start>.log); fatal errors logged to file
# ==================================================================

import asyncio
import json
import os
import sys
import argparse
import re
import signal
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

# --- IMPORT CONFIG & TOOLS ---
current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

import obs_utils
import roach_tools # <--- Pure Library

# --- GLOBAL TRACKING ---
active_subprocesses = set()
dry_run_mode = False
session_log_path = obs_utils.LOG_DIR / "active_driver_session.log"
if session_log_path.exists():
    # Archive name = when that session started (its first log line timestamp),
    # matching the sh driver convention; fall back to rotation time if unreadable.
    archive_ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    try:
        with open(session_log_path) as f:
            first_line = f.readline().strip()
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", first_line)
        if m:
            archive_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").strftime('%Y%m%d-%H%M%S')
    except Exception:
        pass
    os.rename(session_log_path, obs_utils.LOG_DIR / f"driver_session_{archive_ts}.log")

def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    with open(session_log_path, "a") as f:
        f.write(formatted + "\n")

# --- HELPER: REMOTE DISK CHECK ---
async def check_remote_disk(host, mount_point="/disk"):
    try:
        cmd = ["ssh", "-o", "ConnectTimeout=3", f"{os.environ['USER']}@{host}", f"df --output=pcent {mount_point} | tail -1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        
        if proc.returncode != 0: return True 
        
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
                target_hosts = {obs_utils.ANTENNA_HOST_MAP[a] for a in self.ants if a in obs_utils.ANTENNA_HOST_MAP}
                for target_host in sorted(target_hosts):
                    if not await check_remote_disk(target_host):
                        log(f"Task {self.index} ABORTED: Disk Full on {target_host}")
                        return

                # RUN CONFIG in Thread Pool
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
                    proc = await asyncio.create_subprocess_exec(
                        "ssh", "-n", f"{os.environ['USER']}@{host}", cmd,
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
            
            log(f"Task {self.index}: Triggering...")
            if not self.dry_run: 
                # Run Trigger in Thread Pool (Precision Timing)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.trigger_sync)

            # 5. Wait for Completion
            now = datetime.now().timestamp()
            remaining = self.end_ts - now
            if remaining > 0:
                log(f"Task {self.index}: Observing... ({remaining:.1f}s remaining)")
                if not self.dry_run: await asyncio.sleep(remaining)
            
            # Cleanup
            for p in workers:
                if p in active_subprocesses: active_subprocesses.remove(p)
            
            log(f"Task {self.index}: Finished.")

        except asyncio.CancelledError:
            log(f"Task {self.index}: CANCELLED.")
            raise
        except Exception as e:
            log(f"Task {self.index} ERROR: {e}")

    def configure_roach_sync(self):
        """Uses roach_tools to configure the board."""
        # 1. Filter the Global Map for just THIS task's antennas
        task_roach_map = {ant: obs_utils.ROACH_HOST_MAP[ant] for ant in self.ants if ant in obs_utils.ROACH_HOST_MAP}
        
        # 2. Instantiate Controller with INJECTED Config
        ctrl = roach_tools.RoachController(
            host_map=task_roach_map, 
            fpga_clk=obs_utils.FPGA_CLK,  # <--- INJECTION POINT
            simulated=self.dry_run
        )
        
        try:
            # 3. Configure Accumulation
            ctrl.configure_accumulation(
                integ_time_us=self.data.get('psr_integ'),
                psr_enabled=self.data.get('psr_enabled')
            )
            
            # 4. Configure Noise Cal
            ctrl.configure_noise_diode(
                cal_on_sec=self.data.get('cal_on', 0),
                cal_off_sec=self.data.get('cal_off', 0)
            )
            
            # 5. ARM (Prepare for trigger)
            ctrl.arm_trigger()
            
        except Exception as e:
            log(f"Task {self.index} Config Error: {e}")
        finally:
            ctrl.close()

    def trigger_sync(self):
        """Precision wait and fire."""
        task_roach_map = {ant: obs_utils.ROACH_HOST_MAP[ant] for ant in self.ants if ant in obs_utils.ROACH_HOST_MAP}
        
        ctrl = roach_tools.RoachController(
            host_map=task_roach_map, 
            fpga_clk=obs_utils.FPGA_CLK,
            simulated=self.dry_run
        )
        
        try:
            target_trigger = self.start_ts - 0.5
            
            # Precision Wait Loop
            while True:
                now = time.time()
                wait = target_trigger - now
                
                if wait < -0.1:
                    log(f"Task {self.index} Trigger LATE by {abs(wait):.4f}s")
                    break
                
                if wait > 0.05: time.sleep(wait - 0.05)
                elif wait > 0: pass # Spin
                else:
                    ctrl.fire_trigger()
                    log(f"Task {self.index} Fired Trigger at {now:.4f}")
                    break
        except Exception as e:
            log(f"Task {self.index} Trigger Error: {e}")
        finally:
            ctrl.close()

    def build_worker_cmd(self, host, ant_str):
        d = self.data
        backends = []
        if d.get('spec_enabled'): backends.append("Spec")
        if d.get('psr_enabled'): backends.append("PSR")
        if d.get('baseband_enabled'): backends.append("Baseband")
        
        cmd = f"{obs_utils.WORKER_SCRIPT} -s \"{d['source']}\" -t \"{self.start_str}\" -d {self.duration} -a \"{ant_str}\" -B \"{','.join(backends)},\""
        cmd += f" -p \"{d['project_id']}\" -o \"{d['observer']}\" -R \"{d['ra']}\" -D \"{d['dec']}\" -r \"{d['receiver']}\" -M \"{d['mode']}\""
        cmd += f" --psr_mode \"{d.get('psr_mode') or '2-Pols'}\" --spec_mode \"{d.get('spec_mode') or 'F'}\""
        
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
            log(f"Killed ssh subprocess PID {proc.pid}")
        except Exception: pass

    # Kill remote recorders; their SIGHUP/SIGTERM trap kills the receiver
    # children. Without this, an abort leaves receivers running on a01/a02.
    # Skipped in dry-run so a simulation cannot kill real receivers.
    if not dry_run_mode:
        user = os.environ.get('USER', '')
        for host in sorted(set(obs_utils.ANTENNA_HOST_MAP.values())):
            try:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", f"{user}@{host}",
                     "pkill -f run_data_recorder.sh; killall -q -9 mbspec specrecv specrecv2 bbrec"],
                    timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log(f"Remote cleanup done on {host}")
            except Exception as e:
                log(f"Remote cleanup on {host} failed: {e}")

    sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("schedule_file")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without executing")
    args = parser.parse_args()
    dry_run_mode = args.dry_run

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    
    # Optional: Check System Clock
    try:
        subprocess.check_call(["timedatectl", "status"], stdout=subprocess.DEVNULL)
    except:
        log("WARNING: Could not verify system clock sync!")

    try:
        asyncio.run(main_loop(args.schedule_file, args.dry_run))
    except SystemExit:
        raise
    except BaseException as e:
        log(f"DRIVER FATAL: {type(e).__name__}: {e}")
        log(traceback.format_exc().strip())
        raise
