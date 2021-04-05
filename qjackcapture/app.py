#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# JACK-Capture frontend, with freewheel and transport support
# Copyright (C) 2010-2018 Filipe Coelho <falktx@falktx.com>
# Copyright (C) 2020 Christopher Arndt <info@chrisarndt.de>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the COPYING file

# -------------------------------------------------------------------------------------------------
# Standard library imports

import logging
import os
import shlex
import sys
from functools import partial
from operator import itemgetter
from os.path import exists, isdir, join, sep as pathsep
from time import sleep

from PyQt5.QtCore import pyqtSlot, QProcess, Qt, QTime, QTimer, QSettings, QModelIndex
from PyQt5.QtGui import QIcon, QStandardItemModel, QStandardItem
from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QHeaderView, QMessageBox

from natsort import humansorted

# -------------------------------------------------------------------------------------------------
# Application-specific imports

from .ui_mainwindow import Ui_MainWindow
from .version import __version__

try:
    from . import jacklib
    from .jacklib_helpers import c_char_p_p_to_list, get_jack_status_error_string
except ImportError:
    jacklib = None


ORGANIZATION = "chrisarndt.de"
PROGRAM = "QJackCapture"

log = logging.getLogger(PROGRAM)

# -------------------------------------------------------------------------------------------------
# Find 'jack_capture' in PATH

gJackCapturePath = None
for pathdir in os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep):
    if exists(join(pathdir, "jack_capture")):
        gJackCapturePath = join(pathdir, "jack_capture")
        break


# -------------------------------------------------------------------------------------------------
# Uitility functions

def get_icon(name, size=16):
    return QIcon.fromTheme(name, QIcon(":/icons/%ix%i/%s.png" % (size, size, name)))


# -------------------------------------------------------------------------------------------------
# Main Window

