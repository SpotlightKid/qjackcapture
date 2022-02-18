#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# JACK-Capture frontend, with freewheel and transport support
# Copyright (C) 2010-2018 Filipe Coelho <falktx@falktx.com>
# Copyright (C) 2020-2021 Christopher Arndt <info@chrisarndt.de>
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
import logging
import os
import queue
import shlex
import sys
from collections import namedtuple
from functools import lru_cache, partial
from operator import attrgetter
from os.path import exists, expanduser, isdir, join
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
        QModelIndex,
        QObject,
        QProcess,
        QSettings,
        Qt,
        QTime,
        QTimer,
        Signal,
        Slot,
    )
    from qtpy.QtGui import QIcon, QStandardItem, QStandardItemModel
    from qtpy.QtWidgets import QApplication, QDialog, QFileDialog, QMenu, QMessageBox
except ImportError:
    from PyQt5.QtCore import QModelIndex, QObject, QProcess, QSettings, Qt, QTime, QTimer
    from PyQt5.QtCore import pyqtSignal as Signal
    from PyQt5.QtCore import pyqtSlot as Slot
    from PyQt5.QtGui import QIcon, QStandardItem, QStandardItemModel
    from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QMenu, QMessageBox

# -------------------------------------------------------------------------------------------------
# Application-specific imports

from .ui_mainwindow import Ui_MainWindow
from .userdirs import get_user_dir
from .version import __version__

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
# Utility functions


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

JackPort = namedtuple(
    "JackPort", ["client", "group", "order", "name", "pretty_name", "uuid", "aliases"]
)


class QJackCaptureClient(QObject):
    PROPERTY_CHANGE_MAP = {
        jacklib.PropertyCreated: "created",
        jacklib.PropertyChanged: "changed",
        jacklib.PropertyDeleted: "deleted",
    }
    ports_changed = Signal()

    def __init__(self, name, connect_interval=3.0, connect_max_attempts=0):
        super().__init__()
        self.client_name = name
        self.connect_max_attempts = connect_max_attempts
        self.connect_interval = connect_interval
        self.default_encoding = jacklib.ENCODING
        self.queue = queue.Queue()

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
                raise RuntimeError(err)

            log.debug("Waiting %.2f seconds to connect again...", self.connect_interval)
            sleep(self.connect_interval)

        name = jacklib.get_client_name(self.client)
        if name is not None:
            self.client_name = name.decode()
        else:
            raise RuntimeError("Could not get JACK client name.")

        jacklib.on_shutdown(self.client, self.shutdown_callback, None)
        log.debug(
            "Client connected, name: %s UUID: %s",
            self.client_name,
            jacklib.client_get_uuid(self.client),
        )
        jacklib.set_port_registration_callback(self.client, self.port_reg_callback, None)
        jacklib.set_port_rename_callback(self.client, self.port_rename_callback, None)
        jacklib.set_property_change_callback(self.client, self.property_callback, None)
        jacklib.activate(self.client)

    def close(self):
        if self.client:
            jacklib.deactivate(self.client)
            return jacklib.client_close(self.client)

    def _refresh(self):
        log.debug("Port list refresh needed.")
        self.ports_changed.emit()

    # ---------------------------------------------------------------------------------------------
    # Callbacks

    def error_callback(self, error):
        error = error.decode(self.default_encoding, errors="ignore")
        log.debug(error)

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
        """
        If JACK server signals shutdown, send ``None`` to the queue to cause client to reconnect.
        """
        log.debug("JACK server signalled shutdown.")
        self.client = None
        self.queue.put(None)

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
# Main Window


