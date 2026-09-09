#!/usr/bin/env python3

# -*- coding: utf-8 -*-

#############################################################################
#
#           FAST 19-Beam Digital Receiver Firmware Controller
#
#   ZHY 2025-10-27: Adapted to python3 and pyqt5 for 40m array
#
#############################################################################

from __future__ import print_function

import os
import time
import socket
import datetime
import random
import struct
import logging
import os.path
import threading
from packaging import version
import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

import katcp
import katcp_wrapper
import katadc
from mbv import Plotter

GODMODE  = True
LOG2FILE = True

FPGA_CLOCK       = 250e6        # Hz
#FPGA_CLOCK       = 901.12e6/4
SCOPE_IDLE_FLAG  = 0x12345678
POLLING_INTERVAL = 1            # Second

#roach_list = ['r1745', 'r1746', 'r1747', 'r1748', 'r1749', 'r1750',
#              'r1801', 'r1802', 'r1803', 'r1805', 'r1806', 'r1807']
roach_list = [ 'r2170', 'r2171', 'r2172', 'r2173' ]

# Define main window class from template
path = os.path.dirname(os.path.abspath(__file__))
uiFile = os.path.join(path, 'mbc.ui')
WindowTemplate, TemplateBaseClass = pg.Qt.loadUiType(uiFile)

