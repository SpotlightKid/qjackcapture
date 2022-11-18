#!/usr/bin/make -f
# Makefile for QJackCapture #
# ------------------------- #

PREFIX ?= /usr/local
DESTDIR ?= /

INSTALL ?= install
PYTHON ?= python3
PYUIC ?= pyuic5
PYRCC ?= pyrcc5
PYPKG = qjackcapture
PROGRAM = qjackcapture
TWINE ?= twine
APP_ICON = resources/icons/scalable/$(PROGRAM).svg
DESKTOP_FILE = resources/xdg/$(PROGRAM).desktop

LRELEASE := lrelease
ifeq (, $(shell which $(LRELEASE)))
 LRELEASE := lrelease-qt5
endif

ifeq (, $(shell which $(LRELEASE)))
 LRELEASE := lrelease-qt4
endif

# -------------------------------------------------------------------------------------------------

all: RES UI

# -------------------------------------------------------------------------------------------------
# Localisations

TRANSLATIONS = \
	resources/locale/qjackcapture_en.qm \
	resources/locale/qjackcapture_fr.qm

resources/locale/%.qm: resources/locale/%.ts
	$(LRELEASE) $< -qm $@

# -------------------------------------------------------------------------------------------------
# Resources

ICONS = $(wildcard resources/icons/*/*.png resources/icons/scalable/*.svg)

RES: $(PYPKG)/resources_rc.py

$(PYPKG)/resources_rc.py: resources/resources.qrc $(ICONS) $(TRANSLATIONS)
	$(PYRCC) $< -o $@

# -------------------------------------------------------------------------------------------------
# UI code

UI: qjackcapture

qjackcapture: \
	$(PYPKG)/ui_mainwindow.py \
	$(PYPKG)/ui_prefixhelpwin.py \
	$(PYPKG)/ui_sourceshelpwin.py \
	$(PYPKG)/app.py \
	$(PYPKG)/__main__.py \
	$(PYPKG)/version.py

$(PYPKG)/ui_%.py: resources/ui/%.ui
	$(PYUIC) --from-imports -o $@ $<

# -------------------------------------------------------------------------------------------------

clean:
	-rm -f *~ $(PYPKG)/*~ $(PYPKG)/*.pyc $(PYPKG)/ui_*.py $(PYPKG)/*_rc.py

# -------------------------------------------------------------------------------------------------

install-data: all
	$(INSTALL) -dm755  $(DESTDIR:/=)$(PREFIX)/share/icons/hicolor/scalable/apps
	$(INSTALL) -m644 $(APP_ICON) $(DESTDIR:/=)$(PREFIX)/share/icons/hicolor/scalable/apps
	$(INSTALL) -dm755 $(DESTDIR:/=)$(PREFIX)/share/applications
	$(INSTALL) -m644 $(DESKTOP_FILE) $(DESTDIR:/=)$(PREFIX)/share/applications
	@if [ -z "$(DESTDIR)" ]; then \
		echo "Updating desktop menu..."; \
		update-desktop-database -q; \
	fi
	@if [ -z "$(DESTDIR)" ]; then \
		echo "Updating icon cache..."; \
		gtk-update-icon-cache -q $(DESTDIR:/=)$(PREFIX)/share/icons/hicolor; \
	fi

install: install-data
	$(PYTHON) setup.py install --prefix=$(PREFIX) --root=$(DESTDIR)

install-pip: install-data
	$(PYTHON) -m pip install --prefix=$(PREFIX) --root=$(DESTDIR) .

# -------------------------------------------------------------------------------------------------

uninstall:
	$(PYTHON) -m pip uninstall $(PYPKG)

# -------------------------------------------------------------------------------------------------

dist: sdist wheel

sdist: all
	$(PYTHON) -m build --sdist

wheel: all
	$(PYTHON) -m build --wheel

release: dist

pypi-upload: release
	$(TWINE) upload --skip-existing dist/*.tar.gz dist/*.whl


.PHONY: all dist install install-data install-pip pypi-upload release sdist uninstall wheel
