#!/usr/bin/env python3
# ==================================================================
# ROACH Hardware Abstraction Layer (HAL)
# Version: v0.1.2
# Updates: Fixed KATCP handshake (ignores #version banners)
# ==================================================================

import socket
import time

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

    def connect(self):
        if self.simulated: return
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            # Clear any initial buffer (banners like #version)
            self._buffer = "" 
        except Exception as e:
            raise RoachError(f"Connection to {self.host} failed: {e}")

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except:
                pass
            self._sock = None
            self._buffer = ""

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
            return "!ok"
        
        if not self._sock: self.connect()
        
        try:
            self._sock.sendall((msg + '\n').encode('ascii'))
            return self._read_line()
        except Exception as e:
            # If sending fails, we might need to reconnect next time
            self.close()
            raise RoachError(f"Comm Error on {self.host}: {e}")

    def write_int(self, register, value, verify=True):
        """Writes a 32-bit integer to a named register."""
        cmd = f"?wordwrite {register} 0 {int(value)}"
        resp = self._send_raw(cmd)
        
        if not resp.startswith("!wordwrite ok"):
            raise RoachError(f"{self.host}: Write to {register} failed. Resp: {resp}")

        # Optional Readback Verify
        if verify and not self.simulated:
            read_val = self.read_int(register)
            # Note: Checking exact equality. 
            if read_val != int(value):
                # Some registers might change (counters), so be careful with verify=True
                raise RoachError(f"{self.host}: Verify failed for {register}. Wrote {value}, Read {read_val}")

    def read_int(self, register):
        """Reads a 32-bit integer from a named register."""
        cmd = f"?wordread {register} 0 1"
        resp = self._send_raw(cmd)
        
        # Resp format: !wordread ok <hex_value>
        parts = resp.split()
        if len(parts) < 3 or parts[1] != 'ok':
             raise RoachError(f"{self.host}: Read {register} failed. Resp: {resp}")
        
        try:
            return int(parts[2], 16)
        except ValueError:
            raise RoachError(f"{self.host}: Invalid hex response for {register}: {parts[2]}")

class RoachController:
    """
    High-level Science Interface.
    Translates physical units (seconds, Hz) into Register Values.
    """
    def __init__(self, host_map, fpga_clk, simulated=False):
        """
        :param host_map: Dict of {antenna_name: roach_ip_or_hostname}
        :param fpga_clk: FPGA Clock rate in Hz (REQUIRED)
        """
        if not fpga_clk:
            raise ValueError("fpga_clk is required for RoachController")
            
        self.fpga_clk = fpga_clk
        self.boards = {}
        
        for ant, host in host_map.items():
            self.boards[ant] = RoachBoard(host, simulated=simulated)

    def configure_accumulation(self, integ_time_us, psr_enabled=False):
        acc_len = 5 # Default
        if psr_enabled:
            if integ_time_us == "50us": acc_len = 5
            elif integ_time_us == "100us": acc_len = 11
            elif integ_time_us == "200us": acc_len = 23
            
        for ant, board in self.boards.items():
            try:
                board.write_int("u0_acc_len", acc_len)
                board.write_int("u1_acc_len", acc_len)
            except RoachError as e:
                print(f"WARNING: Failed to set AccLen on {ant}: {e}")

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
                print(f"WARNING: Failed to set NoiseCal on {ant}: {e}")

    def arm_trigger(self):
        for ant, board in self.boards.items():
            try:
                board.write_int("arm", 0, verify=False)
            except RoachError as e:
                print(f"WARNING: Failed to ARM {ant}: {e}")

    def fire_trigger(self):
        for ant, board in self.boards.items():
            try:
                board.write_int("arm", 3, verify=False)
            except RoachError as e:
                print(f"WARNING: Failed to FIRE {ant}: {e}")

    def close(self):
        for board in self.boards.values():
            board.close()
