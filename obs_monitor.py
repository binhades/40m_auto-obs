#!/usr/bin/env python3
# ==================================================================
# FAST Core Array - ROACH2 Board Monitor (standalone service)
# Version: v0.2.3
# Updates: binds 127.0.0.1 — network exposure moved to the nginx reverse
#          proxy (https://atlas/monitor/, port 443); backends serve plain
#          HTTP on loopback, TLS terminates at nginx. Cert auto-detect
#          stays but the cert pair must NOT exist behind the proxy (an
#          HTTPS backend would 502 nginx).
# Purpose: 24/7 hardware health monitor, independent of observations.
#          - Poll cycles start on the exact minute (:00).
#          - 60 s cycle: RMS (zdok0 scope snapshot), spectra
#            (u0_x4_vacc_scope_AA/BB, raw int32 cached 12 h in the RMS
#            history points — never logged), rfgain/enabled (I2C),
#            dgain/acc_len/bit_select/cal (wordread), clock rate + PPS
#            (double-read counters).
#          - 1 s link watch (persistent ?watchdog connection per board).
#          - One JSONL line per board per poll + link event lines to
#            ~/log/roach_monitor_YYYYMMDD.log.
#          Poller lives at module level (app.on_startup): it runs with zero
#          browser clients and never double-runs with two clients.
# ==================================================================

import asyncio
import json
import math
import socket
import time
from collections import deque
from datetime import datetime
from pathlib import Path
import sys

current_dir = Path(__file__).parent.resolve()
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

import obs_utils
from roach_tools import RoachBoard, RoachError, read_monitor_snapshot, read_spectra

MONITOR_VERSION = "v0.2.3"

LINK_CHECK_INTERVAL_S = 1.0
LINK_TCP_TIMEOUT_S = 0.5
HISTORY_WINDOW = deque(maxlen=720)  # 12 h at 1 poll/min

# {ant: {ok, error, link_up, ...read_monitor_snapshot fields}}
MON_STATE = {ant: {"ok": False, "error": "no data yet", "link_up": None} for ant in obs_utils.ACTIVE_ANTENNAS}
LINK_STATE = {ant: {"up": None, "since_ts": None} for ant in obs_utils.ACTIVE_ANTENNAS}  # up=None until first check

_poll_task = None
_link_task = None
_poll_running = False  # overlap guard: skip if previous cycle overran


def _log_path():
    return obs_utils.LOG_DIR / f"roach_monitor_{datetime.now().strftime('%Y%m%d')}.log"


