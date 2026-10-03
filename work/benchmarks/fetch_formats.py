"""Public Stata, SAS and SPSS files, for the ReadStat path and its tests.

    python3 benchmarks/fetch_formats.py ../IN/formats/

csrc/tests/t_stat.c round-trips every file it finds in IN/formats, and these
are the files the 2026-10-02 measurements in README ("Stata, SPSS and SAS
files") were taken on: Stata Press example datasets, NHANES 2017-18 SAS
transport files, Principles of Econometrics SAS/Stata files, and pyreadstat's
small samples of every format. Existing files are left alone.
"""

import os
import sys
import urllib.request

FILES = (
    [("{}.dta".format(n), "https://www.stata-press.com/data/r18/{}.dta".format(n))
     for n in ("auto", "nlsw88", "nlswork", "census", "lifeexp", "bplong", "sp500",
               "uslifeexp", "citytemp", "nhanes2", "hbp")]
    + [("{}.xpt".format(n), "https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2017/DataFiles/{}.xpt".format(n))
       for n in ("DEMO_J", "BMX_J", "BPX_J", "DIQ_J", "BIOPRO_J", "CBC_J")]
    + [("poe_{}.sas7bdat".format(n), "https://www.principlesofeconometrics.com/sas/{}.sas7bdat".format(n))
       for n in ("br", "andy", "food", "mroz")]
    + [("poe_mroz.dta", "http://www.principlesofeconometrics.com/poe5/data/stata/mroz.dta")]
    + [("pr_{}".format(n), "https://raw.githubusercontent.com/Roche/pyreadstat/master/test_data/basic/" + n)
       for n in ("sample.sav", "sample.zsav", "sample.por", "sample.sas7bdat", "sample.xpt")]
)


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip())
        return 2
    out = argv[0]
    os.makedirs(out, exist_ok=True)
    bad = 0
    for name, url in FILES:
        dst = os.path.join(out, name)
        if os.path.exists(dst):
            continue
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
            with open(dst, "wb") as f:
                f.write(data)
            print("{:<24} {:>10,} B".format(name, len(data)))
        except Exception as exc:
            print("{:<24} FAILED: {}".format(name, exc))
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