class MainForm(TemplateBaseClass):

    sig_update_plot = QtCore.pyqtSignal(tuple, list, name='sigUpdatePlot')

    def __init__(self):
        super(TemplateBaseClass, self).__init__()

        self.setup_ui()

        self.plotter = Plotter(self.ui.plot_area)
        self.bottom_line = self.ui.plot_area.addLayout(row=4, col=0, colspan=2)
        self.lbl_clkstate = self.bottom_line.addLabel('', col=0, justify='left')
        self.lbl_ppsstate = self.bottom_line.addLabel('', col=1, justify='left')
        self.lbl_lastupdate = self.bottom_line.addLabel('', col=4, colspan=4, justify='right')

        self.bottom_line.setSpacing(0)
        self.bottom_line.setContentsMargins(0, 0, 0, 0)
        self.bottom_line.setFixedHeight(self.lbl_clkstate.height())

        self.sig_update_plot.connect(self.on_update_plot)

        self.fpga = None
        self.unit = 0
        self.prefix = 'u%d_' % self.unit

        # Generate a unique id(hopefully) to exclusively access rorach2 snapshots
        random.seed()
        self.id = random.getrandbits(32)
        log.info('Unique id = %08x' % self.id)

        # For polling scopes
        self.poller_thread = None
        self.poller_event = threading.Event()

    def setup_ui(self):
        # Create the main window
        self.ui = WindowTemplate()
        self.ui.setupUi(self)
        self.setFont(QtGui.QFont('Arial', 10))

        # Fill beam_id and bit_select combobox
        for i in range(1, 20):
            self.ui.cbo_beamid.addItem('%d' % i)
        self.ui.cbo_beamid.setCurrentIndex(-1)
        for r in roach_list:
            self.ui.cbo_roach.addItem(r)
        self.ui.cbo_roach.setCurrentIndex(-1)

        # Connect signal/slots
        self.ui.cbo_roach.currentIndexChanged.connect(self.on_board_change)
        self.ui.edt_noisecal_delay.editingFinished.connect(self.on_noisecal_delay_change)
        self.ui.edt_noisecal_on.editingFinished.connect(self.on_noisecal_on_change)
        self.ui.edt_noisecal_off.editingFinished.connect(self.on_noisecal_off_change)
        self.ui.rb_unit0.toggled.connect(self.on_unit_change)
        self.ui.rb_unit1.toggled.connect(self.on_unit_change)
        self.ui.cb_tvg.stateChanged.connect(self.on_tvg_change)
        self.ui.spn_rfgain0.valueChanged.connect(self.on_rfgain_change)
        self.ui.spn_rfgain1.valueChanged.connect(self.on_rfgain_change)
        self.ui.cbo_beamid.currentIndexChanged.connect(self.on_beamid_change)
        self.ui.edt_fftshift.editingFinished.connect(self.on_fftshift_change)
        self.ui.edt_dgain0.editingFinished.connect(self.on_digital_gain_change)
        self.ui.edt_dgain1.editingFinished.connect(self.on_digital_gain_change)
        self.ui.spn_acclen.valueChanged.connect(self.on_acclen_change)
        self.ui.cbo_bitsel_0.currentIndexChanged.connect(self.on_bitsel_change)
        self.ui.cbo_bitsel_1.currentIndexChanged.connect(self.on_bitsel_change)
        self.ui.cbo_bitsel_2.currentIndexChanged.connect(self.on_bitsel_change)
        self.ui.cbo_bitsel_3.currentIndexChanged.connect(self.on_bitsel_change)
        self.ui.edt_dest_ip_0.editingFinished.connect(self.on_dest_ip_change)
        self.ui.edt_dest_ip_1.editingFinished.connect(self.on_dest_ip_change)
        self.ui.edt_dest_ip_2.editingFinished.connect(self.on_dest_ip_change)
        self.ui.edt_dest_ip_3.editingFinished.connect(self.on_dest_ip_change)
        self.ui.edt_fabric_ip_0.editingFinished.connect(self.on_fabric_ip_change)
        self.ui.edt_fabric_ip_1.editingFinished.connect(self.on_fabric_ip_change)
        self.ui.edt_fabric_ip_2.editingFinished.connect(self.on_fabric_ip_change)
        self.ui.edt_fabric_ip_3.editingFinished.connect(self.on_fabric_ip_change)
        self.ui.btn_refresh.clicked.connect(self.on_refresh)
        self.ui.btn_reset.clicked.connect(self.on_reset)
        self.ui.btn_arm.clicked.connect(self.on_arm)

        # Input widgets validator
        rx = QtCore.QRegExp('^\d{1,15}')
        v = QtGui.QRegExpValidator(rx, self)
        for w in (self.ui.edt_noisecal_delay, self.ui.edt_noisecal_on, self.ui.edt_noisecal_off):
            w.setValidator(v)
        self.ui.edt_fftshift.setValidator(QtGui.QRegExpValidator(QtCore.QRegExp('0x[0-9A-Fa-f]{1,8}')))
        v = QtGui.QRegExpValidator(QtCore.QRegExp('0x[0-9A-Fa-f]{1,4}'))
        for w in (self.ui.edt_dgain0, self.ui.edt_dgain1):
            w.setValidator(v)
        v = QtGui.QRegExpValidator(QtCore.QRegExp('(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5]).){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5]):\d{1,5}'))
        for i in range(4):
            getattr(self.ui, 'edt_dest_ip_{:d}'.format(i)).setValidator(v)
        for i in range(4):
            getattr(self.ui, 'edt_fabric_ip_{:d}'.format(i)).setValidator(v)

        # Disable all widgets except the Roach board combobox
        interactive_widget_prefixes = ('edt', 'cbo', 'cb', 'rb', 'spn', 'btn')
        for name, obj in vars(self.ui).items():
            is_interactive = False
            for prefix in interactive_widget_prefixes:
                if name.startswith(prefix):
                    is_interactive = True
                    break
            if name != 'cbo_roach' and is_interactive: obj.setDisabled(True)

    def closeEvent(self, event):
        self.stop_poller_thread()
        self.disconnect_fpga()

    def connect_fpga(self, roach):
        self.disconnect_fpga()
        log.info('Connecting to %s', roach)
        self.fpga = katcp_wrapper.FpgaClient(roach)
        time.sleep(0.5)
        if not self.fpga.is_connected():
            msg = 'Can not connect to %s' % roach
            log.warning(msg)
            self.fpga.stop()
            self.fpga = None
            QtWidgets.QMessageBox.critical(self, 'Error', msg, QtWidgets.QMessageBox.Ok, 0)
            return False
        return True

    def disconnect_fpga(self):
        if self.fpga and self.fpga.is_connected():
            # member variable `bindaddr` renamed to `bind_address` since katcp-0.5.6
            if version.parse(katcp.__version__) <= version.parse('0.5.5'):
                self.fpga.bind_address = self.fpga.bindaddr
            log.info('Disconnect from %s:%d' % self.fpga.bind_address)
            self.fpga.stop()
            self.fpga = None

    def start_poller_thread(self):
        self.poller_event.clear()
        self.poller_thread = threading.Thread(target=self.snapshot_poller)
        self.poller_thread.start()

    def stop_poller_thread(self):
        if self.poller_thread:
            self.poller_event.set()
            self.poller_thread.join()
            self.poller_event.clear()
            self.poller_thread = None

    def on_board_change(self, index):
        log.debug('%d, %s', index, self.ui.cbo_roach.itemText(index))
        self.stop_poller_thread()
        roach = self.ui.cbo_roach.itemText(index)
        if self.connect_fpga(roach):
            # katadc.chip_init(self.fpga, 0)
            # katadc.chip_init(self.fpga, 1)
            self.retrieve_board_level_entries()
            self.retrieve_unit_level_entries()
            rb_unit = self.ui.rb_unit0 if self.unit == 0 else self.ui.rb_unit1
            rb_unit.setChecked(True)
            self.start_poller_thread()

    def on_unit_change(self, checked):
        log.debug('%s %s', self.sender().objectName(), 'selected' if checked else 'deselected')
        if checked:
            self.unit = 0 if self.sender().objectName()[-1] == '0' else 1
            self.prefix = 'u%d_' % self.unit
            self.retrieve_unit_level_entries()

    def on_tvg_change(self, state):
        log.debug('%s %d', self.sender().objectName(), state)
        usetvg = self.fpga.read_uint('use_tvg')
        if QtCore.Qt.Unchecked == state:
            if usetvg & (1 << self.unit) != 0:
                self.write_register('use_tvg', usetvg & ~(1 << self.unit))
        elif QtCore.Qt.Checked == state:
            if usetvg & (1 << self.unit) == 0:
                self.write_register('use_tvg', usetvg | (1 << self.unit))

    def on_rfgain_change(self, value):
        widget_name = str(self.sender().objectName())
        log.debug(widget_name + ' %.1f', value)
        index = int(widget_name[-1])
        input_names = ('I', 'Q')
        if self.rfgain[index] != value:
            katadc.rf_fe_set(self.fpga, self.unit, input_names[index], value)
            log.info('set zdok%d-%s rf gain %f dB' % (self.unit, input_names[index], value))
            self.rfgain[index] = value

    def on_beamid_change(self, index):
        beamid = index + 1
        log.debug('beam id %d', beamid)
        if beamid < 1 or beamid > 19:
            log.error('Attempting to write invalid beam id %d (current saved is %d)', beamid, self.beamid)
            return
        if self.beamid != beamid:
            self.write_register(self.prefix + 'beam_id', beamid)
            self.beamid = beamid

    def on_fftshift_change(self):
        log.debug('fft_shift %s', self.sender().text())
        # int() prefix-guessing
        # Base 0 means to interpret the string exactly as an integer literal,
        # so that the actual base is 2, 8, 10, or 16.
        fftshift = int(str(self.sender().text()), 0)
        if self.fftshift != fftshift:
            self.write_register(self.prefix + 'fft_shift', fftshift)
            self.fftshift = fftshift

    def on_digital_gain_change(self):
        widget_name = str(self.sender().objectName())
        log.debug('%s %s', widget_name, self.sender().text())
        index = int(self.sender().objectName()[-1])
        dgain = [int(str(self.ui.edt_dgain0.text()), 0), int(str(self.ui.edt_dgain1.text()), 0)]
        if self.dgain[index] != dgain[index]:
            gain = (dgain[1] & 0xFFFF) << 16 | (dgain[0] & 0xFFFF)
            self.write_register(self.prefix + 'gain', gain)
            self.dgain[index] = dgain[index]

    def on_acclen_change(self, value):
        log.debug('acclen %d', value)
        if self.acclen != value:
            self.write_register(self.prefix + 'acc_len', value)
            self.acclen = value
            self.on_reset()

    def on_bitsel_change(self, sel):
        log.debug('%s %d', self.sender().objectName(), sel)
        index = int(self.sender().objectName()[-1])
        if self.bitsel[index] !=  sel:
            self.bitsel[index] = sel
            self.write_register(self.prefix + 'bit_select',
                self.bitsel[3] << 6 | self.bitsel[2] << 4 | self.bitsel[1] << 2 | self.bitsel[0])

    def on_dest_ip_change(self):
        log.debug('%s %s', self.sender().objectName(), self.sender().text())
        index = int(self.sender().objectName()[-1])
        ip_str = str(self.sender().text()).strip()
        if self.dest_ip[index] != ip_str:
            ip, sep, port = ip_str.partition(':')
            reg_name = 'xgbe%d_dest_ip' % (index + 4*self.unit)
            reg_val, = struct.unpack('>I', socket.inet_aton(ip))
            self.write_register(reg_name, reg_val)
            reg_name = 'xgbe%d_dest_port' % (index + 4*self.unit)
            reg_val = int(port)
            self.write_register(reg_name, reg_val)
            self.dest_ip[index] = ip_str

    def on_fabric_ip_change(self):
        log.debug('%s %s', self.sender().objectName(), self.sender().text())
        index = int(self.sender().objectName()[-1])
        ip_str = str(self.sender().text()).strip()
        if self.fabric_ip[index] != ip_str:
            ip, sep, port = ip_str.partition(':')
            ip, = struct.unpack('>I', socket.inet_aton(ip))
            port = int(port)
            mac = (2<<40) + (2<<32) + ip
            dev = 'xgbe{:d}'.format(index + 4*self.unit)
            reg = dev + '_core'
            try:
                self.fpga.tap_stop(dev)
            except RuntimeError as e:
                pass
            self.fpga.tap_start(dev, reg, mac, ip, port)
            self.fabric_ip[index] = ip_str

    def on_refresh(self):
        log.debug('refresh')
        self.retrieve_board_level_entries()
        self.retrieve_unit_level_entries()

    def on_reset(self):
        log.debug('reset')
        self.write_register('reset', 0)
        self.write_register('reset', 1 << self.unit)

    def on_arm(self):
        log.debug('arm')
        self.write_register('arm', 0)
        self.write_register('arm', 1 << self.unit)

    def write_uint48(self, reg, val):
        if val >= 2**48:
            log.error('%s=%d exceed range!', reg, val)
        lo = val % 2**32
        hi = val // 2**32
        self.write_register(reg, lo)
        self.write_register(reg + '_hipart', hi)

    def read_uint48(self, reg):
        lo = self.fpga.read_uint(reg)
        hi = self.fpga.read_uint(reg + '_hipart')
        val = hi * 2**32 + lo
        if val >= 2**48:
            log.error('%s=%d exceed range!', reg, val)
        return val

    def on_noisecal_delay_change(self):
        log.debug('%s %s', self.sender().objectName(), self.sender().text())
        noisecal_delay = int(str(self.ui.edt_noisecal_delay.text()), 0)
        if self.noisecal_delay != noisecal_delay:
            self.write_uint48('noisecal_delay', noisecal_delay)
            self.noisecal_delay = noisecal_delay

    def on_noisecal_on_change(self):
        log.debug('%s %s', self.sender().objectName(), self.sender().text())
        noisecal_on = int(str(self.ui.edt_noisecal_on.text()), 0)
        if self.noisecal_on != noisecal_on:
            self.write_uint48('noisecal_on', noisecal_on)
            self.noisecal_on = noisecal_on

    def on_noisecal_off_change(self):
        log.debug('%s %s', self.sender().objectName(), self.sender().text())
        noisecal_off = int(str(self.ui.edt_noisecal_off.text()), 0)
        if self.noisecal_off != noisecal_off:
            self.write_uint48('noisecal_off', noisecal_off)
            self.noisecal_off = noisecal_off

    def write_register(self, name, value):
        self.fpga.write_int(name, value)
        log.info('wrote register ' + name + '=%d(%X)' % (value, value))

    def retrieve_board_level_entries(self):
        rcs_id = self.fpga.read_uint('rcs_id')
        rcs_ver = self.fpga.read_uint('rcs_ver')
        rcs_timestamp = self.fpga.read_uint('rcs_timestamp')
        #rcs_str = struct.pack('>I', rcs_id) + ' - '
        rcs_str = chr(rcs_id>>24 & 0xFF) + chr(rcs_id>>16 & 0xFF) + chr(rcs_id>>8 & 0xFF) + chr(rcs_id & 0xFF) + ' - '
        rcs_str += 'v%d.%d' % (rcs_ver >> 16, rcs_ver & 0xFFFF) + '\n'
        rcs_str += datetime.datetime.fromtimestamp(rcs_timestamp).strftime('%Y-%m-%d %H:%M:%S')
        self.ui.lbl_rcs.setText(rcs_str)
        self.noisecal_delay = self.read_uint48('noisecal_delay')
        self.ui.edt_noisecal_delay.setEnabled(True)
        self.ui.edt_noisecal_delay.setText(str(self.noisecal_delay))
        self.noisecal_on = self.read_uint48('noisecal_on')
        self.ui.edt_noisecal_on.setEnabled(True)
        self.ui.edt_noisecal_on.setText(str(self.noisecal_on))
        self.noisecal_off = self.read_uint48('noisecal_off')
        self.ui.edt_noisecal_off.setEnabled(True)
        self.ui.edt_noisecal_off.setText(str(self.noisecal_off))
        self.last_clkcounter = self.fpga.read_uint('sys_clkcounter')
        self.last_clkcounter_time = time.time()
        self.last_ppscounter = self.fpga.read_uint('pps_counter')
        self.last_ppscounter_time = time.time()
        for widget in (self.ui.rb_unit0, self.ui.rb_unit1, self.ui.btn_arm, self.ui.btn_reset, self.ui.btn_refresh):
            widget.setEnabled(True)

    def get_dest_ipaddr(self, unit, index):
        prefix  = 'xgbe%d_' % (index + unit * 4)
        ip      = self.fpga.read_uint(prefix + 'dest_ip')
        port    = self.fpga.read_uint(prefix + 'dest_port')
        log.debug(prefix + ': ' + socket.inet_ntoa(struct.pack('>I', ip)) + ':' + str(port))
        return socket.inet_ntoa(struct.pack('>I', ip)) + ':' + str(port)

    def get_10gbe_core_info(self, dev_name):
        #assemble struct for header stuff...
        #0x00 - 0x07: My MAC address
        #0x08 - 0x0b: Not used
        #0x0c - 0x0f: Gateway addr
        #0x10 - 0x13: my IP addr
        #0x14 - 0x17: Not assigned
        #0x18 - 0x1b: Buffer sizes
        #0x1c - 0x1f: Not assigned
        #0x20       : soft reset (bit 0)
        #0x21       : fabric enable (bit 0)
        #0x22 - 0x23: fabric port
        #0x24 - 0x27: XAUI status (bit 2,3,4,5=lane sync, bit6=chan_bond)
        #0x28 - 0x2b: PHY config
        #0x28       : RX_eq_mix
        #0x29       : RX_eq_pol
        #0x30 - 0x33: Multicast IP RX base address
        #0x34       : Multicast IP RX IP mask
        #0x2a       : TX_preemph
        #0x2b       : TX_diff_ctrl
        #0x1000     : CPU TX buffer
        #0x2000     : CPU RX buffer
        #0x3000     : ARP tables start
        mem = struct.unpack('>12L', self.fpga.read(dev_name, 48))
        info            = {}
        info['mac']     = mem[0] << 32 | mem[1]
        info['gateway'] = mem[3]
        info['ip']      = mem[4]
        info['port']    = mem[8] & 0xFFFF
        info['enabled'] = mem[8] & 0x10000 != 0
        return info

    def get_fabric_ipaddr(self, unit, index):
        dev     = 'xgbe%d_core' % (index + unit * 4)
        tginfo  = self.get_10gbe_core_info(dev)
        ip      = tginfo['ip']
        port    = tginfo['port']
        log.debug(dev + ': ' + socket.inet_ntoa(struct.pack('>I', ip)) + ':' + str(port))
        return socket.inet_ntoa(struct.pack('>I', ip)) + ':' + str(port)

    def retrieve_unit_level_entries(self):
        log.info('Retrieve parameters from unit %d' % self.unit)
        # beam id
        self.beamid = self.fpga.read_uint(self.prefix + 'beam_id')
        self.ui.cbo_beamid.setEnabled(GODMODE)
        if self.beamid < 1 or self.beamid > 19:
            log.warning('Invalid beam id %d', self.beamid)
            self.ui.cbo_beamid.setCurrentIndex(-1)
        else:
            self.ui.cbo_beamid.setCurrentIndex(self.beamid - 1)
        # fft_shift
        self.fftshift = self.fpga.read_uint(self.prefix + 'fft_shift')
        self.ui.edt_fftshift.setEnabled(True)
        self.ui.edt_fftshift.setText('0x%04X' % self.fftshift)
        # digital gain
        dgain = self.fpga.read_uint(self.prefix + 'gain')
        self.dgain = [dgain & 0xFFFF, dgain >> 16]
        self.ui.edt_dgain0.setEnabled(True)
        self.ui.edt_dgain0.setText('0x%04X' % self.dgain[0])
        self.ui.edt_dgain1.setEnabled(True)
        self.ui.edt_dgain1.setText('0x%04X' % self.dgain[1])
        # acc_len
        self.acclen = self.fpga.read_uint(self.prefix + 'acc_len')
        self.ui.spn_acclen.setEnabled(True)
        self.ui.spn_acclen.setValue(self.acclen)
        # bit_select
        bs = self.fpga.read_uint(self.prefix + 'bit_select')
        self.bitsel = [bs & 0b11, bs >> 2 & 0b11, bs >> 4 & 0b11, bs >> 6 & 0b11]
        widgets = (self.ui.cbo_bitsel_0, self.ui.cbo_bitsel_1, self.ui.cbo_bitsel_2, self.ui.cbo_bitsel_3)
        for i in range(4):
            widgets[i].setEnabled(True)
            widgets[i].setCurrentIndex(self.bitsel[i])
        # dest ip
        self.dest_ip = []
        widgets = (self.ui.edt_dest_ip_0, self.ui.edt_dest_ip_1, self.ui.edt_dest_ip_2, self.ui.edt_dest_ip_3)
        for i in range(4):
            widgets[i].setEnabled(GODMODE)
            self.dest_ip.append(self.get_dest_ipaddr(self.unit, i))
            widgets[i].setText(self.dest_ip[i])
        # fabric ip
        self.fabric_ip = []
        widgets = (self.ui.edt_fabric_ip_0, self.ui.edt_fabric_ip_1, self.ui.edt_fabric_ip_2, self.ui.edt_fabric_ip_3)
        for i in range(4):
            widgets[i].setEnabled(GODMODE)
            self.fabric_ip.append(self.get_fabric_ipaddr(self.unit, i))
            widgets[i].setText(self.fabric_ip[i])
        # RF gain
        self.rfgain = []
        for inp, widget in zip(('I', 'Q'), (self.ui.spn_rfgain0, self.ui.spn_rfgain1)):
            rfgain = katadc.rf_fe_get(self.fpga, self.unit, inp)
            widget.setEnabled(True)
            if rfgain['enabled']:
                self.rfgain.append(rfgain['gain'])
                widget.setValue(rfgain['gain'])
            else:
                self.rfgain.append(None)
                log.warning('katadc %d RF frontend %s not enabled', self.unit, inp)
        # TVG
        usetvg = (self.fpga.read_uint('use_tvg') & (1 << self.unit)) != 0
        self.ui.cb_tvg.setChecked(usetvg)
        self.ui.cb_tvg.setEnabled(GODMODE)

    @staticmethod
    def split_snapshot(snap):
        len = snap['length']
        all = struct.unpack('%db'%len, snap['data'])
        segments = np.array(all).reshape(-1, 4)
        p0 = segments[0::2, :].flatten()
        p1 = segments[1::2, :].flatten()
        return p0, p1

    '''
    def get_mb_scopes(self):
        # Try to gain lock
        locked_id = self.fpga.read_uint('sys_scratchpad')
        if locked_id != self.id and locked_id != SCOPE_IDLE_FLAG:
            log.info('Scope busy! Locked by %08x, will retry at last update.' % locked_id)
            time.sleep(POLLING_INTERVAL / 3)
            return None, None
        # Lock it
        self.fpga.write_int('sys_scratchpad', self.id, blindwrite=True)
        try:
            adc_name = 'zdok%d_scope' % self.unit
            snap = self.fpga.snapshot_get(adc_name, man_trig=True, man_valid=True, wait_period=10)
            adc = self.split_snapshot(snap)
            spec = []
            stokes = ['AA', 'BB', 'CR', 'CI']
            for i in range(4):
                scope_name = self.prefix + 'x4_vacc_scope_' + stokes[i]
                snap = self.fpga.snapshot_get(scope_name, man_valid=True, wait_period=10)
                spec.append(np.array(struct.unpack('>%di' % (snap['length']/4), snap['data'])))
        finally:
            # Release lock
            self.fpga.write_int('sys_scratchpad', SCOPE_IDLE_FLAG, blindwrite=True)
        return adc, spec
    '''

    def get_mb_scopes(self):
        adc_name = 'zdok%d_scope' % self.unit
        snap = self.fpga.snapshot_get(adc_name, man_trig=True, man_valid=True, wait_period=10)
        adc = self.split_snapshot(snap)
        spec = []
        stokes = ['AA', 'BB', 'CR', 'CI']
        for i in range(4):
            scope_name = self.prefix + 'x4_vacc_scope_' + stokes[i]
            snap = self.fpga.snapshot_get(scope_name, man_valid=True, wait_period=10)
            spec.append(np.array(struct.unpack('>%di' % (snap['length']/4), snap['data'])))
        return adc, spec

    def snapshot_poller(self):
        finished = False
        while not finished:
            try:
                adc, spec = self.get_mb_scopes()
                if adc is not None and spec is not None:
                    self.sig_update_plot.emit(adc, spec)
            except Exception as e:
                print(repr(e))
            self.poller_event.wait(POLLING_INTERVAL)
            finished = self.poller_event.is_set()

    def validate_clock_source(self):
        clkcounter = self.fpga.read_uint('sys_clkcounter')
        clkcounter_time = time.time()
        if clkcounter < self.last_clkcounter:
            hz = (clkcounter + 2**32 - self.last_clkcounter) / (clkcounter_time - self.last_clkcounter_time)
        else:
            hz = (clkcounter - self.last_clkcounter) / (clkcounter_time - self.last_clkcounter_time)
        self.last_clkcounter = clkcounter
        self.last_clkcounter_time = clkcounter_time
        # log.debug('Estimated FPGA clock: %.fMHz' % (hz * 1e-6))
        # Only check Fpga clock source present or not, given 10% tolerance
        if hz < FPGA_CLOCK * 0.9 or hz > FPGA_CLOCK * 1.1:
            log.error('Invalid FPGA clock: %.3f MHz, check clock source !!!' % (hz * 1e-6))
            self.lbl_clkstate.setText('CLOCK ERROR', color='r', bold=True)
        else:
            self.lbl_clkstate.setText('CLOCK OK', color='g', bold=True)

    def validate_pps_input(self):
        ppscounter = self.fpga.read_uint('pps_counter')
        ppscounter_time = time.time()
        if ppscounter_time - self.last_ppscounter_time <= 1:
            return
        if ppscounter > self.last_ppscounter:
            self.lbl_ppsstate.setText('PPS OK', color='g', bold=True)
        else:
            self.lbl_ppsstate.setText('PPS ERROR', color='r', bold=True)
        self.last_ppscounter = ppscounter
        self.last_ppscounter_time = ppscounter_time

    def on_update_plot(self, adc, spec):
        self.lbl_lastupdate.setText(time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime()))
        self.plotter.update_plots(adc, spec, self.bitsel)
        if self.fpga and self.fpga.is_connected():
            self.validate_clock_source()
            self.validate_pps_input()


