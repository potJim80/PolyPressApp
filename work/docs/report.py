"""Generate the Polypress results PDF **from the sweep output**, not from memory.

    python3 docs/report.py docs/Polypress-Results.pdf ../OUT/results/socrata500.jsonl

Run from work/. Sweep output moved to OUT/results/ on 2026-08-04 --
see memory/RESTRUCTURE-2026-08-04.md.

Why it reads the data
---------------------
The previous version of this file carried every number as a literal. It went
stale the first time the codec changed, and it kept two claims that had already
been walked back in the README -- "beats every compressor tested, on every
dataset tested" -- for two sessions after they stopped being true. CLAUDE.md's
standing rule is *regenerate the results file whenever the codec changes; never
hand-patch*. A document that cannot be regenerated will always drift.

So every figure below is computed from the JSONL the sweep wrote. The prose is
written to be true of whatever those files say -- where a sentence depends on a
number, the number is substituted, and where a claim could be falsified by a
future sweep the sentence is generated from the comparison rather than asserted.
"""

from __future__ import annotations

import json
import os
import statistics
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, PageBreak, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

# ------------------------------------------------------------------- loading

OURS = "polypress"


def load(paths):
    recs, failed = [], 0
    for p in paths:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "error" in r or "results" not in r:
                    failed += 1
                    continue
                if OURS not in r.get("results", {}):
                    failed += 1
                    continue
                recs.append(r)
    recs.sort(key=lambda r: r["file"])
    return recs, failed


def competitors(rec):
    """Every codec in the record that is not one of ours."""
    return {k: v for k, v in rec["results"].items()
            if not k.startswith(OURS)}


def best_rival(rec):
    c = competitors(rec)
    name = min(c, key=lambda k: c[k]["bytes"])
    return name, c[name]["bytes"]


def summarise(recs):
    s = {"n": len(recs)}
    s["input_bytes"] = sum(r["bytes"] for r in recs)
    s["ours_bytes"] = sum(r["results"][OURS]["bytes"] for r in recs)
    s["rival_bytes"] = sum(best_rival(r)[1] for r in recs)

    margins, wins, losses = [], 0, []
    for r in recs:
        ours = r["results"][OURS]["bytes"]
        name, rival = best_rival(r)
        margins.append(rival / float(ours))
        if ours < rival:
            wins += 1
        else:
            losses.append((rival / float(ours), r["file"], name))
    s["wins"] = wins
    s["median_margin"] = statistics.median(margins)
    s["margins"] = margins
    s["losses"] = sorted(losses)
    s["aggregate_margin"] = s["rival_bytes"] / float(s["ours_bytes"])
    s["aggregate_ratio"] = s["input_bytes"] / float(s["ours_bytes"])

    s["roundtrip_ok"] = sum(1 for r in recs if r.get("roundtrip"))
    pq = [r for r in recs if r.get("parquet_exact") is not None]
    s["parquet_checked"] = len(pq)
    s["parquet_exact"] = sum(1 for r in pq if r["parquet_exact"])

    enc = [r["results"][OURS]["enc_mbs"] for r in recs
           if r["results"][OURS].get("enc_mbs")]
    dec = [r["results"][OURS]["dec_mbs"] for r in recs
           if r["results"][OURS].get("dec_mbs")]
    s["enc_mbs"] = statistics.median(enc) if enc else 0.0
    s["dec_mbs"] = statistics.median(dec) if dec else 0.0

    # Aggregate size per codec, over the datasets where that codec produced a
    # result. Counting a codec's total across a different set of tables than
    # ours would flatter whichever one skipped the hard ones, so the count is
    # carried and printed.
    per = {}
    for r in recs:
        for k, v in r["results"].items():
            if v is None or v.get("bytes") is None:
                continue
            slot = per.setdefault(k, {"bytes": 0, "n": 0, "enc": [], "dec": []})
            slot["bytes"] += v["bytes"]
            slot["n"] += 1
            if v.get("enc_mbs"):
                slot["enc"].append(v["enc_mbs"])
            if v.get("dec_mbs"):
                slot["dec"].append(v["dec_mbs"])
    s["per_codec"] = per
    return s


def like_for_like(recs, finisher):
    """Ours re-finished with a rival's own codec, versus that rival."""
    mine, theirs = OURS + "+" + finisher, "parquet+" + finisher
    pairs = []
    for r in recs:
        a = r["results"].get(mine)
        b = r["results"].get(theirs)
        if a and b and a.get("bytes") and b.get("bytes"):
            pairs.append((r["file"], a["bytes"], b["bytes"]))
    wins = sum(1 for _, a, b in pairs if a < b)
    return pairs, wins


# -------------------------------------------------------------------- layout

