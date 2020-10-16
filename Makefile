#!/usr/bin/make -f
# Makefile for QJackCapture #
# ------------------------- #

PREFIX  ?= /usr/local
DESTDIR =

LINK   = ln -s
PYUIC ?= pyuic5
PYRCC ?= pyrcc5
PYPKG = qjackcapture
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
	pip install .

# -------------------------------------------------------------------------------------------------

uninstall:
	pip unistall $(PYPKG)

# -------------------------------------------------------------------------------------------------

dist:
	python setup.py sdist --format=xztar bdist_wheel


.PHONY: dist
