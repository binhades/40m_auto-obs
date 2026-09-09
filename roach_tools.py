#!/usr/bin/env python3
# ==================================================================
# ROACH Hardware Abstraction Layer (HAL)
# Version: v0.1.5
# Updates: read_spectra — pol0/pol1 spectra from u0_x4_vacc_scope_{AA,BB}
#          (arm ctrl=4 then 5, poll _status bit31, blob-read _bram,
#          big-endian int32) + u0_bit_select selectors; raw int32 arrays out
#          (log2/byte extraction happens at plot time). Monitor only.
# v0.1.4:  monitor read support - read_blob (?read with KATCP unescape),
#          snapshot_rms (zdok0_scope ADC capture), read_monitor_snapshot
#          (rfgain/enabled/dgain/acc_len/bit_select/cal/clock/pps).
#          RoachController (driver path) unchanged.
# v0.1.3:  RF gain (I2C via iic_adc0 word ops) + digital gain (u0_gain);
#          per-antenna gain offset table (rfgain_table.lst); log_fn support
# ==================================================================

import math
import socket
import sys
import time
from array import array
from pathlib import Path

# RF frontend gain limits (KATADC rf frontend: 6-bit field, 0.5 dB steps)
RF_GAIN_MIN_DB = -11.5
RF_GAIN_MAX_DB = 20.0

class RoachError(Exception):
    pass

