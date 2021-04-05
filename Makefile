#!/usr/bin/make -f
# Makefile for QJackCapture #
# ------------------------- #

PREFIX ?= /usr/local
DESTDIR ?= /

PYTHON ?= python3
PYUIC ?= pyuic5
PYRCC ?= pyrcc5
PYPKG = QJackCapture
PROGRAM = qjackcapture

# -------------------------------------------------------------------------------------------------

all: RES UI

# -------------------------------------------------------------------------------------------------
# Resources

RES: $(PYPKG)/resources_rc.py

$(PYPKG)/resources_rc.py: resources/resources.qrc
	$(PYRCC) $< -o $@

# -------------------------------------------------------------------------------------------------
# UI code

UI: qjackcapture

qjackcapture: $(PYPKG)/ui_mainwindow.py \
	$(PYPKG)/app.py \
	$(PYPKG)/__main__.py \
	$(PYPKG)/jacklib.py \
	$(PYPKG)/jacklib_helpers.py

$(PYPKG)/ui_%.py: resources/ui/%.ui
	$(PYUIC) --from-imports -o $@ $<

# -------------------------------------------------------------------------------------------------

clean:
	-rm -f *~ $(PYPKG)/*~ $(PYPKG)/*.pyc $(PYPKG)/ui_*.py $(PYPKG)/*_rc.py

# -------------------------------------------------------------------------------------------------

install:
	$(PYTHON) setup.py install --prefix=$(PREFIX) --root=$(DESTDIR)

install-pip:
	$(PYTHON) -m pip install --prefix=$(PREFIX) --root=$(DESTDIR) .

# -------------------------------------------------------------------------------------------------

uninstall:
	$(PYTHON) -m pip uninstall $(PYPKG)

# -------------------------------------------------------------------------------------------------

dist:
	$(PYTHON) setup.py sdist --format=xztar bdist_wheel


.PHONY: dist
