# FAST 40m Core Array — Automated Observation System

Scripts for unattended radio-astronomy observations on the FAST 40m telescope's
Core Array (4 antennas). This repo is the *source of truth*; running copies live
on three servers (see Deployment). The user moves the newest server versions
into this folder by hand before asking for changes — the repo may briefly lag
or lead what's deployed.

## Topology (not in code comments anywhere)

```
atlas  (obs@atlas)     nginx :443 — the ONLY port open in firewalld; TLS here.
                       Backends bind 127.0.0.1:
                       /scheduler/  → obs_scheduler.py  127.0.0.1:8080
                       /uploader/   → obs_uploader.py   127.0.0.1:8081
                       /controller/ → obs_controller.py 127.0.0.1:8082
                                       → run_auto_obs.py driver
                       /monitor/    → obs_monitor.py    127.0.0.1:8083
                                       (24/7 poller)
                       also: run_auto_obs.sh (terminal driver, still in use)
a01    (obs@ / bliu@)  run_data_recorder.sh — workers for CA01 + CA02
a02    (obs@ / bliu@)  run_data_recorder.sh — workers for CA03 + CA04
```

- Driver → worker is `ssh <host> run_data_recorder.sh ...`; receivers
  (`mbspec`, `specrecv`/`specrecv2`, `bbrec`) live in `~/scripts` on a01/a02.
- Antenna→worker-host and antenna→ROACH maps are in `obs_utils.py`.
- ROACH2 boards: KATCP on port 7147. Register `arm`: 0=disarmed, 3=fired.
  Boards are 2013 bitfiles (`memcpy-88-g38ad77a-dirty`, rcs_id "mb4k");
  destination IPs are hardcoded in the bitfile — multicast failures are
  almost always link/SFP/cable, never configurable from software.
- Multicast streams: .1/.2 = pulsar, .3/.4 = spec/baseband (`MC_MAP` in
  `run_data_recorder.sh`). Receivers bind a per-host local IP, port 12345.

## Why the recorder watchdog exists (incident history)

Receivers are quota-driven with **no internal timeout** — they wait forever
for multicast data. In 2026-08 a dead 10GbE link left a `bbrec` running 4 days
(24 MB of zero-rate log), blocking all later tasks. `run_data_recorder.sh`
v0.1.8 adds: SIGHUP trap (SSH drop), NO-DATA kill at T0+45s (log shows zero
received bytes), hard kill at T0+duration+60s. Healthy baseband ≈ 2.0 GB/s.
The CA02/CA03 failures that prompted all this were **hardware** — 6/8 10GbE
ports down on r2171, same on r2172 — not script bugs.

A second death mode (2026-09-01 onward, every PSR task): receivers die
mid-task with `no more storage pool!` — the receiver's internal buffer pool
starves when the NFS `/data7` write path stalls (write rate → 0 while
receiving continues, free pool drains, `.partial` file left behind). PSR/Spec
data goes to `DATA3=/data7` (NFS), NOT `/disk` — the drivers' df pre-check
doesn't cover the real write path, and `df` can't see a write stall anyway.
Driver v0.2.1 polls worker liveness every 10 s during the observe phase and
logs `Task N ERROR: worker on {host} exited early (rc=...)` + a 5-line tail
of the worker's `active_*_ant.log` when a worker dies early. The 2026-09-08
`line 329: syntax error near 'done'` in driver logs was NOT a script bug —
scp-overwriting `run_data_recorder.sh` while workers were live corrupted
bash's incremental read (bash resumes at a stale fd offset in the new
content; error cites a phantom line number). Never overwrite the recorder on
a01/a02 while workers run — deploy between tasks, or scp to a temp name and
`mv` (rename is atomic; a running instance keeps its fd).

## Operating rules

- **Never run both drivers simultaneously.** Neither sees the other; the
  GUI's double-launch guard only checks for `run_auto_obs.py`. They would
  fight over the same ROACH boards and receiver sockets. (2026-09-08/09: both
  actually ran at once for a full day — the bash session's board writes
  clobbered the Python engine's T−20s configs.)
- ROACH board config belongs in the T−20s window only. The bash driver
  (≤v0.2.7) configured acc_len/cal at **task-iteration start** — hours before
  T0 for late tasks — so launching a session mid-observation stomped the live
  task's registers (2026-09-08 17:13:17: three running 200µs drift scans
  silently became 50µs for their last ~9.7 h; caught by the ROACH monitor's
  acc_len timeline). Fixed in v0.2.8: config now runs right after the wait,
  and `kwrite` checks `!wordwrite ok` replies instead of discarding them.
