"""Polypress -- the Mac front end, built on native dialogs.

Not Tkinter. The only Tk on a stock macOS is Apple's 8.5.9 from 2010. It will
happily *construct* ttk widgets, which is what makes this trap so easy to fall
into -- but the moment the window is actually mapped and drawn it wedges, and
the app hangs with no window and no error. Re-verified on 2026-07-28 rather
than taken on trust: widget construction and `destroy()` both succeed, and a
single `update()` on a mapped window never returns. So this drives the real
system dialogs through osascript: native look, no dependencies, and it works
on a clean machine where nothing has been installed.

The shape is a menu, not a wizard. The old version inferred what you wanted
from the file extension and offered no choices at all; it could only compress
and restore. This one asks, and exposes the things the command line could
always do but the app could not:

    Compress a table        with a choice of normal or low-memory mode
    Restore an archive      into any of five formats, not just CSV
    Convert a table         between formats without compressing at all
    Inspect an archive      what is inside it, without unpacking it

Nothing is written until the compressed blob has been decoded in memory and
compared against the original table.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
# Works from both layouts: app/gui.py in the repo, where the package sits in
# the parent directory, and Resources/gui.py in the app bundle, where the
# package is copied alongside.
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from polypress import __version__, dtz, fast, stream

PACKED_EXT = ".ppz"
LEGACY_EXT = ".tcz"      # archives written before the rename
APP = "Polypress"

# Above this, a single-shot encode is a bad idea: fast.py expands CSV roughly
# 8.5x into Python strings and holds an encoded and a decoded copy at once.
# The number is not a hard limit, it is where low-memory mode stops being
# paranoia and starts being the right answer, so it drives a recommendation
# rather than a refusal.
BIG_FILE_MB = 80


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

def _script_choose_file(prompt: str) -> str:
    """No `of type` filter on purpose.

    AppleScript resolves that list through UTIs, and `.ppz` is an extension
    macOS has never heard of -- so filtering by it greys out exactly the
    files you need in order to restore anything. The kind is checked in
    Python after the pick instead."""
    return (_activate() +
            'set f to choose file with prompt "{}"\n'
            'POSIX path of f'.format(_esc(prompt)))


def _script_choose_save(default_name: str, prompt: str) -> str:
    return (_activate() +
            'set f to choose file name with prompt "{}" default name "{}"\n'
            'POSIX path of f'.format(_esc(prompt), _esc(default_name)))


def _script_choose_from_list(items, prompt: str, default: str,
                             ok: str = "Continue") -> str:
    """A menu. `display dialog` caps out at three buttons, which is why the
    old front end had no menu at all -- five choices simply do not fit. This
    one returns the item text, or the string "false" when cancelled."""
    lst = ", ".join('"{}"'.format(_esc(i)) for i in items)
    return (_activate() +
            'choose from list {{{}}} with prompt "{}" default items {{"{}"}} '
            'OK button name "{}" cancel button name "Cancel"'.format(
                lst, _esc(prompt), _esc(default), _esc(ok)))


def _script_dialog(text: str, buttons, default: str) -> str:
    btns = ", ".join('"{}"'.format(_esc(b)) for b in buttons)
    return (_activate() +
            'set r to display dialog "{}" with title "{}" buttons {{{}}} '
            'default button "{}" with icon note\n'
            'button returned of r'.format(
                _esc(text), APP, btns, _esc(default)))


def choose_file(prompt: str) -> str:
    return _osa(_script_choose_file(prompt))


def choose_save(default_name: str, prompt: str) -> str:
    return _osa(_script_choose_save(default_name, prompt))


def choose_from_list(items, prompt: str, default=None, ok: str = "Continue"):
    got = _osa(_script_choose_from_list(items, prompt, default or items[0], ok))
    # Cancelling `choose from list` is not an osascript error -- it returns
    # the literal `false`, which would otherwise sail on as a menu choice.
    if got == "false" or not got:
        raise Cancelled()
    return got


def dialog(text: str, buttons, default: str) -> str:
    return _osa(_script_dialog(text, buttons, default))


def notify(text: str) -> None:
    try:
        _osa('display notification "{}" with title "{}"'.format(
            _esc(text), APP))
    except Exception:
        pass


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "{:,.0f} {}".format(n, unit) if unit == "B" \
                else "{:,.1f} {}".format(n, unit)
        n /= 1024.0
    return str(n)


# ------------------------------------------------------------------ menus

MENU_COMPRESS = "Compress a table into a Polypress archive"
MENU_RESTORE = "Restore an archive back into a data file"
MENU_CONVERT = "Convert a table to another format"
MENU_INSPECT = "Inspect an archive without unpacking it"
MENU_ABOUT = "About Polypress"

MAIN_MENU = [MENU_COMPRESS, MENU_RESTORE, MENU_CONVERT, MENU_INSPECT,
             MENU_ABOUT]

MODE_NORMAL = "Normal - fastest, holds the table in memory"
MODE_STREAM = "Low memory - one block at a time, for very large files"

# Label -> extension. Parquet is offered only when pyarrow is importable,
# because otherwise picking it produces a failure at the very last step,
# after the user has already chosen a filename.
_ALL_FORMATS = [
    ("CSV", ".csv"),
    ("TSV (tab separated)", ".tsv"),
    ("JSON", ".json"),
    ("JSON Lines", ".jsonl"),
    ("Parquet", ".parquet"),
]


def formats():
    return [(k, v) for k, v in _ALL_FORMATS
            if v != ".parquet" or dtz.pa is not None]


def format_ext(label: str) -> str:
    for k, v in _ALL_FORMATS:
        if k == label:
            return v
    return ".csv"


# ------------------------------------------------------------- containers

CONTAINERS = {
    fast.MAGIC: "modelled",
    fast.MAGIC_V0: "modelled (written by an older version)",
    fast.MAGIC_RAW_XZ: "plain xz -- no modelling helped on this table",
    fast.MAGIC_RAW_BZ: "plain bzip2 -- no modelling helped on this table",
    stream.MAGIC: "streamed, one block at a time",
}


def archive_kind(path: str):
    """Which container a file is, or None if it is not an archive at all.

    Read from the magic rather than the extension, because a streamed archive
    and a single-shot one share the .ppz suffix but need different readers.
    Guessing from the name here would mean handing a stream archive to
    fast.decode and reporting it as corrupt."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return None
    return magic if magic in CONTAINERS else None


