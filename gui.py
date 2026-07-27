"""TableZip -- the Mac front end, built on native dialogs.

Not Tkinter. The only Tk on a stock macOS is Apple's 8.5.9 from 2010, and it
crashes during widget construction on current macOS -- the window opens and
paints nothing. Rather than make the user install a second Python, this
drives the real system dialogs through osascript: native look, no
dependencies, and it works on a clean machine.

Flow: pick a file, pick where to save, watch a notification, read the result.
Nothing is written until the compressed blob has been decoded in memory and
compared against the original table.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dtz
import fast

PACKED_EXT = ".tcz"
TABLE_EXT = ("csv", "tsv", "psv", "txt", "dat", "json", "jsonl", "ndjson",
             "parquet")
APP = "TableZip"


# ------------------------------------------------------------- applescript

class Cancelled(Exception):
    pass


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osa(script: str) -> str:
    p = subprocess.run(["osascript", "-e", script],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = p.stderr.decode()
    if p.returncode != 0:
        if "User canceled" in err or "-128" in err:
            raise Cancelled()
        raise RuntimeError(err.strip() or "osascript failed")
    return p.stdout.decode().strip()


def _activate() -> str:
    """Bring our dialogs to the front.

    Wrapped in try/end try on purpose: this needs Automation permission, and
    if the user has not granted it the dialog must still appear rather than
    the whole script dying with -1743."""
    return ('try\n'
            'tell application "System Events" to set frontmost of '
            'the first process whose unix id is {} to true\n'
            'end try\n').format(os.getpid())


# The script builders are separate from the calls that run them so the
# self-test can validate the exact text that will be executed. An earlier
# version tested a hand-written approximation instead and shipped a list
# joined with spaces where AppleScript wanted commas.

def _script_choose_file() -> str:
    """No `of type` filter on purpose.

    AppleScript resolves that list through UTIs, and `.tcz` is an extension
    macOS has never heard of -- so filtering by it greys out exactly the
    files you need in order to restore anything. The extension is checked in
    Python after the pick instead."""
    return (_activate() +
            'set f to choose file with prompt '
            '"Choose a table to compress, or a .tcz file to restore:"\n'
            'POSIX path of f')


def _script_choose_save(default_name: str, prompt: str) -> str:
    return (_activate() +
            'set f to choose file name with prompt "{}" default name "{}"\n'
            'POSIX path of f'.format(_esc(prompt), _esc(default_name)))


def choose_file() -> str:
    return _osa(_script_choose_file())


def choose_save(default_name: str, prompt: str) -> str:
    return _osa(_script_choose_save(default_name, prompt))


def notify(text: str) -> None:
    try:
        _osa('display notification "{}" with title "{}"'.format(
            _esc(text), APP))
    except Exception:
        pass


def _script_dialog(text: str, buttons, default: str) -> str:
    btns = ", ".join('"{}"'.format(_esc(b)) for b in buttons)
    return (_activate() +
            'set r to display dialog "{}" with title "{}" buttons {{{}}} '
            'default button "{}" with icon note\n'
            'button returned of r'.format(
                _esc(text), APP, btns, _esc(default)))


def dialog(text: str, buttons, default: str) -> str:
    return _osa(_script_dialog(text, buttons, default))


def selftest() -> int:
    """Parse every script we can generate, without opening any dialog.

    `if false then ... end if` still forces AppleScript to compile the body,
    so a malformed list or a bad quote fails here instead of in front of the
    user."""
    cases = {
        "choose_file": _script_choose_file(),
        "choose_save": _script_choose_save('a "b".csv.tcz', "Save as:"),
        "dialog": _script_dialog(
            'multi\nline "quoted" \\ backslash', ["Quit", "Do Another"],
            "Do Another"),
        "notify": 'display notification "{}" with title "{}"'.format(
            _esc('x "y" \\ z'), APP),
    }
    bad = 0
    for name, script in cases.items():
        p = subprocess.run(["osascript", "-e",
                            "if false then\n" + script + "\nend if"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ok = p.returncode == 0
        bad += 0 if ok else 1
        print("  {:<14} {}".format(
            name, "OK" if ok else "FAIL " + p.stderr.decode().strip()[:140]))
    return bad


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "{:,.0f} {}".format(n, unit) if unit == "B" \
                else "{:,.1f} {}".format(n, unit)
        n /= 1024.0
    return str(n)


# -------------------------------------------------------------------- work

def compress(src: str) -> str:
    raw = os.path.getsize(src)
    dst = choose_save(os.path.basename(src) + PACKED_EXT,
                      "Save the compressed file as:")
    notify("Reading " + os.path.basename(src))
    table = dtz.read_any(src)
    rows, cols = table.shape

    plan = fast.classify(table)
    kinds: dict = {}
    for c in plan:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    groups = fast.find_2d_groups(plan)

    notify("Compressing {:,} rows x {} columns…".format(rows, cols))
    t0 = time.time()
    blob = fast.encode(table)
    secs = time.time() - t0

    notify("Verifying…")
    back = fast.decode(blob)
    if back.columns != table.columns or back.rows != table.rows:
        return ("VERIFICATION FAILED.\n\n"
                "The compressed data did not decode back to the original "
                "table, so nothing was written.")

    with open(dst, "wb") as fh:
        fh.write(blob)

    lines = [
        "Compressed {:,} rows x {} columns.".format(rows, cols),
        "",
        "Original      {:>12}".format(human(raw)),
        "Compressed    {:>12}".format(human(len(blob))),
        "Ratio         {:>11.2f}x".format(raw / max(len(blob), 1)),
        "Saved         {:>12}".format(human(raw - len(blob))),
        "Speed         {:>9.1f} MB/s".format(raw / 1e6 / max(secs, 1e-9)),
        "",
        "Plan: " + ", ".join("{} {}".format(v, k)
                             for k, v in sorted(kinds.items())),
    ]
    if groups:
        lines.append("2D groups: " + "; ".join(
            ", ".join(table.columns[plan[p]["j"]] for p in g) for g in groups))
    lines += [
        "",
        "Verified: every cell, column name and row order was",
        "reproduced exactly from the compressed file.",
        "",
        "Saved to " + dst,
    ]
    return "\n".join(lines)


def restore(src: str) -> str:
    base = os.path.basename(src)
    if base.lower().endswith(PACKED_EXT):
        base = base[:-len(PACKED_EXT)]
    dst = choose_save(base, "Save the restored table as:")
    notify("Restoring " + os.path.basename(src))
    blob = open(src, "rb").read()
    t0 = time.time()
    table = fast.decode(blob)
    secs = time.time() - t0
    rows, cols = table.shape
    dtz.write_any(table, dst)
    out = os.path.getsize(dst)
    return "\n".join([
        "Restored {:,} rows x {} columns.".format(rows, cols),
        "",
        "Archive       {:>12}".format(human(len(blob))),
        "Restored      {:>12}".format(human(out)),
        "Speed         {:>9.1f} MB/s".format(out / 1e6 / max(secs, 1e-9)),
        "",
        "Note: this writes canonical CSV, so quoting and line endings",
        "may differ from the original file. The table is identical;",
        "the bytes are not.",
        "",
        "Saved to " + dst,
    ])


def main() -> None:
    # A path on the command line comes from double-clicking or dropping a
    # file on the app; use it once, then fall back to the picker.
    pending = [a for a in sys.argv[1:] if not a.startswith("-")]
    while True:
        try:
            src = pending.pop(0) if pending else choose_file()
            if src.lower().endswith(PACKED_EXT):
                text = restore(src)
            else:
                text = compress(src)
            again = dialog(text, ["Quit", "Do Another"], "Do Another")
            if again == "Quit":
                return
        except Cancelled:
            return
        except Exception:
            try:
                again = dialog("Something went wrong:\n\n" +
                               traceback.format_exc()[-900:],
                               ["Quit", "Try Again"], "Try Again")
                if again == "Quit":
                    return
            except Exception:
                return


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
