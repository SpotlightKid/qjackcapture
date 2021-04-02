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
from time import sleep
from os.path import exists, isdir, join, sep as pathsep

from PyQt5.QtCore import pyqtSlot, QProcess, QTime, QTimer, QSettings
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

# -------------------------------------------------------------------------------------------------
# Application-specific imports

from .ui_mainwindow import Ui_MainWindow
from .version import __version__

try:
    from . import jacklib
    from .jacklib_helpers import get_jack_status_error_string
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

        self.createUi()
        self.loadSettings()

    def populateFormats(self):
        # Get List of sample formats
        self.fProcess.start(gJackCapturePath, ["-pf"])
        self.fProcess.waitForFinished()

        formats = []

        for fmt in str(self.fProcess.readAllStandardOutput(), encoding="utf-8").split():
            fmt = fmt.strip()
            if fmt:
                formats.append(fmt)

        # Put all formats in combo-box, select 'wav' option
        for i, fmt in enumerate(sorted(formats)):
            self.ui.cb_format.addItem(fmt)

            if fmt == "wav":
                self.ui.cb_format.setCurrentIndex(i)

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

        self.populateFormats()

        self.ui.cb_depth.setCurrentIndex(4)  # Float
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

        self.fFreewheel = bool(self.ui.cb_render_mode.currentIndex() == 1)
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

        # Format
        arguments.append("-f")
        arguments.append(self.ui.cb_format.currentText())

        # Bit depth
        arguments.append("-b")
        arguments.append(self.ui.cb_depth.currentText())

        # Channels
        arguments.append("-c")
        if self.ui.rb_mono.isChecked():
            arguments.append("1")
        elif self.ui.rb_stereo.isChecked():
            arguments.append("2")
        else:
            arguments.append(str(self.ui.sb_channels.value()))

        # Controlled only by freewheel
        if self.fFreewheel:
            arguments.append("-jf")

        # Controlled by transport
        elif useTransport:
            arguments.append("-jt")

        # Silent mode
        arguments.append("-dc")
        arguments.append("-s")

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
            if (
                jacklib.transport_query(self.fJackClient, None) > jacklib.JackTransportStopped
            ):  # rolling or starting
                jacklib.transport_stop(self.fJackClient)

            jacklib.transport_locate(self.fJackClient, minTime * self.fSampleRate)

        log.debug("jack_capture command line: %r", arguments)
        self.fProcess.start(gJackCapturePath, arguments)
        self.fProcess.waitForStarted()

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

    @pyqtSlot(bool)
    def slot_transportChecked(self, yesNo):
        if yesNo:
            renderEnabled = bool(self.ui.te_end.time() > self.ui.te_start.time())
        else:
            renderEnabled = True

        self.ui.b_render.setEnabled(renderEnabled)

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
        settings.setValue("EncodingDepth", self.ui.cb_depth.currentText())
        settings.setValue("EncodingChannels", channels)
        settings.setValue("UseTransport", self.ui.group_time.isChecked())
        settings.setValue("StartTime", self.ui.te_start.time())
        settings.setValue("EndTime", self.ui.te_end.time())
        settings.setValue("ExtraArgs", self.ui.le_extra_args.text().strip())

    def loadSettings(self):
        settings = QSettings(ORGANIZATION, PROGRAM)

        self.restoreGeometry(settings.value("Geometry", b""))

        outputFolder = settings.value("OutputFolder", os.getenv("HOME"))

        if isdir(outputFolder):
            self.ui.le_folder.setText(outputFolder)

        self.ui.le_prefix.setText(settings.value("FilenamePrefix", "jack_capture_"))

        encFormat = settings.value("EncodingFormat", "Wav", type=str)

        for i in range(self.ui.cb_format.count()):
            if self.ui.cb_format.itemText(i) == encFormat:
                self.ui.cb_format.setCurrentIndex(i)
                break

        encDepth = settings.value("EncodingDepth", "Float", type=str)

        for i in range(self.ui.cb_depth.count()):
            if self.ui.cb_depth.itemText(i) == encDepth:
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

        self.ui.group_time.setChecked(settings.value("UseTransport", False, type=bool))
        self.ui.te_start.setTime(settings.value("StartTime", self.ui.te_start.time(), type=QTime))
        self.ui.te_end.setTime(settings.value("EndTime", self.ui.te_end.time(), type=QTime))

        self.ui.le_extra_args.setText(settings.value("ExtraArgs", "", type=str))

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

    logging.basicConfig(level=logging.DEBUG, format="%(name)s:%(levelname)s: %(message)s")

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
