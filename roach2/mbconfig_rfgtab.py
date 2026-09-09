#!/usr/bin/env python2

from __future__ import print_function

import sys
import time
import struct
import datetime
import argparse
import katadc
import katcp_wrapper


def load_rfgain_table(filepath):
    table = {}
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('Beam'):
                continue
            parts = [p.strip() for p in line.split(',')]
            beam_id = int(parts[0])
            table[beam_id] = (float(parts[1]), float(parts[2]))
    return table


def get_unit_index(unit):
    if unit == 0 or unit == 1:
        u = unit
    elif unit == 'u0' or unit == 'u1':
        u = int(unit[1])
    else:
        raise ValueError('Invalid unit name ' + unit)
    return u


def get_unit_prefix(unit):
    if unit == 0 or unit == 1:
        u = 'u' + str(unit) + '_'
    elif unit == 'u0' or unit == 'u1':
        u = unit + '_'
    else:
        raise ValueError('Invalid unit name "' + unit + '"')
    return u


def read_unit_config(fpga, unit):
    u = get_unit_prefix(unit)
    cfg = {}
    cfg['beam_id'] = fpga.read_uint(u + 'beam_id')
    cfg['fftshift'] = fpga.read_uint(u + 'fft_shift')
    dgain = fpga.read_uint(u + 'gain')
    cfg['dgain'] = [dgain & 0xFFFF, dgain >> 16]
    cfg['acclen'] = fpga.read_uint(u + 'acc_len')
    bs = fpga.read_uint(u + 'bit_select')
    cfg['bitsel'] = [bs & 0b11, bs >> 2 & 0b11, bs >> 4 & 0b11, bs >> 6 & 0b11]
    unit_index = get_unit_index(unit)
    rfgain0 = katadc.rf_fe_get(fpga, unit_index, 'I')
    rfgain1 = katadc.rf_fe_get(fpga, unit_index, 'Q')
    cfg['rfgain'] = (rfgain0['gain'], rfgain1['gain'])
    return cfg


def list_board_config(roach, fpga):
    cfg = { }
    rcs_id = fpga.read_uint('rcs_id')
    rcs_ver = fpga.read_uint('rcs_ver')
    rcs_ts = fpga.read_uint('rcs_timestamp')
    cfg['rcs_id'] = str(struct.pack('>I', rcs_id))
    cfg['rcs_ver'] = 'v%d.%d' % (rcs_ver >> 16, rcs_ver & 0xFFFF)
    cfg['rcs_ts'] = datetime.datetime.fromtimestamp(rcs_ts).strftime('%Y-%m-%d %H:%M:%S')
    for u in (0, 1):
        cfg[u] = read_unit_config(fpga, u)
    board = '  '.join((roach, cfg['rcs_id'], cfg['rcs_ver'])) + '    '
    unit = '{:2d} {:5.1f}/{:.1f}dB  {:04X}  {:04X} {:04X} {:4d}  {:d} {:d} {:d} {:d}'.format(
            cfg[0]['beam_id'], cfg[0]['rfgain'][0], cfg[0]['rfgain'][1], cfg[0]['fftshift'],
            cfg[0]['dgain'][0], cfg[0]['dgain'][1], cfg[0]['acclen'],
            cfg[0]['bitsel'][0], cfg[0]['bitsel'][1], cfg[0]['bitsel'][2], cfg[0]['bitsel'][3])
    print(board + unit)
    unit = '{:2d} {:5.1f}/{:.1f}dB  {:04X}  {:04X} {:04X} {:4d}  {:d} {:d} {:d} {:d}'.format(
            cfg[1]['beam_id'], cfg[1]['rfgain'][0], cfg[1]['rfgain'][1], cfg[1]['fftshift'],
            cfg[1]['dgain'][0], cfg[1]['dgain'][1], cfg[1]['acclen'],
            cfg[1]['bitsel'][0], cfg[1]['bitsel'][1], cfg[1]['bitsel'][2], cfg[1]['bitsel'][3])
    print(' '*len(board) + unit)


def update_unit_config(fpga, unit, opts, rfg_table=None):
    u = get_unit_prefix(unit)
    if opts.acclen is not None:
        fpga.write_int(u + 'acc_len', opts.acclen)
    if opts.dgain is not None:
        if opts.dgain[:2] == '0x' or opts.dgain[:2] == '0X':
            dgain = int(opts.dgain, 16)
        elif opts.dgain[:2] == '0b' or opts.dgain[:2] == '0B':
            dgain = int(opts.dgain, 2)
        else:
            dgain = int(opts.dgain, 10)
        fpga.write_int(u + 'gain', dgain << 16 | dgain)
    if opts.bitsel is not None:
        fpga.write_int(u + 'bit_select', opts.bitsel << 6 | opts.bitsel << 4 | opts.bitsel << 2 | opts.bitsel )
    if opts.rfgain is not None:
        unit_index = get_unit_index(unit)
        gain_I = opts.rfgain
        gain_Q = opts.rfgain
        if rfg_table is not None:
            beam_id = fpga.read_uint(u + 'beam_id')
            if beam_id in rfg_table:
                offset_I, offset_Q = rfg_table[beam_id]
                gain_I = opts.rfgain + offset_I
                gain_Q = opts.rfgain + offset_Q
        katadc.rf_fe_set(fpga, unit_index, 'I', gain_I)
        katadc.rf_fe_set(fpga, unit_index, 'Q', gain_Q)


