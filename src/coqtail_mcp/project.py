"""Project-file discovery for Rocq sessions.

This mirrors Coqtail's Vim-side project handling:

* check explicit directories for ``_CoqProject`` / ``_RocqProject`` files,
  then search upward;
* parse only the options that affect an interactive Rocq process;
* prefer project files over Dune by default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


DEFAULT_PROJECT_NAMES = ["_CoqProject", "_RocqProject"]
DEFAULT_PROJECT_SEARCH_DIRS = [".", "./theories"]
BUILD_SYSTEMS = {"prefer-dune", "prefer-coqproject", "dune", "coqproject"}


class ProjectError(RuntimeError):
    """Raised when project configuration cannot be interpreted."""


@dataclass(frozen=True)
class ProjectConfig:
    """Resolved project settings for one Rocq session."""

    build_system: str
    project_search_dirs: List[str]
    project_files: List[str]
    coqproject_args: List[str]
    extra_args: List[str]
    launch_args: List[str]
    in_dune_project: bool
    use_dune: bool
    dune_project_file: Optional[str] = None


def resolve_project_config(
    filename: Optional[str],
    *,
    extra_args: Optional[Sequence[str]] = None,
    build_system: str = "prefer-coqproject",
    project_names: Optional[Sequence[str]] = None,
    project_search_dirs: Optional[Sequence[str]] = None,
) -> ProjectConfig:
    """Find project settings and decide what to pass to ``coqidetop``.

    ``extra_args`` are always appended last so callers can override project
    defaults, matching Coqtail's ``:RocqStart`` argument behavior.
    """

    explicit_args = [_expand_arg(arg) for arg in (extra_args or [])]
    if build_system not in BUILD_SYSTEMS:
        valid = ", ".join(sorted(BUILD_SYSTEMS))
        raise ProjectError(
            f"invalid build_system {build_system!r}; expected one of: {valid}"
        )

    if filename is None:
        if build_system == "dune":
            raise ProjectError(
                "build_system='dune' requires rocq_start(file_path=...)"
            )
        return ProjectConfig(
            build_system=build_system,
            project_search_dirs=list(
                DEFAULT_PROJECT_SEARCH_DIRS
                if project_search_dirs is None
                else project_search_dirs
            ),
            project_files=[],
            coqproject_args=[],
            extra_args=explicit_args,
            launch_args=explicit_args,
            in_dune_project=False,
            use_dune=False,
        )

    start_path = Path(filename).expanduser().resolve()
    names = list(DEFAULT_PROJECT_NAMES if project_names is None else project_names)
    search_dirs = list(
        DEFAULT_PROJECT_SEARCH_DIRS
        if project_search_dirs is None
        else project_search_dirs
    )
    project_files = locate_project_files(start_path, names, search_dirs=search_dirs)
    coqproject_args: List[str] = []
    for project_file in project_files:
        coqproject_args.extend(parse_coqproject(project_file))

    dune_project = locate_upwards(start_path.parent, "dune-project")
    in_dune_project = dune_project is not None

    if build_system == "prefer-dune":
        use_dune = in_dune_project
    elif build_system == "prefer-coqproject":
        use_dune = in_dune_project and not project_files
    elif build_system == "dune":
        use_dune = True
    else:
        use_dune = False

    launch_args = explicit_args if use_dune else coqproject_args + explicit_args
    return ProjectConfig(
        build_system=build_system,
        project_search_dirs=search_dirs,
        project_files=[str(path) for path in project_files],
        coqproject_args=coqproject_args,
        extra_args=explicit_args,
        launch_args=launch_args,
        in_dune_project=in_dune_project,
        use_dune=use_dune,
        dune_project_file=str(dune_project) if dune_project is not None else None,
    )


def locate_project_files(
    start_path: Path,
    project_names: Iterable[str],
    *,
    search_dirs: Optional[Iterable[str]] = None,
) -> List[Path]:
    """Find the first project file for each requested project filename.

    Search directories are checked first, relative to the current working
    directory unless absolute. If a project filename is not found there, fall
    back to Coqtail's upward search from ``start_path``.
    """

    start_dir = start_path if start_path.is_dir() else start_path.parent
    dirs = DEFAULT_PROJECT_SEARCH_DIRS if search_dirs is None else list(search_dirs)
    files: List[Path] = []
    for name in project_names:
        found = locate_in_dirs(dirs, name)
        if found is None:
            found = locate_upwards(start_dir, name)
        if found is not None:
            files.append(found)
    return files


def locate_in_dirs(search_dirs: Iterable[str], name: str) -> Optional[Path]:
    """Search explicit directories for ``name`` in order."""

    for search_dir in search_dirs:
        base = Path(search_dir).expanduser()
        if not base.is_absolute():
            base = Path.cwd() / base
        candidate = (base / name).resolve()
        if candidate.is_file():
            return candidate
    return None


def locate_upwards(start_dir: Path, name: str) -> Optional[Path]:
    """Search ``start_dir`` and its parents for ``name``."""

    cur = start_dir.expanduser().resolve()
    while True:
        candidate = cur / name
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def parse_coqproject(path: Path | str) -> List[str]:
    """Parse a ``_CoqProject``-style file into Rocq process arguments.

    The parser is intentionally narrow, following Coqtail's behavior: it keeps
    load-path options and arguments nested under ``-arg`` while ignoring source
    file names and build-only directives.
    """

    project_path = Path(path).expanduser().resolve()
    raw_args = _parse_args(project_path.read_text(encoding="utf-8"))
    dir_opts = {"-R": 2, "-Q": 2, "-I": 1, "-include": 1}

    args: List[str] = []
    idx = 0
    while idx < len(raw_args):
        arg = raw_args[idx]

        if arg in dir_opts:
            if idx + 1 >= len(raw_args):
                raise ProjectError(f"{project_path}: missing path after {arg}")

            raw_args[idx + 1] = _absolute_dir_arg(
                project_path.parent,
                raw_args[idx + 1],
            )
            end = idx + dir_opts[arg]
            if end >= len(raw_args):
                raise ProjectError(f"{project_path}: incomplete {arg} option")

            if raw_args[end] == "-as" or (
                end + 1 < len(raw_args) and raw_args[end + 1] == "-as"
            ):
                end = idx + 3
                if end >= len(raw_args):
                    raise ProjectError(f"{project_path}: incomplete {arg} -as option")

            args.extend(raw_args[idx : end + 1])
            idx = end

        if idx < len(raw_args) and raw_args[idx] == "-arg":
            if idx + 1 >= len(raw_args):
                raise ProjectError(f"{project_path}: missing value after -arg")
            args.extend(_process_extra_arg(raw_args[idx + 1]))
            idx += 1

        idx += 1

    return args


def _parse_args(text: str) -> List[str]:
    args: List[str] = []
    idx = 0
    while idx < len(text):
        char = text[idx]
        idx += 1
        if char in " \r\n\t":
            continue
        if char == "#":
            idx = _skip_comment(text, idx)
            continue
        if char == '"':
            token, idx = _parse_double_quoted(text, idx)
            args.append(token)
            continue
        token, idx = _parse_unquoted(text, idx)
        args.append(char + token)
    return args


def _skip_comment(text: str, idx: int) -> int:
    while idx < len(text):
        char = text[idx]
        idx += 1
        if char == "\n":
            break
    return idx


def _parse_double_quoted(text: str, idx: int) -> tuple[str, int]:
    buf: List[str] = []
    while idx < len(text):
        char = text[idx]
        idx += 1
        if char == '"':
            break
        buf.append(char)
    return "".join(buf), idx


def _parse_unquoted(text: str, idx: int) -> tuple[str, int]:
    buf: List[str] = []
    while idx < len(text):
        char = text[idx]
        idx += 1
        if char in " \r\n\t":
            break
        if char == "#":
            idx = _skip_comment(text, idx)
        else:
            buf.append(char)
    return "".join(buf), idx


def _process_extra_arg(arg: str) -> List[str]:
    out: List[str] = []
    inside_quotes = False
    has_leftovers = False
    buf: List[str] = []

    for char in arg:
        if char == "'":
            inside_quotes = not inside_quotes
            has_leftovers = True
        elif char == " ":
            if inside_quotes:
                has_leftovers = True
                buf.append(" ")
            elif has_leftovers:
                has_leftovers = False
                out.append("".join(buf))
                buf = []
        else:
            has_leftovers = True
            buf.append(char)

    if has_leftovers:
        out.append("".join(buf))
    return out


def _absolute_dir_arg(project_dir: Path, arg: str) -> str:
    expanded = Path(os.path.expandvars(os.path.expanduser(arg)))
    if not expanded.is_absolute():
        expanded = project_dir / expanded
    return str(expanded.resolve(strict=False))


def _expand_arg(arg: str) -> str:
    return os.path.expandvars(os.path.expanduser(arg))
