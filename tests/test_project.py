from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coqtail_mcp.project import (  # noqa: E402
    ProjectError,
    locate_project_files,
    parse_coqproject,
    resolve_project_config,
)


def test_parse_coqproject_loadpath_and_arg_options(tmp_path: Path) -> None:
    subdir = tmp_path / "space subdir"
    subdir.mkdir()
    project = tmp_path / "_CoqProject"
    project.write_text(
        '# comments and source files are ignored\n'
        '-R "space subdir" Top\n'
        "-I .\n"
        '-arg "-w all"\n'
        "IgnoredFile.v\n",
        encoding="utf-8",
    )

    assert parse_coqproject(project) == [
        "-R",
        str(subdir.resolve()),
        "Top",
        "-I",
        str(tmp_path.resolve()),
        "-w",
        "all",
    ]


def test_parse_coqproject_as_form_and_single_quote_arg(tmp_path: Path) -> None:
    project = tmp_path / "_CoqProject"
    project.write_text(
        "-R . -as Top\n"
        "-arg \"-set 'Default Goal Selector=!'\"\n",
        encoding="utf-8",
    )

    assert parse_coqproject(project) == [
        "-R",
        str(tmp_path.resolve()),
        "-as",
        "Top",
        "-set",
        "Default Goal Selector=!",
    ]


def test_locate_project_files_searches_upward_per_name(tmp_path: Path) -> None:
    root_project = tmp_path / "_CoqProject"
    root_project.write_text("-arg root\n", encoding="utf-8")
    child = tmp_path / "child"
    src_dir = child / "src"
    src_dir.mkdir(parents=True)
    child_project = child / "_RocqProject"
    child_project.write_text("-arg child\n", encoding="utf-8")
    source = src_dir / "F.v"
    source.write_text("Check nat.\n", encoding="utf-8")

    assert locate_project_files(source, ["_CoqProject", "_RocqProject"]) == [
        root_project,
        child_project,
    ]


def test_resolve_project_config_prefers_dune_by_default(tmp_path: Path) -> None:
    (tmp_path / "dune-project").write_text("(lang dune 3.0)\n", encoding="utf-8")
    project = tmp_path / "_CoqProject"
    project.write_text("-Q theories My.Project\n", encoding="utf-8")
    source_dir = tmp_path / "theories"
    source_dir.mkdir()
    source = source_dir / "F.v"
    source.write_text("Check nat.\n", encoding="utf-8")

    config = resolve_project_config(str(source), extra_args=["-w", "all"])

    assert config.in_dune_project is True
    assert config.use_dune is True
    assert config.project_files == [str(project.resolve())]
    assert config.launch_args == ["-w", "all"]


def test_resolve_project_config_can_prefer_coqproject(tmp_path: Path) -> None:
    (tmp_path / "dune-project").write_text("(lang dune 3.0)\n", encoding="utf-8")
    (tmp_path / "theories").mkdir()
    project = tmp_path / "_CoqProject"
    project.write_text("-Q theories My.Project\n", encoding="utf-8")
    source = tmp_path / "theories" / "F.v"
    source.write_text("Check nat.\n", encoding="utf-8")

    config = resolve_project_config(
        str(source),
        extra_args=["-w", "all"],
        build_system="prefer-coqproject",
    )

    assert config.use_dune is False
    assert config.launch_args == [
        "-Q",
        str((tmp_path / "theories").resolve()),
        "My.Project",
        "-w",
        "all",
    ]


def test_inline_sessions_do_not_auto_discover_projects() -> None:
    config = resolve_project_config(None, extra_args=["-w", "all"])

    assert config.use_dune is False
    assert config.project_files == []
    assert config.launch_args == ["-w", "all"]


def test_dune_mode_requires_file_path_for_auto_context() -> None:
    with pytest.raises(ProjectError, match="requires rocq_start"):
        resolve_project_config(None, build_system="dune")