# Start Qt event loop unless running in interactive mode or using pyside.
if __name__ == '__main__':

    logname = os.path.splitext(os.path.basename(__file__))[0]
    log = logging.getLogger(logname)
    # log.setLevel(logging.DEBUG)
    log.setLevel(logging.INFO)
    # log.setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('%(asctime)s %(name)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
    log.addHandler(handler)

    # katcp_wrapper.log.setLevel(logging.DEBUG)
    # katcp_wrapper.log.setLevel(logging.INFO)
    katcp_wrapper.log.setLevel(logging.WARNING)
    katcp_wrapper.log.addHandler(handler)

    # also logging to file
    if LOG2FILE:
        logfile_path = os.path.join(path, 'log')
        logfile_name = os.path.join(logfile_path, logname + '-' + time.strftime('%Y%m%d') + '.log')
        if not os.path.exists(logfile_path):
            os.mkdir(logfile_path, mode=0o0775)
        handler = logging.FileHandler(logfile_name)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter('%(asctime)s %(name)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
        log.addHandler(handler)
        katcp_wrapper.log.addHandler(handler)

    pg.mkQApp()
    win = MainForm()
    win.show()

    import sys
    try:
        if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
            QtWidgets.QApplication.instance().exec_()
    finally:
        if win.fpga:
            win.disconnect_fpga()