def looks_like_archive(path: str) -> bool:
    return archive_kind(path) is not None or \
        path.lower().endswith((PACKED_EXT, LEGACY_EXT))


# -------------------------------------------------------------------- work

def do_compress() -> str:
    src = choose_file("Choose a table to compress:")
    if looks_like_archive(src):
        return ("That file is already a Polypress archive.\n\n"
                "Choose \"Restore an archive\" from the menu to unpack it.")

    raw = os.path.getsize(src)
    big = raw > BIG_FILE_MB * 1024 * 1024
    default_mode = MODE_STREAM if big else MODE_NORMAL
    prompt = "{}  ({})\n\nHow should it be compressed?".format(
        os.path.basename(src), human(raw))
    if big:
        prompt += "\n\nThis file is large, so low-memory mode is suggested."
    mode = choose_from_list([MODE_NORMAL, MODE_STREAM], prompt, default_mode,
                            ok="Compress")

    dst = choose_save(os.path.basename(src) + PACKED_EXT,
                      "Save the compressed file as:")

    if mode == MODE_STREAM:
        return _compress_streaming(src, dst, raw)
    return _compress_normal(src, dst, raw)


def _compress_normal(src: str, dst: str, raw: int) -> str:
    notify("Reading " + os.path.basename(src))
    table = dtz.read_any(src)
    rows, cols = table.shape

    plan = fast.classify(table)
    kinds: dict = {}
    for c in plan:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    # nrows matters: the planar predictor is refused below 3 rows, so calling
    # this without it would report groups the encoder never actually forms.
    groups = fast.find_2d_groups(plan, rows)

    notify("Compressing {:,} rows x {} columns...".format(rows, cols))
    t0 = time.time()
    blob = fast.encode(table)
    secs = time.time() - t0

    notify("Verifying...")
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
    # encode() returns whichever is smaller of the modelled encoding and the
    # whole table under a plain codec. If the fallback won, the plan above
    # describes work that was measured and then discarded -- say so, rather
    # than leave a summary that implies it was used.
    if blob[:4] in (fast.MAGIC_RAW_XZ, fast.MAGIC_RAW_BZ):
        lines.append(
            "None of the modelling helped on this table, so it was stored "
            "with plain {} instead -- which came out smaller.".format(
                "xz" if blob[:4] == fast.MAGIC_RAW_XZ else "bzip2"))
    lines += [
        "",
        "Verified: every cell, column name and row order was",
        "reproduced exactly from the compressed file.",
        "",
        "Saved to " + dst,
    ]
    return "\n".join(lines)