class RoachBoard:
    """
    Low-level interface for a single ROACH board via KATCP.
    Handles socket buffering and protocol cleaning.
    """
    def __init__(self, host, port=7147, timeout=2.0, simulated=False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.simulated = simulated
        self._sock = None
        self._buffer = ""
        self._bin_buffer = b""
        self._sim_iic = {}  # simulated I2C device registers: (unit, dev, reg) -> byte
        self._sim_bram = {}  # simulated blob devices: device name -> bytes

    def connect(self):
        if self.simulated: return
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            # Clear any initial buffer (banners like #version)
            self._buffer = ""
            self._bin_buffer = b""
        except Exception as e:
            raise RoachError(f"Connection to {self.host} failed: {e}")

    def reconnect(self):
        """Drop and re-open the connection. Any half-consumed reply debris
        from a mid-stream interleaved inform dies with the old socket."""
        self.close()
        self.connect()

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except:
                pass
            self._sock = None
            self._buffer = ""
            self._bin_buffer = b""

    def _read_line(self):
        """
        Reads from socket until a newline is found.
        Filters out asynchronous logs (starting with #).
        """
        start_time = time.time()
        while True:
            # Check timeout
            if time.time() - start_time > self.timeout:
                raise RoachError(f"Timeout waiting for response from {self.host}")

            # Process buffer
            if '\n' in self._buffer:
                line, self._buffer = self._buffer.split('\n', 1)
                line = line.strip()
                if not line: continue
                # KATCP Protocol: Ignore lines starting with #
                if line.startswith("#"): 
                    continue 
                return line

            # Fetch more data
            try:
                data = self._sock.recv(4096)
                if not data:
                    raise RoachError("Connection closed by remote host")
                self._buffer += data.decode('ascii', errors='ignore')
            except socket.timeout:
                continue
            except Exception as e:
                # Force close to reset state on error
                self.close() 
                raise RoachError(f"Socket read error: {e}")

    def _send_raw(self, msg):
        if self.simulated:
            print(f"[SIM] {self.host} TX: {msg}")
            if msg.startswith("?wordread"):
                return "!wordread ok 0"
            return "!wordwrite ok"
        
        if not self._sock: self.connect()
        
        try:
            self._sock.sendall((msg + '\n').encode('ascii'))
            return self._read_line()
        except Exception as e:
            # If sending fails, we might need to reconnect next time
            self.close()
            raise RoachError(f"Comm Error on {self.host}: {e}")

    def write_int(self, register, value, verify=True, offset=0):
        """Writes a 32-bit integer to a named register (offset in 32-bit words)."""
        cmd = f"?wordwrite {register} {int(offset)} {int(value)}"
        resp = self._send_raw(cmd)

        if not resp.startswith("!wordwrite ok"):
            raise RoachError(f"{self.host}: Write to {register} failed. Resp: {resp}")

        # Optional Readback Verify
        if verify and not self.simulated:
            read_val = self.read_int(register, offset=offset)
            # Note: Checking exact equality.
            if read_val != int(value):
                # Some registers might change (counters), so be careful with verify=True
                raise RoachError(f"{self.host}: Verify failed for {register}. Wrote {value}, Read {read_val}")

    def read_int(self, register, offset=0):
        """Reads a 32-bit integer from a named register (offset in 32-bit words)."""
        cmd = f"?wordread {register} {int(offset)} 1"
        resp = self._send_raw(cmd)

        # Resp format: !wordread ok <hex_value>
        parts = resp.split()
        if len(parts) < 3 or parts[1] != 'ok':
             raise RoachError(f"{self.host}: Read {register} failed. Resp: {resp}")

        try:
            return int(parts[2], 16)
        except ValueError:
            raise RoachError(f"{self.host}: Invalid hex response for {register}: {parts[2]}")

    # --- I2C via the KATADC IIC controller (unit 0 = iic_adc0) ---
    # Each katadc iic_* operation is a sequence of single 32-bit word writes to a
    # command FIFO (see ./roach2/katadc.py — every blindwrite is one >4B word).
    # FIFO block/unblock live at byte offset 12 = word offset 3; the RX data word
    # lands at byte offset 4 = word offset 1.

    IIC_CTRL_FIFO_BLOCK = 3
    IIC_CTRL_DATA = 0

    def iic_write(self, unit, dev_addr, reg_addr, reg_value):
        """Writes one byte to an I2C device register through iic_adc{unit}."""
        fifo = f"iic_adc{int(unit)}"
        ctrl = 0x0A  # WR|START|LOCK
        self.write_int(fifo, 1, verify=False, offset=self.IIC_CTRL_FIFO_BLOCK)
        self.write_int(fifo, (ctrl << 8) | (((dev_addr & 0x7F) << 1) | 0x0), verify=False, offset=self.IIC_CTRL_DATA)
        self.write_int(fifo, (0x08 << 8) | (reg_addr & 0xFF), verify=False, offset=self.IIC_CTRL_DATA)  # WR|LOCK
        self.write_int(fifo, (0x04 << 8) | (reg_value & 0xFF), verify=False, offset=self.IIC_CTRL_DATA)  # WR|STOP
        self.write_int(fifo, 0, verify=False, offset=self.IIC_CTRL_FIFO_BLOCK)
        if self.simulated:
            self._sim_iic[(int(unit), dev_addr & 0x7F, reg_addr & 0xFF)] = reg_value & 0xFF

    def iic_read(self, unit, dev_addr, reg_addr):
        """Reads one byte from an I2C device register through iic_adc{unit}."""
        fifo = f"iic_adc{int(unit)}"
        ctrl = 0x0A  # WR|START|LOCK
        self.write_int(fifo, 1, verify=False, offset=self.IIC_CTRL_FIFO_BLOCK)
        self.write_int(fifo, (ctrl << 8) | (((dev_addr & 0x7F) << 1) | 0x0), verify=False, offset=self.IIC_CTRL_DATA)
        self.write_int(fifo, (0x08 << 8) | (reg_addr & 0xFF), verify=False, offset=self.IIC_CTRL_DATA)  # WR|LOCK
        self.write_int(fifo, (ctrl << 8) | (((dev_addr & 0x7F) << 1) | 0x1), verify=False, offset=self.IIC_CTRL_DATA)  # repeated START, RD
        self.write_int(fifo, (0x05 << 8) | 0x00, verify=False, offset=self.IIC_CTRL_DATA)  # RD|STOP
        self.write_int(fifo, 0, verify=False, offset=self.IIC_CTRL_FIFO_BLOCK)
        if self.simulated:
            return self._sim_iic.get((int(unit), dev_addr & 0x7F, reg_addr & 0xFF), 0)
        time.sleep(0.1)
        word = self.read_int(fifo, offset=1)
        return word & 0xFF

    # --- Blob reads (?read) for snapshot/bram devices ---
    # Wire facts (verified against katcp 0.6.2/0.9.3 core.py + hardware):
    # reply is "!read ok <escaped-data>" — no offset field. Escaping encodes
    # ONLY the 7 special bytes (ESCAPE_RE [\\ \0\n\r\x1b\t]); every other byte
    # including >=0x80 passes through raw. Selectors per ESCAPE_LOOKUP:
    # '\\'->0x5C '_'->0x20 '0'->0x00 'n'->0x0A 'r'->0x0D 'e'->0x1B 't'->0x09
    # and '@'->empty (encodes an empty argument; appended as nothing).
    _KATCP_UNESCAPE = {ord('\\'): 0x5C, ord('_'): 0x20, ord('0'): 0x00,
                       ord('n'): 0x0A, ord('r'): 0x0D, ord('e'): 0x1B,
                       ord('t'): 0x09, ord('@'): None}

    @classmethod
    def _katcp_unescape(cls, payload):
        """Bytes-level, spec-exact with katcp Message._parse_arg: all bytes
        pass through except 0x5C+selector pairs; unknown selector or trailing
        lone 0x5C raises RoachError (protocol violation)."""
        out = bytearray()
        i = 0
        n = len(payload)
        while i < n:
            b = payload[i]
            if b == 0x5C:
                if i + 1 >= n:
                    raise RoachError("KATCP escape at end of payload")
                sel = payload[i + 1]
                mapped = cls._KATCP_UNESCAPE.get(sel)
                if mapped is None and sel not in cls._KATCP_UNESCAPE:
                    raise RoachError(f"Invalid KATCP escape selector 0x{sel:02x}")
                if mapped is not None:
                    out.append(mapped)
                i += 2
            else:
                out.append(b)
                i += 1
        return bytes(out)

    def _read_reply_bytes(self, expect):
        """Reads one full reply line as raw bytes, skipping # inform lines.
        Only a trailing CR is removed — payload bytes may be whitespace."""
        start_time = time.time()
        buf = getattr(self, '_bin_buffer', b'')
        sock = self._sock
        while True:
            if time.time() - start_time > self.timeout:
                raise RoachError(f"Timeout waiting for response from {self.host}")
            if b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                self._bin_buffer = buf
                if line.endswith(b'\r'):
                    line = line[:-1]
                if not line:
                    continue
                if line.startswith(b'#'):
                    continue  # async log inform
                if not line.startswith(expect):
                    self.close()
                    raise RoachError(f"{self.host}: expected {expect.decode()}, got: {line[:60]!r}")
                return line
            try:
                data = sock.recv(65536)
                if not data:
                    raise RoachError("Connection closed by remote host")
                buf += data
            except socket.timeout:
                continue
            except RoachError:
                raise
            except Exception as e:
                self.close()
                raise RoachError(f"Socket read error: {e}")

    def read_blob(self, device, size, offset=0):
        """Reads `size` raw bytes from a bram/snapshot device via ?read."""
        if self.simulated:
            data = self._sim_bram.get(device, b'')
            return data[offset:offset + size]

        if not self._sock:
            self.connect()
        try:
            self._sock.sendall(f"?read {device} {int(offset)} {int(size)}\n".encode('ascii'))
            line = self._read_reply_bytes(b"!read")
            # !read ok <escaped-data>
            parts = line.split(b' ', 2)
            if len(parts) < 3 or parts[1] != b'ok':
                raise RoachError(f"{self.host}: read {device} failed: {line[:60]!r}")
            data = self._katcp_unescape(parts[2])
            if len(data) != int(size):
                raise RoachError(f"{self.host}: read {device} short: got {len(data)} of {size} bytes")
            return data
        except RoachError:
            raise
        except Exception as e:
            self.close()
            raise RoachError(f"Comm Error on {self.host}: {e}")

    def snapshot_rms(self, unit=0):
        """ADC time-domain RMS per pol for one zdok unit (raw 8-bit codes,
        full scale ~128). Snapshot device is zdok{u}_scope; katcp_wrapper's
        snapshot_arm/get append _ctrl/_status/_bram to that full name.
        Samples are 4-way interleaved (even rows -> pol0/I, odd -> pol1/Q)."""
        adc = f"zdok{int(unit)}_scope"
        self.write_int(f"{adc}_ctrl", 6, verify=False)  # enable + man_trig + man_valid
        self.write_int(f"{adc}_ctrl", 7, verify=False)  # + trigger

        if self.simulated:
            data = self._sim_bram.get(f"{adc}_bram")
            if not data:
                raise RoachError(f"{self.host}: no simulated {adc}_bram content")
            size = len(data)
        else:
            deadline = time.time() + 3.0
            size = 0
            while time.time() < deadline:
                status = self.read_int(f"{adc}_status")
                size = status & 0x7FFFFFFF
                if not (status & 0x80000000):
                    break
                time.sleep(0.05)
            else:
                raise RoachError(f"{self.host}: {adc} did not finish capturing")
            if size == 0:
                raise RoachError(f"{self.host}: {adc} captured 0 bytes")
            data = self.read_blob(f"{adc}_bram", size)

        samples = array('b')  # signed byte
        samples.frombytes(data)
        # mbc.py split_snapshot: reshape(-1, 4), even rows -> pol0, odd rows ->
        # pol1. So the stream is 4-sample groups alternating P0, P1, P0, P1...
        n_groups = len(samples) // 4
        p0_sq = p1_sq = 0.0
        n0 = n1 = 0
        for g in range(n_groups):
            base = g * 4
            sq = 0.0
            for k in range(4):
                v = samples[base + k]
                sq += v * v
            if g % 2 == 0:
                p0_sq += sq
                n0 += 4
            else:
                p1_sq += sq
                n1 += 4
        if n0 == 0 or n1 == 0:
            raise RoachError(f"{self.host}: {pre}_scope too short for 2-pol split")
        return [math.sqrt(p0_sq / n0), math.sqrt(p1_sq / n1)]

# --- RF Frontend Gain (KATADC rf frontend on the ADC's I2C GPIO expander) ---
# Ported from ./roach2/katadc.py rf_fe_set/rf_fe_get. Pol 0 = I (Pol-A), 1 = Q (Pol-B).
# Unit 0 only (Core Array dishes use unit 0 exclusively).

def rf_fe_set(board, pol, gain_db, enabled=True):
    if pol not in (0, 1):
        raise RoachError(f"Invalid pol {pol} (must be 0=I or 1=Q)")
    if gain_db < RF_GAIN_MIN_DB:
        raise RoachError(f"Invalid gain {gain_db} dB. Valid range is {RF_GAIN_MIN_DB} to +{RF_GAIN_MAX_DB} dB.")
    if gain_db > RF_GAIN_MAX_DB:
        raise RoachError(f"Invalid gain {gain_db} dB. Valid range is {RF_GAIN_MIN_DB} to +{RF_GAIN_MAX_DB} dB. "
                         f"Values above +{RF_GAIN_MAX_DB} dB would silently overflow the 6-bit gain field.")
    dev_addr = 0x20 + pol
    reg_bitmap = 0x40 + ((0x80 if enabled else 0)) + int(gain_db * 2 + 23)
    board.iic_write(0, dev_addr, 6, 0x00)   # output enable (active low) for byte 2
    board.iic_write(0, dev_addr, 2, reg_bitmap & 0xFF)

def rf_fe_get(board, pol):
    if pol not in (0, 1):
        raise RoachError(f"Invalid pol {pol} (must be 0=I or 1=Q)")
    bitmap = board.iic_read(0, 0x20 + pol, 2)
    return {'enabled': bool(bitmap >> 7),
            'gain': RF_GAIN_MIN_DB + (bitmap & 0x3F) / 2.0}


def read_monitor_snapshot(board, fpga_clk=250e6):
    """One board's full monitor read set for obs_monitor. Read-only except the
    zdok0_ctrl snapshot trigger. Returns a flat dict; raises RoachError on any
    failure so the caller can log ok=false.

    Counters (pps_counter, sys_clkcounter) are double-read across one shared
    ~1.05 s window: a counter is only meaningful when seen advancing, and the
    clock rate is the delta. Callers should budget ~2-2.5 s per board."""
    w0 = time.time()
    clk0 = board.read_int("sys_clkcounter")
    pps0 = board.read_int("pps_counter")
    rms = board.snapshot_rms(0)
    time.sleep(max(0.0, 1.05 - (time.time() - w0)))
    clk1 = board.read_int("sys_clkcounter")
    pps1 = board.read_int("pps_counter")
    elapsed = time.time() - w0

    delta = (clk1 - clk0) & 0xFFFFFFFF  # 32-bit wraparound-safe
    clk_mhz = round(delta / elapsed / 1e6, 3)

    rfg = [rf_fe_get(board, pol) for pol in (0, 1)]
    gain_raw = board.read_int("u0_gain")

    # u0_bit_select packs four 2-bit selectors (selector i = bits 2i..2i+1,
    # lowest first per mbc.py); selector i picks the 8-bit byte position
    # [0-3] extracted from the 32-bit accumulated value for path i.
    bs = board.read_int("u0_bit_select")
    bit_select_str = "".join(str((bs >> (2 * k)) & 0b11) for k in range(4))

    def cal_seconds(reg):
        lo = board.read_int(reg)
        hi = board.read_int(reg + "_hipart")
        return ((hi << 32) | lo) / fpga_clk

    return {
        "rms": [round(rms[0], 2), round(rms[1], 2)],
        "rfgain_db": [rfg[0]['gain'], rfg[1]['gain']],
        "enabled": [rfg[0]['enabled'], rfg[1]['enabled']],
        "dgain": gain_raw & 0xFFFF,
        "acc_len": board.read_int("u0_acc_len"),
        "bit_select": bit_select_str,
        "cal_on_s": round(cal_seconds("noisecal_on"), 3),
        "cal_off_s": round(cal_seconds("noisecal_off"), 3),
        "clk_mhz": clk_mhz,
        "pps_ok": pps1 != pps0,
    }


def read_spectra(board, unit=0, deadline_s=3.0):
    """Pol0/pol1 spectra of one vacc unit: u{unit}_x4_vacc_scope_{AA,BB}.
    Reference: roach2/mbc.py get_mb_scopes — the vacc output stream is the
    capture trigger, so the scope is armed with man_valid ONLY (ctrl value 4,
    then 5 to start; no man_trig bit), _status bit31 clears when done and the
    low bits give the byte count. Words are big-endian signed 32-bit.
    Returns raw int32 host-order arrays; callers derive the display form
    (log2(|v|+1) or the bit-selected byte) at plot time. Raises RoachError
    on any failure.

    One retry on a fresh connection (board.reconnect): the board's kcs
    broadcasts '#log info ... received_end_of_file_from_<ip>' informs when
    ANY client disconnects (the monitor's own 1 s link-watch does this every
    second), and one of those informs can land mid-reply — it breaks line
    framing so the NEXT reply is unparseable. A reconnect drops the debris."""
    prefix = f"u{int(unit)}_"

    def capture_scope(product):
        dev = f"{prefix}x4_vacc_scope_{product}"
        if board.simulated:
            data = board._sim_bram.get(f"{dev}_bram")
            if not data:
                raise RoachError(f"{board.host}: no simulated {dev}_bram content")
            size = len(data)
            data_out = data
        else:
            # snapshot_arm(man_trig=False, man_valid=True): bit1=man_trig,
            # bit2=man_valid (roach2/katcp_wrapper.py snapshot_arm).
            board.write_int(f"{dev}_ctrl", 4, verify=False)
            board.write_int(f"{dev}_ctrl", 5, verify=False)
            deadline = time.time() + deadline_s
            size = 0
            while time.time() < deadline:
                status = board.read_int(f"{dev}_status")
                size = status & 0x7FFFFFFF
                if not (status & 0x80000000):
                    break
                time.sleep(0.05)
            else:
                raise RoachError(f"{board.host}: {dev} did not finish capturing")
            if size == 0:
                raise RoachError(f"{board.host}: {dev} captured 0 bytes")
            data_out = board.read_blob(f"{dev}_bram", size)
        return data_out

    for attempt in (1, 2):
        try:
            spec = []
            for product in ("AA", "BB"):
                data = capture_scope(product)
                words = array('i')  # signed 32-bit, host order
                words.frombytes(data[:len(data) - (len(data) % 4)])
                if not board.simulated and sys.byteorder == 'little':
                    words.byteswap()  # bram words are big-endian
                spec.append(words)

            if len(spec[0]) == 0 or len(spec[1]) == 0:
                raise RoachError(f"{board.host}: empty spectra from {prefix}x4_vacc_scope")

            # u0_bit_select packs four 2-bit selectors, one per Stokes
            # product in AA,BB,CR,CI order (mbc.py on_bitsel_change);
            # selector i picks byte position [0-3] of the 32-bit value.
            bs = board.read_int(f"{prefix}bit_select")
            sels = [(bs >> (2 * k)) & 0b11 for k in (0, 1)]

            return {"n_ch": min(len(spec[0]), len(spec[1])), "bitsel": sels,
                    "spec": spec}
        except RoachError:
            if attempt == 2:
                raise
            try:
                board.reconnect()  # discard any interleaved-inform debris
            except (RoachError, OSError):
                raise


class RoachController:
    """
    High-level Science Interface.
    Translates physical units (seconds, Hz) into Register Values.
    """
    def __init__(self, host_map, fpga_clk, simulated=False, log_fn=None):
        """
        :param host_map: Dict of {antenna_name: roach_ip_or_hostname}
        :param fpga_clk: FPGA Clock rate in Hz (REQUIRED)
        :param log_fn: callable(str) for warnings/info (default: print)
        """
        if not fpga_clk:
            raise ValueError("fpga_clk is required for RoachController")

        self.fpga_clk = fpga_clk
        self.simulated = simulated
        self.log = log_fn if log_fn else print
        self.boards = {}

        for ant, host in host_map.items():
            self.boards[ant] = RoachBoard(host, simulated=simulated)

        self.rfgain_table = self._load_rfgain_table(Path(__file__).parent / "rfgain_table.lst")

    def _load_rfgain_table(self, filepath):
        """Per-antenna RF gain offsets in dB: lines of `CA01, <pol0_dB>, <pol1_dB>`."""
        table = {}
        try:
            with open(filepath) as f:
                for lineno, line in enumerate(f, 1):
                    line = line.split('#', 1)[0].strip()
                    if not line:
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    try:
                        table[parts[0]] = (float(parts[1]), float(parts[2]))
                    except (IndexError, ValueError):
                        self.log(f"WARNING: rfgain_table.lst line {lineno} malformed, skipped: {line}")
        except OSError:
            self.log(f"WARNING: {filepath.name} not found - RF gain offsets unavailable (user value applied as-is)")
        return table

    def configure_accumulation(self, integ_time_us, psr_enabled=False):
        acc_len = 5 # Default
        if psr_enabled:
            if integ_time_us == "50us": acc_len = 5
            elif integ_time_us == "100us": acc_len = 11
            elif integ_time_us == "200us": acc_len = 23
            else:
                self.log(f"WARNING: Unknown psr_integ '{integ_time_us}' - acc_len falling back to 5 (50us)")
            
        for ant, board in self.boards.items():
            try:
                board.write_int("u0_acc_len", acc_len)
                board.write_int("u1_acc_len", acc_len)
            except RoachError as e:
                self.log(f"WARNING: Failed to set AccLen on {ant}: {e}")

    def configure_noise_diode(self, cal_on_sec, cal_off_sec):
        on_cnt = int(float(cal_on_sec) * self.fpga_clk)
        off_cnt = int(float(cal_off_sec) * self.fpga_clk)
        
        on_hi = on_cnt >> 32
        on_lo = on_cnt & 0xFFFFFFFF
        off_hi = off_cnt >> 32
        off_lo = off_cnt & 0xFFFFFFFF
        
        for ant, board in self.boards.items():
            try:
                board.write_int("noisecal_delay_hipart", 0, verify=False)
                board.write_int("noisecal_delay", 0, verify=False)
                board.write_int("noisecal_on_hipart", on_hi)
                board.write_int("noisecal_on", on_lo)
                board.write_int("noisecal_off_hipart", off_hi)
                board.write_int("noisecal_off", off_lo)
            except RoachError as e:
                self.log(f"WARNING: Failed to set NoiseCal on {ant}: {e}")

    def configure_gains(self, rfgain_db=None, dgain=None):
        """
        Applies per task: rfgain_db = user RF gain (dB); actual = user + per-antenna
        offset from rfgain_table.lst. dgain = digital gain 0-65535, written to u0_gain
        with the same value in both 16-bit halves. Unit 0 only.
        """
        for ant, board in self.boards.items():
            try:
                if dgain is not None:
                    d = int(dgain) & 0xFFFF
                    board.write_int("u0_gain", (d << 16) | d)

                if rfgain_db is not None:
                    offsets = self.rfgain_table.get(ant)
                    if offsets is None:
                        self.log(f"WARNING: {ant} not in rfgain_table.lst - applying RF gain {rfgain_db} dB without offset")
                        offsets = (0.0, 0.0)
                    for pol, offset in zip((0, 1), offsets):
                        final = float(rfgain_db) + offset
                        rf_fe_set(board, pol, final)
                        readback = rf_fe_get(board, pol)
                        if abs(readback['gain'] - final) > 0.01:
                            raise RoachError(f"{ant} pol{pol} gain verify failed: set {final}, read {readback['gain']}")
            except RoachError as e:
                self.log(f"WARNING: Failed to set gains on {ant}: {e}")

    def arm_trigger(self):
        for ant, board in self.boards.items():
            try:
                board.write_int("arm", 0, verify=False)
            except RoachError as e:
                self.log(f"WARNING: Failed to ARM {ant}: {e}")

    def fire_trigger(self):
        for ant, board in self.boards.items():
            try:
                board.write_int("arm", 3, verify=False)
            except RoachError as e:
                self.log(f"WARNING: Failed to FIRE {ant}: {e}")

    def close(self):
        for board in self.boards.values():
            board.close()
