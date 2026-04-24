"""Smoke tests for :mod:`coqtail_mcp.session`.

Offline tests exercise the buffer/registry logic without touching Rocq.
Live tests spawn a real ``coqidetop`` subprocess; they skip if none is
available on ``$PATH`` (or at ``COQ_PATH`` / ``COQ_PROG`` env vars).

Run with::

    pip install -e . pytest
    pytest tests/
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coqtail_mcp.session import RocqSession, SessionRegistry, SessionError, _make_buffer  # noqa: E402
from coqtail_mcp.formatting import apply_line_range, format_goals, summarize_goals  # noqa: E402
from xmlInterface import Goal, Goals  # noqa: E402


# ------------------------------------------------------------------ offline


def test_offline_make_buffer_roundtrip() -> None:
    buf = _make_buffer("a\nb\n")
    assert buf == [b"a", b"b", b""]


def test_offline_empty_buffer() -> None:
    assert _make_buffer("") == [b""]


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
def test_live_info_rollup_carries_idtac_and_errors(session: RocqSession) -> None:
    """Whatever would land in Coqtail's info panel shows up in `info`."""
    from coqtail_mcp import server as srv

    src = (
        "Theorem t : 1 = 1.\n"
        "Proof.\n"
        '  idtac "hello-from-idtac".\n'
        "  reflexivity.\n"
        "Qed.\n"
        "Fail Definition bad : nat := true.\n"
    )

    r = srv.rocq_start(session_id="info_rollup", content=src,
                       coq_path=COQ_PATH, coq_prog=COQ_PROG)
    try:
        assert r["ok"], r

        r = srv.rocq_step_to(session_id="info_rollup", line=5)
        assert r["success"]
        assert "hello-from-idtac" in r["info"], r["info"]

        r = srv.rocq_step_to(session_id="info_rollup", line=6)
        assert r["success"]
        assert "indeed failed" in r["info"], r["info"]

        r = srv.rocq_query(session_id="info_rollup",
                           query="Check no_such_identifier")
        assert r["success"] is False
        assert "not found" in r["info"], r["info"]

        r = srv.rocq_goals(session_id="info_rollup")
        assert "message" in r
        assert "info" in r
    finally:
        srv.rocq_close(session_id="info_rollup")


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
