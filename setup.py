"""Build the pip package: compile the C program and put it inside the package.

The wheel is tied to one platform (it carries a binary) but not to a Python
version (it carries no extension module), so one wheel per platform covers
every Python 3. Installing from the source archive compiles the program on
the spot, which needs a C compiler.

The version comes from PPZ_VERSION in work/csrc/ppz.h: "1.00" -> 1.0.0,
"1.01" -> 1.0.1, "1.10" -> 1.1.0, so pip versions never collide.
"""

import os
import re
import shutil
import stat
import subprocess
import sys

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ImportError:  # setuptools < 70
    from wheel.bdist_wheel import bdist_wheel

HERE = os.path.dirname(os.path.abspath(__file__))
CSRC = os.path.join(HERE, "work", "csrc")


def version():
    with open(os.path.join(CSRC, "ppz.h")) as f:
        v = re.search(r'#define PPZ_VERSION\s+"(\d+)\.(\d)(\d)"', f.read())
    if not v:
        sys.exit("setup.py: PPZ_VERSION in work/csrc/ppz.h is not X.YZ")
    return "%s.%s.%s" % v.groups()


class BuildProgram(build_py):
    """Compile work/csrc and copy the program and the Parquet converter in."""

    def run(self):
        super().run()
        if sys.platform == "darwin":
            # the wheel says macOS 11+, so the binary must run there too
            os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")
        subprocess.check_call(["sh", os.path.join(CSRC, "build.sh")])
        pkg = os.path.join(self.build_lib, "polypress")
        os.makedirs(os.path.join(pkg, "bin"), exist_ok=True)
        exe = os.path.join(pkg, "bin", "polypress")
        shutil.copy2(os.path.join(CSRC, "polypress"), exe)
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        shutil.copy2(os.path.join(HERE, "work", "py", "parquet.py"),
                     os.path.join(pkg, "parquet.py"))


class BinaryDistribution(Distribution):
    """Platform-specific: the package carries a compiled program."""

    def has_ext_modules(self):
        return True


class PlatformWheel(bdist_wheel):
    """py3-none-<platform>: a binary inside, no Python extension."""

    def get_tag(self):
        return ("py3", "none", super().get_tag()[2])


setup(version=version(), distclass=BinaryDistribution,
      cmdclass={"build_py": BuildProgram, "bdist_wheel": PlatformWheel})
