#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# JACK-Capture frontend, with freewheel and transport support
# Copyright (C) 2010-2018 Filipe Coelho <falktx@falktx.com>
# Copyright (C) 2020-2022 Christopher Arndt <info@chrisarndt.de>
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
#
"""A graphical user interface for jack_capture."""

# -------------------------------------------------------------------------------------------------
# Standard library imports

import argparse
import datetime
import logging
import os
import re
import shlex
import sys
from collections import namedtuple
from enum import IntEnum
from functools import lru_cache, partial
from operator import attrgetter
from os.path import exists, expanduser, isdir, join, sep as pathsep
from signal import SIGINT, SIGTERM, Signals, signal
from string import Template
from time import sleep

# -------------------------------------------------------------------------------------------------
# Third-party package imports

try:
    import jacklib
    from jacklib import JACK_METADATA_ORDER, JACK_METADATA_PORT_GROUP, JACK_METADATA_PRETTY_NAME
    from jacklib.helpers import c_char_p_p_to_list, get_jack_status_error_string
except ImportError:
    jacklib = None

from natsort import humansorted

try:
    from qtpy.QtCore import (
        QLibraryInfo,
        QLocale,
        QModelIndex,
        QObject,
        QProcess,
        QProcessEnvironment,
        QSettings,
        Qt,
        QTime,
        QTimer,
        QTranslator,
        Signal,
        Slot,
    )
    from qtpy.QtGui import QIcon, QStandardItem, QStandardItemModel
    from qtpy.QtWidgets import QApplication, QDialog, QFileDialog, QMenu, QMessageBox
except ImportError:
    from PyQt5.QtCore import (
        QLibraryInfo,
        QLocale,
        QModelIndex,
        QObject,
        QProcess,
        QProcessEnvironment,
        QSettings,
        Qt,
        QTime,
        QTimer,
        QTranslator,
    )
    from PyQt5.QtCore import pyqtSignal as Signal
    from PyQt5.QtCore import pyqtSlot as Slot
    from PyQt5.QtGui import QIcon, QStandardItem, QStandardItemModel
    from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QMenu, QMessageBox

# -------------------------------------------------------------------------------------------------
# Application-specific imports

from .nsmclient import NSMClient
from .ui_mainwindow import Ui_MainWindow
from .ui_outputhelpwin import Ui_outputHelpWin
from .ui_sourceshelpwin import Ui_sourcesHelpWin
from .userdirs import get_user_dir, get_user_dirs
from .version import __version__

# -------------------------------------------------------------------------------------------------
# Global constants

ORGANIZATION = "chrisarndt.de"
PROGRAM = "QJackCapture"
DEFAULT_OUTPUT_FOLDER = "${musicdir}"
DEFAULT_OUTPUT_FOLDER_NSM = "${nsmclientdir}/recordings"
DEFAULT_FILENAM_PREFIX = "${jackclientname}-${timestamp}"
DEFAULT_FILENAM_PREFIX_NSM = "${nsmsessionname}-${timestamp}"

# -------------------------------------------------------------------------------------------------
# Global objects

log = logging.getLogger(PROGRAM)


# -------------------------------------------------------------------------------------------------
# Custom exceptions


class JackConnectError(Exception):
    """Raised when no connection to JACK server can be established."""

    pass


class JackCaptureUnsupportedError(Exception):
    """Raised when external jack_capture program version is not supported."""

    pass


# -------------------------------------------------------------------------------------------------
# Types

JackPort = namedtuple(
    "JackPort", ["client", "group", "order", "name", "pretty_name", "uuid", "aliases"]
)


class RecordingStatus(IntEnum):
    STOPPED = 1
    RECORDING = 2
    STOPPING = 3


# -------------------------------------------------------------------------------------------------
# Utility functions


def clean_filename(filename, extra_chars=""):
    rx = r"[^a-zA-Z0-9,\_\-\.\(\) " + extra_chars + r"]"
    return re.sub(rx, "_", filename)


def get_icon(name, size=16):
    """Return QIcon from resources given name and, optional, size."""
    return QIcon.fromTheme(name, QIcon(":/icons/%ix%i/%s.png" % (size, size, name)))


def posnum(arg):
    """Make sure that command line arg is a positive number."""
    value = float(arg)
    if value < 0:
        raise argparse.ArgumentTypeError("Value must not be negative!")
    return value


# -------------------------------------------------------------------------------------------------
# JACK client


