#!/bin/sh
# Remove the track-splitter skill installed by install.sh.
#
#   ./uninstall.sh                  the copy for every project  (~/.claude/skills/track-splitter)
#   ./uninstall.sh --local [DIR]    the copy for one project    (DIR/.claude/skills/track-splitter, DIR defaults to .)
#
# Only the installed link or copy is removed. This folder, and any takes or
# songs the skill produced, are left alone.
set -eu
target="$HOME/.claude/skills"
while [ $# -gt 0 ]; do
  case "$1" in
    --local) shift; dir="."; if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then dir=$1; shift; fi
             target="$(cd "$dir" && pwd)/.claude/skills" ;;
    *) echo "usage: $0 [--local [DIR]]" >&2; exit 2 ;;
  esac
done
dest="$target/track-splitter"
if [ -L "$dest" ]; then
  rm "$dest"
  echo "Removed link $dest"
elif [ -d "$dest" ]; then
  # A copy: make sure it really is this skill before deleting a folder.
  if grep -q '^name: track-splitter$' "$dest/SKILL.md" 2>/dev/null; then
    rm -r "$dest"
    echo "Removed $dest"
  else
    echo "$dest does not look like the track-splitter skill; leaving it alone." >&2
    exit 1
  fi
else
  echo "Not installed at $dest"
fi
