# Restructure log — 2026-08-04

**Read this if a path looks wrong, or if something that used to work no longer does.**

The repository was reorganised from a flat layout into a four-folder convention:
`memory/`, `IN/`, `OUT/`, `work/`. This was done by Claude Code at the request of the
repository owner's parent, during a general cleanup of the machine. It was **not**
requested by the author of the codec, so if you are that author and this surprises you,
the undo instructions are at the bottom and nothing was committed.

**Nothing was committed.** Everything below sits in the working tree and the git index,
staged as renames. Review with `git status` and `git diff --cached -M` before committing.

---

## Why the layout changed

The convention being applied across this machine is: `memory/` for context and notes,
`IN/` for inputs, `OUT/` for outputs, `work/` for everything else. Applied here:

| Folder | Holds | Rationale |
|---|---|---|
| `memory/` | this log, future working notes | Not code. `CLAUDE.md` stays at the repo root — Claude Code auto-loads it from there, and it stops being read if it moves. |
| `IN/` | the five corpora + the two demo CSVs | They are inputs, they are gitignored, and they are all re-fetchable. |
| `OUT/` | `results/` | Sweep output. The JSONL/summaries/CSVs stay committed — they are the evidence. |
| `work/` | all code, plus `pyproject.toml` and the build dirs | The build lives beside `pyproject.toml`, so `build/`, `dist/` and `*.egg-info` regenerate in `work/`. Putting them in `OUT/` would not have stuck. |

**The single most important consequence: commands are now run from `work/`, not the
repo root.** That is why data paths read `../IN/…` and `../OUT/…`.

---

## What moved

Tracked files moved with `git mv`, so git records renames rather than delete+add.
Gitignored and untracked files moved with plain `mv` (`git mv` refuses them).

```
app/  attic/  benchmarks/  csrc/  docs/  polypress/  tests/       ->  work/
pyproject.toml  tzip.py                                           ->  work/
build/  dist/  polypress.egg-info/  .tmp/                         ->  work/
results/                                                          ->  OUT/results/
corpus/  corpus100/  corpus500/  corpus_matrix/  corpus_hostile/  ->  IN/
polypress-demo.csv  polypress-demo-hard.csv                       ->  IN/
```

Unchanged at the repo root: `.git/`, `.gitignore`, `CLAUDE.md`, `README.md`, `.DS_Store`.

`git status` showed 106 changed entries, all of them renames (`R`).

---

## Every file edited, and exactly what changed

### 1. `work/tests/test_turbo.py` — the only hardcoded filesystem path in the codebase

```python
# before
hostile = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "corpus_hostile")
# after
hostile = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "IN", "corpus_hostile")
```

`__file__` is now one level deeper (`work/tests/…`), so this needs **three** `dirname`
calls to reach the repo root, then `IN/corpus_hostile`.

**This edit was load-bearing and its failure mode is silent.** The code is
`if os.path.isdir(hostile): … else: print("  (corpus_hostile not present)")`. Left
unfixed, `test_turbo.py` would have skipped all 10 adversarial tables and still printed
`all turbo round-trips exact`. Verified after the change: it finds all 10 and processes
them.

### 2. `work/benchmarks/fetch_socrata100.py` — argparse default

```python
ap.add_argument("outdir", nargs="?", default="corpus100")      # before
ap.add_argument("outdir", nargs="?", default="../IN/corpus100")  # after
```

Relative to `work/`, which is where the script is run from.

### 3. `work/benchmarks/run_sweep_500.sh`

Line 33 is `cd "$(dirname "$0")/.."`, which now lands in `work/` — so `benchmarks/…`
still resolves and was left alone. Changed:

- `OUT=results/socrata500.jsonl` → `OUT=../OUT/results/socrata500.jsonl`
- `benchmarks/sweep.py corpus500/*.csv` → `benchmarks/sweep.py ../IN/corpus500/*.csv`
- `--csv results/socrata500-results.csv` → `--csv ../OUT/results/…`
- `> results/socrata500-summary.txt` → `> ../OUT/results/…`
- `tail -40 results/socrata500-summary.txt` → `tail -40 ../OUT/results/…`
- `pgrep -f "fetch_socrata100.py corpus500"` → `pgrep -f "fetch_socrata100.py.*corpus500"`
  — deliberately loosened to `.*` so it matches the fetcher whether it was launched with
  the old bare `corpus500` or the new `../IN/corpus500`. A missed match here silently
  makes the sweep think the fetcher has finished and stop a pass early.

### 4. `work/benchmarks/run_sweep_500_turbo.sh`

Same pattern: `OUT=` → `../OUT/results/`, `corpus500/*.csv` → `../IN/corpus500/*.csv`,
both report output paths → `../OUT/results/`.

### 5. `.gitignore` — every path rewritten

`corpus*/` → `IN/corpus*/`; `results/truncated/` and `results/*.log` →
`OUT/results/…`; `build/`, `dist/`, `csrc/polypress`, `csrc/*.o`, `benchmarks/*.log` →
`work/…`. `*.egg-info/` left as a bare glob. Added `work/.tmp/`.