class QJackCaptureClient(QObject):
    PROPERTY_CHANGE_MAP = {
        jacklib.PropertyCreated: "created",
        jacklib.PropertyChanged: "changed",
        jacklib.PropertyDeleted: "deleted",
    }
    ports_changed = Signal()
    jack_disconnect = Signal()
    freewheel = Signal(int)

    def __init__(self, client_name, connect_interval=3.0, connect_max_attempts=0):
        super().__init__()
        self.client_name = client_name
        self.connect_max_attempts = connect_max_attempts
        self.connect_interval = connect_interval
        self.default_encoding = jacklib.ENCODING

        jacklib.set_error_function(self.error_callback)
        self.connect()

    # ---------------------------------------------------------------------------------------------
    # JACK connection & setup

    def connect(self, max_attempts=None):
        if max_attempts is None:
            max_attempts = self.connect_max_attempts

        tries = 0
        while True:
            log.debug("Attempting to connect to JACK server...")
            status = jacklib.jack_status_t(0x0)
            self.client = jacklib.client_open(self.client_name, jacklib.JackNoStartServer, status)
            tries += 1

            if status.value:
                err = get_jack_status_error_string(status)
                if status.value & jacklib.JackNameNotUnique:
                    log.debug(err)
                elif status.value & jacklib.JackServerStarted:
                    # Should not happen, since we use the JackNoStartServer option
                    log.warning("Unexpected JACK status: %s", err)
                else:
                    log.warning("JACK connection error (attempt %i): %s", tries, err)

            if self.client:
                break

            if max_attempts and tries >= max_attempts:
                log.error(
                    "Maximum number (%i) of connection attempts reached. Aborting.", max_attempts
                )
                raise JackConnectError(err)

            log.debug("Waiting %.2f seconds to connect again...", self.connect_interval)
            sleep(self.connect_interval)

        name = jacklib.get_client_name(self.client)
        if name is not None:
            self.client_name = name.decode()
        else:
            self.close()
            raise JackConnectError("Could not get JACK client name.")

        jacklib.on_shutdown(self.client, self.shutdown_callback, None)
        log.debug(
            "Client connected, name: %s UUID: %s",
            self.client_name,
            jacklib.client_get_uuid(self.client),
        )
        jacklib.set_port_registration_callback(self.client, self.port_reg_callback, None)
        jacklib.set_port_rename_callback(self.client, self.port_rename_callback, None)
        jacklib.set_property_change_callback(self.client, self.property_callback, None)
        jacklib.set_freewheel_callback(self.client, self.freewheel_callback, None)
        jacklib.activate(self.client)

    def close(self):
        if self.client:
            log.debug("Closing JACK connection.")
            return jacklib.client_close(self.client)

    def _refresh(self):
        log.debug("Port list refresh needed.")
        self.ports_changed.emit()

    # ---------------------------------------------------------------------------------------------
    # Callbacks

    def error_callback(self, error):
        error = error.decode(self.default_encoding, errors="ignore")
        log.debug(error)

    def freewheel_callback(self, freewheel, *args):
        log.debug("JACK freewheel mode changed to: %i", freewheel)
        self.freewheel.emit(freewheel)

    def property_callback(self, subject, name, type_, *args):
        if name is not None:
            name = name.decode(self.default_encoding, errors="ignore")

        if not name and type_ == jacklib.PropertyDeleted:
            log.debug("All properties on subject %s deleted.", subject)
        else:
            action = self.PROPERTY_CHANGE_MAP.get(type_)
            log.debug("Property '%s' on subject %s %s.", name, subject, action)

        if name in (JACK_METADATA_ORDER, JACK_METADATA_PORT_GROUP, JACK_METADATA_PRETTY_NAME):
            self._refresh()

    def port_reg_callback(self, port_id, action, *args):
        port = jacklib.port_by_id(self.client, port_id)
        if action == 0:
            log.debug("Port unregistered: %s", jacklib.port_name(port))
        else:
            port = jacklib.port_by_id(self.client, port_id)
            log.debug("New port registered: %s", jacklib.port_name(port))

        self._refresh()

    def port_rename_callback(self, port_id, old_name, new_name, *args):
        if old_name:
            old_name = old_name.decode(self.default_encoding, errors="ignore")

        if new_name:
            new_name = new_name.decode(self.default_encoding, errors="ignore")

        log.debug("Port name %s changed to %s.", old_name, new_name)
        self._refresh()

    def shutdown_callback(self, *args):
        """Emit 'jack_disconnect' signal when JACK server signals shutdown."""
        log.debug("JACK server signalled shutdown.")
        self.jack_disconnect.emit()

    # ---------------------------------------------------------------------------------------------
    # Port discovery

    @lru_cache()
    def _get_port(self, port_name):
        return jacklib.port_by_name(self.client, port_name)

    @lru_cache()
    def _get_port_uuid(self, port_name):
        return jacklib.port_uuid(self._get_port(port_name))

    @lru_cache()
    def _get_port_group(self, port_name):
        prop = jacklib.get_port_property(
            self.client, self._get_port(port_name), JACK_METADATA_PORT_GROUP
        )
        if prop:
            return int(prop.value)

    @lru_cache()
    def _get_port_order(self, port_name):
        prop = jacklib.get_port_property(
            self.client, self._get_port(port_name), JACK_METADATA_ORDER
        )
        if prop:
            try:
                return int(prop.value)
            except (TypeError, ValueError):
                return None

    @lru_cache()
    def _get_port_pretty_name(self, port_name):
        return jacklib.get_port_pretty_name(self.client, self._get_port(port_name))

    @lru_cache()
    def _get_aliases(self, port_name):
        port = self._get_port(port_name)
        num_aliases, *aliases = jacklib.port_get_aliases(port)
        return aliases[:num_aliases]

    def get_ports(self, inout=jacklib.JackPortIsOutput, typeptn=jacklib.JACK_DEFAULT_AUDIO_TYPE):
        for i, port_name in enumerate(
            c_char_p_p_to_list(jacklib.get_ports(self.client, "", typeptn, inout))
        ):
            client, name = port_name.split(":", 1)
            uuid = self._get_port_uuid(port_name)
            pretty_name = self._get_port_pretty_name(port_name)
            group = self._get_port_group(port_name)
            order = self._get_port_order(port_name)
            aliases = self._get_aliases(port_name)
            yield JackPort(client, group, order, name, pretty_name, uuid, aliases)

    def get_input_ports(self):
        return self.get_ports(inout=jacklib.JackPortIsInput)

    def get_output_ports(self):
        return self.get_ports(inout=jacklib.JackPortIsOutput)

    # ---------------------------------------------------------------------------------------------
    # Delegation methods

    def deactivate(self):
        return jacklib.deactivate(self.client)

    def get_buffer_size(self):
        return int(jacklib.get_buffer_size(self.client))

    def set_buffer_size(self, bufsize):
        return jacklib.set_buffer_size(self.client, bufsize)

    def get_sample_rate(self):
        return int(jacklib.get_sample_rate(self.client))

    def set_freewheel(self, flag):
        return jacklib.set_freewheel(self.client, flag)

    def transport_locate(self, frame):
        return jacklib.transport_locate(self.client, frame)

    def transport_query(self, position=None):
        return jacklib.transport_query(self.client, position)

    def transport_running(self):
        return self.transport_query() > jacklib.JackTransportStopped

    def transport_start(self):
        return jacklib.transport_start(self.client)

    def transport_stop(self):
        return jacklib.transport_stop(self.client)

    def transport_frame(self):
        return int(jacklib.get_current_transport_frame(self.client))


# -------------------------------------------------------------------------------------------------
# Helper Windows


class OutputHelpWin(QDialog):
    def __init__(self, *args):
        QDialog.__init__(self, *args)
        self.ui = Ui_outputHelpWin()
        self.ui.setupUi(self)
        # self.setWindowFlag(Qt.FramelessWindowHint)
        self.hide()


class SourcesHelpWin(QDialog):
    def __init__(self, *args):
        QDialog.__init__(self, *args)
        self.ui = Ui_sourcesHelpWin()
        self.ui.setupUi(self)
        # self.setWindowFlag(Qt.FramelessWindowHint)
        self.hide()


# -------------------------------------------------------------------------------------------------
# Main Window