class QJackCaptureMainWindow(QDialog):
    sample_formats = {
        "32-bit float": "FLOAT",
        "8-bit integer": "8",
        "16-bit integer": "16",
        "24-bit integer": "24",
        "32-bit integer": "32",
    }

    def __init__(self, parent, jack_client):
        QDialog.__init__(self, parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.fFreewheel = False
        self.fLastTime = -1
        self.fMaxTime = 180

        self.fTimer = QTimer(self)
        self.fProcess = QProcess(self)
        self.fJackClient = jack_client

        self.fBufferSize = int(jacklib.get_buffer_size(self.fJackClient))
        self.fSampleRate = int(jacklib.get_sample_rate(self.fJackClient))

        # Selected ports used as recording sources
        self.rec_sources = set()

        self.createUi()
        self.loadSettings()
        self.populatePortLists()

    def populateFileFormats(self):
        # Get list of supported file formats
        self.fProcess.start(gJackCapturePath, ["-pf"])
        self.fProcess.waitForFinished()

        formats = []

        for fmt in str(self.fProcess.readAllStandardOutput(), encoding="utf-8").split():
            fmt = fmt.strip()
            if fmt:
                formats.append(fmt)

        # Put all file formats in combo-box, select 'wav' option
        self.ui.cb_format.clear()
        for i, fmt in enumerate(sorted(formats)):
            self.ui.cb_format.addItem(fmt)

            if fmt == "wav":
                self.ui.cb_format.setCurrentIndex(i)

    def populateSampleFormats(self):
        # Put all sample formats in combo-box, select 'FLOAT' option
        self.ui.cb_depth.clear()
        for i, (label, fmt) in enumerate(self.sample_formats.items()):
            self.ui.cb_depth.addItem(label, fmt)

            if fmt == "FLOAT":
                self.ui.cb_depth.setCurrentIndex(i)

    def populatePortLists(self):
        output_ports = self.getJackPorts(jacklib.JackPortIsOutput)
        self.outputs_model = QStandardItemModel(0, 1, self)
        self.populatePortList(self.outputs_model, self.ui.tree_outputs, output_ports)

        input_ports = self.getJackPorts(jacklib.JackPortIsInput)
        self.inputs_model = QStandardItemModel(0, 1, self)
        self.populatePortList(self.inputs_model, self.ui.tree_inputs, input_ports)

        # Remove ports, which are no longer present from recording sources
        self.rec_sources.intersection_update(set(output_ports) | set(input_ports))

        self.ui.tree_outputs.clicked.connect(partial(self.slot_portSelected, self.outputs_model))
        self.ui.tree_inputs.clicked.connect(partial(self.slot_portSelected, self.inputs_model))

        self.slot_toggleRecordingSource()

    def populatePortList(self, model, tv, ports):
        tv.setModel(model)
        tv.setHeaderHidden(True)
        root = model.invisibleRootItem()

        portsdict = {}
        for client, port in ports:
            if client not in portsdict:
                portsdict[client] = []
            portsdict[client].append(port)

        for client in humansorted(portsdict):
            clientitem = QStandardItem(client)

            for port in humansorted(portsdict[client]):
                portitem = QStandardItem(port)
                portitem.setCheckable(True)

                if (client, port) in self.rec_sources:
                    portitem.setCheckState(True)

                clientitem.appendRow(portitem)

            root.appendRow(clientitem)

        tv.expandAll()

    def getJackPorts(self, type_=jacklib.JackPortIsInput):
        return [tuple(port.split(':', 1))
                for port in c_char_p_p_to_list(jacklib.get_ports(self.fJackClient, '',
                                               jacklib.JACK_DEFAULT_AUDIO_TYPE, type_))]

    def createUi(self):
        # -------------------------------------------------------------
        # Set-up GUI stuff

        for i in range(self.ui.cb_buffer_size.count()):
            if int(self.ui.cb_buffer_size.itemText(i)) == self.fBufferSize:
                self.ui.cb_buffer_size.setCurrentIndex(i)
                break
        else:
            self.ui.cb_buffer_size.addItem(str(self.fBufferSize))
            self.ui.cb_buffer_size.setCurrentIndex(self.ui.cb_buffer_size.count() - 1)

        self.populateFileFormats()
        self.populateSampleFormats()

        self.ui.rb_stereo.setChecked(True)
        self.ui.te_end.setTime(QTime(0, 3, 0))
        self.ui.progressBar.setFormat("")
        self.ui.progressBar.setMinimum(0)
        self.ui.progressBar.setMaximum(1)
        self.ui.progressBar.setValue(0)

        self.ui.b_render.setIcon(get_icon("media-record"))
        self.ui.b_stop.setIcon(get_icon("media-playback-stop"))
        self.ui.b_close.setIcon(get_icon("window-close"))
        self.ui.b_open.setIcon(get_icon("document-open"))
        self.ui.b_stop.setVisible(False)
        # XXX use x-platform media dir as default
        self.ui.le_folder.setText(os.getenv("HOME"))

        # -------------------------------------------------------------
        # Set-up connections

        self.ui.b_render.clicked.connect(self.slot_renderStart)
        self.ui.b_stop.clicked.connect(self.slot_renderStop)
        self.ui.b_open.clicked.connect(self.slot_getAndSetPath)
        self.ui.b_now_start.clicked.connect(self.slot_setStartNow)
        self.ui.b_now_end.clicked.connect(self.slot_setEndNow)
        self.ui.te_start.timeChanged.connect(self.slot_updateStartTime)
        self.ui.te_end.timeChanged.connect(self.slot_updateEndTime)
        self.ui.group_time.clicked.connect(self.slot_transportChecked)
        self.ui.rb_source_default.toggled.connect(self.slot_toggleRecordingSource)
        self.ui.rb_source_manual.toggled.connect(self.slot_toggleRecordingSource)
        self.ui.rb_source_selected.toggled.connect(self.slot_toggleRecordingSource)
        self.fTimer.timeout.connect(self.slot_updateProgressbar)

    @pyqtSlot()
    def slot_renderStart(self):
        if not exists(self.ui.le_folder.text()):
            QMessageBox.warning(
                self,
                self.tr("Warning"),
                self.tr("The selected directory does not exist. Please choose a valid one."),
            )
            return

        timeStart = self.ui.te_start.time()
        timeEnd = self.ui.te_end.time()
        minTime = (timeStart.hour() * 3600) + (timeStart.minute() * 60) + (timeStart.second())
        maxTime = (timeEnd.hour() * 3600) + (timeEnd.minute() * 60) + (timeEnd.second())

        newBufferSize = int(self.ui.cb_buffer_size.currentText())
        useTransport = self.ui.group_time.isChecked()

        self.fFreewheel = self.ui.rb_freewheel.isChecked()
        self.fLastTime = -1
        self.fMaxTime = maxTime

        if self.fFreewheel:
            self.fTimer.setInterval(100)
        else:
            self.fTimer.setInterval(500)

        self.ui.group_render.setEnabled(False)
        self.ui.group_time.setEnabled(False)
        self.ui.group_encoding.setEnabled(False)
        self.ui.b_render.setVisible(False)
        self.ui.b_stop.setVisible(True)
        self.ui.b_close.setEnabled(False)

        if useTransport:
            self.ui.progressBar.setFormat("%p%")
            self.ui.progressBar.setMinimum(minTime)
            self.ui.progressBar.setMaximum(maxTime)
            self.ui.progressBar.setValue(minTime)
        else:
            self.ui.progressBar.setFormat("")
            self.ui.progressBar.setMinimum(0)
            self.ui.progressBar.setMaximum(0)
            self.ui.progressBar.setValue(0)

        self.ui.progressBar.update()

        arguments = []

        # Filename prefix
        arguments.append("-fp")
        arguments.append(self.ui.le_prefix.text())

        # File format
        arguments.append("-f")
        arguments.append(self.ui.cb_format.currentText())

        # Sanple format (bit depth, int/float)
        arguments.append("-b")
        arguments.append(self.ui.cb_depth.currentData())

        # Channels
        arguments.append("-c")
        if self.ui.rb_mono.isChecked():
            arguments.append("1")
        elif self.ui.rb_stereo.isChecked():
            arguments.append("2")
        else:
            arguments.append(str(self.ui.sb_channels.value()))

        # Recording sources
        if self.ui.rb_source_manual.isChecked():
            arguments.append("-mc")
        elif self.ui.rb_source_selected.isChecked():
            for client, port in self.rec_sources:
                arguments.append("-p")
                arguments.append("{}:{}".format(client, port))

        # Controlled only by freewheel
        if self.fFreewheel:
            arguments.append("-jf")

        # Controlled by transport
        elif useTransport:
            arguments.append("-jt")

        # Silent mode
        arguments.append("--daemon")

        # Extra arguments
        extra_args = self.ui.le_extra_args.text().strip()

        if extra_args:
            arg_list = shlex.split(extra_args)
            log.debug("Extra args: %r", arg_list)
            arguments.extend(arg_list)

        # Change current directory
        os.chdir(self.ui.le_folder.text())

        if newBufferSize != int(jacklib.get_buffer_size(self.fJackClient)):
            log.info("buffer size changed before render")
            jacklib.set_buffer_size(self.fJackClient, newBufferSize)

        if useTransport:
            if jacklib.transport_query(self.fJackClient, None) > jacklib.JackTransportStopped:
                # rolling or starting
                jacklib.transport_stop(self.fJackClient)

            jacklib.transport_locate(self.fJackClient, minTime * self.fSampleRate)

        log.debug("jack_capture command line: %r", arguments)
        self.fProcess.start(gJackCapturePath, arguments)
        status = self.fProcess.waitForStarted()

        if not status:
            self.fProcess.close()
            log.error("Could not start jack_capture.")
            return

        if self.fFreewheel:
            log.info("rendering in freewheel mode")
            sleep(1)
            jacklib.set_freewheel(self.fJackClient, 1)

        if useTransport:
            self.fTimer.start()
            jacklib.transport_start(self.fJackClient)

    @pyqtSlot()
    def slot_renderStop(self):
        useTransport = self.ui.group_time.isChecked()

        if useTransport:
            jacklib.transport_stop(self.fJackClient)

        if self.fFreewheel:
            jacklib.set_freewheel(self.fJackClient, 0)
            sleep(1)

        self.fProcess.terminate()
        # self.fProcess.waitForFinished(5000)

        if useTransport:
            self.fTimer.stop()

        self.ui.group_render.setEnabled(True)
        self.ui.group_time.setEnabled(True)
        self.ui.group_encoding.setEnabled(True)
        self.ui.b_render.setVisible(True)
        self.ui.b_stop.setVisible(False)
        self.ui.b_close.setEnabled(True)

        self.ui.progressBar.setFormat("")
        self.ui.progressBar.setMinimum(0)
        self.ui.progressBar.setMaximum(1)
        self.ui.progressBar.setValue(0)
        self.ui.progressBar.update()

        # Restore buffer size
        newBufferSize = int(jacklib.get_buffer_size(self.fJackClient))

        if newBufferSize != self.fBufferSize:
            jacklib.set_buffer_size(self.fJackClient, newBufferSize)

    @pyqtSlot()
    def slot_getAndSetPath(self):
        new_path = QFileDialog.getExistingDirectory(
            self, self.tr("Set Path"), self.ui.le_folder.text(), QFileDialog.ShowDirsOnly
        )

        if new_path:
            self.ui.le_folder.setText(new_path)

    @pyqtSlot()
    def slot_setStartNow(self):
        time = int(jacklib.get_current_transport_frame(self.fJackClient) / self.fSampleRate)
        secs = time % 60
        mins = int(time / 60) % 60
        hrs = int(time / 3600) % 60
        self.ui.te_start.setTime(QTime(hrs, mins, secs))

    @pyqtSlot()
    def slot_setEndNow(self):
        time = int(jacklib.get_current_transport_frame(self.fJackClient) / self.fSampleRate)
        secs = time % 60
        mins = int(time / 60) % 60
        hrs = int(time / 3600) % 60
        self.ui.te_end.setTime(QTime(hrs, mins, secs))

    @pyqtSlot(QTime)
    def slot_updateStartTime(self, time):
        if time >= self.ui.te_end.time():
            self.ui.te_end.setTime(time)
            renderEnabled = False
        else:
            renderEnabled = True

        if self.ui.group_time.isChecked():
            self.ui.b_render.setEnabled(renderEnabled)

    @pyqtSlot(QTime)
    def slot_updateEndTime(self, time):
        if time <= self.ui.te_start.time():
            self.ui.te_start.setTime(time)
            renderEnabled = False
        else:
            renderEnabled = True

        if self.ui.group_time.isChecked():
            self.ui.b_render.setEnabled(renderEnabled)

    def slot_portSelected(self, model, idx):
        item = model.itemFromIndex(idx)
        if item and item.isCheckable():
            port = (item.parent().text(), item.text())
            if item.checkState() == 0 and port in self.rec_sources:
                self.rec_sources.remove(port)
            elif item.checkState():
                self.rec_sources.add(port)

            self.checkRecordEnable()

    def checkRecordEnable(self):
        enable = True

        if self.ui.rb_source_selected.isChecked() and not self.rec_sources:
            enable = False

        if self.ui.group_time.isChecked() and self.ui.te_end.time() <= self.ui.te_start.time():
            enable = False

        self.ui.b_render.setEnabled(enable)

    @pyqtSlot(bool)
    def slot_toggleRecordingSource(self, dummy=None):
        enabled = self.ui.rb_source_selected.isChecked()
        self.ui.tree_outputs.setEnabled(enabled)
        self.ui.tree_inputs.setEnabled(enabled)
        self.checkRecordEnable()

    @pyqtSlot(bool)
    def slot_transportChecked(self, dummy=None):
        self.checkRecordEnable()

    @pyqtSlot()
    def slot_updateProgressbar(self):
        time = int(jacklib.get_current_transport_frame(self.fJackClient)) / self.fSampleRate
        self.ui.progressBar.setValue(time)

        if time > self.fMaxTime or (self.fLastTime > time and not self.fFreewheel):
            self.slot_renderStop()

        self.fLastTime = time

    def saveSettings(self):
        settings = QSettings(ORGANIZATION, PROGRAM)

        if self.ui.rb_mono.isChecked():
            channels = 1
        elif self.ui.rb_stereo.isChecked():
            channels = 2
        else:
            channels = self.ui.sb_channels.value()

        settings.setValue("Geometry", self.saveGeometry())
        settings.setValue("OutputFolder", self.ui.le_folder.text())
        settings.setValue("FilenamePrefix", self.ui.le_prefix.text())
        settings.setValue("EncodingFormat", self.ui.cb_format.currentText())
        settings.setValue("EncodingDepth", self.ui.cb_depth.currentData())
        settings.setValue("EncodingChannels", channels)
        settings.setValue("UseTransport", self.ui.group_time.isChecked())
        settings.setValue("StartTime", self.ui.te_start.time())
        settings.setValue("EndTime", self.ui.te_end.time())
        settings.setValue("ExtraArgs", self.ui.le_extra_args.text().strip())

        if self.ui.rb_source_default.isChecked():
            settings.setValue("RecordingSource", 0)
        elif self.ui.rb_source_manual.isChecked():
            settings.setValue("RecordingSource", 1)
        elif self.ui.rb_source_selected.isChecked():
            settings.setValue("RecordingSource", 2)

        settings.beginWriteArray("Sources")
        for i, (client, port) in enumerate(self.rec_sources):
            settings.setArrayIndex(i);
            settings.setValue("Client", client)
            settings.setValue("Port", port)
        settings.endArray()

    def loadSettings(self):
        settings = QSettings(ORGANIZATION, PROGRAM)

        self.restoreGeometry(settings.value("Geometry", b""))

        outputFolder = settings.value("OutputFolder", os.getenv("HOME"))

        if isdir(outputFolder):
            self.ui.le_folder.setText(outputFolder)

        self.ui.le_prefix.setText(settings.value("FilenamePrefix", "jack_capture_"))

        encFormat = settings.value("EncodingFormat", "wav", type=str)

        for i in range(self.ui.cb_format.count()):
            if self.ui.cb_format.itemText(i) == encFormat:
                self.ui.cb_format.setCurrentIndex(i)
                break

        encDepth = settings.value("EncodingDepth", "FLOAT", type=str)

        for i in range(self.ui.cb_depth.count()):
            if self.ui.cb_depth.itemData(i) == encDepth:
                self.ui.cb_depth.setCurrentIndex(i)
                break

        encChannels = settings.value("EncodingChannels", 2, type=int)

        if encChannels == 1:
            self.ui.rb_mono.setChecked(True)
        elif encChannels == 2:
            self.ui.rb_stereo.setChecked(True)
        else:
            self.ui.rb_outro.setChecked(True)
            self.ui.sb_channels.setValue(encChannels)

        recSource = settings.value("RecordingSource", 0, type=int)

        if recSource == 1:
            self.ui.rb_source_manual.setChecked(True)
        elif recSource == 2:
            self.ui.rb_source_selected.setChecked(True)
        else:
            self.ui.rb_source_default.setChecked(True)

        self.ui.group_time.setChecked(settings.value("UseTransport", False, type=bool))
        self.ui.te_start.setTime(settings.value("StartTime", self.ui.te_start.time(), type=QTime))
        self.ui.te_end.setTime(settings.value("EndTime", self.ui.te_end.time(), type=QTime))

        self.ui.le_extra_args.setText(settings.value("ExtraArgs", "", type=str))

        size = settings.beginReadArray("Sources")
        for i in range(size):
            settings.setArrayIndex(i)
            client = settings.value("Client", type=str)
            port = settings.value("Port", type=str)
            if client and port:
                self.rec_sources.add((client, port))
        settings.endArray()

    def closeEvent(self, event):
        self.saveSettings()

        if self.fJackClient:
            jacklib.client_close(self.fJackClient)

        QDialog.closeEvent(self, event)

    def done(self, r):
        QDialog.done(self, r)
        self.close()


# ------------------------------------------------------------------------------------------------------------
# Allow to use this as a standalone app


def main(args=None):
    # App initialization
    app = QApplication(sys.argv if args is None else args)
    app.setApplicationName(PROGRAM)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(ORGANIZATION)
    app.setWindowIcon(QIcon(":/icons//scalable/qjackcapture.svg"))

    logging.basicConfig(level=logging.INFO, format="%(name)s:%(levelname)s: %(message)s")

    if jacklib is None:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "JACK is not available in this system, cannot use this application.",
            ),
        )
        return 1

    if not gJackCapturePath:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "The 'jack_capture' application is not available.\n"
                "Is not possible to render without it!",
            ),
        )
        return 2

    jack_status = jacklib.jack_status_t(0x0)
    jack_client = jacklib.client_open(
        PROGRAM, jacklib.JackNoStartServer, jacklib.pointer(jack_status)
    )

    if not jack_client:
        errorString = get_jack_status_error_string(jack_status)
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM, "Could not connect to JACK, possible reasons:\n" "%s" % errorString
            ),
        )
        return 1

    # Show GUI
    gui = QJackCaptureMainWindow(None, jack_client)
    gui.setWindowIcon(get_icon("media-record", 48))
    gui.show()

    # App-Loop
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main() or 0)
