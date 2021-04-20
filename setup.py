#!/usr/bin/env python3
#
# setup.py
#
"""A GUI for easy recording of JACK audio sources using jack_capture."""

from os.path import join
from setuptools import setup


def read(*paths):
    with open(join(*paths), encoding='utf-8') as fp:
        return fp.read()


exec(read('qjackcapture', 'version.py'))


setup(
    name='QJackCapture',
    version=__version__,  # noqa
    description=__doc__.splitlines()[0],
    long_description=read('README.md'),
    long_description_content_type="text/markdown",
    author="Filipe Coelho (falkTX)",
    maintainer="Christopher Arndt",
    maintainer_email="info@chrisarndt.de",
    url="https://github.com/SpotlightKid/qjackcapture",
    packages=["qjackcapture"],
    install_requires=[
        'natsort',
        'pyjacklib',
        'PyQt5',
    ],
    entry_points={
        'console_scripts': [
            "qjackcapture = qjackcapture.app:main",
        ]
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: X11 Applications :: Qt',
        'Intended Audience :: End Users/Desktop',
        'License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3 :: Only',
        'Topic :: Multimedia :: Sound/Audio',
        'Topic :: Utilities',
    ]
)
