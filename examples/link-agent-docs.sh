#!/usr/bin/env sh
set -eu

usage() {
  echo "Usage: $0 PROJECT_DIR" >&2
  echo "Create AGENTS.md and CLAUDE.md symlinks in PROJECT_DIR." >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
project_dir=$(CDPATH= cd -- "$1" && pwd -P)

link_doc() {
  name=$1
  src=$script_dir/AGENTS_CLAUDE.md
  dest=$project_dir/$name

  if [ ! -f "$src" ]; then
    echo "Missing source file: $src" >&2
    exit 1
  fi

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
      echo "Already linked: $dest -> $src"
      return
    fi
    echo "Refusing to overwrite existing path: $dest" >&2
    exit 1
  fi

  ln -s "$src" "$dest"
  echo "Linked: $dest -> $src"
}

link_doc AGENTS.md
link_doc CLAUDE.md
