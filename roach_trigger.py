#!/usr/bin/env python3
# ==================================================================
# ROACH TRIGGER SCRIPT v0.1.0
# ==================================================================

import socket
import time
import argparse
import sys
import subprocess
from datetime import datetime, timezone, timedelta

def check_ntp_sync():
    try:
        output = subprocess.check_output(["timedatectl", "status"]).decode()
        if "System clock synchronized: yes" not in output and "NTP service: active" not in output:
             print("[WARNING] SYSTEM CLOCK NOT SYNCHRONIZED! Timing may be unreliable.")
    except:
        pass 

def create_connection(host, port=7147, timeout=3):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        return s
    except Exception as e:
        print(f"ERROR: Connect {host}: {e}")
        return None

def send_katcp(sock, msg):
    if sock:
        try:
            sock.sendall((msg + '\n').encode('ascii'))
        except Exception as e:
            print(f"ERROR: Send failed: {e}")

def get_target_timestamp(time_str):
    """Parses 'YYYY-MM-DDTHH:MM:SS' (CST) and returns Unix Epoch."""
    try:
        # Define CST as UTC+8 fixed offset
        cst_tz = timezone(timedelta(hours=8))
        
        # Parse the string (Now expects 'T' between date and time)
        dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
        
        # Attach the timezone info
        dt = dt.replace(tzinfo=cst_tz)
        
        # Convert to Unix Timestamp (float)
        return dt.timestamp()
    except ValueError as e:
        print(f"[ERROR] Date format mismatch. Expected 'YYYY-MM-DDTHH:MM:SS', got '{time_str}'")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', '--time', type=str, required=True, help="Target Start Time CST (YYYY-MM-DDTHH:MM:SS)")
    parser.add_argument('-r', '--roaches', nargs='+', required=True)
    args = parser.parse_args()

    check_ntp_sync()

    # 1. Convert Input String to Unix Timestamp
    target_unix = get_target_timestamp(args.time)
    print(f"[TRIGGER] Target: {args.time} CST -> {target_unix:.2f} Unix")

    # 2. Connect
    sockets = []
    print(f"[TRIGGER] Connecting to {len(args.roaches)} boards...")
    for host in args.roaches:
        s = create_connection(host)
        if s: sockets.append((host, s))
        else: sys.exit(1)

    try:
        # 3. HARD RESET
        print("[TRIGGER] Ensuring ARM=0...")
        for host, s in sockets:
            send_katcp(s, "?wordwrite arm 0 0")

        # 4. Precision Wait (Target: T - 0.5s)
        target_trigger = target_unix - 0.5
        
        while True:
            now = time.time()
            wait = target_trigger - now
            
            if wait < -0.1: 
                print(f"[CRITICAL FAILURE] System Lag! Late by {abs(wait):.4f}s.")
                break 

            if wait > 0.05:
                time.sleep(wait - 0.05)
            elif wait > 0:
                pass 
            else:
                # 5. FIRE
                for host, s in sockets:
                    send_katcp(s, "?wordwrite arm 0 3")
                fire_t = time.time()
                print(f"[TRIGGER] Fired at {fire_t:.4f} (Delta: {(fire_t-target_trigger)*1000:.2f}ms)")
                break

    finally:
        for host, s in sockets: s.close()

if __name__ == "__main__":
    main()
