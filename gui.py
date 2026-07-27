"""TableZip -- a small Mac app for compressing data tables with fast.py.

Pick a file, get a .tcz. Pick a .tcz, get the table back. The work runs on a
worker thread so the window stays responsive, and every compression is
verified by decoding it in memory and comparing before anything is written.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from tkinter import filedialog, ttk

import dtz
import fast

TABLE_EXT = (".csv", ".tsv", ".psv", ".txt", ".dat", ".json", ".jsonl",
             ".ndjson", ".parquet")
PACKED_EXT = ".tcz"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "{:,.0f} {}".format(n, unit) if unit == "B" \
                else "{:,.1f} {}".format(n, unit)
        n /= 1024.0
    return str(n)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.path: str | None = None
        self.busy = False
        root.title("TableZip")
        root.geometry("620x460")
        root.minsize(560, 420)

        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="TableZip",
                  font=("Helvetica", 22, "bold")).pack(anchor="w")
        ttk.Label(outer, foreground="#666",
                  text="Lossless table compression. Verified on every run."
                  ).pack(anchor="w", pady=(0, 14))

        box = ttk.LabelFrame(outer, text="File", padding=12)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.file_label = ttk.Label(row, text="No file selected",
                                    foreground="#888")
        self.file_label.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose File…", command=self.choose
                   ).pack(side="right")

        self.action = ttk.Button(outer, text="Compress", state="disabled",
                                 command=self.run)
        self.action.pack(fill="x", pady=14, ipady=6)

        self.bar = ttk.Progressbar(outer, mode="indeterminate")
        self.bar.pack(fill="x")

        res = ttk.LabelFrame(outer, text="Result", padding=12)
        res.pack(fill="both", expand=True, pady=(14, 0))
        self.out = tk.Text(res, height=11, wrap="word", relief="flat",
                           font=("Menlo", 11), background="#f7f7f7")
        self.out.pack(fill="both", expand=True)
        self.log("Choose a CSV, TSV, JSON or Parquet file to compress,\n"
                 "or a .tcz file to restore it.\n")

    # ---------------------------------------------------------------- ui
    def log(self, text: str) -> None:
        self.out.insert("end", text + "\n")
        self.out.see("end")

    def clear(self) -> None:
        self.out.delete("1.0", "end")

    def choose(self) -> None:
        if self.busy:
            return
        p = filedialog.askopenfilename(
            title="Choose a table or a .tcz file",
            filetypes=[("Tables and archives",
                        " ".join("*" + e for e in TABLE_EXT + (PACKED_EXT,))),
                       ("All files", "*.*")])
        if not p:
            return
        self.path = p
        size = os.path.getsize(p)
        self.file_label.config(
            text="{}   ({})".format(os.path.basename(p), human(size)),
            foreground="#000")
        packing = not p.lower().endswith(PACKED_EXT)
        self.action.config(text="Compress" if packing else "Restore",
                           state="normal")
        self.clear()
        self.log("Ready to {}.".format("compress" if packing else "restore"))

    def run(self) -> None:
        if self.busy or not self.path:
            return
        packing = not self.path.lower().endswith(PACKED_EXT)
        if packing:
            out = filedialog.asksaveasfilename(
                title="Save compressed file",
                initialfile=os.path.basename(self.path) + PACKED_EXT,
                defaultextension=PACKED_EXT)
        else:
            base = os.path.basename(self.path)
            if base.lower().endswith(PACKED_EXT):
                base = base[:-len(PACKED_EXT)]
            out = filedialog.asksaveasfilename(title="Save restored table",
                                               initialfile=base)
        if not out:
            return
        self.busy = True
        self.action.config(state="disabled")
        self.bar.start(12)
        self.clear()
        threading.Thread(target=self._work, args=(self.path, out, packing),
                         daemon=True).start()

    def done(self) -> None:
        self.busy = False
        self.bar.stop()
        self.action.config(state="normal")

    def post(self, text: str) -> None:
        self.root.after(0, self.log, text)

    # ------------------------------------------------------------- work
    def _work(self, src: str, dst: str, packing: bool) -> None:
        try:
            if packing:
                self._compress(src, dst)
            else:
                self._restore(src, dst)
        except Exception:
            self.post("FAILED\n" + traceback.format_exc())
        finally:
            self.root.after(0, self.done)

    def _compress(self, src: str, dst: str) -> None:
        raw = os.path.getsize(src)
        self.post("Reading {}…".format(os.path.basename(src)))
        t = dtz.read_any(src)
        rows, cols = t.shape
        self.post("{:,} rows x {} columns".format(rows, cols))

        plan = fast.classify(t)
        kinds = {}
        for c in plan:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        groups = fast.find_2d_groups(plan)
        self.post("Plan: " + ", ".join(
            "{} {}".format(v, k) for k, v in sorted(kinds.items())))
        if groups:
            self.post("2D groups: " + "; ".join(
                ", ".join(t.columns[plan[p]["j"]] for p in g) for g in groups))

        self.post("Compressing…")
        t0 = time.time()
        blob = fast.encode(t)
        enc = time.time() - t0

        self.post("Verifying…")
        back = fast.decode(blob)
        if back.columns != t.columns or back.rows != t.rows:
            self.post("VERIFICATION FAILED — nothing was written.")
            return

        with open(dst, "wb") as fh:
            fh.write(blob)
        mb = raw / 1e6
        self.post("")
        self.post("  original    {:>12}".format(human(raw)))
        self.post("  compressed  {:>12}".format(human(len(blob))))
        self.post("  ratio       {:>11.2f}x".format(raw / max(len(blob), 1)))
        self.post("  saved       {:>12}".format(human(raw - len(blob))))
        self.post("  speed       {:>9.1f} MB/s".format(mb / max(enc, 1e-9)))
        self.post("")
        self.post("Verified: every cell, column name and row order was")
        self.post("reproduced exactly from the compressed file.")
        self.post("")
        self.post("Note: restoring writes canonical CSV, so quoting and line")
        self.post("endings may differ from the original bytes. The table is")
        self.post("identical; the file is not byte-for-byte.")
        self.post("")
        self.post("Written to " + dst)

    def _restore(self, src: str, dst: str) -> None:
        self.post("Reading archive…")
        blob = open(src, "rb").read()
        t0 = time.time()
        table = fast.decode(blob)
        dec = time.time() - t0
        rows, cols = table.shape
        self.post("{:,} rows x {} columns".format(rows, cols))
        dtz.write_any(table, dst)
        out = os.path.getsize(dst)
        self.post("")
        self.post("  archive     {:>12}".format(human(len(blob))))
        self.post("  restored    {:>12}".format(human(out)))
        self.post("  speed       {:>9.1f} MB/s".format(
            out / 1e6 / max(dec, 1e-9)))
        self.post("")
        self.post("Written to " + dst)


def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 2.0)
    except tk.TclError:
        pass
    App(root)
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()


if __name__ == "__main__":
    main()
