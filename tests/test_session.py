"""Smoke tests for :mod:`coqtail_mcp.session`.

Offline tests exercise the buffer/registry logic without touching Rocq.
Live tests spawn a real ``coqidetop`` subprocess; they skip if none is
available on ``$PATH`` (or at ``COQ_PATH`` / ``COQ_PROG`` env vars).

Run with::

    pip install -e . pytest
    pytest tests/
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import shutil
import sys
import threading
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
from xmlInterface import Goal, Goals, TIMEOUT_ERR  # noqa: E402


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


def test_offline_resolve_target_negative_one_means_eof() -> None:
    buf = _make_buffer("Theorem t : True.\nProof.\n  exact I.\n\n")

    assert _resolve_target(buf, -1, None) == (4, 0)
    assert _resolve_target(buf, -1, 1) == (4, 0)


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


def test_offline_server_rejects_invalid_step_timeout() -> None:
    from coqtail_mcp import server as srv

    result = srv.rocq_step_to(
        session_id="missing",
        line=1,
        step_timeout=-1,
    )

    assert result["ok"] is False
    assert "step_timeout" in result["error"]


def test_offline_server_rejects_invalid_query_timeout() -> None:
    from coqtail_mcp import server as srv

    result = srv.rocq_query(
        session_id="missing",
        query="Check nat",
        query_timeout=-1,
    )

    assert result["ok"] is False
    assert "query_timeout" in result["error"]


def test_offline_server_step_timeout_default_is_configurable(monkeypatch) -> None:
    from coqtail_mcp import server as srv

    monkeypatch.delenv(srv.STEP_TIMEOUT_ENV, raising=False)
    assert srv._resolve_step_timeout(None) == srv.DEFAULT_STEP_TIMEOUT_SECONDS

    monkeypatch.setenv(srv.STEP_TIMEOUT_ENV, "9")
    assert srv._resolve_step_timeout(None) == 9
    assert srv._resolve_step_timeout(4) == 4


def test_offline_server_query_timeout_default_is_configurable(monkeypatch) -> None:
    from coqtail_mcp import server as srv

    monkeypatch.delenv(srv.QUERY_TIMEOUT_ENV, raising=False)
    assert srv._resolve_query_timeout(None) == srv.DEFAULT_QUERY_TIMEOUT_SECONDS

    monkeypatch.setenv(srv.QUERY_TIMEOUT_ENV, "11")
    assert srv._resolve_query_timeout(None) == 11
    assert srv._resolve_query_timeout(5) == 5


def test_offline_server_omits_empty_timeout_fields() -> None:
    from coqtail_mcp import server as srv

    result = srv._omit_empty_fields(
        {
            "ok": True,
            "success": True,
            "endpoint": (1, 18),
            "timed_out": None,
            "timeout_seconds": None,
        }
    )

    assert result == {"ok": True, "success": True, "endpoint": (1, 18)}


def test_offline_query_timeout_reports_non_state_changing_timeout() -> None:
    reg = SessionRegistry()
    s = reg.create(session_id="query-timeout", content="")
    s._started = True
    calls: list[tuple[str, int | None]] = []

    def fake_dispatch(
        cmd: str,
        *,
        in_script: bool,
        encoding: str,
        timeout: int | None,
        stderr_is_warning: bool,
    ):
        del encoding, stderr_is_warning
        calls.append((cmd, timeout))
        assert in_script is False
        return False, TIMEOUT_ERR.msg, TIMEOUT_ERR.loc, ""

    s._coqtop.dispatch = fake_dispatch  # type: ignore[method-assign]

    result = s.query("Search nat", query_timeout=6)

    assert result.success is False
    assert result.timed_out is True
    assert result.timeout_seconds == 6
    assert "rocq_query intentionally stopped after 6 seconds" in result.message
    assert "Queries do not advance" in result.message
    assert calls == [("Search nat.", 6)]


def test_offline_server_query_timeout_response(monkeypatch) -> None:
    from coqtail_mcp import server as srv

    reg = SessionRegistry()
    s = reg.create(session_id="server-query-timeout", content="")
    s._started = True
    monkeypatch.setattr(srv, "_registry", reg)

    def fake_dispatch(
        cmd: str,
        *,
        in_script: bool,
        encoding: str,
        timeout: int | None,
        stderr_is_warning: bool,
    ):
        del cmd, in_script, encoding, timeout, stderr_is_warning
        return False, TIMEOUT_ERR.msg, TIMEOUT_ERR.loc, ""

    s._coqtop.dispatch = fake_dispatch  # type: ignore[method-assign]

    result = srv.rocq_query(
        session_id="server-query-timeout",
        query="Search nat",
        query_timeout=8,
    )

    assert result["ok"] is True
    assert result["success"] is False
    assert result["timed_out"] is True
    assert result["timeout_seconds"] == 8
    assert "rocq_query intentionally stopped after 8 seconds" in result["message"]


def test_offline_step_to_timeout_reports_partial_progress() -> None:
    reg = SessionRegistry()
    s = reg.create(
        session_id="timeout",
        content=(
            "Definition a := 0.\n"
            "Definition b := 1.\n"
            "Definition c := 2.\n"
        ),
    )
    s._started = True
    calls: list[tuple[str, int | None]] = []

    def fake_dispatch(
        cmd: str,
        _cmd_no_comment: str,
        *,
        encoding: str,
        timeout: int | None,
        stderr_is_warning: bool,
    ):
        del encoding, stderr_is_warning
        calls.append((cmd, timeout))
        if "Definition b" in cmd:
            return False, TIMEOUT_ERR.msg, TIMEOUT_ERR.loc, ""
        return True, "", None, ""

    s._coqtop.dispatch = fake_dispatch  # type: ignore[method-assign]

    result = s.step_to(line=-1, step_timeout=7)

    assert result.success is False
    assert result.timed_out is True
    assert result.timeout_seconds == 7
    assert result.sentences_applied == 1
    assert result.endpoint == (1, 18)
    assert result.error_range == ((1, 19), (2, 18))
    assert "intentionally stopped after 7 seconds" in (result.error or "")
    assert "line 1, col 18" in (result.error or "")
    assert "endpoint advances" in (result.error or "")
    assert calls == [
        ("Definition a := 0.", 7),
        ("\nDefinition b := 1.", 7),
    ]


def test_offline_step_to_timeout_on_qed_suggests_larger_timeout() -> None:
    reg = SessionRegistry()
    s = reg.create(
        session_id="qed-timeout",
        content=(
            "Theorem t : True.\n"
            "Proof.\n"
            "  exact I.\n"
            "Qed.\n"
        ),
    )
    s._started = True

    def fake_dispatch(
        cmd: str,
        _cmd_no_comment: str,
        *,
        encoding: str,
        timeout: int | None,
        stderr_is_warning: bool,
    ):
        del encoding, timeout, stderr_is_warning
        if "Qed" in cmd:
            return False, TIMEOUT_ERR.msg, TIMEOUT_ERR.loc, ""
        return True, "", None, ""

    s._coqtop.dispatch = fake_dispatch  # type: ignore[method-assign]

    result = s.step_to(line=-1, step_timeout=7)

    assert result.success is False
    assert result.timed_out is True
    assert "appears to close a proof" in (result.error or "")
    assert "step_timeout=0" in (result.error or "")


def test_offline_capture_out_stops_at_stdout_eof() -> None:
    """The reader thread must not spin forever after the backend exits."""
    from coqtail_mcp.session import CT

    allow_second_read = threading.Event()

    class EOFStream(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(b"")
            self.read_count = 0

        def read(self, size: int = -1) -> bytes:
            self.read_count += 1
            if self.read_count > 1:
                allow_second_read.wait(timeout=1)
            return super().read(size)

    coqtop = CT.Coqtop()
    stream = EOFStream()
    reader = threading.Thread(
        target=coqtop.capture_out,
        args=(coqtop.out_q, stream),
        daemon=True,
    )
    reader.start()
    reader.join(timeout=1)
    stopped_at_eof = not reader.is_alive()

    # Let the buggy implementation escape its second read before asserting,
    # so a failing test does not leave a hot background thread behind.
    coqtop.stopping = True
    allow_second_read.set()
    reader.join(timeout=1)

    assert stopped_at_eof
    assert stream.read_count == 1


def test_offline_get_answer_detects_dead_backend() -> None:
    """A dead process must release dispatch rather than retain the session lock."""
    from coqtail_mcp.session import CT

    release = threading.Event()

    class DeadProcess:
        @staticmethod
        def poll() -> int:
            return 1

    class FakeXml:
        warnings_wf = True

    class ControlledQueue:
        @staticmethod
        def empty() -> bool:
            return True

        @staticmethod
        def get(timeout: float) -> bytes:
            del timeout
            if release.wait(timeout=0.05):
                raise RuntimeError("test cleanup")
            raise queue.Empty

    coqtop = CT.Coqtop()
    coqtop.coqtop = DeadProcess()
    coqtop.xml = FakeXml()
    coqtop.out_q = ControlledQueue()
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            coqtop.get_answer()
        except BaseException as exc:  # noqa: BLE001 - assertion captures type
            outcome.append(exc)

    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()
    waiter.join(timeout=1)
    returned_after_death = not waiter.is_alive()
    release.set()
    waiter.join(timeout=1)

    assert returned_after_death
    assert len(outcome) == 1
    assert isinstance(outcome[0], CT.CoqtopError)


def test_offline_mcp_cancel_busy_step_keeps_server_responsive(monkeypatch) -> None:
    """A busy Rocq worker must not block list or cancellation handling."""
    from coqtail_mcp import server as srv

    class BlockingSession:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.aborted = threading.Event()
            self.finished = threading.Event()

        def step_to(self, *_args, **_kwargs):
            try:
                self.started.set()
                self.release.wait(timeout=2)
                raise SessionError("aborted")
            finally:
                self.finished.set()

        def abort(self) -> None:
            self.aborted.set()
            self.release.set()

    class FakeRegistry:
        def __init__(self, session: BlockingSession) -> None:
            self.sessions = {"busy": session}

        def get(self, session_id: str):
            return self.sessions[session_id]

        def drop(self, session_id: str):
            session = self.sessions.pop(session_id, None)
            if session is None:
                raise SessionError(f"no such session: {session_id!r}")
            return session

        def list_ids(self):
            return list(self.sessions)

    session = BlockingSession()
    monkeypatch.setattr(srv, "_registry", FakeRegistry(session))

    async def scenario() -> None:
        step = asyncio.create_task(
            srv.mcp.call_tool(
                "rocq_step_to",
                {"session_id": "busy", "line": 1, "step_timeout": 0},
            )
        )
        assert await asyncio.to_thread(session.started.wait, 2)

        _content, structured = await asyncio.wait_for(
            srv.mcp.call_tool("rocq_list", {}),
            timeout=1,
        )
        assert structured["result"]["session_ids"] == ["busy"]

        step.cancel()
        try:
            await step
        except asyncio.CancelledError:
            pass

        assert session.aborted.wait(timeout=1)
        assert session.finished.wait(timeout=1)
        _content, structured = await asyncio.wait_for(
            srv.mcp.call_tool("rocq_list", {}),
            timeout=1,
        )
        assert structured["result"]["session_ids"] == []

    asyncio.run(scenario())


def test_offline_session_close_aborts_before_waiting_for_traffic_lock() -> None:
    session = RocqSession("close-busy", content="")
    session._started = True
    lock_held = threading.Event()
    backend_aborted = threading.Event()
    release_lock = threading.Event()

    class FakeCoqtop:
        @staticmethod
        def abort() -> None:
            backend_aborted.set()
            release_lock.set()

        @staticmethod
        def stop() -> None:
            pass

    session._coqtop = FakeCoqtop()

    def hold_traffic_lock() -> None:
        with session._lock:
            lock_held.set()
            release_lock.wait(timeout=1)

    holder = threading.Thread(target=hold_traffic_lock, daemon=True)
    holder.start()
    assert lock_held.wait(timeout=1)

    closer = threading.Thread(target=session.close, daemon=True)
    closer.start()
    closer.join(timeout=1)

    assert backend_aborted.is_set()
    assert not closer.is_alive()
    holder.join(timeout=1)


def test_offline_coqtop_timeout_returns_after_successful_interrupt() -> None:
    """A responsive interrupt reports a timeout without killing the backend."""
    from coqtail_mcp.session import CT

    release = threading.Event()
    interrupted = threading.Event()
    killed = threading.Event()

    class AliveProcess:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill() -> None:
            killed.set()

    class FakeXml:
        @staticmethod
        def standardize(_cmd, response):
            return response

    coqtop = CT.Coqtop()
    coqtop.coqtop = AliveProcess()
    coqtop.xml = FakeXml()
    coqtop.empty_out = lambda: None
    coqtop.send_cmd = lambda _msg: None

    def interrupt() -> None:
        interrupted.set()
        release.set()

    coqtop.interrupt = interrupt
    coqtop.get_answer = lambda _stderr=False: (
        release.wait(timeout=1) and (object(), "")
    )

    response, stderr = coqtop.call(("Fake", b"<fake/>"), timeout=0.05)

    assert response is TIMEOUT_ERR
    assert stderr == ""
    assert interrupted.is_set()
    assert not killed.is_set()


def test_offline_coqtop_timeout_force_aborts_unresponsive_reader(monkeypatch) -> None:
    """The interrupt grace period is a hard bound, not an executor wait."""
    import time

    from coqtail_mcp.session import CT

    release = threading.Event()
    interrupted = threading.Event()
    killed = threading.Event()

    class UnresponsiveProcess:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill() -> None:
            killed.set()
            release.set()

    class FakeXml:
        @staticmethod
        def standardize(_cmd, response):
            return response

    coqtop = CT.Coqtop()
    coqtop.coqtop = UnresponsiveProcess()
    coqtop.xml = FakeXml()
    coqtop.empty_out = lambda: None
    coqtop.send_cmd = lambda _msg: None
    coqtop.interrupt = interrupted.set
    coqtop.get_answer = lambda _stderr=False: (
        release.wait(timeout=1) and (object(), "")
    )
    monkeypatch.setattr(CT, "INTERRUPT_GRACE_SECONDS", 0.05)

    started = time.monotonic()
    with pytest.raises(CT.CoqtopError, match="terminated"):
        coqtop.call(("Fake", b"<fake/>"), timeout=0.05)
    elapsed = time.monotonic() - started

    assert interrupted.is_set()
    assert killed.is_set()
    assert elapsed < 1


@pytest.mark.parametrize("operation", ["step", "query", "goals", "rewind"])
def test_offline_transport_error_marks_session_stopped(operation: str) -> None:
    """A fatal backend error must not leave a racy started status behind."""
    from coqtail_mcp.session import CT

    session = RocqSession("transport-error", content="Check nat.")
    session._started = True

    def fail_dispatch(*_args, **_kwargs):
        raise CT.CoqtopError("backend terminated")

    session._coqtop.running = lambda: True

    if operation in {"step", "query"}:
        session._coqtop.dispatch = fail_dispatch
        result = (
            session.step_to(line=-1)
            if operation == "step"
            else session.query("Check nat")
        )
        assert result.success is False
    elif operation == "goals":
        session._coqtop.goals = fail_dispatch
        with pytest.raises(CT.CoqtopError, match="terminated"):
            session.goals_text()
    else:
        session.endpoints = [(0, 10)]
        session._coqtop.rewind = fail_dispatch
        with pytest.raises(CT.CoqtopError, match="terminated"):
            session.step_to(line=1, col=1)

    assert session.status()["started"] is False


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
def test_live_cancel_looping_tactic_recovers_mcp_server() -> None:
    """Cancellation must kill a looping backend and leave lifecycle tools usable."""
    from coqtail_mcp import server as srv

    source = "Ltac loop := loop.\nGoal True.\nProof.\n  loop.\n"

    async def call(name: str, arguments: dict):
        _content, structured = await srv.mcp.call_tool(name, arguments)
        return structured["result"]

    async def scenario() -> None:
        started = await call(
            "rocq_start",
            {
                "session_id": "cancel_loop_live",
                "content": source,
                "coq_path": COQ_PATH,
                "coq_prog": COQ_PROG,
            },
        )
        assert started["ok"], started
        session = srv._registry.get("cancel_loop_live")
        try:
            prefix = await call(
                "rocq_step_to",
                {
                    "session_id": "cancel_loop_live",
                    "line": 3,
                    "step_timeout": 5,
                },
            )
            assert prefix["ok"] and prefix["success"], prefix

            command_sent = threading.Event()
            send_cmd = session._coqtop.send_cmd

            def observe_send(cmd: bytes) -> None:
                send_cmd(cmd)
                command_sent.set()

            session._coqtop.send_cmd = observe_send
            busy = asyncio.create_task(
                srv.mcp.call_tool(
                    "rocq_step_to",
                    {
                        "session_id": "cancel_loop_live",
                        "line": 4,
                        # Keep the test bounded even if cancellation regresses.
                        "step_timeout": 3,
                    },
                )
            )
            assert await asyncio.to_thread(command_sent.wait, 2)
            assert not busy.done()
            busy.cancel()
            try:
                await busy
            except asyncio.CancelledError:
                pass

            listed = await asyncio.wait_for(call("rocq_list", {}), timeout=2)
            assert "cancel_loop_live" not in listed["session_ids"]
            for _ in range(30):
                if not session._coqtop.running():
                    break
                await asyncio.sleep(0.1)
            assert not session._coqtop.running()
        finally:
            if "cancel_loop_live" in srv._registry.list_ids():
                srv.rocq_close(session_id="cancel_loop_live")
            elif session._coqtop.running():
                session.abort()

    asyncio.run(scenario())


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
