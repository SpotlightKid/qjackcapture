# Changelog


## Version 0.2.0 (2022-11-02)

Bugfixes and build system changes


### Fixes

* Maintain port order on `jack_capture` command line. For multi-channel output
  files, this should result in channels being in a sensible order.


### Changes

* Moved project and packaging meta data to `setup.cfg`, making the project more
  compliant with [PEP 517].


## Version 0.1.2 (2022-03-24)

Bugfix release


### Fixes

* Added check whether `-jn` option is supported by `jack_capture`.


## Version 0.1.1 (2021-11-30)

Bugfix release


### Fixes

* Fixed exception due to reference to wrong widget name when applying settings
  from `QSettings` file.


## Version 0.1.0 (2021-04-21)

Initial release as a stand-alone project


### New features

* Added recording source selector.
* Added dynamic input (sink) and output (source) port list view
  to manually select ports as recording sources.
* Added text entry to add extra command line argument for `jack_capture`.
* Re-organized UI layout a bit, changed some labels and added mnemonics.
* Output directory is set to "Music" XDG user dir by default.
* Added desktop menu file.
* Added command line options to control JACK server connection parameters.
* Added command line option to set JACK client name.
* Added command line option to enable debug logging.


### Other changes

* Re-organized code into a Python package.
* Re-worked / re-formatted source code using black, isort and flake8.
* Added Python packaging files and QA tool configuration.
* Added makefile for generating UI and resource files and (un-)installation and
  packaging.
* Added starter script via setuptools command line entry point script.
* Moved jack interfacing code into separate class.
* Moved `jacklib.py` and `jacklib_helpers.py` into new PyPI package
  [pyjacklib].
* Changed standard command line options passed to `jack_capture` (e.g.
  now uses `--daemon` option).
* Added readme and this changelog.
* And lots of other minor changes, see the Git commit log.


[pyjacklib]: https://github.com/jackaudio/pyjacklib
[PEP 517]: https://peps.python.org/pep-0517/