def _compress_streaming(src: str, dst: str, raw: int) -> str:
    notify("Compressing in low-memory mode...")
    t0 = time.time()
    st = stream.compress(src, dst, progress=None)
    secs = time.time() - t0
    return "\n".join([
        "Compressed {:,} rows in {} blocks.".format(st["rows"], st["blocks"]),
        "",
        "Original      {:>12}".format(human(raw)),
        "Compressed    {:>12}".format(human(st["bytes"])),
        "Ratio         {:>11.2f}x".format(raw / max(st["bytes"], 1)),
        "Saved         {:>12}".format(human(raw - st["bytes"])),
        "Speed         {:>9.1f} MB/s".format(raw / 1e6 / max(secs, 1e-9)),
        "",
        "Low-memory mode: the table was never held in memory all at",
        "once. It was compressed {:,} rows at a time, and each block".format(
            st["rows_per_block"]),
        "was verified as it was written.",
        "",
        "Saved to " + dst,
    ])


def do_restore() -> str:
    src = choose_file("Choose a Polypress archive to restore:")
    magic = archive_kind(src)
    if magic is None:
        return ("That does not look like a Polypress archive.\n\n"
                "Archives are the files this app writes, usually ending "
                "in .ppz.")

    opts = formats()
    label = choose_from_list(
        [k for k, _ in opts],
        "{}  ({})\n\nWhich format should it be restored into?".format(
            os.path.basename(src), human(os.path.getsize(src))),
        "CSV", ok="Restore")
    ext = format_ext(label)

    base = os.path.basename(src)
    if base.lower().endswith((PACKED_EXT, LEGACY_EXT)):
        base = base[:-4]
    base = os.path.splitext(base)[0] + ext
    dst = choose_save(base, "Save the restored table as:")

    notify("Restoring " + os.path.basename(src))
    t0 = time.time()
    try:
        if magic == stream.MAGIC:
            st = stream.restore(src, dst)
            rows, cols = st["rows"], len(stream.info(src)["columns"])
        else:
            blob = open(src, "rb").read()
            table = fast.decode(blob)
            rows, cols = table.shape
            dtz.write_any(table, dst)
    except dtz.FormatLimit as exc:
        # Not every table fits every format -- duplicate column names have
        # nowhere to go in JSON or Parquet, for instance. The archive is
        # fine; the destination format is the problem, and saying so beats
        # a traceback.
        return ("This table cannot be written as {}.\n\n{}\n\n"
                "Restore it as CSV or TSV instead -- those can hold it."
                .format(label, exc))
    secs = time.time() - t0
    out = os.path.getsize(dst)

    lines = [
        "Restored {:,} rows x {} columns.".format(rows, cols),
        "",
        "Archive       {:>12}".format(human(os.path.getsize(src))),
        "Restored      {:>12}".format(human(out)),
        "Format        {:>12}".format(label),
        "Speed         {:>9.1f} MB/s".format(out / 1e6 / max(secs, 1e-9)),
    ]
    if ext in (".csv", ".tsv"):
        lines += [
            "",
            "Note: this writes canonical {}, so quoting and line".format(
                label.split(" ")[0]),
            "endings may differ from the original file. The table is",
            "identical; the bytes are not.",
        ]
    lines += ["", "Saved to " + dst]
    return "\n".join(lines)


