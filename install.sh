#!/bin/sh
# Install the track-splitter skill for Claude Code.
#
#   ./install.sh                  for every project  (~/.claude/skills/track-splitter)
#   ./install.sh --local [DIR]    for one project    (DIR/.claude/skills/track-splitter, DIR defaults to .)
#   add --copy to copy the files instead of linking to this folder
#
# Linking (the default) means edits here take effect immediately.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
target="$HOME/.claude/skills"
copy=no
while [ $# -gt 0 ]; do
  case "$1" in
    --local) shift; dir="."; if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then dir=$1; shift; fi
             target="$(cd "$dir" && pwd)/.claude/skills" ;;
    --copy) copy=yes; shift ;;
    *) echo "usage: $0 [--local [DIR]] [--copy]" >&2; exit 2 ;;
  esac
done
dest="$target/track-splitter"
if [ -e "$dest" ] || [ -L "$dest" ]; then
  echo "Already installed at $dest. Remove it first to reinstall." >&2
  exit 1
fi
mkdir -p "$target"
if [ "$copy" = yes ]; then
  mkdir "$dest"
  cp -R "$here/SKILL.md" "$here/scripts" "$here/LICENSE" "$dest/"
  echo "Copied to $dest"
else
  ln -s "$here" "$dest"
  echo "Linked $dest -> $here"
fi
command -v uv >/dev/null 2>&1 || echo "Note: the scripts need uv (https://docs.astral.sh/uv/), which was not found on your PATH."
