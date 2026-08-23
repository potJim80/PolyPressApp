#!/usr/bin/env bash
#
# Put this checkout on GitHub, in one command.
#
#   ./push-to-github.sh "what you changed"     commit everything, then publish
#   ./push-to-github.sh                        publish commits already made
#   ./push-to-github.sh --skip-tests "..."     publish without running the tests
#
# Why this exists: this repository has no remote of its own. Handrail lives on
# GitHub inside potJim80/PolyPressApp, under handrail/, merged in with its
# history rather than copied. That merge is a separate step in a separate
# checkout, and forgetting it is how a fortnight of commits stays on one Mac.
#
# What it does NOT do: change the website. Nothing on polypressapp.com reads
# this source. A number on that site changes when someone edits
# PolyPressWebsite/content/handrail.json and files a request. See HANDOFF.md.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mono="${POLYPRESSAPP:-$HOME/Desktop/ideas/PolyPressApp}"
prefix="handrail"
run_tests=1

if [ "${1:-}" = "--skip-tests" ]; then run_tests=0; shift; fi
message="${1:-}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. commit
cd "$here"
branch="$(git rev-parse --abbrev-ref HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  [ -n "$message" ] || die "There are changes to commit, but no message.

  ./push-to-github.sh \"what you changed\"

$(git status --short)"
  say "Committing on $branch"
  git add -A
  git commit -q -m "$message"
  git --no-pager log -1 --format='  %h  %s'
else
  [ -z "$message" ] || echo "Nothing to commit; the message is unused."
  echo "Working tree clean at $(git rev-parse --short HEAD)."
fi

# ----------------------------------------------------------------- 2. tests
# An app that appends to the only copy of someone's script does not get to
# publish code that has not been run.
if [ "$run_tests" = 1 ]; then
  if command -v swift >/dev/null 2>&1; then
    say "Running the tests"
    swift run handrail-test || die "Tests failed. Nothing was published.
Fix them, or publish anyway with:  ./push-to-github.sh --skip-tests"
  else
    echo "swift not found — skipping the tests."
  fi
fi

# ---------------------------------------------------------------- 3. publish
[ -d "$mono/.git" ] || die "No PolyPressApp checkout at: $mono
Point at one with:  POLYPRESSAPP=/path/to/checkout ./push-to-github.sh"

say "Publishing to GitHub via $mono"
cd "$mono"
git remote get-url origin | grep -q 'PolyPressApp' \
  || die "$mono is not a PolyPressApp checkout."

target="$(git rev-parse --abbrev-ref HEAD)"
git fetch -q origin
git merge -q --ff-only "origin/$target" 2>/dev/null \
  || die "$mono is not fast-forwardable onto origin/$target. Sort it out there first."

before="$(git rev-parse HEAD)"
./scripts/sync-app.sh "$prefix" "$here" "$branch"

if [ "$(git rev-parse HEAD)" = "$before" ]; then
  echo "GitHub already had it."
else
  git push -q origin "$target"
  say "Published."
  git --no-pager log -1 --format='  %h  %s'
fi

echo "  https://github.com/potJim80/PolyPressApp/tree/$target/$prefix"
echo
echo "The website is unchanged. It does not read this source — see HANDOFF.md §4."