def do_convert() -> str:
    """Read a table, write it as something else. No compression involved.

    This is not a new capability -- `restore` has always picked its format
    from the output extension -- but it was unreachable from the app, and it
    is the thing a researcher with a 200 MB CSV and a colleague who wants
    Parquet actually needs."""
    src = choose_file("Choose a table to convert:")
    if looks_like_archive(src):
        return ("That is a Polypress archive, not a table.\n\n"
                "Choose \"Restore an archive\" instead -- it can write any "
                "of the same formats.")

    opts = formats()
    label = choose_from_list(
        [k for k, _ in opts],
        "{}  ({})\n\nConvert it into which format?".format(
            os.path.basename(src), human(os.path.getsize(src))),
        "Parquet" if dtz.pa is not None else "JSON", ok="Convert")
    ext = format_ext(label)

    base = os.path.splitext(os.path.basename(src))[0] + ext
    dst = choose_save(base, "Save the converted table as:")

    notify("Converting " + os.path.basename(src))
    table = dtz.read_any(src)
    rows, cols = table.shape
    try:
        dtz.write_any(table, dst)
    except dtz.FormatLimit as exc:
        return ("This table cannot be written as {}.\n\n{}".format(label, exc))
    out = os.path.getsize(dst)
    return "\n".join([
        "Converted {:,} rows x {} columns.".format(rows, cols),
        "",
        "Original      {:>12}".format(human(os.path.getsize(src))),
        "Converted     {:>12}".format(human(out)),
        "Format        {:>12}".format(label),
        "",
        "This is a plain format change, not compression. To make it",
        "smaller, use \"Compress a table\" from the menu.",
        "",
        "Saved to " + dst,
    ])


def do_inspect() -> str:
    """What is in an archive, read from its index alone.

    Deliberately does not decode the data: on a large archive that would be
    the slowest thing the app can do, and the question being asked is "what
    is this file", which the header already answers."""
    src = choose_file("Choose a Polypress archive to inspect:")
    magic = archive_kind(src)
    if magic is None:
        return ("That does not look like a Polypress archive.\n\n"
                "Archives are the files this app writes, usually ending "
                "in .ppz.")

    size = os.path.getsize(src)
    lines = ["{}".format(os.path.basename(src)),
             "",
             "Size          {:>12}".format(human(size)),
             "Stored as     {}".format(CONTAINERS[magic])]

    if magic == stream.MAGIC:
        # header["blocks"] is a list of block *byte sizes*, not block records;
        # the row count lives in "nrows". Summing it as if it held dicts is
        # the first thing the functional test caught.
        h = stream.info(src)
        lines += [
            "Rows          {:>12,}".format(h["nrows"]),
            "Columns       {:>12}".format(len(h["columns"])),
            "Blocks        {:>12}".format(len(h["blocks"])),
            "Rows/block    {:>12,}".format(h["rows_per_block"]),
        ]
    elif magic in (fast.MAGIC, fast.MAGIC_V0):
        import json
        import lzma
        blob = open(src, "rb").read()
        ml = int.from_bytes(blob[4:8], "big")
        meta = json.loads(lzma.decompress(blob[16:16 + ml], **fast.XZ))
        kinds: dict = {}
        for c in meta["cols"]:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        parented = sum(1 for c in meta["cols"]
                       if c["kind"] == "dict" and c.get("parent") is not None)
        lines += [
            "Rows          {:>12,}".format(meta["nrows"]),
            "Columns       {:>12}".format(len(meta["cols"])),
            "",
            "Plan: " + ", ".join("{} {}".format(v, k)
                                 for k, v in sorted(kinds.items())),
            "Reordered: {} columns stored sorted by another column".format(
                parented),
        ]
        if meta["groups"]:
            lines.append("2D groups: " + "; ".join(
                ", ".join(meta["columns"][i] for i in g)
                for g in meta["groups"]))
        names = meta["columns"]
        shown = names[:12]
        lines += ["", "Columns: " + ", ".join(shown) +
                  ("  ... and {} more".format(len(names) - len(shown))
                   if len(names) > len(shown) else "")]
    else:
        lines += ["", "This archive holds the whole table under a plain",
                  "compressor, because none of the modelling beat it.",
                  "Restoring it works exactly the same way."]

    return "\n".join(lines)


