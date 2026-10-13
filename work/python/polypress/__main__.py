"""The `polypress` command: hands over to the C program in this package."""

import os
import sys

from polypress import program


def main():
    exe = program()
    if not os.access(exe, os.X_OK):
        sys.exit("polypress: the program is missing from this install (%s)" % exe)
    os.execv(exe, [exe] + sys.argv[1:])


if __name__ == "__main__":
    main()