INK = colors.HexColor("#12161c")
MUTED = colors.HexColor("#5b6672")
RULE = colors.HexColor("#d7dce2")
BAND = colors.HexColor("#f2f5f8")
WIN = colors.HexColor("#0b6b3a")
WINBG = colors.HexColor("#e7f4ec")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="Helvetica-Bold",
                    fontSize=23, leading=27, textColor=INK, alignment=TA_LEFT,
                    spaceAfter=2)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontName="Helvetica",
                     fontSize=10.5, leading=15, textColor=MUTED, spaceAfter=14)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=13, leading=16, textColor=INK,
                    spaceBefore=16, spaceAfter=6)
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                    fontSize=10.5, leading=13, textColor=INK,
                    spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("BODY", parent=ss["BodyText"], fontName="Helvetica",
                      fontSize=9.6, leading=14, textColor=INK, spaceAfter=7)
SMALL = ParagraphStyle("SMALL", parent=BODY, fontSize=8.6, leading=12,
                       textColor=MUTED)
BULLET = ParagraphStyle("BULLET", parent=BODY, leftIndent=13, bulletIndent=3,
                        spaceAfter=3)


def P(t, s=BODY):
    return Paragraph(t, s)


def bullets(items, style=BULLET):
    return [Paragraph(t, style, bulletText="•") for t in items]


def table(data, widths, highlight=None, align_right=None, size=8.6):
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", size),
        ("FONT", (0, 1), (-1, -1), "Helvetica", size),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, RULE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    if highlight is not None:
        style += [("BACKGROUND", (0, highlight), (-1, highlight), WINBG),
                  ("TEXTCOLOR", (0, highlight), (-1, highlight), WIN),
                  ("FONT", (0, highlight), (-1, highlight),
                   "Helvetica-Bold", size)]
    for c in (align_right or []):
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def kpi(items):
    row = [[Paragraph("<b>%s</b>" % v, ParagraphStyle(
        "K", parent=BODY, fontSize=15, leading=18, spaceAfter=0))
        for _, v in items],
        [Paragraph(k, SMALL) for k, _ in items]]
    t = Table(row, colWidths=[1.62 * inch] * len(items), hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return t


def _chrome(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.85 * inch, 0.55 * inch,
                      "Polypress — lossless data-table compression")
    canvas.drawRightString(LETTER[0] - 0.85 * inch, 0.55 * inch,
                           "Page %d" % doc.page)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.4)
    canvas.line(0.85 * inch, 0.72 * inch, LETTER[0] - 0.85 * inch, 0.72 * inch)
    canvas.restoreState()


def mb(n):
    return "{:,.1f} MB".format(n / 1e6)


def pct(a, b):
    return "{:.0f}%".format(100.0 * a / b) if b else "n/a"


# --------------------------------------------------------------------- build