def log_event(record):
    record.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(_log_path(), "a") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def poll_board(ant):
    """Blocking full poll of one board -> state dict (never raises)."""
    host = obs_utils.ROACH_HOST_MAP[ant]
    entry = {"ok": False, "error": None, "link_up": LINK_STATE[ant]["up"]}
    board = RoachBoard(host, timeout=3.0)
    try:
        board.connect()
        snap = read_monitor_snapshot(board, fpga_clk=obs_utils.FPGA_CLK)
        entry.update(snap)
        entry["ok"] = True
        try:
            entry["spec"] = {"ok": True, **read_spectra(board)}
        except (RoachError, OSError) as e:
            # Spectra failure must not red-row the health table.
            entry["spec"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except (RoachError, OSError) as e:
        entry["error"] = f"{type(e).__name__}: {e}"
    finally:
        board.close()
    return entry


async def _poll_board_async(ant):
    return await asyncio.to_thread(poll_board, ant)


def _tcp_ping(host, port, timeout):
    """True if the KATCP port accepts a connection. <1ms when healthy."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class _LinkProbe:
    """Persistent KATCP ?watchdog probe for the 1 s link watch.

    Replaced the old connect/close probe: every close made the board's kcs
    broadcast '#log info ... received_end_of_file_from_<ip>' informs to ALL
    clients — once a second per board — and kcs writes informs in pieces,
    so one can land mid-reply and corrupt line framing for the next read
    (2026-09-09: spectra reads failed with 'Resp: received end of file
    from ...'). One long-lived connection = one connect inform per board,
    not one per second. Failure detection is still ~1 s: a power cut kills
    the socket immediately, and a dead board fails send/recv (or the 0.5 s
    connect) within the same cycle."""
    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.buf = b""

    def check(self):
        """One probe cycle; True iff a non-inform KATCP reply line arrived."""
        try:
            if self.sock is None:
                self.sock = socket.create_connection((self.host, self.port),
                                                     timeout=self.timeout)
                self.sock.settimeout(self.timeout)
                self.buf = b""
            self.sock.sendall(b"?watchdog\n")
            deadline = time.time() + self.timeout
            while True:
                while b"\n" not in self.buf:
                    if time.time() > deadline:
                        raise OSError("watchdog reply timeout")
                    data = self.sock.recv(4096)
                    if not data:
                        raise OSError("connection closed by board")
                    self.buf += data
                line, self.buf = self.buf.split(b"\n", 1)
                if line.startswith(b"#") or not line.strip():
                    continue  # log/client informs
                return line.startswith(b"!watchdog")
        except OSError:
            self._drop()
            return False

    def _drop(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""


def log_link_transition(ant, host, up, prev_since_ts):
    ev = {"type": "link_down" if not up else "link_up", "ant": ant, "board": host}
    if not up:
        ev["last_up_since"] = datetime.fromtimestamp(prev_since_ts).strftime("%Y-%m-%d %H:%M:%S")
    else:
        ev["down_s"] = round(time.time() - prev_since_ts, 1) if prev_since_ts else None
    log_event(ev)


_LINK_PROBES = {}  # ant -> _LinkProbe (persistent ?watchdog connections)


async def link_watch_loop():
    while True:
        for ant in obs_utils.ACTIVE_ANTENNAS:
            host = obs_utils.ROACH_HOST_MAP[ant]
            probe = _LINK_PROBES.setdefault(
                ant, _LinkProbe(host, 7147, LINK_TCP_TIMEOUT_S))
            up = await asyncio.to_thread(probe.check)
            st = LINK_STATE[ant]
            if st["up"] is None:
                st["up"], st["since_ts"] = up, time.time()
                log_event({"type": "link_init", "ant": ant, "board": host, "up": up})
            elif up != st["up"]:
                prev = st["since_ts"]
                log_link_transition(ant, host, up, prev)
                st["up"], st["since_ts"] = up, time.time()
                if ant in MON_STATE:
                    MON_STATE[ant]["link_up"] = up
        await asyncio.sleep(LINK_CHECK_INTERVAL_S)


async def poll_cycle(trigger="timer"):
    global _poll_running
    if _poll_running:
        log_event({"type": "poll_skipped", "trigger": trigger,
                   "reason": "previous cycle still running"})
        return
    _poll_running = True
    try:
        t0 = time.time()
        # Label the point with the cycle-start time (the aligned :00 second),
        # not the gather-finish time.
        poll_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entries = await asyncio.gather(*[_poll_board_async(a) for a in obs_utils.ACTIVE_ANTENNAS])
        history_point = {"ts": poll_ts, "spec": {}}
        for ant, entry in zip(obs_utils.ACTIVE_ANTENNAS, entries):
            entry["board"] = obs_utils.ROACH_HOST_MAP[ant]
            entry["link_up"] = LINK_STATE[ant]["up"]
            # Spectra arrays must never reach the JSONL log (~180 MB/day);
            # swap them out for scalars before serializing.
            spec = entry.pop("spec", None)
            entry["spec_ok"] = bool(spec and spec.get("ok"))
            entry["spec_n_ch"] = spec.get("n_ch") if entry["spec_ok"] else None
            record = {"type": "poll", "ant": ant, **entry}
            log_event(record)
            MON_STATE[ant] = entry
            if entry["ok"]:
                history_point[ant] = entry["rms"]
            if spec is not None:
                history_point["spec"][ant] = (
                    {"ok": True, "sels": spec["bitsel"],
                     "aa": spec["spec"][0], "bb": spec["spec"][1]}
                    if spec.get("ok") else {"ok": False, "error": spec.get("error")})
        HISTORY_WINDOW.append(history_point)
        MON_STATE["last_poll_ts"] = poll_ts
        MON_STATE["cycle_s"] = round(time.time() - t0, 2)
    finally:
        _poll_running = False


def seconds_to_next_minute(now=None):
    """Sleep target so poll cycles start on the exact minute (:00)."""
    if now is None:
        now = time.time()
    return max(0.05, (int(now // 60) + 1) * 60 - now)


async def poll_loop():
    await poll_cycle(trigger="startup")
    while True:
        await asyncio.sleep(seconds_to_next_minute())
        await poll_cycle(trigger="timer")


async def main():
    global _poll_task, _link_task
    _link_task = asyncio.create_task(link_watch_loop())
    _poll_task = asyncio.create_task(poll_loop())


# ------------------------------- PAGE ----------------------------------
from nicegui import ui, app

BOARD_COLORS = {"CA01": "#2563eb", "CA02": "#16a34a", "CA03": "#dc2626", "CA04": "#9333ea"}


def set_chart_options(chart, options):
    # The `options` property is read-only on the atlas NiceGUI install
    # (AttributeError on assignment), but the getter returns the live props
    # dict — the canonical pattern is in-place mutation + chart.update().
    try:
        current = chart.options
        current.clear()
        current.update(options)
    except Exception:
        chart.options = options  # version with a real setter
    chart.update()


def build_plot_options(pin_idx=None):
    # x labels are HH:MM (no seconds — the poll cadence is minute-aligned).
    xs = [p["ts"][11:16] for p in HISTORY_WINDOW]
    series = []
    for ant in obs_utils.ACTIVE_ANTENNAS:
        for pol in (0, 1):
            data = []
            for p in HISTORY_WINDOW:
                if ant in p:
                    data.append([p["ts"][11:16], p[ant][pol]])
            s = {
                "name": f"{ant} pol{pol}",
                "type": "line",
                "showSymbol": False,
                "data": data,
                "lineStyle": {"width": 1.5, "type": "solid" if pol == 0 else "dashed",
                              "color": BOARD_COLORS[ant]},
                "color": BOARD_COLORS[ant],
            }
            series.append(s)
    # Pinned slider position: amber markLine at the selected minute.
    if pin_idx is not None and 0 <= pin_idx < len(HISTORY_WINDOW):
        series[0].setdefault("markLine", {
            "silent": True, "symbol": "none",
            "lineStyle": {"color": "#f59e0b", "width": 1.5, "type": "solid"},
            "label": {"show": False},
            "data": [{"xAxis": xs[pin_idx]}],
        })
    return {
        "animation": False,
        # No in-chart title: it collided with the y-axis name; the legend
        # already identifies pol0/pol1 per board.
        "tooltip": {"trigger": "axis"},
        "legend": {"data": [s["name"] for s in series], "top": 2,
                   "left": "center", "textStyle": {"fontSize": 11}},
        "grid": {"left": 50, "right": 16, "top": 26, "bottom": 44},
        "xAxis": {"type": "category", "data": xs,
                  "axisLabel": {"fontSize": 10, "interval": "auto"}},
        "yAxis": {"type": "value", "name": "RMS (ADC codes)",
                  "axisLabel": {"fontSize": 10}},
        "series": series,
    }


def build_table_rows():
    rows = []
    for ant in obs_utils.ACTIVE_ANTENNAS:
        s = MON_STATE[ant]
        rows.append({
            "ant": ant,
            "board": obs_utils.ROACH_HOST_MAP[ant],
            "ok": s.get("ok", False),
            "error": s.get("error") or "-",
            "rms0": f"{s['rms'][0]:.1f}" if s.get("ok") else "-",
            "rms1": f"{s['rms'][1]:.1f}" if s.get("ok") else "-",
            "rfg0": f"{s['rfgain_db'][0]:+.1f}" if s.get("ok") else "-",
            "rfg1": f"{s['rfgain_db'][1]:+.1f}" if s.get("ok") else "-",
            "enabled": f"{'ON' if s['enabled'][0] else 'OFF'}/{'ON' if s['enabled'][1] else 'OFF'}"
                       if s.get("ok") else "-",
            "dgain": f"0x{s['dgain']:04X}" if s.get("ok") else "-",
            "acc_len": str(s["acc_len"]) if s.get("ok") else "-",
            "bit_select": str(s["bit_select"]) if s.get("ok") else "-",
            "cal": f"{s['cal_on_s']:.2f}/{s['cal_off_s']:.2f}s" if s.get("ok") else "-",
            "clk": f"{s['clk_mhz']:.3f}" if s.get("ok") else "-",
            "pps": "OK" if s.get("pps_ok") else ("FAIL" if s.get("ok") else "-"),
            "link": "up" if s.get("link_up") else "DOWN",
            "_class": "" if s.get("ok") else "bg-red-100 text-red-900 font-bold",
        })
    return rows


def spec_series_values(arr, sel, n_ch):
    """Display values from one cached raw int32 spectrum. Both series are
    log2-magnitude with the +1 convention (mbv.py): raw = log2(|P|+1), and
    the bit-selected byte b = (P >> 8*sel) & 0xFF becomes log2(b+1), so
    byte 255 lands exactly on the 2^8 axis top."""
    n = min(n_ch, len(arr))
    raw = [round(math.log2(abs(v) + 1), 2) for v in arr[:n]]
    bitsel = [round(math.log2(((v >> (8 * sel)) & 0xFF) + 1), 2) for v in arr[:n]]
    return raw, bitsel


def build_spec_options(ant, pol, point):
    """ECharts options for one spectra panel; point is a HISTORY_WINDOW entry."""
    title = f"{ant} pol{pol}"
    if point is None or point.get("spec", {}).get(ant) is None:
        return {"title": {"text": title, "subtext": "no data",
                          "left": "center", "top": 4,
                          "textStyle": {"fontSize": 13},
                          "subtextStyle": {"fontSize": 11, "color": "#aaa"}},
                "xAxis": {"type": "category", "data": []},
                "yAxis": {"type": "value"},
                "series": []}
    ps = point["spec"][ant]
    if not ps.get("ok"):
        return {"title": {"text": title, "subtext": ps.get("error", "error"),
                          "left": "center", "top": 4,
                          "textStyle": {"fontSize": 13},
                          "subtextStyle": {"fontSize": 11, "color": "#f87171"}},
                "xAxis": {"type": "category", "data": []},
                "yAxis": {"type": "value"},
                "series": []}

    n_ch = min(len(ps["aa"]), len(ps["bb"]))
    sel = ps["sels"][pol]
    raw, bitsel = spec_series_values(ps["aa"] if pol == 0 else ps["bb"], sel, n_ch)

    xs = list(range(n_ch))
    return {
        "animation": False,
        "title": {"text": (f"{ant} pol{pol} — {point['ts']} (bitsel {sel})"),
                  "left": "center", "top": 2, "textStyle": {"fontSize": 12}},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["raw log2(P+1)", "bitsel log2(b+1)"], "top": 20,
                   "textStyle": {"fontSize": 10}},
        "grid": {"left": 50, "right": 50, "top": 40, "bottom": 30},
        "xAxis": {"type": "category", "data": xs,
                  "axisLabel": {"fontSize": 9, "interval": 255}},
        "yAxis": [
            {"type": "value", "name": "log2(P+1)", "min": 0, "max": 32,
             "interval": 8,
             "axisLabel": {"fontSize": 9,
                           # ticks at 2^N, matching mbv.py's axis labels
                           "formatter": "2^{value}"},
             "axisTick": {"alignWithLabel": True}},
            {"type": "value", "name": "log2(byte+1)", "min": 0, "max": 8,
             "interval": 2,  # ticks every 2nd power: 2^0, 2^2, ... 2^8
             "axisLabel": {"fontSize": 9, "formatter": "2^{value}"},
             "splitLine": {"show": False}},
        ],
        "series": [
            {"name": "raw log2(P+1)", "type": "line", "showSymbol": False,
             "sampling": "lttb", "data": raw,
             "lineStyle": {"width": 1, "color": BOARD_COLORS[ant]},
             "color": BOARD_COLORS[ant]},
            {"name": "bitsel log2(b+1)", "type": "line", "showSymbol": False,
             "sampling": "lttb", "data": bitsel, "yAxisIndex": 1,
             "lineStyle": {"width": 1, "type": "dashed", "color": "#f59e0b"},
             "color": "#f59e0b"},
        ],
    }


@ui.page('/')
def main_page():
    ui.query('.q-page').classes('flex-center w-full max-w-full px-4')
    with ui.header().classes('bg-slate-800 text-white min-h-0'):
        ui.label("FAST Core Array - ROACH2 Monitor").classes('text-lg font-bold py-1 px-4')
        ui.label(MONITOR_VERSION).classes('text-xs text-gray-400 font-mono bg-gray-900 px-2 rounded self-center')
        ui.space()
        last_label = ui.label("-").classes('font-mono text-gray-300 text-sm mr-2')
        ui.button('Poll Now', on_click=lambda: asyncio.create_task(poll_cycle(trigger="manual")),
                  icon='refresh').props('flat color=white dense')

    # Heights budgeted so header + RMS + slider + spectra + table fit a
    # 1920x1080 page with no scrolling; no RMS block title (it collided with
    # the y-axis name — user request 2026-09-09).
    chart = ui.echart(build_plot_options()).classes('w-full h-[26vh]')

    # Slider gets its own full-width row so the track sits visually under
    # the RMS x-axis; the user reads the hovered minute straight off the
    # chart. Position = index into HISTORY_WINDOW, right edge = LIVE; any
    # other position pins that minute — the RMS markLine and the spectra
    # both follow it. max mirrors the actual window length (refresh keeps
    # it in step): a hardcoded 720 left the whole not-yet-filled right
    # side of the track dead after a service restart — the window is
    # in-memory, so any position >= stored points resolved to LIVE and
    # the drag changed nothing (the long-standing "slider does not work"
    # bug, 2026-09-09).
    n0 = max(1, len(HISTORY_WINDOW))
    with ui.row().classes('w-full flex-nowrap items-center gap-3 py-0 my-0'):
        ui.label('Time:').classes('text-xs text-gray-500')
        time_slider = ui.slider(min=0, max=n0, step=1, value=n0).props(
            'dense label-always=false').classes('flex-1')
        time_label = ui.label('').classes('font-mono text-xs text-amber-500')

    with ui.row().classes('w-full flex-nowrap items-center gap-4 py-0 my-0'):
        ui.label('ROACH2:').classes('text-xs text-gray-500')
        spec_radio = ui.radio(
            {a: a for a in obs_utils.ACTIVE_ANTENNAS},
            value=obs_utils.ACTIVE_ANTENNAS[0],
            on_change=lambda _e: update_spec_charts()).props('dense inline').classes(
            'text-xs')

    _last_spec_key = [None]

    pin_ts = [None]  # pinned minute's ts; None = LIVE (follow latest)

    def update_spec_charts(_e=None):  # also wired via radio's on_change lambda
        ant = spec_radio.value
        idx, point = effective_point()
        # Charts only change once a minute; skip the (expensive, ~8k point)
        # re-serialization when nothing changed. Board flips force a redraw
        # via the key's ant component.
        key = (ant, point["ts"] if point else None)
        if key == _last_spec_key[0]:
            return
        _last_spec_key[0] = key
        for pol in (0, 1):
            set_chart_options(spec_charts[pol], build_spec_options(ant, pol, point))

    def on_slider_change(_e=None):  # event arg: wired as slider on_value_change
        n = len(HISTORY_WINDOW)
        idx = time_slider.value
        i = int(idx) if idx is not None else None
        # Slider position = index into HISTORY_WINDOW (max == len); right
        # edge (i == n) = LIVE. Pin by ts, not index — the deque shifts
        # left on every new point.
        pin_ts[0] = HISTORY_WINDOW[i]["ts"] if (i is not None and 0 <= i < n) else None
        idx, point = effective_point()
        time_label.set_text("LIVE" if pin_ts[0] is None else (point["ts"] if point else pin_ts[0]))
        set_chart_options(chart, build_plot_options(pin_idx=idx))
        update_spec_charts()

    time_slider.on_value_change(lambda e: on_slider_change())

    with ui.row().classes('w-full flex-nowrap items-start gap-2'):
        spec_charts = [
            ui.echart(build_spec_options(spec_radio.value, pol, None)).classes(
                'flex-1 min-w-0 h-[22vh]')
            for pol in (0, 1)
        ]

    cols = [
        {'name': 'ant', 'label': 'Ant', 'field': 'ant', 'align': 'left'},
        {'name': 'board', 'label': 'Board', 'field': 'board', 'align': 'left'},
        {'name': 'status', 'label': 'Status', 'field': 'ok', 'align': 'left'},
        {'name': 'link', 'label': 'Link', 'field': 'link', 'align': 'left'},
        {'name': 'rms', 'label': 'RMS p0/p1', 'field': 'rms0', 'align': 'left'},
        {'name': 'rfgain', 'label': 'RF Gain p0/p1 dB', 'field': 'rfg0', 'align': 'left'},
        {'name': 'enabled', 'label': 'Enabled', 'field': 'enabled', 'align': 'left'},
        {'name': 'dgain', 'label': 'DGain', 'field': 'dgain', 'align': 'left'},
        {'name': 'acc_len', 'label': 'Acc Len', 'field': 'acc_len', 'align': 'left'},
        {'name': 'bit_select', 'label': 'Bit Sel', 'field': 'bit_select', 'align': 'left'},
        {'name': 'cal', 'label': 'Cal On/Off', 'field': 'cal', 'align': 'left'},
        {'name': 'clk', 'label': 'Clock MHz', 'field': 'clk', 'align': 'left'},
        {'name': 'pps', 'label': 'PPS', 'field': 'pps', 'align': 'left'},
        {'name': 'error', 'label': 'Error', 'field': 'error', 'align': 'left'},
    ]

    def effective_point():
        """HISTORY_WINDOW point for the slider position: (None, latest) = LIVE.
        A pin whose minute aged out of the 12 h window falls back to LIVE."""
        n = len(HISTORY_WINDOW)
        if not n:
            return None, None
        if pin_ts[0] is None:
            return None, HISTORY_WINDOW[-1]
        for i, p in enumerate(HISTORY_WINDOW):
            if p["ts"] == pin_ts[0]:
                return i, p
        return None, HISTORY_WINDOW[-1]

    def refresh():
        last = MON_STATE.get("last_poll_ts")
        last_label.set_text(f"last poll: {last or '-'}" +
                            (f" ({MON_STATE.get('cycle_s', '?')}s)" if last else ""))
        # The track must mirror the window: it refills minute-by-minute
        # after a restart and ages points off the front at 12 h. (max was
        # hardcoded to 720 — with a partly filled window every position
        # beyond the data resolved to LIVE and the drag did nothing: the
        # long-standing "slider does not work" bug, fixed 2026-09-09.)
        n = len(HISTORY_WINDOW) or 1
        if time_slider._props["max"] != n:
            time_slider._props["max"] = n
            time_slider.update()  # _props edits only reach the client via update()
        idx, point = effective_point()
        if pin_ts[0] is not None and idx is None:
            # Pinned minute aged out of the 12 h window - back to LIVE.
            pin_ts[0] = None
        if pin_ts[0] is None:
            if time_slider.value != n:
                # LIVE: the knob rides the right edge (also catches a pin
                # that just aged out). Assignment fires on_value_change,
                # whose handler re-derives LIVE - harmless duplicate work.
                time_slider.value = n
        elif time_slider.value != idx:
            # Pinned: keep the knob on the pinned minute while the deque
            # shifts beneath it (full window moves it one step per minute).
            time_slider.value = idx
        set_chart_options(chart, build_plot_options(pin_idx=idx))
        time_label.set_text("LIVE" if idx is None else point["ts"] if point else "")
        update_spec_charts()
        table.rows = build_table_rows()
        table.update()

    table = ui.table(columns=cols, rows=build_table_rows(), row_key='ant').classes('w-full mt-2')
    table.add_slot('body', r'''
        <q-tr :props="props" :class="props.row._class">
            <q-td key="ant" :props="props">{{ props.row.ant }}</q-td>
            <q-td key="board" :props="props">{{ props.row.board }}</q-td>
            <q-td key="status" :props="props">{{ props.row.ok ? 'OK' : 'FAIL' }}</q-td>
            <q-td key="link" :props="props">{{ props.row.link }}</q-td>
            <q-td key="rms" :props="props">{{ props.row.rms0 }} / {{ props.row.rms1 }}</q-td>
            <q-td key="rfgain" :props="props">{{ props.row.rfg0 }} / {{ props.row.rfg1 }}</q-td>
            <q-td key="enabled" :props="props">{{ props.row.enabled }}</q-td>
            <q-td key="dgain" :props="props">{{ props.row.dgain }}</q-td>
            <q-td key="acc_len" :props="props">{{ props.row.acc_len }}</q-td>
            <q-td key="bit_select" :props="props">{{ props.row.bit_select }}</q-td>
            <q-td key="cal" :props="props">{{ props.row.cal }}</q-td>
            <q-td key="clk" :props="props">{{ props.row.clk }}</q-td>
            <q-td key="pps" :props="props">{{ props.row.pps }}</q-td>
            <q-td key="error" :props="props">{{ props.row.error }}</q-td>
        </q-tr>
    ''')

    refresh()
    ui.timer(5.0, refresh)


app.on_startup(main)
# No on_disconnect shutdown: the poller is process-global and must keep
# running with zero browser clients.

# HTTPS when a cert pair exists (self-signed; see docs/https_setup.md),
# otherwise plain HTTP so a missing/broken cert can never take the monitor
# down. Behind the nginx proxy (v0.2.3) the pair must NOT exist: a backend
# serving HTTPS would 502 the proxy — TLS terminates at nginx.
_cert_dir = Path(__file__).parent / "certs"
_cert_file = _cert_dir / "monitor.crt"
_key_file = _cert_dir / "monitor.key"
_ssl_kwargs = {}
if _cert_file.is_file() and _key_file.is_file():
    _ssl_kwargs = {"ssl_certfile": str(_cert_file), "ssl_keyfile": str(_key_file)}
    print(f"[MONITOR] HTTPS enabled ({_cert_file})")
else:
    print("[MONITOR] No cert pair in ./certs - serving plain HTTP")

# Loopback-only bind (v0.2.3): the network entry point is the nginx reverse
# proxy (https://atlas/monitor/ -> 127.0.0.1:8083). Do not rebind 0.0.0.0 —
# that would bypass the proxy, and firewalld now blocks 8083 anyway.
ui.run(title=f'FAST ROACH2 Monitor {MONITOR_VERSION}', host='127.0.0.1', port=8083,
       reload=False, show=False, **_ssl_kwargs)
