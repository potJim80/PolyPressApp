#!/usr/bin/env bash
#
# Merge a standalone project repository into this one, under a directory,
# keeping its full history.
#
#   scripts/sync-app.sh handrail /Users/you/Desktop/ideas/handrail master
#
# Safe to run again: the first run creates the directory, every run after it
# brings in whatever is new. There is one mechanism, not two.
#
# This is what `git subtree` does, written out. The Command Line Tools ship a
# git without the `subtree` subcommand, which is why it is written out here
# rather than called — and writing it out has the side benefit that you can
# read what it does to your history before you run it.
set -euo pipefail

prefix="${1:?usage: sync-app.sh <prefix> <repo-path-or-url> [branch]}"
source_repo="${2:?usage: sync-app.sh <prefix> <repo-path-or-url> [branch]}"
branch="${3:-main}"
remote="src-${prefix}"

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "The working tree is not clean. Commit or stash first." >&2
  exit 1
fi

git remote get-url "$remote" >/dev/null 2>&1 || git remote add "$remote" "$source_repo"
git remote set-url "$remote" "$source_repo"
git fetch --quiet "$remote" "$branch"

ref="$remote/$branch"

if [ -d "$prefix" ]; then
  # Already here. Merge the new commits, then take that branch's tree wholesale
  # for this prefix -- so a file deleted over there is deleted here too.
  if git merge-base --is-ancestor "$ref" HEAD 2>/dev/null; then
    echo "$prefix is already up to date with $ref."
    exit 0
  fi
  git merge -s ours --no-commit --allow-unrelated-histories "$ref" >/dev/null
else
  git merge -s ours --no-commit --allow-unrelated-histories "$ref" >/dev/null
fi

git rm -r --quiet --cached --ignore-unmatch "$prefix" >/dev/null
git read-tree --prefix="$prefix/" -u "$ref"
git commit --quiet -m "Sync $prefix from $ref"

echo "Merged $ref into $prefix/. Review with:  git show --stat HEAD"
echo "Then:  git push origin $(git rev-parse --abbrev-ref HEAD)"