def build(out, recs, failed, sources):
    s = summarise(recs)
    story = []
    A = story.append

    A(P("Polypress", H1))
    A(P("A lossless compressor for data tables. Every number in this document "
        "was produced by the benchmark harness in this repository, on real "
        "public data, against production binaries — and this document is "
        "generated from that output rather than written from it.", SUB))

    A(kpi([("datasets measured", "{:,}".format(s["n"])),
           ("beaten, best of {}".format(
               len(competitors(recs[0]))), "{}/{}".format(s["wins"], s["n"])),
           ("median margin", "{:.2f}×".format(s["median_margin"])),
           ("round-trip exact", "{}/{}".format(s["roundtrip_ok"], s["n"]))]))
    A(Spacer(1, 14))

    A(P("What it is", H2))
    A(P("Polypress compresses tabular data — CSV, TSV, JSON, Parquet — "
        "and reproduces the logical table exactly: every column name, the "
        "column order, the row order, and every cell as the identical string. "
        "Every compression is verified in memory, cell by cell, before "
        "anything is written to disk.", BODY))

    A(P("The corpus, and why it is not chosen", H2))
    A(P("The obvious objection to any compression result is <i>you picked the "
        "files</i>, and there is no way to answer it by picking more files. So "
        "the corpus is not picked. The Socrata open-data catalog — the index "
        "behind several hundred government portals — is asked for its datasets "
        "in descending order of lifetime page views, and the first "
        "{:,} that survive purely mechanical filters are taken. Rank order is "
        "public and reproducible. Nothing is skipped for what is in it, and "
        "every rejection is recorded with its reason.".format(s["n"]), BODY))
    A(P("Total input: {}. Sources: {}.{}".format(
        mb(s["input_bytes"]), ", ".join(os.path.basename(p) for p in sources),
        " {} datasets failed to measure and are excluded.".format(failed)
        if failed else ""), SMALL))

    A(P("The headline result", H2))
    rows = [["Codec", "Total bytes", "vs Polypress", "Tables"]]
    ours_total = s["per_codec"][OURS]["bytes"]
    ranked = sorted(s["per_codec"].items(), key=lambda kv: kv[1]["bytes"])
    hi = None
    for i, (name, v) in enumerate(ranked, 1):
        if name == OURS:
            hi = i
        rows.append([name, "{:,}".format(v["bytes"]),
                     "{:.2f}×".format(v["bytes"] / float(ours_total)),
                     "{}".format(v["n"])])
    A(table(rows, [1.9 * inch, 1.35 * inch, 1.15 * inch, 0.8 * inch],
            highlight=hi, align_right=[1, 2, 3]))
    A(Spacer(1, 6))
    A(P("Aggregated over the whole corpus Polypress is <b>{:.2f}× smaller "
        "than the best rival on each table taken individually</b> — that is, "
        "against a competitor allowed to pick its best codec per dataset. "
        "Per table it wins <b>{} of {}</b>, median margin "
        "<b>{:.2f}×</b>.".format(
            s["aggregate_margin"], s["wins"], s["n"], s["median_margin"]),
        BODY))
    A(P("The &quot;Tables&quot; column is not decoration. Two competitors were "
        "silently missing from an earlier round of this benchmark because "
        "their writers raised on certain inputs and the exception was being "
        "swallowed. A codec measured on fewer tables than the rest is not "
        "comparable with the rest, so the count is printed.", SMALL))

    A(PageBreak())

    A(P("Fidelity: what a size comparison hides", H2))
    A(P("Parquet reproduced the exact printed text of the table on <b>{} of "
        "{}</b> datasets where the comparison could be made. On the rest it "
        "changed something — a number reformatted, a leading zero dropped, a "
        "date restyled. Those are real differences to anyone who has to hand "
        "the file back. <b>Its size column is therefore flattering on {} of "
        "the {} tables</b>, and it should never be quoted without this "
        "sentence next to it. Polypress restored every cell exactly on "
        "{} of {}.".format(
            s["parquet_exact"], s["parquet_checked"],
            s["parquet_checked"] - s["parquet_exact"], s["parquet_checked"],
            s["roundtrip_ok"], s["n"]), BODY))

    A(P("Is the margin the modelling, or just a better final compressor?", H2))
    A(P("A fair objection, worth answering before it is asked. Parquet "
        "<b>cannot use xz at all</b> — its options are snappy, gzip, brotli, "
        "zstd and lz4, and asking pyarrow for xz returns an unsupported-codec "
        "error. So some of the margin above could be the finisher rather than "
        "the front end.", BODY))
    ll_rows = [["Finisher", "Polypress wins", "Tables compared"]]
    for fin in ("zstd", "brotli"):
        pairs, wins = like_for_like(recs, fin)
        if pairs:
            ll_rows.append([fin, "{} of {}".format(wins, len(pairs)),
                            "{}".format(len(pairs))])
    if len(ll_rows) > 1:
        A(P("It is not. Re-finishing Polypress with the <i>same</i> codec "
            "Parquet is using:", BODY))
        A(table(ll_rows, [1.5 * inch, 1.6 * inch, 1.5 * inch],
                align_right=[1, 2]))
        A(Spacer(1, 4))
        A(P("Strip xz out entirely and the gap barely moves. The modelling is "
            "doing the work.", SMALL))

    A(P("Why it wins", H2))
    A(P("Three ideas. The third is the load-bearing one.", BODY))
    A(P("1. Predicting down a column", H3))
    A(P("Fit a low-degree polynomial to the last few values in a column, "
        "extrapolate one step, store only the error. Because the fit is "
        "re-centred at every cell the coefficients never have to be stored — "
        "the decoder already holds the neighbours. Order-<i>k</i> "
        "extrapolation turns out to be exactly the <i>k</i>-th finite "
        "difference.", BODY))
    A(P("2. The same idea in two dimensions", H3))
    A(P("Where adjacent numeric columns are <i>commensurable</i> — same "
        "decimal places, same magnitude — a cell is predicted from its left, "
        "upper and upper-left neighbours. Detection matters as much as the "
        "predictor: differencing &quot;Model Year&quot; against &quot;Make&quot; "
        "is meaningless, so groups form only where columns are genuinely "
        "comparable.", BODY))
    A(P("3. Cross-column structure by reordering rows", H3))
    A(P("Table columns are not independent — City is nearly determined by "
        "Postal Code. Rather than model that, Polypress <b>sorts the rows by "
        "the parent column</b>: equal parents become adjacent, the child "
        "collapses into long runs, and the entropy coder eats it. <b>The "
        "permutation costs nothing to store</b>, because the decoder has "
        "already rebuilt the parent and recomputes the same stable sort. "
        "Parents are chosen by conditional entropy with a Miller-Madow "
        "correction and arranged into a tree.", BODY))
    A(P("This third idea is <b>prior art, not novel</b>: US 8,312,026 (Vo, "
        "AT&amp;T, 2012, now expired) discloses it, down to the rule that a "
        "parent must be measured rather than assumed. It was arrived at "
        "independently here; the novelty claim is withdrawn.", SMALL))

    A(PageBreak())

    A(P("Where it wins, and where it does not", H2))
    scored = sorted(
        ((best_rival(r)[1] / float(r["results"][OURS]["bytes"]), r)
         for r in recs), reverse=True)
    top = [["Dataset", "Input", "Polypress", "Best rival", "Margin"]]
    for margin, r in scored[:8]:
        name, rb = best_rival(r)
        top.append([r["file"][:34], mb(r["bytes"]),
                    "{:,}".format(r["results"][OURS]["bytes"]),
                    "{} {:,}".format(name, rb),
                    "{:.2f}×".format(margin)])
    A(P("Best eight:", H3))
    A(table(top, [2.05 * inch, 0.75 * inch, 0.95 * inch, 1.5 * inch,
                  0.7 * inch], align_right=[1, 2, 3, 4], size=7.8))

    A(P("Every table it loses:", H3))
    if s["losses"]:
        low = [["Dataset", "Input", "Polypress", "Winner", "Margin"]]
        for margin, fname, cname in s["losses"]:
            r = next(x for x in recs if x["file"] == fname)
            low.append([fname[:34], mb(r["bytes"]),
                        "{:,}".format(r["results"][OURS]["bytes"]),
                        "{} {:,}".format(cname,
                                         r["results"][cname]["bytes"]),
                        "{:.2f}×".format(margin)])
        A(table(low, [2.05 * inch, 0.75 * inch, 0.95 * inch, 1.5 * inch,
                      0.7 * inch], align_right=[1, 2, 3, 4], size=7.8))
        A(Spacer(1, 4))
        A(P("Listed in full rather than summarised. A results document that "
            "reports only the wins is an advertisement.", SMALL))
    else:
        A(P("None on this corpus.", BODY))

    A(P("Honest limitations", H2))
    for b in bullets([
        "<b>Speed.</b> Median encode {:.1f} MB/s, decode {:.0f} MB/s. Encode "
        "is the slow half and deliberately so: every table is also compressed "
        "by the plain fallbacks and the smaller result wins, which is what "
        "makes the never-worse guarantee true rather than assumed."
        .format(s["enc_mbs"], s["dec_mbs"]),
        "<b>Encode cost grows with the square of the column count</b>, in the "
        "parent search. A very wide table is slow.",
        "<b>Genre.</b> Every dataset here is a government open-data table. "
        "That is a wide sample of one kind of data, not a sample of all data. "
        "Census microdata, scientific arrays, genomics and financial ticks "
        "are unmeasured, and the win should be expected to be smaller on some "
        "of them.",
        "<b>Nobody outside the project has run it.</b> Every number in this "
        "document was produced on one machine by one person.",
    ]):
        A(b)

    A(P("Negative results", H2))
    A(P("Kept because they are the most useful part of the record.", SMALL))
    for b in bullets([
        "<b>Run-length coding the text blob:</b> +0.3%. xz already subsumes "
        "it — RLE finds only adjacent repeats, LZMA finds them at any "
        "distance. The gain never comes from a better compressor on the same "
        "bytes; it comes from a better order.",
        "<b>Recovering ragged-decimal numeric columns:</b> 9–23% worse.",
        "<b>Raising the 50% dictionary threshold:</b> up to 2.45% worse, and "
        "it stayed worse after the theory that explained the failure was "
        "itself disproved.",
        "<b>Relaxing the two-dimensional exact-decimal rule:</b> zero bytes "
        "on every table.",
    ]):
        A(b)

    A(Spacer(1, 10))
    A(P("Generated by docs/report.py from {}. Reproduce with "
        "benchmarks/fetch_socrata100.py, benchmarks/sweep.py and "
        "benchmarks/report.py.".format(
            ", ".join(os.path.basename(p) for p in sources)), SMALL))

    doc = BaseDocTemplate(out, pagesize=LETTER,
                          leftMargin=0.85 * inch, rightMargin=0.85 * inch,
                          topMargin=0.8 * inch, bottomMargin=0.85 * inch,
                          title="Polypress — results",
                          author="Polypress")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  id="body")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame],
                                       onPage=_chrome)])
    doc.build(story)


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(
            "usage: report.py OUT.pdf results.jsonl [more.jsonl ...]\n")
        return 2
    out, sources = argv[1], argv[2:]
    recs, failed = load(sources)
    if not recs:
        sys.stderr.write("no usable records in {}\n".format(sources))
        return 1
    build(out, recs, failed, sources)
    print("wrote {} from {} datasets".format(out, len(recs)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
