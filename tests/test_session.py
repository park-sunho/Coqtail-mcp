"""Smoke tests for :mod:`coqtail_mcp.session`.

Offline tests exercise the buffer/registry logic without touching Rocq.
Live tests spawn a real ``coqidetop`` subprocess; they skip if none is
available on ``$PATH`` (or at ``COQ_PATH`` / ``COQ_PROG`` env vars).

Run with::

    pip install -e . pytest
    pytest tests/
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coqtail_mcp.session import (  # noqa: E402
    RocqSession,
    SessionError,
    SessionRegistry,
    _make_buffer,
    _resolve_target,
)
from coqtail_mcp.formatting import (  # noqa: E402
    apply_line_range,
    format_goals,
    summarize_goals,
    truncate_strings,
)
from xmlInterface import Goal, Goals  # noqa: E402


# ------------------------------------------------------------------ offline


def test_offline_make_buffer_roundtrip() -> None:
    buf = _make_buffer("a\nb\n")
    assert buf == [b"a", b"b", b""]


def test_offline_empty_buffer() -> None:
    assert _make_buffer("") == [b""]


def test_offline_resolve_target_clamps_oversized_line_to_eof() -> None:
    buf = _make_buffer("Theorem t : True.\nProof.\n  exact I.")

    assert _resolve_target(buf, 99, None) == (2, 9)
    assert _resolve_target(buf, 99, 1) == (2, 9)


def test_offline_registry_create_and_drop() -> None:
    reg = SessionRegistry()
    s = reg.create(session_id="foo", content="")
    assert reg.list_ids() == ["foo"]
    reg.drop("foo")
    assert reg.list_ids() == []


def test_offline_registry_missing_raises() -> None:
    reg = SessionRegistry()
    with pytest.raises(SessionError):
        reg.get("nope")


def test_offline_registry_duplicate_id_raises() -> None:
    reg = SessionRegistry()
    reg.create(session_id="x", content="")
    with pytest.raises(SessionError):
        reg.create(session_id="x", content="")


def test_offline_format_goals_no_proof() -> None:
    text = format_goals(None)
    assert "No proof in progress" in text
    summary = summarize_goals(None)
    assert summary["in_proof"] is False


def test_offline_apply_line_range_positive_and_negative() -> None:
    text = "one\ntwo\nthree\nfour\nfive\n"

    ranged, meta = apply_line_range(text, [2, 4])
    assert ranged == "two\nthree\nfour\n"
    assert meta == {
        "requested": [2, 4],
        "resolved": [2, 4],
        "selected": [2, 4],
        "total_lines": 5,
        "truncated": True,
    }

    ranged, meta = apply_line_range(text, [-2, -1])
    assert ranged == "four\nfive\n"
    assert meta["resolved"] == [4, 5]
    assert meta["selected"] == [4, 5]


def test_offline_apply_line_range_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError, match="exactly two"):
        apply_line_range("one\n", [1])
    with pytest.raises(ValueError, match="non-zero"):
        apply_line_range("one\n", [0, 1])
    with pytest.raises(ValueError, match="start must be <= range end"):
        apply_line_range("one\ntwo\n", [-1, -2])


def test_offline_summarize_goals_can_omit_details() -> None:
    goals = Goals(
        [Goal(["H1 : nat", "H2 : bool"], "line 1\nline 2", "g")],
        [],
        [],
        [],
    )

    summary = summarize_goals(goals, include_details=False)
    assert summary["details_included"] is False
    assert summary["fg"] == [
        {"name": "g", "hypothesis_count": 2, "conclusion_line_count": 2}
    ]


def test_offline_summarize_goals_can_slice_hypotheses() -> None:
    goals = Goals(
        [Goal(["H1 : nat", "H2 : bool", "H3 : Prop"], "True", None)],
        [],
        [],
        [],
    )

    summary = summarize_goals(goals, hypothesis_range=[-2, -1])
    assert summary["fg"] == [
        {
            "hypotheses": ["H2 : bool", "H3 : Prop"],
            "conclusion": "True",
            "hypothesis_count": 3,
        }
    ]


def test_offline_truncate_strings_caps_each_string_entry() -> None:
    value = {
        "hypotheses": ["abcdef", "xyz"],
        "conclusion": "long conclusion",
        "nested": [{"name": "goal_name"}],
        "count": 3,
    }

    assert truncate_strings(value, 4) == {
        "hypotheses": ["a...", "xyz"],
        "conclusion": "l...",
        "nested": [{"name": "g..."}],
        "count": 3,
    }
    assert truncate_strings("abcdef", 2) == ".."


def test_offline_server_ok_false_has_brief_error(tmp_path) -> None:
    from coqtail_mcp import server as srv

    missing = tmp_path / "missing.v"

    failures = [
        srv.rocq_start(file_path=str(missing)),
        srv.rocq_close(session_id="missing"),
        srv.rocq_step_to(session_id="missing", line=1),
        srv.rocq_goals(session_id="missing"),
        srv.rocq_query(session_id="missing", query="Check nat"),
        srv.rocq_status(session_id="missing"),
        srv.rocq_goals(session_id="missing", max_chars=0),
        srv.rocq_query(
            session_id="missing",
            query="Check nat",
            max_chars=-1,
        ),
    ]

    for result in failures:
        assert set(result) == {"ok", "error"}
        assert result["ok"] is False
        assert isinstance(result["error"], str)
        assert 0 < len(result["error"]) <= 300

    assert "does not exist" in failures[0]["error"]
    assert "no such session" in failures[1]["error"]
    assert "positive integer" in failures[-1]["error"]


def test_offline_server_ok_false_error_is_compact() -> None:
    from coqtail_mcp import server as srv

    result = srv._err(RuntimeError("first line\n" + ("x" * 400)))

    assert result["ok"] is False
    assert len(result["error"]) == 300
    assert "\n" not in result["error"]
    assert result["error"].endswith("...")


# -------------------------------------------------------------------- live


def _find_rocq() -> tuple[str | None, str | None]:
    """Return (coq_path, coq_prog) suitable for Coqtail's find_coq, or (None, None).

    We return ``coq_prog=None`` so that ``find_coq`` auto-picks the right
    binary (``coqidetop`` on 8.9+, ``coqtop`` on 8.5-8.8, ``rocq repl`` on 9.x
    if ever needed). That's important: on Rocq 9.x, ``coqtop`` no longer
    speaks the IDE protocol and will hang waiting for text input.
    """
    coq_path = os.environ.get("COQ_PATH")
    coq_prog = os.environ.get("COQ_PROG")
    if coq_path:
        return coq_path, coq_prog or None
    for candidate in ("coqidetop", "coqtop", "rocq"):
        exe = shutil.which(candidate)
        if exe:
            return os.path.dirname(exe), None
    # Opam switches we found on this machine during development.
    for hint in (
        "/home/kingdoctor/promising-ir/_opam/bin",
        "/home/kingdoctor/irc11/_opam/bin",
    ):
        if os.path.isfile(os.path.join(hint, "coqidetop")):
            return hint, None
    return None, None


COQ_PATH, COQ_PROG = _find_rocq()
needs_rocq = pytest.mark.skipif(
    COQ_PATH is None,
    reason="no rocq/coqtop found (set COQ_PATH + COQ_PROG to override)",
)


@pytest.fixture
def session() -> RocqSession:
    content = (ROOT / "examples" / "demo.v").read_text(encoding="utf-8")
    s = RocqSession(
        "live",
        filename=str(ROOT / "examples" / "demo.v"),
        content=content,
        coq_path=COQ_PATH,
        coq_prog=COQ_PROG,
    )
    s.start()
    yield s
    s.close()


@needs_rocq
def test_live_start_version(session: RocqSession) -> None:
    assert session.version_info is not None
    assert isinstance(session.version_info["str_version"], str)


@needs_rocq
def test_live_step_to_mid_proof_shows_goal(session: RocqSession) -> None:
    # Advance past `Proof.` and `intros n.` (lines 4 and 5 of demo.v).
    # After line 5 we should be mid-proof with a hypothesis `n : nat`.
    result = session.step_to(line=5)
    assert result.success, f"step failed: {result.error}"
    assert result.sentences_applied >= 1

    goals, _msg, _stderr = session.goals_text()
    text = format_goals(goals)
    summary = summarize_goals(goals)
    assert summary["in_proof"] is True
    # The focused goal should mention the hypothesis `n : nat`.
    fg = summary["fg"][0]
    joined_hyps = "\n".join(fg["hypotheses"])
    assert "n" in joined_hyps and "nat" in joined_hyps, (joined_hyps, text)


@needs_rocq
def test_live_query_check(session: RocqSession) -> None:
    res = session.query("Check nat")
    assert res.success
    assert "nat" in res.message


@needs_rocq
def test_live_server_outputs_are_minimal() -> None:
    """Server tools expose only the compact public response shape."""
    from coqtail_mcp import server as srv

    src = (
        "Theorem t : forall A B C : Prop, A -> B -> C -> A.\n"
        "Proof.\n"
        "  intros A B C HA HB HC.\n"
    )

    r = srv.rocq_start(session_id="minimal_outputs", content=src,
                       coq_path=COQ_PATH, coq_prog=COQ_PROG)
    try:
        assert r["ok"], r
        assert set(r).issubset({"ok", "session_id", "startup_stderr"})
        assert {"ok", "session_id"} <= set(r)
        assert "startup_stderr" not in r or r["startup_stderr"] != ""

        r = srv.rocq_step_to(session_id="minimal_outputs", line=3)
        assert r["success"]
        assert set(r).issubset(
            {"ok", "success", "endpoint", "error", "error_range", "stderr"}
        )
        assert {"ok", "success", "endpoint"} <= set(r)
        assert "error" not in r
        assert "error_range" not in r
        assert "stderr" not in r or r["stderr"] != ""

        r = srv.rocq_step_to(session_id="minimal_outputs", line=999)
        assert r["ok"] and r["success"], r

        r = srv.rocq_query(session_id="minimal_outputs",
                           query="Check no_such_identifier")
        assert set(r).issubset({"ok", "success", "message", "stderr"})
        assert {"ok", "success", "message"} <= set(r)
        assert r["success"] is False
        assert "not found" in r["message"], r["message"]
        assert "stderr" not in r or r["stderr"] != ""

        r = srv.rocq_goals(session_id="minimal_outputs", range=[-1, -1])
        assert set(r).issubset({"ok", "summary", "stderr"})
        assert {"ok", "summary"} <= set(r)
        assert "stderr" not in r or r["stderr"] != ""
        goal = r["summary"]["fg"][0]
        assert "name" not in goal
        assert goal["hypothesis_count"] == 4
        hypotheses = goal["hypotheses"]
        assert len(hypotheses) == 1
        assert "HC" in hypotheses[0]

        r = srv.rocq_goals(
            session_id="minimal_outputs",
            range=[-2, -1],
            max_chars=3,
        )
        goal = r["summary"]["fg"][0]
        assert all(len(hyp) <= 3 for hyp in goal["hypotheses"])
        assert len(goal["conclusion"]) <= 3
        assert goal["hypothesis_count"] == 4

        r = srv.rocq_query(
            session_id="minimal_outputs",
            query="Check nat",
            max_chars=5,
        )
        assert r["success"]
        assert len(r["message"]) <= 5
        assert r["message"].endswith("...")

        r = srv.rocq_status(session_id="minimal_outputs")
        assert r == {"ok": True, "started": True}
    finally:
        srv.rocq_close(session_id="minimal_outputs")


@needs_rocq
def test_live_server_writes_full_output_file(tmp_path) -> None:
    """Side-file output keeps the complete payload before response limits."""
    from coqtail_mcp import server as srv

    src = (
        "Theorem t : forall A B C : Prop, A -> B -> C -> A.\n"
        "Proof.\n"
        "  intros A B C HA HB HC.\n"
    )
    goals_file = tmp_path / "goals.json"
    query_file = tmp_path / "query.json"

    r = srv.rocq_start(session_id="full_output_file", content=src,
                       coq_path=COQ_PATH, coq_prog=COQ_PROG)
    try:
        assert r["ok"], r
        r = srv.rocq_step_to(session_id="full_output_file", line=3)
        assert r["success"], r

        r = srv.rocq_goals(
            session_id="full_output_file",
            range=[-1, -1],
            max_chars=3,
            full_output_file=str(goals_file),
        )
        assert r["ok"], r
        assert r["full_output_written_to"] == str(goals_file.resolve())
        limited_goal = r["summary"]["fg"][0]
        assert len(limited_goal["hypotheses"]) == 1
        assert all(len(hyp) <= 3 for hyp in limited_goal["hypotheses"])

        full_goals = json.loads(goals_file.read_text(encoding="utf-8"))
        full_goal = full_goals["summary"]["fg"][0]
        assert full_goals["ok"] is True
        assert len(full_goal["hypotheses"]) == 4
        assert any("HA" in hyp for hyp in full_goal["hypotheses"])

        r = srv.rocq_query(
            session_id="full_output_file",
            query="Check nat",
            max_chars=5,
            full_output_file=str(query_file),
        )
        assert r["ok"] and r["success"], r
        assert r["full_output_written_to"] == str(query_file.resolve())
        assert len(r["message"]) <= 5

        full_query = json.loads(query_file.read_text(encoding="utf-8"))
        assert full_query["ok"] is True
        assert full_query["success"] is True
        assert "Set" in full_query["message"]
        assert len(full_query["message"]) > len(r["message"])
    finally:
        srv.rocq_close(session_id="full_output_file")


@needs_rocq
def test_live_rewind_roundtrip(session: RocqSession) -> None:
    r1 = session.step_to(line=6)
    assert r1.success
    sent = r1.sentences_applied
    r2 = session.step_to(line=1)
    assert r2.success
    assert r2.sentences_rewound == sent
    assert session.endpoints == []


def test_offline_reload_from_file_requires_source_path() -> None:
    """An inline-content session has no file to reload from."""
    reg = SessionRegistry()
    s = reg.create(session_id="inline", content="Theorem t : 1 = 1.")
    assert s.source_path is None
    with pytest.raises(SessionError, match="inline content"):
        s.reload_buffer_from_file()


def test_offline_reload_from_file_detects_missing_file(tmp_path) -> None:
    """A session whose recorded file has since vanished reports a clean error."""
    reg = SessionRegistry()
    missing = tmp_path / "gone.v"
    s = reg.create(session_id="filed", filename=str(missing), content="")
    assert s.source_path == str(missing)
    with pytest.raises(SessionError, match="no longer exists"):
        s.reload_buffer_from_file()


def test_offline_reload_from_file_refreshes_buffer(tmp_path) -> None:
    """Rewriting the on-disk file and calling reload updates the session buffer."""
    reg = SessionRegistry()
    src = tmp_path / "f.v"
    src.write_text("Theorem a : 1 = 1.\n", encoding="utf-8")
    s = reg.create(session_id="filed", filename=str(src), content=src.read_text())
    assert s.buffer_text().startswith("Theorem a")

    src.write_text("Theorem b : 2 = 2.\n", encoding="utf-8")
    s.reload_buffer_from_file()
    assert s.buffer_text().startswith("Theorem b")


@needs_rocq
def test_live_reload_from_file_via_server(tmp_path) -> None:
    """End-to-end: editing the file on disk and calling rocq_step_to with
    reload_from_file=True picks up the new contents (and reports the failure
    the new version introduces)."""
    from coqtail_mcp import server as srv

    src = tmp_path / "reload.v"
    src.write_text(
        "Theorem t : forall n : nat, n + 0 = n.\n"
        "Proof.\n"
        "  intros n.\n"
        "  induction n as [| n' IH].\n"
        "  - reflexivity.\n"
        "  - simpl. rewrite IH. reflexivity.\n"
        "Qed.\n",
        encoding="utf-8",
    )
    r = srv.rocq_start(session_id="reload_live", file_path=str(src),
                       coq_path=COQ_PATH, coq_prog=COQ_PROG)
    try:
        assert r["ok"], r
        r = srv.rocq_step_to(session_id="reload_live", line=7)
        assert r["ok"] and r["success"], r

        # Break line 6 on disk, rewind, reload, and confirm the failure.
        src.write_text(
            src.read_text().replace("rewrite IH.", "rewrite wrong_ih."),
            encoding="utf-8",
        )
        r = srv.rocq_step_to(session_id="reload_live", line=1)
        assert r["ok"] and r["success"]

        r = srv.rocq_step_to(session_id="reload_live", line=7,
                             reload_from_file=True)
        assert r["ok"]
        assert r["success"] is False
        assert "wrong_ih" in (r["error"] or "")

        # Inline-content session: reload_from_file must produce ok=false.
        srv.rocq_start(session_id="reload_inline", content="Theorem t : 1 = 1.",
                       coq_path=COQ_PATH, coq_prog=COQ_PROG)
        try:
            r = srv.rocq_step_to(session_id="reload_inline", line=1,
                                 reload_from_file=True)
            assert r["ok"] is False
            assert "inline content" in r["error"]
        finally:
            srv.rocq_close(session_id="reload_inline")
    finally:
        srv.rocq_close(session_id="reload_live")
