#!/usr/bin/env python3

# -*- coding: utf-8 -*-

#############################################################################
#
#           FAST 19-Beam Digital Receiver Firmware Monitor
#
#############################################################################

from __future__ import print_function

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui


class Plotter(object):

    def __init__(self, glw, hist_brush=(0,255,0,150), zoom_pen=(0,128,0,150), show_title=True):
        super(Plotter, self).__init__()
        self.glw = glw
        self.show_title = show_title
        self.init_plots(hist_brush, zoom_pen)

    def init_plots(self, hist_brush, zoom_pen):
        self.hists = []
        self.hist_plots = []
        for i in range(2):
            plt = self.glw.addPlot(row=0, col=i)
            if self.show_title:
                plt.setTitle('pol%d hist' % i)
            plt.getAxis('left').setStyle(tickTextHeight=5)
            plt.setXRange(-128,127)
            fake_x = np.zeros(2)
            fake_y = np.zeros(1)
            self.hists.append(plt.plot(x=fake_x, y=fake_y, stepMode=True, fillLevel=0, brush=hist_brush))
            self.hist_plots.append(plt)

        self.waves = []
        self.wave_plots = []
        for i in range(2):
            plt = self.glw.addPlot(row=1, col=i)
            if self.show_title:
                plt.setTitle('pol%d waveform' % i)
            plt.setYRange(-128,127)
            plt.getAxis('left').setTicks([[(-128, '-128'), (-64, '-64'), (0, '0'), (64, '64'), (128, '128')]])
            self.waves.append(plt.plot())
            self.wave_plots.append(plt)

        self.specs = []
        self.spec_plots = []
        for i in range(4):
            plt = self.glw.addPlot(row=2+i//2, col=i%2)
            if self.show_title:
                titles = ['AA', 'BB', 'CR', 'CI']
                plt.setTitle(titles[i])
            plt.showGrid(y=True)
            plt.setYRange(0, 32)
            plt.getAxis('left').setTicks([[(0, '0'), (8, '2^8'), (16, '2^16'), (24, '2^24'), (32, '2^32')]])
            self.specs.append(plt.plot())
            self.spec_plots.append(plt)

        self.zooms = []
        self.spec_vbrs = []
        for i in range(4):
            plt = self.spec_plots[i]
            vbr = pg.ViewBox()
            plt.showAxis('right')
            plt.scene().addItem(vbr)
            axis = plt.getAxis('right')
            axis.linkToView(vbr)
            vbr.setXLink(plt)
            axis.setPen(zoom_pen)
            axis.setGrid(False)
            if i < 2:
                vbr.setYRange(0, 255)
                axis.setTicks([[(0, '0'), (64, '64'), (128, '128'), (192, '192'), (256, '256')]])
            else:
                vbr.setYRange(-128,127)
                axis.setTicks([[(-128, '-128'), (-64, '-64'), (0, '0'), (64, '64'), (128, '128')]])
            curve = pg.PlotCurveItem(pen=zoom_pen)
            vbr.addItem(curve)
            plt.getViewBox().sigResized.connect(self.update_view)
            self.zooms.append(curve)
            self.spec_vbrs.append(vbr)

        tickfont = QtGui.QFont()
        tickfont.setPointSize(7)
        tickwidth = QtGui.QFontMetrics(tickfont).width ('2560')
        for plt in self.hist_plots + self.wave_plots + self.spec_plots:
            plt.getAxis('left').setTickFont(tickfont)
            plt.getAxis('right').setTickFont(tickfont)
            plt.getAxis('right').setWidth(tickwidth)
            plt.getAxis('bottom').setTickFont(tickfont)

    def update_view(self, vbl):
        for i in range(len(self.spec_plots)):
            if vbl is self.spec_plots[i].getViewBox():
                vbr = self.spec_vbrs[i]
                vbr.setGeometry(vbl.sceneBoundingRect())
                vbr.linkedViewChanged(vbl, vbr.XAxis)
                break

    @staticmethod
    def rms(x):
        return np.sqrt(x.dot(x) / x.size)

    def update_plots(self, adc, spec, bitsel=(1, 1, 1, 1)):
        for i in range(2):
            self.waves[i].setData(adc[i][0:1024])
            # if self.show_title:
            #     self.wave_plots[i].setTitle('MEAN %6.2f' % np.mean(adc[i]))
            y, x = np.histogram(adc[i], adc[i].max() - adc[i].min() + 1)
            self.hists[i].setData(x, y)
            if self.show_title:
                self.hist_plots[i].setTitle('MEAN %.2f, RMS %.2f' % (np.mean(adc[i]), self.rms(adc[i])))

        for i in range(2):
            self.specs[i].setData(np.log2(np.fabs(spec[i]) + 1))
            self.zooms[i].setData((spec[i] >> bitsel[i]*8) & 0xFF)

        for i in range(2,4):
            self.specs[i].setData(np.log2(np.fabs(spec[i]) + 1))
            self.zooms[i].setData((spec[i] >> bitsel[i]*8))


if __name__ == '__main__':

    import sys
    import time
    import struct
    import logging
    import os.path
    import katcp_wrapper

    def init_logger():
        logname = os.path.splitext(os.path.basename(__file__))[0]
        log = logging.getLogger(logname)
        log.setLevel(logging.DEBUG)
        # log.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter('%(asctime)s %(name)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
        log.addHandler(handler)
        # katcp_wrapper.log.setLevel(logging.DEBUG)
        katcp_wrapper.log.setLevel(logging.INFO)
        katcp_wrapper.log.addHandler(handler)
        return log

    def setup_gui(title, width = 640, height = 480):
        global app, mw, plotter
        mw = QtGui.QWidget()
        mw.resize(width, height)
        mw.setWindowTitle(title)
        vbox = QtGui.QVBoxLayout(mw)
        vbox.setContentsMargins(0, 0, 0, 0)
        glw = pg.GraphicsLayoutWidget(mw)
        vbox.addWidget(glw)
        plotter = Plotter(glw, show_title=False)
        mw.show()

    def split_snapshot(snap):
        len = snap['length']
        all = struct.unpack('%db'%len, snap['data'])
        segments = np.array(all).reshape(-1, 4)
        p0 = segments[0::2, :].flatten()
        p1 = segments[1::2, :].flatten()
        return p0, p1

    def plot_anim(unit):
        global plotter, fpga
        prefix = 'u{:d}_'.format(unit)
        adc_name = 'zdok{:d}_scope'.format(unit)
        snap = fpga.snapshot_get(adc_name, man_trig=True, man_valid=True)
        adc = split_snapshot(snap)
        spec = []
        stokes = ['AA', 'BB', 'CR', 'CI']
        for i in range(4):
            scope_name = prefix + 'x4_vacc_scope_' + stokes[i]
            snap = fpga.snapshot_get(scope_name, man_valid=True)
            spec.append(np.array(struct.unpack('>%di' % (snap['length']/4), snap['data'])))
        bitsel = fpga.read_uint('u{:d}_bit_select'.format(unit))
        plotter.update_plots(adc, spec, (bitsel & 3, bitsel >> 2 & 3, bitsel >> 4 & 3, bitsel >> 6 & 3))


    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(("Usage: {0:s} roach_board [unit]\n" +
               "e.g. {0:s} r1745 0").format(os.path.basename(sys.argv[0])))
        exit()

    log = init_logger()

    roach = sys.argv[1]
    katcp_port = 7147
    unit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    if not unit in (0, 1):
        log.critical('Invalid unit number %d', unit)
        exit()

    try:
        log.info('Connecting to server %s on port %i ... ' % (roach, katcp_port))
        fpga = katcp_wrapper.FpgaClient(roach, katcp_port, timeout=1)
        time.sleep(0.1)
        if not fpga.is_connected():
            log.error('ERROR connecting to server %s on port %i.\n' % (roach, katcp_port))
            exit()

        app = QtGui.QApplication([])

        setup_gui(roach + ' - {:d}'.format(unit))
        plot_anim(unit)

        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: plot_anim(unit))
        timer.start(3000)

        if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
            QtGui.QApplication.instance().exec_()

    finally:
        if fpga:
            fpga.stop()
