# QJackCapture

A GUI for [jack_capture] using PyQt5.

![QJackCapture screenshot](resources/screenshots/qjackcapture.png)


## Dependencies

To run **QJackCapture**, you need to have the following installed:

* [JACK] server and library
* [jack_capture]
* Python 3 and [PyQt5]
* [natsort]

To build and install it, you additioanlly need


* `make`
* `pyuic5`
* `pyrcc5`
* [setuptools]
* (optional) [pip]


## Building and Installing

```con
make
[sudo] make install
```

Or, if you want to install via `pip` and let it install any missing required
Python packages:

```con
make
[sudo] make install-pip
```

The install commands respect the usual `PREFIX` and `DESTDIR` variables.


## Running

For now, after installation, just run `qjackcapture` from a terminal or your
preferred launcher. An XDG `.desktop` menu file and icon will be added to the
installation later.


## License

**QJackCapture** is licensed under the GNU Public License Version v2, or
any later version.

Please see the file [LICENSE](./LICENSE) for more information.


## Authors

Created by *Filipe Coelho (falkTX)* as part of [Cadence].

Turned into stand-alone project and enhanced by *Christopher Arndt*.


[Cadence]: https://github.com/falkTX/Cadence.git
[jack_capture]: https://github.com/kmatheussen/jack_capture
[JACK]: https://jackaudio.org/
[natsort]: https://github.com/SethMMorton/natsort
[pip]: https://pypi.org/project/pip/
[PyQt5]: https://www.riverbankcomputing.com/software/pyqt/
[setuptools]: https://pypi.org/project/setuptools/
