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

all: RES LOCALE UI

# -------------------------------------------------------------------------------------------------
# Resources

ICONS = $(wildcard resources/icons/*/*.png resources/icons/scalable/*.svg)

RES: $(PYPKG)/resources_rc.py $(ICONS)

$(PYPKG)/resources_rc.py: resources/resources.qrc
	$(PYRCC) $< -o $@

# -------------------------------------------------------------------------------------------------
# Localisations

LOCALE: locale

locale: locale/qjackcapture_en.qm \
		locale/qjackcapture_fr.qm \

locale/%.qm: locale/%.ts
	$(LRELEASE) $< -qm $@

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

install-data:
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

	# install Localisations
	$(INSTALL) -dm755 $(DESTDIR:/=)$(PREFIX)/share/$(PROGRAM)
	$(INSTALL) -dm755 $(DESTDIR:/=)$(PREFIX)/share/$(PROGRAM)/locale
	$(INSTALL) -m 644 locale/$(PROGRAM)_en.qm \
		$(DESTDIR:/=)$(PREFIX)/share/$(PROGRAM)/locale
	$(INSTALL) -m 644 locale/$(PROGRAM)_fr.qm \
		$(DESTDIR:/=)$(PREFIX)/share/$(PROGRAM)/locale

install: install-data
	$(PYTHON) setup.py install --prefix=$(PREFIX) --root=$(DESTDIR)

install-pip: install-data
	$(PYTHON) -m pip install --prefix=$(PREFIX) --root=$(DESTDIR) .

# -------------------------------------------------------------------------------------------------

uninstall:
	$(PYTHON) -m pip uninstall $(PYPKG)

# -------------------------------------------------------------------------------------------------

dist: sdist wheel

sdist: RES UI
	$(PYTHON) -m build --sdist

wheel: RES UI
	$(PYTHON) -m build --wheel

release: sdist wheel

pypi-upload: release
	$(TWINE) upload --skip-existing dist/*.tar.gz dist/*.whl


.PHONY: all dist install install-data install-pip pypi-upload release sdist uninstall wheel