class QJackCaptureMainWindow(QDialog):
    sampleFormats = {
        "32-bit float": "FLOAT",
        "8-bit integer": "8",
        "16-bit integer": "16",
        "24-bit integer": "24",
        "32-bit integer": "32",
    }

    def __init__(self, app, visible=True):
        QDialog.__init__(self)
        self.app = app
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.visible = visible

        self.recordingStatus = RecordingStatus.STOPPED
        self.freewheelStatus = False
        self.useFreewheel = False
        self.useTransport = False
        self.lastUpdateTime = -1
        self.recEndTime = 600
        self.maxFolderHistory = 15
        self.maxPrefixHistory = 15
        self.processTimeout = 3000

        self.refreshTimer = None
        self.progressTimer = QTimer(self)
        self.jackCaptureProcess = QProcess(self)

        # Selected ports used as recording sources
        self.recSources = set()

        self.createUi()
        self.populatePortLists(init=True)
        self.checkSupportedOptions()

        # listen to changes to JACK ports
        self.jackClient.ports_changed.connect(self.slot_refreshPortsLists)
        self.jackClient.jack_disconnect.connect(self.slot_jackDisconnect)
        self.jackClient.freewheel.connect(self.slot_freewheelChanged)
        self.jackCaptureProcess.finished.connect(self.slot_jackCaptureExit)

    @property
    def jackClient(self):
        return self.app.jackClient

    @property
    def sampleRate(self):
        return self.jackClient.get_sample_rate()

    @property
    def bufferSize(self):
        return self.jackClient.get_buffer_size()

    @Slot()
    def slot_refreshPortsLists(self, delay=200):
        if not self.refreshTimer or not self.refreshTimer.isActive():
            log.debug("Scheduling port lists refresh in %i ms...", delay)

            if not self.refreshTimer:
                self.refreshTimer = QTimer()

            self.refreshTimer.setSingleShot(True)
            self.refreshTimer.timeout.connect(self.populatePortLists)
            self.refreshTimer.start(delay)

    def _makeSubstitutions(self, **extra_data):
        utcnow = datetime.datetime.now(datetime.timezone.utc)
        sampleformat = self.ui.cb_samplefmt.currentData()
        subst = dict(
            channels="1"
            if self.ui.rb_mono.isChecked()
            else ("2" if self.ui.rb_stereo.isChecked() else str(self.ui.sb_channels.value())),
            fileformat=self.ui.cb_format.currentText(),
            sampleformat="f32" if sampleformat == "FLOAT" else f"i{sampleformat}",
            samplerate=str(self.sampleRate),
            jackclientname=self.app.jackClientName,
            **extra_data,
        )

        if self.app.nsmClient is not None:
            subst.update(
                dict(
                    nsmsessionname=self.app.nsmClient.sessionName,
                    nsmclientid=self.app.nsmClient.ourClientId,
                    nsmclientname=self.app.nsmClient.ourClientNameUnderNSM,
                )
            )

        for dt, key_prefix in ((utcnow.astimezone(), ""), (utcnow, "utc")):
            fmt = dt.strftime
            data = dict(
                isodate=dt.date().today().isoformat(),
                ctime=str(int(dt.timestamp())),
                date=fmt("%Y%m%d"),
                timestamp=fmt("%Y%m%d-%H%M%S"),
                year=fmt("%Y"),
                month=fmt("%m"),
                day=fmt("%d"),
                hour=fmt("%H"),
                minute=fmt("%M"),
                second=fmt("%S"),
                hm=fmt("%H%M"),
                hms=fmt("%H%M%S"),
            )
            subst.update({key_prefix + k: v for k, v in data.items()})

        return subst

    def _genOutputFolder(self):
        tmpl = Template(self.ui.cb_folder.currentText())
        substitutions = self._makeSubstitutions()
        user_dirs = get_user_dirs()
        substitutions.update(
            {key.lower()[4:].replace("_", ""): val for key, val in user_dirs.items()}
        )

        if self.app.nsmClient is not None:
            substitutions["nsmclientdir"] = self.app.nsmClient.ourPath

        log.debug("Output folder substitutions: %r", substitutions)

        try:
            folder = tmpl.substitute(substitutions)
            success = True
        except Exception as exc:
            success = False
            log.warning("Could not format output folder template: %s", exc)
            folder = tmpl.safe_substitute(substitutions)

        return folder, success

    def _genFilenamePrefix(self):
        tmpl = Template(self.ui.cb_prefix.currentText())
        substitutions = self._makeSubstitutions()
        log.debug("Filename prefix substitutions: %r", substitutions)

        try:
            prefix = tmpl.substitute(substitutions)
            success = True
        except Exception as exc:
            success = False
            log.warning("Could not format filename prefix template: %s", exc)
            prefix = tmpl.safe_substitute(substitutions)

        return prefix, success

    def _updateCbFolderHistory(self):
        cb_folder = self.ui.cb_folder
        folder_tmpl = cb_folder.currentText()

        if cb_folder.findText(folder_tmpl) == -1:
            # Add to folder combo-box history
            cb_folder.insertItem(0, folder_tmpl)

        cb_folder.setMaxCount(self.maxFolderHistory)

    def _updateCbPrefixHistory(self):
        cb_prefix = self.ui.cb_prefix
        prefix_tmpl = cb_prefix.currentText()

        if cb_prefix.findText(prefix_tmpl) == -1:
            # Add to filename prefix combo-box history
            cb_prefix.insertItem(0, prefix_tmpl)

        cb_prefix.setMaxCount(self.maxPrefixHistory)

    def checkSupportedOptions(self):
        # Get help text to check for existence of options missing in older jack_capture versions
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("LC_ALL", "C")
        proc.setProcessEnvironment(env)
        proc.start(self.app.jackCapturePath, ["--help2"])
        proc.waitForFinished(self.processTimeout)
        help_text = str(proc.readAllStandardOutput(), "utf-8")
        proc.close()
        self.supportedOptions = {}
        self.supportedOptions["jack-name"] = "[-jn]" in help_text
        log.debug("Options supported by jack_capture: %s", self.supportedOptions)

    def populateFileFormats(self):
        """Get and save list of supported file formats."""
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("LC_ALL", "C")
        proc.setProcessEnvironment(env)
        proc.start(self.app.jackCapturePath, ["-pf"])
        proc.waitForFinished(self.processTimeout)

        if proc.exitCode() != 0:
            raise JackCaptureUnsupportedError(
                self.tr("Could not get list of supported output formats from jack_capture.")
            )

        formats = []

        for fmt in str(proc.readAllStandardOutput(), encoding="utf-8").strip().split():
            fmt = fmt.strip()
            if fmt:
                formats.append(fmt)

        proc.close()
        log.debug("File formats supported by jack_capture: %s", formats)

        if not formats:
            raise JackCaptureUnsupportedError(
                self.tr("List of supported output formats reported by jack_capture is empty.")
            )

        # Put all file formats in combo-box, select 'wav' option
        self.ui.cb_format.clear()
        for i, fmt in enumerate(sorted(formats)):
            self.ui.cb_format.addItem(fmt)

            if fmt == "wav":
                self.ui.cb_format.setCurrentIndex(i)

    def populateSampleFormats(self):
        # Put all sample formats in combo-box, select 'FLOAT' option
        self.ui.cb_samplefmt.clear()
        for i, (label, fmt) in enumerate(self.sampleFormats.items()):
            self.ui.cb_samplefmt.addItem(label, fmt)

            if fmt == "FLOAT":
                self.ui.cb_samplefmt.setCurrentIndex(i)

    def populatePortLists(self, init=False):
        log.debug("Populating port lists (init=%s)...", init)
        if init:
            self.outputs_model = QStandardItemModel(0, 1, self)
            self.inputs_model = QStandardItemModel(0, 1, self)
        else:
            self.outputs_model.clear()
            self.inputs_model.clear()

        output_ports = list(self.jackClient.get_output_ports())
        self.output_ports = self.populatePortList(
            self.outputs_model, self.ui.tree_outputs, output_ports
        )
        input_ports = list(self.jackClient.get_input_ports())
        self.input_ports = self.populatePortList(
            self.inputs_model, self.ui.tree_inputs, input_ports
        )

        self.slot_toggleRecordingSource()

    def makePortTooltip(self, port):
        s = []

        if port.pretty_name:
            s.append(f"<b>Pretty name:</b> <em>{port.pretty_name}</em><br>")

        s.append(f"<b>Port:</b> <tt>{port.client}:{port.name}</tt><br>")

        for i, alias in enumerate(port.aliases, 1):
            s.append(f"<b>Alias {i}:</b> <tt>{alias}</tt><br>")

        s.append(f"<b>UUID:</b> <tt>{port.uuid}</tt>")
        return "<small>{}</small>".format("\n".join(s))

    def populatePortList(self, model, tv, ports):
        tv.setModel(model)
        root = model.invisibleRootItem()

        portsdict = {}

        for port in ports:
            if port.client not in portsdict:
                portsdict[port.client] = []

            portsdict[port.client].append(port)

        for client in humansorted(portsdict):
            clientitem = QStandardItem(client)
            portsdict[client] = humansorted(
                portsdict[client], key=attrgetter("group", "order", "pretty_name", "name")
            )

            for port in portsdict[client]:
                portspec = (port.client, port.name)

                if port.pretty_name:
                    label = "%s (%s)" % (port.pretty_name, port.name)
                else:
                    label = port.name

                portitem = QStandardItem(label)
                portitem.setData(portspec)
                portitem.setCheckable(True)
                portitem.setUserTristate(False)
                # Check box toggling is done in the treeview clicked handler "on_port_clicked"
                portitem.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                portitem.setToolTip(self.makePortTooltip(port))

                if portspec in self.recSources:
                    portitem.setCheckState(2)

                clientitem.appendRow(portitem)

            root.appendRow(clientitem)

        tv.expandAll()
        return portsdict

    def createUi(self):
        # -------------------------------------------------------------
        # Set-up GUI stuff

        for i in range(self.ui.cb_buffer_size.count()):
            if int(self.ui.cb_buffer_size.itemText(i)) == self.bufferSize:
                self.ui.cb_buffer_size.setCurrentIndex(i)
                break
        else:
            self.ui.cb_buffer_size.addItem(str(self.bufferSize))
            self.ui.cb_buffer_size.setCurrentIndex(self.ui.cb_buffer_size.count() - 1)

        self.populateFileFormats()
        self.populateSampleFormats()

        self.ui.lbl_srdisplay.setText(str(self.sampleRate))
        self.ui.rb_stereo.setChecked(True)
        self.ui.te_end.setTime(QTime(0, 10, 0))
        self.ui.progressBar.setFormat("")
        self.ui.progressBar.setMinimum(0)
        self.ui.progressBar.setMaximum(1)
        self.ui.progressBar.setValue(0)

        self.ui.b_render.setIcon(get_icon("media-record"))
        self.ui.b_stop.setIcon(get_icon("media-playback-stop"))
        self.ui.b_close.setIcon(get_icon("window-close"))
        self.ui.b_folder.setIcon(get_icon("document-open"))
        self.ui.b_stop.setVisible(False)

        self.outputHelpWin = OutputHelpWin(self)
        self.sourcesHelpWin = SourcesHelpWin(self)

        # -------------------------------------------------------------
        # Set-up connections

        self.ui.b_render.clicked.connect(self.slot_renderStart)
        self.ui.b_stop.clicked.connect(self.slot_renderStop)
        self.ui.b_folder.clicked.connect(self.slot_getAndSetPath)
        self.ui.b_now_start.clicked.connect(self.slot_setStartNow)
        self.ui.b_now_end.clicked.connect(self.slot_setEndNow)
        self.ui.te_start.timeChanged.connect(self.slot_updateStartTime)
        self.ui.te_end.timeChanged.connect(self.slot_updateEndTime)
        self.ui.group_time.clicked.connect(self.slot_transportChecked)
        self.ui.rb_source_default.toggled.connect(self.slot_toggleRecordingSource)
        self.ui.rb_source_manual.toggled.connect(self.slot_toggleRecordingSource)
        self.ui.rb_source_selected.toggled.connect(self.slot_toggleRecordingSource)
        self.ui.cb_prefix.currentTextChanged.connect(self.slot_cbPrefixChanged)
        self.ui.cb_folder.currentTextChanged.connect(self.slot_cbFolderChanged)
        self.ui.b_prefix_help.clicked.connect(self.slot_togglePrefixHelp)
        self.ui.b_sources_help.clicked.connect(self.slot_toggleSourcesHelp)
        self.progressTimer.timeout.connect(self.slot_updateProgressbar)

        for tv in (self.ui.tree_outputs, self.ui.tree_inputs):
            menu = QMenu()
            menu.addAction(get_icon("expand-all"), self.tr("E&xpand all"), tv.expandAll)
            menu.addAction(get_icon("collapse-all"), self.tr("&Collapse all"), tv.collapseAll)
            menu.addSeparator()
            menu.addAction(
                get_icon("list-select-all"),
                self.tr("&Select all in group"),
                partial(self.on_select_port_group, tv),
            )
            menu.addAction(
                get_icon("list-select-none"),
                self.tr("&Unselect all in group"),
                partial(self.on_select_port_group, tv, enable=False),
            )
            menu.addSeparator()
            if tv is self.ui.tree_outputs:
                menu.addAction(
                    get_icon("select-none"),
                    self.tr("Unselect all &outputs"),
                    partial(self.on_clear_all_ports, tv),
                )
            else:
                menu.addAction(
                    get_icon("select-none"),
                    self.tr("Unselect all &inputs"),
                    partial(self.on_clear_all_ports, tv),
                )

            tv.setContextMenuPolicy(Qt.CustomContextMenu)
            tv.customContextMenuRequested.connect(
                partial(self.on_port_menu, treeview=tv, menu=menu)
            )
            tv.clicked.connect(self.on_port_clicked)

    def enable_port(self, item, enable=True):
        item.setCheckState(2 if enable else 0)
        port = item.data()
        if enable:
            self.recSources.add(port)
        else:
            self.recSources.discard(port)

    def on_port_menu(self, pos, treeview=None, menu=None):
        if treeview and menu:
            menu.popup(treeview.viewport().mapToGlobal(pos))

    def foreach_item(self, model, parent, func, leaves_only=True):
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            is_leaf = not model.hasChildren(index)

            if is_leaf or not leaves_only:
                func(model.itemFromIndex(index))

            if not is_leaf:
                self.foreach_item(model, index, func)

    def on_clear_all_ports(self, treeview):
        self.foreach_item(treeview.model(), QModelIndex(), partial(self.enable_port, enable=False))
        self.checkRecordEnable()

    def on_port_clicked(self, index):
        model = index.model()
        item = model.itemFromIndex(index)
        if not model.hasChildren(index):
            self.enable_port(item, not item.checkState())
            self.checkRecordEnable()

    def on_select_port_group(self, treeview, enable=True):
        index = treeview.currentIndex()
        model = index.model()

        if not model.hasChildren(index):
            index = index.parent()

        self.foreach_item(model, index, partial(self.enable_port, enable=enable))
        self.checkRecordEnable()

    @Slot()
    def slot_renderStart(self):
        # Output folder
        output_folder_tmpl = self.ui.cb_folder.currentText()
        output_folder, folder_valid = self._genOutputFolder()
        output_folder = clean_filename(output_folder, extra_chars=pathsep)

        if not isdir(output_folder):
            try:
                if exists(output_folder):
                    raise FileExistsError(f"File exists and is not a directory: {output_folder}")

                if self.app.nsmClient is None or output_folder_tmpl != DEFAULT_OUTPUT_FOLDER_NSM:
                    res = QMessageBox.question(
                        self,
                        self.tr("Missing output folder"),
                        self.tr(
                            "The selected output folder does not exist:\n\n{}\n\n"
                            "Create it now and proceed?"
                        ).format(output_folder),
                        QMessageBox.Abort,
                        QMessageBox.Yes,
                    )

                    if res != QMessageBox.Yes:
                        return

                os.makedirs(output_folder, exist_ok=True)
            except (IOError, FileExistsError) as exc:
                QMessageBox.critical(
                    self,
                    self.tr("Error"),
                    self.tr(
                        "Invalid output folder:\n\n{}\n\nPlease check you settings.",
                    ).format(exc),
                    QMessageBox.Abort,
                )
                return

        timeStart = self.ui.te_start.time()
        timeEnd = self.ui.te_end.time()
        recStartTime = (timeStart.hour() * 3600) + (timeStart.minute() * 60) + (timeStart.second())
        recEndTime = (timeEnd.hour() * 3600) + (timeEnd.minute() * 60) + (timeEnd.second())

        newBufferSize = int(self.ui.cb_buffer_size.currentText())
        self.useTransport = self.ui.group_time.isChecked()

        self.useFreewheel = self.ui.rb_freewheel.isChecked()
        self.lastUpdateTime = -1
        self.recEndTime = recEndTime

        self.ui.group_render.setEnabled(False)
        self.ui.group_time.setEnabled(False)
        self.ui.group_encoding.setEnabled(False)
        self.ui.b_render.setVisible(False)
        self.ui.b_stop.setVisible(True)
        self.ui.b_close.setEnabled(False)

        if self.useTransport:
            self.ui.progressBar.setFormat("%p%")
            self.ui.progressBar.setMinimum(recStartTime)
            self.ui.progressBar.setMaximum(recEndTime)
            self.ui.progressBar.setValue(recStartTime)
        else:
            self.ui.progressBar.setFormat("")
            self.ui.progressBar.setMinimum(0)
            self.ui.progressBar.setMaximum(0)
            self.ui.progressBar.setValue(0)

        self.ui.progressBar.update()

        arguments = []

        # JACK client name
        if self.supportedOptions.get("jack-name"):
            arguments.append("-jn")

            if self.app.nsmClient is None:
                arguments.append(self.app.jackClientName)
            else:
                arguments.append(self.app.nsmClient.ourClientNameUnderNSM)

        # Channels
        arguments.append("-c")
        if self.ui.rb_mono.isChecked():
            arguments.append("1")
        elif self.ui.rb_stereo.isChecked():
            arguments.append("2")
        else:
            arguments.append(str(self.ui.sb_channels.value()))

        # File format
        arguments.append("-f")
        arguments.append(self.ui.cb_format.currentText())

        # Sample format (bit depth, int/float)
        arguments.append("-b")
        arguments.append(self.ui.cb_samplefmt.currentData())

        # Filename prefix
        arguments.append("-fp")
        prefix, prefix_valid = self._genFilenamePrefix()
        arguments.append(clean_filename(prefix))

        # Recording sources
        if self.ui.rb_source_manual.isChecked():
            arguments.append("-mc")
        elif self.ui.rb_source_selected.isChecked():
            for ports in (self.output_ports, self.input_ports):
                for client in humansorted(ports):
                    for port in ports[client]:
                        if (port.client, port.name) in self.recSources:
                            arguments.append("-p")
                            arguments.append("{}:{}".format(port.client, port.name))

        # Controlled only by freewheel
        if self.useFreewheel:
            arguments.append("-jf")

        # Controlled by transport
        elif self.useTransport:
            arguments.append("-jt")

        # Silent mode
        arguments.append("--daemon")

        # Extra arguments
        extra_args = self.ui.le_extra_args.text().strip()

        if extra_args:
            arg_list = shlex.split(extra_args)
            arguments.extend(arg_list)

        if newBufferSize != self.jackClient.get_buffer_size():
            log.info("Buffer size changed before render.")
            self.jackClient.set_buffer_size(newBufferSize)

        if self.useTransport:
            if self.jackClient.transport_running():
                # rolling or starting
                self.jackClient.transport_stop()

            self.jackClient.transport_locate(recStartTime * self.sampleRate)

        # Change working directory for jack_capture process
        self.jackCaptureProcess.setWorkingDirectory(output_folder)

        log.debug("'jack_capture' command line args: %r", arguments)
        self.jackCaptureProcess.start(self.app.jackCapturePath, arguments)
        status = self.jackCaptureProcess.waitForStarted(self.processTimeout)

        if not status:
            self.jackCaptureProcess.close()
            log.error("Could not start 'jack_capture'.")
            return

        self.recordingStatus = RecordingStatus.RECORDING

        if self.useFreewheel:
            log.info("Rendering in freewheel mode.")
            sleep(1)
            self.jackClient.set_freewheel(True)

        if self.useTransport:
            log.info("Rendering using JACK transport.")
            self.progressTimer.setInterval(500)
            self.progressTimer.start()
            self.jackClient.transport_start()

        if folder_valid:
            self._updateCbFolderHistory()

        if prefix_valid:
            self._updateCbPrefixHistory()

    @Slot(int, QProcess.ExitStatus)
    def slot_jackCaptureExit(self, exit_code, exit_status):
        log.debug(
            "'jack_capture' terminated with exit code %i, status = %r", exit_code, exit_status
        )
        if self.recordingStatus is RecordingStatus.RECORDING:
            self.beforeRecordingStop()

        if self.recordingStatus in (RecordingStatus.RECORDING, RecordingStatus.STOPPING):
            self.afterRecordingStop()

    @Slot()
    def slot_renderStop(self):
        if not self.jackCaptureProcess.state() in (QProcess.Starting, QProcess.Running):
            log.debug("No 'jack_capture' process running.")
            return

        log.debug("Stopping recording...")
        self.beforeRecordingStop()

        log.debug("Terminating 'jack_capture' process...")
        self.jackCaptureProcess.terminate()

    @Slot(int)
    def slot_freewheelChanged(self, freewheel):
        self.freewheelStatus = freewheel

        if (
            not freewheel
            and self.useFreewheel
            and self.recordingStatus is RecordingStatus.RECORDING
        ):
            self.slot_renderStop()

    def beforeRecordingStop(self):
        self.recordingStatus = RecordingStatus.STOPPING

        if self.useTransport and self.jackClient.transport_running():
            log.debug("Stopping JACK transport.")
            self.jackClient.transport_stop()

        if self.useFreewheel and self.freewheelStatus:
            log.debug("Leaving JACK Freewheel mode.")
            self.jackClient.set_freewheel(False)
            sleep(1)

    def afterRecordingStop(self):
        log.debug("Cleaning up after recording stop...")
        if self.useTransport:
            self.progressTimer.stop()

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
        newBufferSize = self.jackClient.get_buffer_size()

        if newBufferSize != self.bufferSize:
            self.jackClient.set_buffer_size(newBufferSize)

        self.recordingStatus = RecordingStatus.STOPPED

    @Slot()
    def slot_getAndSetPath(self):
        new_path = QFileDialog.getExistingDirectory(
            self, self.tr("Set Path"), self.ui.cb_folder.currentText(), QFileDialog.ShowDirsOnly
        )

        if new_path:
            self.ui.cb_folder.setCurrentText(new_path)

    @Slot()
    def slot_setStartNow(self):
        time = self.jackClient.transport_frame() // self.sampleRate
        secs = int(time % 60)
        mins = int(time / 60) % 60
        hrs = int(time / 3600) % 60
        self.ui.te_start.setTime(QTime(hrs, mins, secs))

    @Slot()
    def slot_setEndNow(self):
        time = self.jackClient.transport_frame() // self.sampleRate
        secs = int(time % 60)
        mins = int(time / 60) % 60
        hrs = int(time / 3600) % 60
        self.ui.te_end.setTime(QTime(hrs, mins, secs))

    @Slot(QTime)
    def slot_updateStartTime(self, time):
        if time >= self.ui.te_end.time():
            self.ui.te_end.setTime(time)
            renderEnabled = False
        else:
            renderEnabled = True

        if self.ui.group_time.isChecked():
            self.ui.b_render.setEnabled(renderEnabled)

    @Slot(QTime)
    def slot_updateEndTime(self, time):
        if time <= self.ui.te_start.time():
            self.ui.te_start.setTime(time)
            renderEnabled = False
        else:
            renderEnabled = True

        if self.ui.group_time.isChecked():
            self.ui.b_render.setEnabled(renderEnabled)

    @Slot(bool)
    def slot_toggleRecordingSource(self, dummy=None):
        enabled = self.ui.rb_source_selected.isChecked()
        self.ui.tree_outputs.setEnabled(enabled)
        self.ui.tree_inputs.setEnabled(enabled)
        self.checkRecordEnable()

    @Slot(bool)
    def slot_transportChecked(self, dummy=None):
        self.checkRecordEnable()

    @Slot()
    def slot_updateProgressbar(self):
        time = self.jackClient.transport_frame() / self.sampleRate
        self.ui.progressBar.setValue(int(time))

        if time > self.recEndTime or (self.lastUpdateTime > time and not self.useFreewheel):
            self.slot_renderStop()

        self.lastUpdateTime = time

    @Slot(str)
    def slot_cbFolderChanged(self, text):
        log.debug("Output folder template changed: %s", text)
        folder, valid = self._genOutputFolder()
        self.ui.cb_folder.setToolTip(
            self.tr("Current output folder: %s") % clean_filename(folder, extra_chars=pathsep)
        )

        if not valid:
            self.ui.cb_folder.lineEdit().setStyleSheet("background-color: orange")
        else:
            self.ui.cb_folder.lineEdit().setStyleSheet("")

    @Slot(str)
    def slot_cbPrefixChanged(self, text):
        log.debug("Filename prefix template changed: %s", text)
        prefix, valid = self._genFilenamePrefix()
        self.ui.cb_prefix.setToolTip(
            self.tr("Current filename prefix: %s") % clean_filename(prefix)
        )

        if not valid:
            self.ui.cb_prefix.lineEdit().setStyleSheet("background-color: orange")
        else:
            self.ui.cb_prefix.lineEdit().setStyleSheet("")

    @Slot()
    def slot_togglePrefixHelp(self):
        if self.outputHelpWin.isVisible():
            self.outputHelpWin.hide()
        else:
            self.outputHelpWin.show()

    @Slot()
    def slot_toggleSourcesHelp(self):
        if self.sourcesHelpWin.isVisible():
            self.sourcesHelpWin.hide()
        else:
            self.sourcesHelpWin.show()

    def checkRecordEnable(self):
        enable = True

        if self.ui.rb_source_selected.isChecked() and not self.recSources:
            enable = False

        if self.ui.group_time.isChecked() and self.ui.te_end.time() <= self.ui.te_start.time():
            enable = False

        self.ui.b_render.setEnabled(enable)

    def saveSettings(self, path=None):
        if path is None:
            log.debug("Saving user settings.")
            settings = QSettings()
        else:
            log.debug("Saving project settings to '%s'.", path)
            # For this form of the QSettings constructor, we need to set the
            # format to QSettings.IniFormat explicitly, even though above we
            # already did QSettings.setDefaultFormat(QSettings.IniFormat).
            settings = QSettings(path, QSettings.IniFormat)

        if self.ui.rb_mono.isChecked():
            channels = 1
        elif self.ui.rb_stereo.isChecked():
            channels = 2
        else:
            channels = self.ui.sb_channels.value()

        settings.setValue("Geometry", self.saveGeometry())
        settings.setValue("WindowVisible", self.visible)
        settings.setValue("OutputFolder", self.ui.cb_folder.currentText())
        settings.setValue("FilenamePrefix", self.ui.cb_prefix.currentText())
        settings.setValue("EncodingFormat", self.ui.cb_format.currentText())
        settings.setValue("EncodingDepth", self.ui.cb_samplefmt.currentData())
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

        idx = 0
        for ports in (self.output_ports, self.input_ports):
            for client in humansorted(ports):
                for port in ports[client]:
                    if (port.client, port.name) in self.recSources:
                        settings.setArrayIndex(idx)
                        settings.setValue("Client", port.client)
                        settings.setValue("Port", port.name)
                        idx += 1

        settings.endArray()

        settings.beginWriteArray("OutputFolderHistory")

        for idx in range(self.ui.cb_folder.count()):
            settings.setArrayIndex(idx)
            settings.setValue("Entry", self.ui.cb_folder.itemText(idx))

        settings.endArray()

        settings.beginWriteArray("FilenamePrefixHistory")

        for idx in range(self.ui.cb_prefix.count()):
            settings.setArrayIndex(idx)
            settings.setValue("Entry", self.ui.cb_prefix.itemText(idx))

        settings.endArray()

    def loadSettings(self, path=None):
        if path is None:
            log.debug("Loading user settings.")
            settings = QSettings()
        else:
            log.debug("Loading project settings from '%s'.", path)
            # For this form of the QSettings constructor, we need to set the
            # format to QSettings.IniFormat explicitly, even though above we
            # already did QSettings.setDefaultFormat(QSettings.IniFormat).
            settings = QSettings(path, QSettings.IniFormat)

        self.restoreGeometry(settings.value("Geometry", b""))
        self.visible = settings.value("WindowVisible", True, type=bool)

        if self.app.nsmClient is None:
            outputFolder = settings.value("OutputFolder", DEFAULT_OUTPUT_FOLDER)
        else:
            outputFolder = settings.value("OutputFolder", DEFAULT_OUTPUT_FOLDER_NSM)

        self.ui.cb_folder.setCurrentText(outputFolder)

        if self.app.nsmClient is None:
            fallback = DEFAULT_FILENAM_PREFIX
        else:
            fallback = DEFAULT_FILENAM_PREFIX_NSM

        self.ui.cb_prefix.setCurrentText(settings.value("FilenamePrefix", fallback, type=str))

        encFormat = settings.value("EncodingFormat", "wav", type=str)

        for i in range(self.ui.cb_format.count()):
            if self.ui.cb_format.itemText(i) == encFormat:
                self.ui.cb_format.setCurrentIndex(i)
                break

        encDepth = settings.value("EncodingDepth", "FLOAT", type=str)

        for i in range(self.ui.cb_samplefmt.count()):
            if self.ui.cb_samplefmt.itemData(i) == encDepth:
                self.ui.cb_samplefmt.setCurrentIndex(i)
                break

        encChannels = settings.value("EncodingChannels", 2, type=int)

        if encChannels == 1:
            self.ui.rb_mono.setChecked(True)
        elif encChannels == 2:
            self.ui.rb_stereo.setChecked(True)
        else:
            self.ui.rb_multi.setChecked(True)
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

        for idx in range(size):
            settings.setArrayIndex(idx)
            client = settings.value("Client", type=str)
            port = settings.value("Port", type=str)
            if client and port:
                self.recSources.add((client, port))

        settings.endArray()

        folder_history = []
        size = settings.beginReadArray("OutputFolderHistory")

        for idx in range(size):
            settings.setArrayIndex(idx)
            folder_history.append(settings.value("Entry", type=str))

        settings.endArray()

        self.ui.cb_folder.addItems(folder_history)

        prefix_history = []
        size = settings.beginReadArray("FilenamePrefixHistory")

        for idx in range(size):
            settings.setArrayIndex(idx)
            prefix_history.append(settings.value("Entry", type=str))

        settings.endArray()

        self.ui.cb_prefix.addItems(prefix_history)

        self.populatePortLists()

    @Slot()
    def slot_jackDisconnect(self):
        self.shutdown()

    def shutdown(self):
        log.debug("Deactivating JACK client.")
        self.jackClient.deactivate()

        log.debug("Disabling port-change signal handler.")
        self.jackClient.ports_changed.disconnect(self.slot_refreshPortsLists)

        if self.refreshTimer is not None and self.refreshTimer.isActive():
            self.refreshTimer.stop()

        self.slot_renderStop()

        if self.app.nsmClient is None:
            self.saveSettings()

        self.app.shutdown()

    def closeEvent(self, event=None):
        if self.app.nsmClient is None:
            self.shutdown()
        else:
            self.app.nsmHideUICallback()

        event.ignore()

    def done(self, result):
        QDialog.done(self, result)
        self.close()