- Driver choice: `run_auto_obs.py` is required by the GUI and the only one
  that runs time-overlapping antenna-disjoint schedules concurrently
  (bash driver serializes and would skip the later task). `run_auto_obs.sh`
  is the manual terminal fallback; it has the JSON version check.
- **bbrec has no `-w` flag** (verified `-h`: only -m/-d/-t/-f/-l/-h). Watch
  for invented flags — one was accidentally added and caught here before.
- The GUI's status table is driven by regex over the driver's log lines.
  Those line formats are a contract (N is the task uid — see Live queue):
  `--- Task N: ... Started ---`, `Task N: Finished`,
  `Task N ERROR:`/`ABORTED:`/`: CANCELLED.`, and the
  `Loaded N tasks. Engine Start` banner (resets the table). Don't reword
  them in the driver without updating `LogReader` in `obs_controller.py`.
  Anything that must NOT show in the status table uses the lowercase
  `task <uid>` / `[QUEUE]` / `[VALIDATION]` prefixes.
- Driver logs to `~/log/active_driver_session.log` (fixed name; previous
  session rotated to `driver_session_<session start time>.log` — the archive
  timestamp is read from the old file's first line, not the rotation time).
  GUI tails that file, so terminal-launched drivers appear in the GUI too.
- Deploy **order matters**: driver before GUI. New GUI + old driver = blind
  (nothing writes the active log). The uid log contract is joint: deploy
  run_auto_obs v0.2.1 and obs_controller v0.4.1 together. ABORT leaves the
  queue file intact — RUN relaunches and the engine resumes remaining tasks
  (past ones are skipped with `check_past=False` validation).
- Driver stdout goes to DEVNULL when GUI-launched; worker progress is only
  visible in `~/log/active_{spec,psr,bb}_<ant>.log` on a01/a02. Driver-side
  RoachController warnings route into the session log via `log_fn=log` — do
  not construct RoachControllers without it.

## Live task queue (since 2026-09-07)

`~/schedule/.active_queue.json` (hidden from `*.json` glob) is the interface:
**GUI is the only writer** (tmp + `os.replace` atomic; drops finished tasks on
write), **driver is the only reader** (polls inode+mtime each 1s tick). The
engine **stays alive until ABORT** — a drained queue is not an exit. On file
change the driver re-validates (`check_past=False`); an invalid file is
rejected with `[VALIDATION]` lines and the last-good task set is kept. Reconcile
rules: added task → spawns in its 30s window (already past → skipped); removed
pending task → `Task <uid>: CANCELLED.`; removed but in flight (board already
configured/armed) → never yanked, `[QUEUE] ... letting it finish.`. Task
identity is `uid_for(task)` in obs_utils: `<start_time digits>_<sanitized
source>`, `-2`/`-3` suffixes on duplicate (start, source) pairs. Terminal use
`run_auto_obs.py <file>` watches whatever file it's given.

## JSON data format (v0.2.0, since 2026-09-07)

- `version` stamps compare by **major.minor** everywhere (`get_major_minor`);
  the format bumped to v0.2.0 when `rfgain` (required, dB, 0.5-step,
  −11.5…+20.0) and `dgain` (required with PSR, 0–65535, int or `"0x…"` string;
  accepted-but-ignored without PSR) were added to the task schema.
- `run_auto_obs.sh` deliberately stays at `SUPPORTED_DATA_VERSION="v0.1.0"`:
  it CRITICAL-ERRORs and refuses a v0.2 schedule. That's the safety design —
  the bash driver cannot apply gains, so it must not run v0.2 files. Remove
  this paragraph only if someone ports gain support to the bash driver.
- obs_uploader/obs_controller follow `obs_utils.DATA_VERSION` automatically.

## Deployment

`./sync_scripts.sh` pushes: recorder → a01/a02; everything else → atlas.
Deployed state as of 2026-09-09 (md5-verified repo == atlas/a01): driver
v0.2.1 + controller v0.4.3 (joint uid log contract — deploy together),
bash driver v0.2.8, recorder v0.1.8 on a01/a02, utils DATA_VERSION v0.2.0,
roach_tools v0.1.5, scheduler v0.3.6, uploader v0.2.10, monitor v0.2.3
(slider fix + localhost bind). Restart with
`sudo systemctl restart fast-obs-{sche,load,ctrl,moni}` — plain
`systemctl restart` as obs hits polkit ("Interactive authentication
required"). Controller restart only when no driver is running (systemd
cgroup kill would take the engine down). Monitor restart wipes the
in-memory 12 h UI history (JSONL unaffected).

### Network entry (as-built 2026-09-09)

nginx on atlas is the only network door: 443/tcp is the sole port in the
firewalld `work` zone; 8080–8083 are firewalled off and all four services
bind 127.0.0.1. `docs/nginx_fast-obs.conf` is the canonical copy of
`/etc/nginx/conf.d/fast-obs.conf` — edit the repo copy, then scp +
`sudo install -m 644 ... /etc/nginx/conf.d/fast-obs.conf` +
`sudo nginx -t` + `sudo systemctl reload nginx`. One-time setup already
done: self-signed pair `/etc/pki/tls/certs/atlas.crt` +
`/etc/pki/tls/private/atlas.key` (10 y, CN=atlas, SAN IP:10.128.3.39), and
SELinux `setsebool -P httpd_can_network_connect on` — without it nginx's
backend connects are silently denied while Enforcing. NiceGUI (3.6.1 live
in the service venv; same mechanism source-verified in 3.16) builds asset
and socket.io URLs from the `X-Forwarded-Prefix` request header — each
location strips its path prefix (trailing-slash `proxy_pass`) and sets
that header; verified end-to-end incl. socket.io handshake. Per-service
cert pairs (`~/observe/certs/*.{crt,key}`) must NOT be created behind the
proxy — a backend serving HTTPS would 502 nginx, which owns TLS.
Gotcha: atlas's shell exports
`http_proxy`/`https_proxy=socks5://10.128.3.20:1080`, so curl on atlas
silently tunnels even 127.0.0.1 requests through SOCKS and fails — use
`curl --noproxy '*'`.
`update_version.sh` selects versioned files that aren't in this folder and
its selections are stale (v0.1.7 recorder / v0.1.5 py driver / v0.2.6 bash)
— do not trust it until updated.

## Schedule semantics

`verify_schedule()` in obs_utils.py rejects time-overlapping tasks that share
an antenna; back-to-back and disjoint-antenna overlaps are legal by design.
The collision check only runs after *all* schema errors are fixed — a task
with a bad mode/RA hides conflict checking for the whole schedule.

## ROACH2 monitor facts (register map recovered from roach2/ code)

- KATCP wire protocol reference: `katcp-0.9.3/` (vendored upstream package;
  escape table identical in 0.6.2 used by the 2013 era). Escaping covers ONLY
  `[\\ \0\n\r\x1b\t]` → `\X` selectors (plus `\@`=empty); `?read` reply is
  `!read ok <escaped-data>` — no offset field; arguments split on `[ \t]+`
  BEFORE unescaping (why escaped payloads never contain raw spaces).
  `RoachBoard._katcp_unescape` is bytes-level and spec-exact (differential-
  tested against core.py's tables); unknown/trailing escapes raise.
- **There is no RMS register.** RMS comes from an ADC time-domain snapshot:
  write 6 then 7 to `zdok0_ctrl` (arm w/ man_trig+man_valid), poll `zdok0_status`
  until bit31 clears (size = value & 0x7fffffff bytes), blob-read `zdok0_bram`
  via KATCP `?read` (escaped binary), signed bytes, groups of 4 alternating
  pol0/pol1 (mbc.py `split_snapshot`). Full scale ≈ 128 codes.
- Counters are double-read ~1.05 s apart inside each 60 s cycle: `pps_counter`
  (alive iff it increments) and `sys_clkcounter` (delta/elapsed = FPGA MHz,
  32-bit-wraparound-safe). `u0_acc_len`, `u0_bit_select` (4×2-bit packed),
  `u0_gain` (lo16=pol0, hi16=pol1), `noisecal_on/off(+_hipart)` (48-bit counts,
  /250e6 = s), `rf_fe_get` for rfgain/enabled.
- obs_monitor.py (atlas :8083, standalone service — survives controller
  restarts; binds 127.0.0.1 and is reached via nginx
  `https://atlas/monitor/` — see Deployment "Network entry"; cert
  auto-detect remains but the cert pair must NOT exist behind the proxy):
  poll cycles start on
  the exact minute (:00) via
  `seconds_to_next_minute`; poll_ts (history/point label) is captured at
  cycle START, before the gather. Per board (3 s timeout, concurrent;
  failures logged as ok=false lines, never silent gaps): health reads +
  spectra (`roach_tools.read_spectra` — arms `u0_x4_vacc_scope_{AA,BB}`
  with ctrl 4→5, blob-reads big-endian int32, caches raw arrays in the
  history point; a spectra failure never red-rows the health table). 12 h
  in-memory history (`HISTORY_WINDOW` deque, one point per minute carrying
  RMS + spectra arrays; a service restart clears it — JSONL log unaffected)
  feeds the RMS chart + spectra charts; spectra arrays are NEVER logged —
  JSONL poll records carry only `spec_ok`/`spec_n_ch` scalars. UI (budgeted
  for 1920x1080 no-scroll; section titles render inside the charts): one
  control row = time slider + board selector (radio, per-client).
  Slider max mirrors `len(HISTORY_WINDOW)` (refresh keeps it in step) —
  hardcoded 720 was the long-standing "slider does not work" bug: the
  in-memory window restarts empty, so after a restart every position
  beyond the stored points resolved to LIVE and drags did nothing
  (2026-09-09). Right edge = LIVE; other positions pin a minute by ts
  (deque shifts would drift an index pin; NiceGUI slider values arrive as
  floats — `int()` them before indexing the deque); pinned minute renders
  an amber markLine on the RMS chart and drives the spectra shown; aged-out
  pins reset to LIVE (server-side `slider.value = X` DOES fire
  on_value_change — `BindableProperty.__set__` fires the handler on any
  changed assignment); RMS x labels are HH:MM,
  no seconds. Spectra axes:
  left log2(P+1) 0–32, right log2(byte+1) 0–8, both labeled `2^{value}`
  (interval 8 / 2). + 1 s link watch on port 7147
  (transition-only `link_down`/`link_up` JSONL events = power-cut forensics).
  The watch uses ONE persistent `?watchdog` connection per board
  (`_LinkProbe`), NOT connect/close pings: every client close makes the
  board's kcs broadcast `received_end_of_file_from_<ip>` informs to all
  clients, and an inform landing mid-reply breaks line framing for the next
  read (spectra reads failed this way on 2026-09-09). `read_spectra` retries
  once on a fresh connection for the same reason.
  Log `~/log/roach_monitor_YYYYMMDD.log` (JSONL, daily rotation). Poller is
  module-level via `app.on_startup` — runs with zero browsers, never
  double-polls with two. Monitor board writes: `zdok0_ctrl` (RMS snapshot)
  and `u0_x4_vacc_scope_{AA,BB}_ctrl` (passive scope taps on the vacc
  output); observation registers — acc_len, gain, cal, `arm` — are never
  written (arm is write-only and never read).

## Deployed data files (edit on the server, not in code)

- `~/observe/rfgain_table.lst` on atlas: per-antenna RF-gain offsets (dB),
  `CA01, <pol0>, <pol1>` per line. **This is measured data, not code** — the
  repo ships zeros; real offsets get filled in on atlas. Loaded when each
  RoachController is created (every task's config window), so edits take
  effect at the next task without restarting anything.

## Testing notes (local machine)

- `nicegui` is not installed locally — `obs_controller.py` can only be
  py_compile'd or imported with a mocked `nicegui` module; real GUI testing
  happens on atlas at `https://atlas/controller/` (nginx proxy).
- `bbrec`/`mbspec`/`specrecv*` exist only on a01/a02 — check `-h` there,
  not locally.
- Running the drivers locally writes to local `~/log/` and (for real runs)
  touches live ROACH registers (acc_len/noisecal — harmless, `arm` is
  untouched). Clean up local `~/log` test artifacts; use `--dry-run`
  wherever possible.