def do_about() -> str:
    have = "yes" if dtz.pa is not None else "no (install pyarrow to enable)"
    return "\n".join([
        "Polypress {}".format(__version__),
        "Lossless compression for data tables.",
        "",
        "It reads a table, works out how each column is best predicted",
        "from the columns around it, and reorders rows so that repeated",
        "values fall together. On government and survey data it is",
        "typically 1.1x to 3.7x smaller than the best general-purpose",
        "compressor, and it is never larger.",
        "",
        "Every compression is checked by decoding it in memory and",
        "comparing every cell before anything is written to disk.",
        "",
        "Reads:  CSV, TSV, PSV, TXT, JSON, JSON Lines, Parquet",
        "Writes: CSV, TSV, JSON, JSON Lines, Parquet",
        "Parquet support: {}".format(have),
    ])


ACTIONS = {
    MENU_COMPRESS: do_compress,
    MENU_RESTORE: do_restore,
    MENU_CONVERT: do_convert,
    MENU_INSPECT: do_inspect,
    MENU_ABOUT: do_about,
}


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    """Parse every script we can generate, without opening any dialog.

    `if false then ... end if` still forces AppleScript to compile the body,
    so a malformed list or a bad quote fails here instead of in front of the
    user. Every builder must appear here -- the menu was added with five
    items containing hyphens and parentheses, exactly the sort of text that
    breaks an AppleScript list if it is quoted wrongly."""
    cases = {
        "choose_file": _script_choose_file("Pick a \"file\":"),
        "choose_save": _script_choose_save('a "b".csv.ppz', "Save as:"),
        "dialog": _script_dialog(
            'multi\nline "quoted" \\ backslash', ["Quit", "Do Another"],
            "Do Another"),
        "notify": 'display notification "{}" with title "{}"'.format(
            _esc('x "y" \\ z'), APP),
        "main_menu": _script_choose_from_list(
            MAIN_MENU, "What would you like to do?", MAIN_MENU[0], "Continue"),
        "mode_menu": _script_choose_from_list(
            [MODE_NORMAL, MODE_STREAM], "file.csv  (1.2 MB)\n\nHow?",
            MODE_NORMAL, "Compress"),
        "format_menu": _script_choose_from_list(
            [k for k, _ in _ALL_FORMATS], "Which format?", "CSV", "Restore"),
        "quoted_menu": _script_choose_from_list(
            ['a "quoted" item', "back\\slash"], "x", 'a "quoted" item'),
    }
    bad = 0
    for name, script in sorted(cases.items()):
        p = subprocess.run(["osascript", "-e",
                            "if false then\n" + script + "\nend if"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ok = p.returncode == 0
        bad += 0 if ok else 1
        print("  {:<14} {}".format(
            name, "OK" if ok else "FAIL " + p.stderr.decode().strip()[:140]))

    # Every menu item must map to something callable, or picking it does
    # nothing and the app silently returns to the menu.
    for item in MAIN_MENU:
        if item not in ACTIONS:
            print("  {:<14} FAIL no action bound".format("menu/" + item[:20]))
            bad += 1
    if not bad:
        print("  {:<14} OK ({} items, all bound)".format(
            "menu_wiring", len(MAIN_MENU)))
    return bad


def functest() -> int:
    """Drive every menu action end to end, with the dialogs stubbed out.

    Compiling the AppleScript proves the dialogs will open; it proves nothing
    about what happens after the user clicks. Everything below the dialogs is
    real logic now -- routing an archive by its magic rather than its name,
    choosing streaming over single-shot, writing five output formats -- and
    none of it was reachable by a test until this existed. The stubs replace
    only the four functions that would block on a human.
    """
    import shutil
    import tempfile

    g = globals()
    real = {k: g[k] for k in
            ("choose_file", "choose_save", "choose_from_list", "notify")}
    tmp = tempfile.mkdtemp(prefix="ppz-gui-")
    answers: list = []
    files: list = []
    bad = 0

    def check(name, cond, detail=""):
        nonlocal bad
        if not cond:
            bad += 1
        print("  {:<14} {}".format(name, "OK" if cond else "FAIL " + detail))

    try:
        g["notify"] = lambda *a, **k: None
        g["choose_file"] = lambda prompt: files.pop(0)
        g["choose_save"] = lambda default, prompt: files.pop(0)
        g["choose_from_list"] = lambda items, prompt, default=None, ok="": \
            answers.pop(0)

        src = os.path.join(tmp, "t.csv")
        rows = [["{}".format(i), ["red", "green", "blue"][i % 3],
                 "{:.2f}".format(i * 1.5), "note {}".format(i % 7)]
                for i in range(400)]
        with open(src, "w", newline="", encoding="utf-8") as fh:
            import csv as _csv
            w = _csv.writer(fh, lineterminator="\n")
            w.writerow(["id", "colour", "value", "note"])
            w.writerows(rows)
        want = dtz.read_any(src)

        # --- compress, normal mode ---
        arc = os.path.join(tmp, "t.ppz")
        files[:] = [src, arc]
        answers[:] = [MODE_NORMAL]
        out = do_compress()
        check("compress", os.path.exists(arc) and "Compressed 400" in out,
              out.splitlines()[0] if out else "no output")
        check("verified", "Verified" in out or "plain" in out)

        # --- compress, low-memory mode: a different container entirely ---
        sarc = os.path.join(tmp, "s.ppz")
        files[:] = [src, sarc]
        answers[:] = [MODE_STREAM]
        out = do_compress()
        check("compress/low-mem",
              archive_kind(sarc) == stream.MAGIC, "wrong container")

        # --- inspect, both containers, without decoding ---
        files[:] = [arc]
        out = do_inspect()
        check("inspect", "Rows" in out and "400" in out)
        files[:] = [sarc]
        out = do_inspect()
        check("inspect/low-mem", "Blocks" in out)

        # --- restore each offered format, and check the cells survive ---
        for label, ext in formats():
            dst = os.path.join(tmp, "back" + ext)
            files[:] = [arc, dst]
            answers[:] = [label]
            do_restore()
            got = dtz.read_any(dst)
            check("restore/" + ext[1:],
                  got.rows == want.rows and got.columns == want.columns,
                  "cells differ")

        # --- a streamed archive must restore through the streaming reader ---
        dst = os.path.join(tmp, "sback.csv")
        files[:] = [sarc, dst]
        answers[:] = ["CSV"]
        do_restore()
        check("restore/low-mem", dtz.read_any(dst).rows == want.rows,
              "cells differ")

        # --- a format that cannot hold the table must explain, not crash ---
        dup = os.path.join(tmp, "dup.csv")
        with open(dup, "w", encoding="utf-8") as fh:
            fh.write("a,a\n1,2\n3,4\n")
        duparc = os.path.join(tmp, "dup.ppz")
        files[:] = [dup, duparc]
        answers[:] = [MODE_NORMAL]
        do_compress()
        files[:] = [duparc, os.path.join(tmp, "dup.json")]
        answers[:] = ["JSON"]
        out = do_restore()
        check("restore/impossible", "cannot be written as JSON" in out,
              out.splitlines()[0] if out else "no output")

        # --- convert, no compression involved ---
        dst = os.path.join(tmp, "conv.jsonl")
        files[:] = [src, dst]
        answers[:] = ["JSON Lines"]
        out = do_convert()
        check("convert", dtz.read_any(dst).rows == want.rows, "cells differ")

        # --- the guard rails: each must refuse, not crash ---
        files[:] = [arc]
        check("guard/compress-archive",
              "already a Polypress archive" in do_compress())
        files[:] = [src]
        check("guard/restore-table",
              "does not look like" in do_restore())
        files[:] = [arc]
        check("guard/convert-archive",
              "not a table" in do_convert())

        check("about", "Polypress" in do_about() and __version__ in do_about())
    finally:
        g.update(real)
        shutil.rmtree(tmp, ignore_errors=True)
    return bad


# -------------------------------------------------------------------- main

# A file dropped on the app, or passed on the command line, skips the menu --
# dropping is already a statement of intent. It only skips the one
# unambiguous decision, archive or not; everything with options still asks.

def _compress_dropped(src: str) -> str:
    raw = os.path.getsize(src)
    big = raw > BIG_FILE_MB * 1024 * 1024
    prompt = "{}  ({})\n\nHow should it be compressed?".format(
        os.path.basename(src), human(raw))
    if big:
        prompt += "\n\nThis file is large, so low-memory mode is suggested."
    mode = choose_from_list([MODE_NORMAL, MODE_STREAM], prompt,
                            MODE_STREAM if big else MODE_NORMAL, ok="Compress")
    dst = choose_save(os.path.basename(src) + PACKED_EXT,
                      "Save the compressed file as:")
    if mode == MODE_STREAM:
        return _compress_streaming(src, dst, raw)
    return _compress_normal(src, dst, raw)


def _restore_dropped(src: str) -> str:
    magic = archive_kind(src)
    if magic is None:
        return "That does not look like a Polypress archive."
    opts = formats()
    label = choose_from_list([k for k, _ in opts],
                             "{}\n\nWhich format should it be restored into?"
                             .format(os.path.basename(src)), "CSV",
                             ok="Restore")
    ext = format_ext(label)
    base = os.path.basename(src)
    if base.lower().endswith((PACKED_EXT, LEGACY_EXT)):
        base = base[:-4]
    dst = choose_save(os.path.splitext(base)[0] + ext,
                      "Save the restored table as:")
    t0 = time.time()
    if magic == stream.MAGIC:
        st = stream.restore(src, dst)
        rows, cols = st["rows"], len(stream.info(src)["columns"])
    else:
        table = fast.decode(open(src, "rb").read())
        rows, cols = table.shape
        dtz.write_any(table, dst)
    secs = time.time() - t0
    out = os.path.getsize(dst)
    return "\n".join([
        "Restored {:,} rows x {} columns.".format(rows, cols),
        "",
        "Restored      {:>12}".format(human(out)),
        "Format        {:>12}".format(label),
        "Speed         {:>9.1f} MB/s".format(out / 1e6 / max(secs, 1e-9)),
        "",
        "Saved to " + dst,
    ])


def main() -> None:
    # A path on the command line comes from double-clicking or dropping a
    # file on the app; use it once, then fall back to the menu.
    pending = [a for a in sys.argv[1:] if not a.startswith("-")]
    while True:
        try:
            if pending:
                src = pending.pop(0)
                text = (_restore_dropped(src) if looks_like_archive(src)
                        else _compress_dropped(src))
            else:
                choice = choose_from_list(
                    MAIN_MENU, "What would you like to do?", MENU_COMPRESS)
                text = ACTIONS[choice]()
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
        rc = selftest()
        print()
        rc += functest()
        sys.exit(rc)
    main()