# -------------------------------------------------------------------------------------------------
# Main Application


class QJackCaptureApp(QApplication):
    def __init__(self, args=None, **options):
        """The main application class."""
        super().__init__(args)
        self.options = options
        self.jackClient = None
        self.setApplicationName(PROGRAM)
        self.setApplicationVersion(__version__)
        self.setOrganizationName(ORGANIZATION)
        QSettings.setDefaultFormat(QSettings.IniFormat)
        self.setWindowIcon(QIcon(":/icons//scalable/qjackcapture.svg"))

    def initialize(self):
        self.mainTimer = QTimer()

        if os.getenv("NSM_URL"):
            self.nsmClient = NSMClient(
                prettyName=PROGRAM,
                saveCallback=self.nsmSaveCallback,
                openOrNewCallback=self.nsmOpenCallback,
                supportsSaveStatus=False,
                exitProgramCallback=self.nsmExitCallback,
                hideGUICallback=self.nsmHideUICallback,
                showGUICallback=self.nsmShowUICallback,
                broadcastCallback=None,
                sessionIsLoadedCallback=None,
                logLevel=self.options.get("log_level", logging.INFO),
            )
            self.nsmClient.announceOurselves()
            self.mainTimer.timeout.connect(self.nsmClient.reactToMessage)
            self.nsmClient.announceGuiVisibility(self.mainwin.visible)
        else:
            self.nsmClient = None
            self.createJackClient(self.jackClientName + "/ui")
            self.createMainWin()
            self.mainwin.loadSettings()
            log.debug("Attaching POSIX signal handlers...")
            signal(SIGINT, self.posixSignalHandler)
            signal(SIGTERM, self.posixSignalHandler)
            # Give Python interpreter a chance to handle signals every now and then
            self.mainTimer.timeout.connect(lambda *args: None)

        self.mainTimer.start(200)

        log.debug("Window visible: %s", "yes" if self.mainwin.visible else "no")
        if self.mainwin.visible:
            self.mainwin.show()

    def createMainWin(self):
        log.debug("Creating application main window...")
        self.mainwin = QJackCaptureMainWindow(self)

    def createJackClient(self, client_name):
        log.debug("Creating JACK client...")
        self.jackClient = QJackCaptureClient(
            client_name,
            connect_interval=self.options.get("connect_interval", 3.0),
            connect_max_attempts=self.options.get("connect_max_attempts", 0),
        )

    def shutdown(self):
        log.debug("Shutting down.")
        self.jackClient.close()
        self.quit()

    @property
    def jackClientName(self):
        return self.options.get("client_name", PROGRAM)

    # ---------------------------------------------------------------------------------------------
    # POSIX Signal Callbacks

    def posixSignalHandler(self, sig, frame):
        log.debug("Received signal %s. Closing application.", Signals(sig).name)
        if self.mainTimer.isActive():
            self.mainTimer.stop()

        self.mainwin.shutdown()

    # ---------------------------------------------------------------------------------------------
    # NSM Callbacks

    def nsmOpenCallback(self, open_path, session_name, client_id):
        """Session manager tells us to open a project file."""
        log.debug("nsmOpenCallback: %r, %r, %r", open_path, session_name, client_id)
        self.createJackClient(client_id + "/ui")
        self.createMainWin()
        os.makedirs(open_path, exist_ok=True)
        config_path = join(open_path, PROGRAM + ".ini")
        self.mainwin.loadSettings(path=config_path)
        client_id = client_id.rsplit(".", 1)[-1]
        window_title = "{}: {} ({})".format(self.mainwin.windowTitle(), session_name, client_id)
        self.mainwin.setWindowTitle(window_title)

    def nsmSaveCallback(self, save_path, session_name, client_id):
        """Session manager tells us to save a project file."""
        log.debug("nsmSaveCallback: %r, %r, %r", save_path, session_name, client_id)
        config_path = join(save_path, PROGRAM + ".ini")
        self.mainwin.saveSettings(path=config_path)

    def nsmExitCallback(self, save_path, session_name, client_id):
        """Session manager tells us to unconditionally quit."""
        log.debug("nsmExitCallback: %r, %r, %r", save_path, session_name, client_id)
        if self.mainTimer.isActive():
            self.mainTimer.stop()

        self.mainwin.shutdown()

    def nsmHideUICallback(self):
        """Session manager tells us to close our UI."""
        self.mainwin.hide()
        self.mainwin.visible = False
        self.nsmClient.announceGuiVisibility(False)

    def nsmShowUICallback(self):
        """Session manager tells us to open our UI."""
        self.mainwin.show()
        self.mainwin.visible = True
        self.nsmClient.announceGuiVisibility(True)