class QJackCaptureMainWindow(QDialog):
    sample_formats = {
        "32-bit float": "FLOAT",
        "8-bit integer": "8",
        "16-bit integer": "16",
        "24-bit integer": "24",
        "32-bit integer": "32",
    }

    def __init__(self, parent, jack_client, jack_name=PROGRAM):
        QDialog.__init__(self, parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.fFreewheel = False
        self.fLastTime = -1
        self.fMaxTime = 180

        self.fTimer = QTimer(self)
        self.fProcess = QProcess(self)
        self.fJackClient = jack_client
        self.fJackName = jack_name

        self.fBufferSize = self.fJackClient.get_buffer_size()
        self.fSampleRate = self.fJackClient.get_sample_rate()

        # Selected ports used as recording sources
        self.rec_sources = set()

        self.createUi()
        self.loadSettings()
        self.populatePortLists(init=True)
        self.checkSupportedOptions()

        # listen to changes to JACK ports
        self._refresh_timer = None
        self.fJackClient.ports_changed.connect(self.slot_refreshPortsLists)

    @Slot()
    def slot_refreshPortsLists(self, delay=200):
        if not self._refresh_timer or not self._refresh_timer.isActive():
            log.debug("Scheduling port lists refresh in %i ms...", delay)
            self._refresh_timer = QTimer()
            self._refresh_timer.setSingleShot(True)
            self._refresh_timer.timeout.connect(self.populatePortLists)
            self._refresh_timer.start(delay)

    def checkSupportedOptions(self):
        # Get help text to check for existence of options missing in older jack_capture versions
        self.fProcess.start(gJackCapturePath, ["--help2"])
        self.fProcess.waitForFinished()
        help_text = str(self.fProcess.readAllStandardOutput(), "utf-8")
        self.supportedOptions = {}
        self.supportedOptions["jack-name"] = "[-jn]" in help_text
        log.debug("Options supported by jack_capture: %s", self.supportedOptions)

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

    def populatePortLists(self, init=False):
        log.debug("Populating port lists (init=%s)...", init)
        if init:
            self.outputs_model = QStandardItemModel(0, 1, self)
            self.inputs_model = QStandardItemModel(0, 1, self)
        else:
            self.outputs_model.clear()
            self.inputs_model.clear()

        output_ports = list(self.fJackClient.get_output_ports())
        self.populatePortList(self.outputs_model, self.ui.tree_outputs, output_ports)
        input_ports = list(self.fJackClient.get_input_ports())
        self.populatePortList(self.inputs_model, self.ui.tree_inputs, input_ports)

        # Remove ports, which are no longer present, from recording sources
        all_ports = set((p.client, p.name) for p in output_ports)
        all_ports |= set((p.client, p.name) for p in input_ports)
        self.rec_sources.intersection_update(all_ports)
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

            for port in humansorted(portsdict[client], key=attrgetter("group", "order", "name")):
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

                if portspec in self.rec_sources:
                    portitem.setCheckState(2)

                clientitem.appendRow(portitem)

            root.appendRow(clientitem)

        tv.expandAll()

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
        self.ui.le_folder.setText(expanduser("~"))

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
            self.rec_sources.add(port)
        else:
            self.rec_sources.discard(port)

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

        # JACK client name
        if self.supportedOptions.get("jack-name"):
            arguments.append("-jn")
            arguments.append(self.fJackName)

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
            arguments.extend(arg_list)

        # Change current directory
        os.chdir(self.ui.le_folder.text())

        if newBufferSize != self.fJackClient.get_buffer_size():
            log.info("Buffer size changed before render.")
            self.fJackClient.set_buffer_size(newBufferSize)

        if useTransport:
            if self.fJackClient.transport_running():
                # rolling or starting
                self.fJackClient.transport_stop()

            self.fJackClient.transport_locate(minTime * self.fSampleRate)

        log.debug("jack_capture command line args: %r", arguments)
        self.fProcess.start(gJackCapturePath, arguments)
        status = self.fProcess.waitForStarted()

        if not status:
            self.fProcess.close()
            log.error("Could not start jack_capture.")
            return

        if self.fFreewheel:
            log.info("Rendering in freewheel mode.")
            sleep(1)
            self.fJackClient.set_freewheel(True)

        if useTransport:
            log.info("Rendering using JACK transport.")
            self.fTimer.start()
            self.fJackClient.transport_start()

    @Slot()
    def slot_renderStop(self):
        useTransport = self.ui.group_time.isChecked()

        if useTransport:
            self.fJackClient.transport_stop()

        if self.fFreewheel:
            self.fJackClient.set_freewheel(False)
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
        newBufferSize = self.fJackClient.get_buffer_size()

        if newBufferSize != self.fBufferSize:
            self.fJackClient.set_buffer_size(newBufferSize)

    @Slot()
    def slot_getAndSetPath(self):
        new_path = QFileDialog.getExistingDirectory(
            self, self.tr("Set Path"), self.ui.le_folder.text(), QFileDialog.ShowDirsOnly
        )

        if new_path:
            self.ui.le_folder.setText(new_path)

    @Slot()
    def slot_setStartNow(self):
        time = self.fJackClient.transport_frame() // self.fSampleRate
        secs = time % 60
        mins = int(time / 60) % 60
        hrs = int(time / 3600) % 60
        self.ui.te_start.setTime(QTime(hrs, mins, secs))

    @Slot()
    def slot_setEndNow(self):
        time = self.fJackClient.transport_frame() // self.fSampleRate
        secs = time % 60
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
        time = self.fJackClient.transport_frame() / self.fSampleRate
        self.ui.progressBar.setValue(time)

        if time > self.fMaxTime or (self.fLastTime > time and not self.fFreewheel):
            self.slot_renderStop()

        self.fLastTime = time

    def checkRecordEnable(self):
        enable = True

        if self.ui.rb_source_selected.isChecked() and not self.rec_sources:
            enable = False

        if self.ui.group_time.isChecked() and self.ui.te_end.time() <= self.ui.te_start.time():
            enable = False

        self.ui.b_render.setEnabled(enable)
        log.debug(
            "Recording sources: %s", ", ".join(("%s:%s" % (c, p) for c, p in self.rec_sources))
        )

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
            settings.setArrayIndex(i)
            settings.setValue("Client", client)
            settings.setValue("Port", port)
        settings.endArray()

    def loadSettings(self):
        settings = QSettings(ORGANIZATION, PROGRAM)

        self.restoreGeometry(settings.value("Geometry", b""))

        outputFolder = settings.value("OutputFolder", get_user_dir("MUSIC"))

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
        for i in range(size):
            settings.setArrayIndex(i)
            client = settings.value("Client", type=str)
            port = settings.value("Port", type=str)
            if client and port:
                self.rec_sources.add((client, port))
        settings.endArray()

    def closeEvent(self, event):
        self.saveSettings()
        self.fJackClient.close()
        QDialog.closeEvent(self, event)

    def done(self, r):
        QDialog.done(self, r)
        self.close()


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
        help="Interval between attempts to connect to JACK server " " (default: %(default)s)",
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

    # App initialization
    app = QApplication(args)
    app.setApplicationName(PROGRAM)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(ORGANIZATION)
    app.setWindowIcon(QIcon(":/icons//scalable/qjackcapture.svg"))

    logging.basicConfig(
        level=logging.DEBUG if cargs.debug else logging.INFO,
        format="%(name)s:%(levelname)s: %(message)s",
    )

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

    try:
        jack_client = QJackCaptureClient(
            cargs.client_name + "-ui",
            connect_interval=cargs.connect_interval,
            connect_max_attempts=cargs.max_attempts,
        )
    except KeyboardInterrupt:
        log.info("Aborted.")
        return 1
    except Exception as exc:
        QMessageBox.critical(
            None,
            app.translate(PROGRAM, "Error"),
            app.translate(
                PROGRAM,
                "Could not connect to JACK, possible reasons:\n%s\n\n"
                "See console log for more information.",
            )
            % exc,
        )
        return 1

    # Show GUI
    gui = QJackCaptureMainWindow(None, jack_client, cargs.client_name)
    gui.setWindowIcon(get_icon("media-record", 48))
    gui.show()

    # Main loop
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main() or 0)
