"""Generate a survey-shaped table with realistic skip patterns.

    python3 benchmarks/make_survey.py out.csv [rows]

The hostile suite in make_hostile.py covers the tables Polypress loses on.
This covers the opposite end -- the shape it is actually *for*, and the shape
the intended users have: coded administrative health survey data like NEMSIS
(EMS runs) or NEDS (emergency department visits).

Real datasets of that kind cannot be committed here -- they are large and
several are restricted -- so this reproduces their defining structure:

  * every column is a *code*, not a quantity. "41" in Impression_Code has no
    numeric relationship to "42". The 2D planar predictor is meaningless here
    and the codec correctly declines to use it, so this file exercises the
    reordering path almost exclusively.

  * SKIP PATTERNS, which is the thing that makes survey data compress. Answer
    "No" to Med_Given and the drug name, dose and route columns are all blank
    for that row. One answer determines a whole cascade of others.

  * hierarchies -- State determines County determines Agency.

  * a unique per-row key (PcrKey). This is deliberate bait: a column whose
    values are nearly all distinct scores near-perfect conditional entropy
    purely because each value is seen once, so a naive parent search adopts it
    as everyone's parent and gains nothing. The Miller-Madow correction in
    pick_parents exists to reject exactly this.

What it should find, with no hints: County<-State, Agency<-State,
Med_Name<-Med_Given, Med_Dose<-Med_Name, Med_Route<-Med_Given,
Proc_Attempts<-Proc_Done, Destination<-Transported, Disposition<-
Destination_Type. That tree is the survey's own skip logic, recovered from the
data. Check it with:

    python3 tzip.py compress out.csv && python3 tzip.py info out.csv.ppz
"""

from __future__ import annotations

import csv
import os
import random
import sys

SEED = 11

STATE_COUNTY = {
    "WA": ["King", "Pierce", "Snohomish"],
    "OR": ["Multnomah", "Lane"],
    "CA": ["LosAngeles", "SanDiego", "Alameda"],
}
IMPRESSION_CODE = {
    "ChestPain": ["12", "13"],
    "Trauma": ["41", "42", "43"],
    "Respiratory": ["21", "22"],
    "Behavioral": ["61"],
}

COLUMNS = [
    "PcrKey", "State", "County", "Agency", "Age", "Sex",
    "Dispatch_Reason", "Provider_Impression", "Impression_Code",
    "Med_Given", "Med_Name", "Med_Dose", "Med_Route",
    "Proc_Done", "Proc_Name", "Proc_Attempts",
    "Transported", "Destination", "Destination_Type",
    "Disposition", "Outcome",
]


def build(path: str, nrows: int = 60_000) -> str:
    rnd = random.Random(SEED)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(COLUMNS)
        for i in range(nrows):
            st = rnd.choice(list(STATE_COUNTY))
            county = rnd.choice(STATE_COUNTY[st])
            agency = "{}-EMS-{}".format(st, rnd.randint(1, 6))
            impression = rnd.choice(list(IMPRESSION_CODE))
            code = rnd.choice(IMPRESSION_CODE[impression])

            # skip pattern: "No" blanks the whole medication branch
            med = rnd.choice(["Yes", "No", "No"])
            if med == "Yes":
                mname = rnd.choice(["Aspirin", "Fentanyl", "Albuterol",
                                    "Naloxone"])
                mdose = rnd.choice(["81mg", "100mcg", "2.5mg", "2mg"])
                mroute = rnd.choice(["PO", "IV", "NEB", "IN"])
            else:
                mname = mdose = mroute = ""

            proc = rnd.choice(["Yes", "No", "No", "No"])
            if proc == "Yes":
                pname = rnd.choice(["IV Access", "Intubation", "Splint",
                                    "CPR"])
                pattempts = str(rnd.randint(1, 3))
            else:
                pname = pattempts = ""

            transported = rnd.choice(["Yes", "Yes", "Yes", "No"])
            if transported == "Yes":
                dest = rnd.choice(["Harborview", "StJoseph", "Providence",
                                   "UCSD"])
                dtype = rnd.choice(["Hospital", "TraumaCenter"])
                dispo = "Transported"
            else:
                dest = dtype = ""
                dispo = rnd.choice(["Refused", "TreatedReleased"])

            w.writerow([
                str(900000 + i), st, county, agency,
                str(rnd.randint(1, 98)), rnd.choice(["M", "F"]),
                rnd.choice(["ChestPain", "Fall", "Breathing",
                            "Unresponsive", "MVC"]),
                impression, code,
                med, mname, mdose, mroute,
                proc, pname, pattempts,
                transported, dest, dtype, dispo,
                rnd.choice(["Improved", "Unchanged", "NA"]),
            ])
    return path


def main(argv) -> int:
    path = argv[1] if len(argv) > 1 else "survey.csv"
    nrows = int(argv[2]) if len(argv) > 2 else 60_000
    build(path, nrows)
    print("{:,} rows x {} columns   {:,} B   {}".format(
        nrows, len(COLUMNS), os.path.getsize(path), path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
