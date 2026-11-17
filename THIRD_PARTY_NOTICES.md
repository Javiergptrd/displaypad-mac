# Third-party notices

## Code this project builds on

### oss-mountain-displaypad (MIT)

<https://github.com/AnnikenYT/oss-mountain-displaypad>, by AnnikenToGo (AnnikenYT).

`displaypad_mac/driver.py` reimplements, in Python, the DisplayPad USB protocol from
this project: the command constants, the 64-byte command packets and the image upload
sequence (header, BGR pixel data on the image interface, acknowledgement). A few short
lines follow its `displaypad-driver` package closely. The project declares the MIT
License in its README and in the `pyproject.toml` of its `displaypad-driver` and
`displaypad-lib` packages:

```
MIT License

Copyright (c) AnnikenToGo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Documentation consulted (no code copied)

### BaseCamp-Linux

<https://github.com/ramisotti13-eng/BaseCamp-Linux>, by Ramisotti, under GPL v3 with a
non-commercial restriction. It was read only to understand how the DisplayPad is
switched on (which interface gets the first command). No code from it is included
here. The macOS method (an IOKit `SET_REPORT` on interface 0 without root) was found
and tested on the hardware for this project.

## Libraries installed by `install.sh`

These are downloaded from PyPI and Homebrew when you install; they are not included in
this repository.

| Library | Used for | License |
|---|---|---|
| [hidapi](https://pypi.org/project/hidapi/) (Python bindings for [libusb/hidapi](https://github.com/libusb/hidapi)) | Commands and key events (interface 3) | BSD or GPL v3, at your choice |
| [pyusb](https://pypi.org/project/pyusb/) | Image data (interface 1) | BSD 3-Clause |
| [libusb](https://libusb.info) (Homebrew) | USB access for pyusb | LGPL 2.1 |
| [Pillow](https://python-pillow.org) | Drawing keys, reading images and GIFs | MIT-CMU |
| [rlottie-python](https://github.com/laggykiller/rlottie-python), with Samsung [rlottie](https://github.com/Samsung/rlottie) | Lottie animations | LGPL 2.1 |

## Trademarks

Mountain and DisplayPad are trademarks of their owners. This project is not affiliated
with, endorsed by or supported by Mountain or 360 Service Agency GmbH.