def reprogram_board(fpga, opts, rfg_table=None):
    rcs_id = str(struct.pack('>I', fpga.read_uint('rcs_id')))
    # same model, no need to reprogram, only update registers
    if rcs_id[2:] == opts.model:
        for u in (0, 1):
            update_unit_config(fpga, u, opts, rfg_table)
        return
    # backup configuration parameter
    cfg = []
    for u in (0, 1):
        cfg.append(read_unit_config(fpga, u))
    # program new bof file
    boffile = 'mb' + opts.model + '-latest.bof.gz'
    fpga.progdev(boffile)
    # rewrite registers
    for u in (0, 1):
        prefix = get_unit_prefix(u)
        fpga.write_int(prefix + 'beam_id', cfg[u]['beam_id'])
        fpga.write_int(prefix + 'fft_shift', cfg[u]['fftshift'])
        fpga.write_int(prefix + 'gain', cfg[u]['dgain'][1] << 16 | cfg[u]['dgain'][1])
        fpga.write_int(prefix + 'acc_len', cfg[u]['acclen'])
        fpga.write_int(prefix + 'bit_select',
            cfg[u]['bitsel'][3] << 6 | cfg[u]['bitsel'][2] << 4 | cfg[u]['bitsel'][1] << 2 | cfg[u]['bitsel'][0])
        for i in range(4):
            # beam_id is the 3rd octec of mcast address
            netif = 'xgbe{:d}'.format(u * 4 + i)
            fpga.write_int(netif + '_dest_ip', (239 << 24) | (1 << 16) | (cfg[u]['beam_id'] << 8) | (i + 1))
            fpga.write_int(netif + '_dest_port', 12345)
            # 8 servers in 1 group
            group = (cfg[u]['beam_id'] - 1) // 8 + 1
            index = (cfg[u]['beam_id'] - 1) % 8 + 1
            ipaddr = (192 << 24) | (168 << 16) | ((10 + group) << 8) | (index * 10 + u * 4 + i + 1)
            fpga.tap_start(netif, netif + '_core', 0x0200 << 32 | ipaddr, ipaddr, 33333)
        # update registers if required
        update_unit_config(fpga, u, opts, rfg_table)
        # rfgain wont't be changed during reprogram
    # reset board
    fpga.write_int('reset', 0)
    fpga.write_int('reset', 3)


def process_board(roach, opts, rfg_table=None):
    try:
        fpga = katcp_wrapper.FpgaClient(roach)
        time.sleep(0.01)
        if not fpga.is_connected():
            return False
        # the "list" parameter is mutual exclusive to other command line arguments
        if opts.list is True:
            list_board_config(roach, fpga)
        else:
            # first reprogramming FPGA if required
            if opts.model is not None:
                print('Reprogramming ' + roach + ' ... ', end='')
                reprogram_board(fpga, opts, rfg_table)
                print('done.')
            # then check if we need update registers
            do_reconfig = False
            for val in (opts.acclen, opts.rfgain, opts.dgain, opts.bitsel):
                if val is not None:
                    do_reconfig = True
                    break
            if do_reconfig:
                print('Reconfiguring ' + roach + ' ... ', end='')
                for unit in (0, 1):
                    update_unit_config(fpga, unit, opts, rfg_table)
                print('done.')
    finally:
        if fpga.is_connected():
            fpga.stop()

    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Configure 19-beam ROACH2 firmware')
    parser.add_argument('-l', '--list', action='store_true', help='List parameters in all ROACH2')
    parser.add_argument('-m', '--model', choices=['1k', '2k', '4k', '8k'], help='Output spectral channels')
    parser.add_argument('-a', '--acclen', type=int, help='Accumulation length')
    parser.add_argument('-g', '--rfgain', type=float, help='Gain of frontend RF amplifier')
    parser.add_argument('-d', '--dgain', help='Digital gain')
    parser.add_argument('-b', '--bitsel', type=int, choices=[0, 1, 2, 3], help='Selected 8 bits from 32 bits word')
    opts = parser.parse_args()

    if len(sys.argv) <= 1:
		parser.print_usage()
		exit()

    with open('mb.lst', 'r') as f:
        ROACH2_BOARDS = f.read().splitlines()

    rfg_table = load_rfgain_table('rfg_tab.lst')

    # print table header if in "list" mode
    if opts.list is True:
        print('ROACH MODEL VERSION  BEAM  RFGAIN SHIFT  DGAIN 0/1  ACC   BITSEL')
        print('-' * 64)

    for roach in ROACH2_BOARDS:
        process_board(roach, opts, rfg_table)