Verified after the change: all five corpora and all three build dirs still resolve as
ignored, and `OUT/results/socrata500.jsonl` and `OUT/results/all-summary.txt` still
resolve as **not** ignored, i.e. the committed evidence is still tracked.

### 6. `README.md`

Added a layout banner at the top. Rewrote the `## Layout` section. Rewrote every
`corpus*/` → `../IN/corpus*/` and `results/` → `../OUT/results/` in command examples,
via a scripted regex with a lookbehind so already-correct paths were not double-prefixed.
Added `cd work` to the headline example block.

### 7. `CLAUDE.md`

Stayed at the repo root deliberately — Claude Code auto-loads it from there. Rewrote the
`## Layout` block to describe the new tree, added a pointer to this file, and applied the
same path rewrite to its command examples.

### 8. `work/docs/report.py` and `work/tests/test_cbin_corpus.py`

Docstring usage examples updated to `../OUT/results/…` and `../IN/corpus100/…`.

---

## What was deliberately NOT changed

- **Prose in code comments.** `polypress/turbo.py`, `polypress/fast.py`,
  `csrc/ppz_encode.c` and others mention "corpus100" and "the corpus" in commentary
  about measurements. Those are English, not paths.
- **Dict keys.** `docs/report.py` is full of `r["results"][OURS]` — those are JSON keys
  in the sweep records, not directories. A naive find-and-replace across the repo would
  have corrupted them; this is the main reason the rewrite was scoped to docs and the
  handful of real path sites.
- **`benchmarks/`, `tests/`, `csrc/`, `app/`, `docs/`, `tzip.py` references.** They all
  still resolve correctly relative to `work/`, so they were left alone.

---

## Verification actually performed

| Check | Result |
|---|---|
| `import polypress` | OK |
| `python3 tzip.py --help` | OK |
| Round-trip `IN/polypress-demo.csv` (40,000 x 21) | 4.2 MB → 168.5 KB, **byte-exact on restore** |
| `tests/test_fast.py` | all cases round-trip exactly |
| `tests/test_dtz.py` | 18 cases, every strategy and format |
| `tests/test_stream.py` | 180 checks |
| `tests/test_encoding.py` | pass |
| `tests/test_lying_header.py` | pass, 0 signals |
| `tests/test_input_guard.py` | 20/20 |
| `tests/test_turbo.py` | finds all 10 `corpus_hostile` CSVs; all turbo round-trips exact |
| `.gitignore` classification | 5 corpora + 3 build dirs ignored; committed results still tracked |

**Not run:** `test_cbin.py`, `test_fuzz.py`, `test_hostile.py`, `app/gui.py --selftest`,
`csrc/build.sh`, and any full sweep. The sweeps need the network and hours; the C tests
need the binary built. **If you want full confidence, run those.**

---

## How to undo all of it

Nothing was committed, so:

```bash
cd ~/Desktop/polypress
git reset                 # unstage the renames
git stash -u              # or: git checkout -- . && git clean -fd
```

That reverts the tracked moves and the file edits. It does **not** move back the
gitignored data, since git never tracked it. For that:

```bash
cd ~/Desktop/polypress
mv IN/corpus IN/corpus100 IN/corpus500 IN/corpus_matrix IN/corpus_hostile .
mv IN/polypress-demo.csv IN/polypress-demo-hard.csv .
mv work/build work/dist work/polypress.egg-info work/.tmp .
rmdir IN OUT work memory 2>/dev/null
```

---

## Known rough edges

- `work/dist/` and `work/.tmp/` were empty at the time of the move and may not have
  survived as directories.
- The `README.md` rewrite was scripted. It was spot-checked and the layout blocks were
  hand-corrected after the regex over-matched inside them once, but **it is 1,000 lines
  and not every line was read.** If a path in the README looks wrong, it probably is —
  the code is the authority, not the docs.
- `polypress-demo.csv` and `polypress-demo-hard.csv` remain **untracked** in `IN/`, the
  same status they had at the repo root. They were not added to git.

---

## Correction — 2026-08-04, later the same day

**Two statements above are now false. Read this before following the undo
instructions.**

1. **"Nothing was committed" no longer holds.** The restructure was committed
   as part of `9d11d92`, which also recorded the retired forks. The undo
   instructions below assume an uncommitted working tree and will not work as
   written; use `git revert` or `git checkout <path>` against that commit
   instead.

2. **The `old/` directory is gone.** The restructure put the codec in `old/`
   and a second codec in `work/`. That left `CLAUDE.md` describing
   `work/polypress`, `work/csrc` and `work/tests`, none of which existed — and
   it broke `benchmarks/measure_one.py`, which puts `work/` on `sys.path` and
   imports `polypress`. The whole benchmark pipeline raised
   ModuleNotFoundError for a day; `sweep.py --help` still worked, so nothing
   surfaced it.

   The forks were retired on 2026-08-04 (commit `0976d8f`) and the codec moved
   back: `old/{polypress,csrc,tests,app,tzip.py,pyproject.toml}` → `work/`,
   in commit `fb16cea`. Everything inside those directories is relative to
   their common parent, so the move needed no code edits. All 9 test suites
   pass and all 7 benchmark entry points import.

**The layout in `CLAUDE.md` is now the accurate one.** This log describes an
intermediate state that no longer exists.
