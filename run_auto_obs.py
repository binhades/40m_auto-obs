#!/usr/bin/env python3
# ==================================================================
# FAST Core Array - Concurrent Python Driver
# Version: v0.2.1
# Updates: Worker liveness poll during the observe phase — an early worker
#          exit (e.g. receiver "no more storage pool!") now logs
#          Task <uid> ERROR: worker on <host> exited early (rc=N) plus a
#          tail of the worker's active logs, instead of a silent "Finished.".
#          (v0.2.0: live task queue, uid-keyed log contract with GUI v0.4.x.)
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
    def __init__(self, task_data, uid, dry_run=False):
        self.data = task_data
        self.uid = uid
        self.dry_run = dry_run
        self.src = task_data['source']
        self.start_str = task_data['start_time_cst']
        self.ants = task_data['antennas']
        self.run_started = False
        self.coro = None

        dt = datetime.strptime(self.start_str, "%Y-%m-%dT%H:%M:%S")
        self.start_ts = dt.timestamp()
        self.duration = obs_utils.parse_duration(task_data['duration'])
        self.end_ts = self.start_ts + self.duration

    async def run(self):
        self.run_started = True
        try:
            log(f"--- Task {self.uid}: {self.src} ({', '.join(self.ants)}) Started ---")

            # 1. Pre-Wait
            now = datetime.now().timestamp()
            wait_time = (self.start_ts - 20) - now
            if wait_time > 0:
                log(f"Task {self.uid}: Waiting {wait_time:.1f}s for config window...")
                if not self.dry_run: await asyncio.sleep(wait_time)

            # 2. Disk Check & Config
            if not self.dry_run:
                target_hosts = {obs_utils.ANTENNA_HOST_MAP[a] for a in self.ants if a in obs_utils.ANTENNA_HOST_MAP}
                for target_host in sorted(target_hosts):
                    if not await check_remote_disk(target_host):
                        log(f"Task {self.uid} ABORTED: Disk Full on {target_host}")
                        return

                # RUN CONFIG in Thread Pool
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.configure_roach_sync)
            
            # 3. Launch Workers
            host_map = {}
            for ant in self.ants:
                h = obs_utils.ANTENNA_HOST_MAP.get(ant)
                if h: host_map.setdefault(h, []).append(ant)
            
            workers = []  # (proc, host, ants_on_host) - host needed for early-exit reporting
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
                    workers.append((proc, host, ants_on_host))
                    log(f"Task {self.uid}: Launched worker on {host} for {ants_on_host}")

            # 4. Wait for Trigger
            now = datetime.now().timestamp()
            wait_trigger = (self.start_ts - 5) - now
            if wait_trigger > 0:
                if not self.dry_run: await asyncio.sleep(wait_trigger)
            
            log(f"Task {self.uid}: Triggering...")
            if not self.dry_run: 
                # Run Trigger in Thread Pool (Precision Timing)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.trigger_sync)

            # 5. Wait for Completion + worker liveness poll.
            # Receivers have no internal timeout and can die mid-task (seen:
            # "no more storage pool!" when the NFS /data7 write path stalls);
            # their output is DEVNULL'd, so the only observable is the ssh
            # process exit. Without this poll the task logs a green "Finished."
            # while half the antennas recorded nothing.
            now = datetime.now().timestamp()
            remaining = self.end_ts - now
            if remaining > 0:
                log(f"Task {self.uid}: Observing... ({remaining:.1f}s remaining)")
            reported = set()
            while True:
                now = datetime.now().timestamp()
                if now >= self.end_ts:
                    break
                if not self.dry_run:
                    for proc, host, _ in workers:
                        if host in reported or proc.returncode is None:
                            continue
                        reported.add(host)
                        log(f"Task {self.uid} ERROR: worker on {host} exited early (rc={proc.returncode})")
                        await self.report_worker_tail(host)
                await asyncio.sleep(min(10, max(0.1, self.end_ts - now)))
            
            # Cleanup
            for proc, host, _ in workers:
                if proc in active_subprocesses: active_subprocesses.remove(proc)

            log(f"Task {self.uid}: Finished.")

        except asyncio.CancelledError:
            log(f"Task {self.uid}: CANCELLED.")
            raise
        except Exception as e:
            log(f"Task {self.uid} ERROR: {e}")

    async def report_worker_tail(self, host):
        """Pull the tail of this task's active worker logs - the only place a
        receiver records why it died (their stdout/stderr are DEVNULL'd)."""
        try:
            types = [t for t, en in (("spec", self.data.get('spec_enabled')),
                                     ("psr", self.data.get('psr_enabled')),
                                     ("bb", self.data.get('baseband_enabled'))) if en]
            paths = " ".join(f"~/log/active_{t}_{a}.log" for t in types for a in self.ants
                             if obs_utils.ANTENNA_HOST_MAP.get(a) == host)
            if not paths:
                return
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=3", "-n", f"{os.environ['USER']}@{host}",
                f"tail -n 5 {paths} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            for line in stdout.decode(errors='replace').splitlines():
                log(f"[WORKER] {host}: {line}")
        except Exception as e:
            log(f"[WORKER] {host}: could not fetch worker log tail ({e})")

    def configure_roach_sync(self):
        """Uses roach_tools to configure the board."""
        # 1. Filter the Global Map for just THIS task's antennas
        task_roach_map = {ant: obs_utils.ROACH_HOST_MAP[ant] for ant in self.ants if ant in obs_utils.ROACH_HOST_MAP}
        
        # 2. Instantiate Controller with INJECTED Config
        ctrl = roach_tools.RoachController(
            host_map=task_roach_map,
            fpga_clk=obs_utils.FPGA_CLK,  # <--- INJECTION POINT
            simulated=self.dry_run,
            log_fn=log  # warnings must reach the session log; stdout is DEVNULL'd under the GUI
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

            # 5. Gains: rfgain every task; dgain only applies to the PSR backend
            ctrl.configure_gains(
                rfgain_db=self.data.get('rfgain'),
                dgain=self.data.get('dgain') if self.data.get('psr_enabled') else None,
            )

            # 6. ARM (Prepare for trigger)
            ctrl.arm_trigger()
            
        except Exception as e:
            log(f"Task {self.uid} Config Error: {e}")
        finally:
            ctrl.close()

    def trigger_sync(self):
        """Precision wait and fire."""
        task_roach_map = {ant: obs_utils.ROACH_HOST_MAP[ant] for ant in self.ants if ant in obs_utils.ROACH_HOST_MAP}
        
        ctrl = roach_tools.RoachController(
            host_map=task_roach_map,
            fpga_clk=obs_utils.FPGA_CLK,
            simulated=self.dry_run,
            log_fn=log
        )
        
        try:
            target_trigger = self.start_ts - 0.5
            
            # Precision Wait Loop
            while True:
                now = time.time()
                wait = target_trigger - now
                
                if wait < -0.1:
                    log(f"Task {self.uid} Trigger LATE by {abs(wait):.4f}s")
                    break
                
                if wait > 0.05: time.sleep(wait - 0.05)
                elif wait > 0: pass # Spin
                else:
                    ctrl.fire_trigger()
                    log(f"Task {self.uid} Fired Trigger at {now:.4f}")
                    break
        except Exception as e:
            log(f"Task {self.uid} Trigger Error: {e}")
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
def read_queue_file(json_file):
    with open(json_file) as f:
        return json.load(f)

def log_validation_errors(result, umap=None):
    if isinstance(result, dict):
        idx_to_uid = {i: uid for uid, i in (umap or {}).items()}
        for key, msgs in result.items():
            if key == 'global':
                prefix = "global"
            elif isinstance(key, int) and idx_to_uid:
                prefix = f"task {idx_to_uid.get(key, key)}"
            else:
                prefix = f"task {key}"
            log(f"[VALIDATION] {prefix}: {' | '.join(msgs)}")
    else:
        log(f"[VALIDATION] {result}")

async def main_loop(json_file, dry_run=False):
    data = read_queue_file(json_file)

    # One validation standard with the scheduler/uploader. Past tasks are allowed:
    # the driver is re-launchable mid-schedule and skips them (below).
    is_valid, result = obs_utils.verify_schedule(data, check_past=False)
    if not is_valid:
        log("[VALIDATION] Schedule rejected. Fix the JSON and re-launch.")
        log_validation_errors(result, obs_utils.uid_map(data.get('schedule', [])))
        raise ValueError("Invalid schedule file")

    pending = {}    # uid -> ObservationTask not yet spawned
    running = {}    # uid -> ObservationTask spawned (coroutine may still be pending)
    umap = obs_utils.uid_map(data['schedule'])
    for uid, i in sorted(umap.items(), key=lambda kv: kv[1]):
        pending[uid] = ObservationTask(data['schedule'][i], uid, dry_run)

    json_stat = os.stat(json_file)
    log(f"Loaded {len(pending)} tasks. Engine Start{' (DRY RUN)' if dry_run else ''}.")

    # The engine stays alive until ABORT even when the queue is dry, so more
    # tasks can be added while it runs.
    while True:
        now = datetime.now().timestamp()

        # --- Spawn tasks entering their 30s window ---
        due = [t for t in pending.values() if t.start_ts - now < 30]
        due.sort(key=lambda t: t.start_ts)
        for task in due:
            del pending[task.uid]
            if task.end_ts < now:
                log(f"Task {task.uid} is in the past. Skipping.")
                continue
            task.coro = asyncio.create_task(task.run())
            running[task.uid] = task

        # --- Reap finished coroutines ---
        for uid in [u for u, t in running.items() if t.coro.done()]:
            del running[uid]

        # --- Watch the queue file: reload + reconcile on change ---
        try:
            new_stat = os.stat(json_file)
        except OSError:
            new_stat = None
        if new_stat is None or (new_stat.st_ino, new_stat.st_mtime) != (json_stat.st_ino, json_stat.st_mtime):
            json_stat = new_stat
            if new_stat is None:
                log(f"[QUEUE] {json_file} disappeared - keeping current task set.")
            else:
                try:
                    new_data = read_queue_file(json_file)
                    new_umap = obs_utils.uid_map(new_data['schedule'])
                    ok, res = obs_utils.verify_schedule(new_data, check_past=False)
                    if not ok:
                        log("[VALIDATION] Queue update rejected - keeping current task set.")
                        log_validation_errors(res, new_umap)
                    else:
                        new_uids = set(new_umap.keys())
                        cur_uids = set(pending.keys()) | set(running.keys())

                        added = new_uids - cur_uids
                        for uid in sorted(added):
                            t = ObservationTask(new_data['schedule'][new_umap[uid]], uid, dry_run)
                            if t.end_ts < now:
                                log(f"Task {uid} is in the past. Skipping.")
                                continue
                            pending[uid] = t

                        removed = cur_uids - new_uids
                        for uid in sorted(removed):
                            if uid in pending:
                                del pending[uid]  # never spawned - no coroutine to cancel
                                log(f"Task {uid}: CANCELLED.")
                                continue
                            task = running.get(uid)
                            if task is None:
                                continue
                            if task.run_started:
                                # Never yank a configured/armed board mid-flight.
                                log(f"[QUEUE] Task {uid} removed from queue but already in flight - letting it finish.")
                            elif task.coro is not None:
                                task.coro.cancel()
                                log(f"[QUEUE] Task {uid} cancelled by queue update.")

                        if added or removed:
                            log(f"[QUEUE] Reloaded: +{len(added)} added, -{len(removed)} removed, "
                                f"{len(running)} in flight, {len(pending)} pending.")
                except (json.JSONDecodeError, OSError, KeyError, ValueError) as e:
                    log(f"[QUEUE] Reload failed ({type(e).__name__}: {e}) - keeping current task set.")

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