# -------------------------------------------------------------------------------------------------
# Allow to use this as a standalone app


def main(args=None):
    ap = argparse.ArgumentParser(
        usage=__doc__.splitlines()[0],
        epilog="You can also pass any command line arguments supported by Qt.",
    )
    ap.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    ap.add_argument(
        "-n",
        "--client-name",
        metavar="NAME",
        default=PROGRAM,
        help="Set JACK client name to NAME (default: '%(default)s')",
    )
    ap.add_argument(
        "-i",
        "--connect-interval",
        type=posnum,
        default=3.0,
        metavar="SECONDS",
        help="Interval between attempts to connect to JACK server (default: %(default)s)",
    )
    ap.add_argument(
        "-m",
        "--max-attempts",
        type=posnum,
        default=1,
        metavar="NUM",
        help="Max. number of attempts to connect to JACK server "
        "(0=infinite, default: %(default)s)).",
    )
    cargs, args = ap.parse_known_args(args)

    log_level = logging.DEBUG if cargs.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(name)s:%(levelname)s: %(message)s")

    # App initialization
    app = QJackCaptureApp(
        args,
        log_level=log_level,
        client_name=cargs.client_name,
        connect_interval=cargs.connect_interval,
        connect_max_attempts=cargs.max_attempts,
    )

    # Translation process
    app_translator = QTranslator()
    if app_translator.load(
        QLocale(),
        PROGRAM.lower(),
        "_",
        ":/locale",
    ):
        app.installTranslator(app_translator)

        # Install Qt base translator for file picker
        # only if app_translator has found a language
        # to prevent languages inconsistence
        # (for example, only "close" buttons translated).
        sys_translator = QTranslator()
        path_sys_translations = QLibraryInfo.location(QLibraryInfo.TranslationsPath)

        if sys_translator.load(QLocale(), "qt", "_", path_sys_translations):
            app.installTranslator(sys_translator)

    if jacklib is None:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "JACK is not available in this system, cannot use this application.",
            ),
            QMessageBox.Close,
        )
        return 1

    # ---------------------------------------------------------------------------------------------
    # Find 'jack_capture' in PATH

    jackCapturePath = None
    for pathdir in os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep):
        if exists(join(pathdir, "jack_capture")):
            app.jackCapturePath = join(pathdir, "jack_capture")
            break
    else:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "The 'jack_capture' application is not available.\n"
                "Is not possible to render without it!",
            ),
            QMessageBox.Close,
        )
        return 2

    try:
        app.initialize()
    except JackCaptureUnsupportedError as exc:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM, "The 'jack_capture' application found is not compatible.\n\nReason: %s"
            )
            % exc,
            QMessageBox.Close,
        )
        return 3
    except JackConnectError as exc:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "Could not connect to JACK, possible reasons:\n\n%s\n\n"
                "See console log for more information.",
            )
            % exc,
            QMessageBox.Close,
        )
        return 4
    except Exception as exc:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "Failed initialize the application:\n\n%s\n\n"
                "See console log for more information.",
            )
            % exc,
            QMessageBox.Close,
        )
        return 5

    # Main loop
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main() or 0)
