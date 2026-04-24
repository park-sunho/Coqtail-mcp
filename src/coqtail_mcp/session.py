"""Session management: one :class:`RocqSession` == one live ``coqidetop`` process.

All Coq-buffer positions are **1-indexed** at the public API boundary (matching
what editors, humans, and AI agents naturally write) and converted to the
0-indexed ``(line, col)`` pairs that Coqtail's internal helpers expect.

The session keeps:
  * ``buffer``       — the source text as ``List[bytes]`` (Coqtail's format)
  * ``endpoints``    — stack of 0-indexed (line, col_after_dot) positions, one
                       per sentence that has been successfully sent
  * ``coqtop``       — the underlying :class:`coqtop.Coqtop` driving the XML
                       protocol

Rewind uses ``edit_at`` under the hood (via :meth:`coqtop.Coqtop.rewind`), which
Rocq translates into a single jump back to the target ``state_id``.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import _COQTAIL_LIB_DIR  # noqa: F401  ensure sys.path shim is applied

import coqtop as CT  # type: ignore  # vendored
import coqtail as CTAIL  # type: ignore  # vendored (for sentence helpers)
from xmlInterface import Goals  # type: ignore  # vendored


class SessionError(RuntimeError):
    """Raised for user-facing errors (session not found, Rocq failed, …)."""


Position = Tuple[int, int]  # 0-indexed (line, col)


@dataclass
class StepResult:
    """Outcome of a :meth:`RocqSession.step_to` call."""

    success: bool
    endpoint: Optional[Tuple[int, int]]  # 1-indexed (line, col_after_dot); None if empty
    sentences_applied: int
    sentences_rewound: int
    messages: List[str] = field(default_factory=list)
    error: Optional[str] = None
    error_range: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None
    # ^^ 1-indexed ((start_line, start_col), (end_line, end_col))
    stderr: str = ""


@dataclass
class QueryResult:
    success: bool
    message: str
    stderr: str = ""


class RocqSession:
    """A single Rocq session backed by a ``coqidetop`` subprocess."""

    def __init__(
        self,
        session_id: str,
        *,
        filename: Optional[str] = None,
        content: str = "",
        coq_path: Optional[str] = None,
        coq_prog: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
        stderr_is_warning: bool = True,
        init_timeout: Optional[int] = 60,
    ) -> None:
        self.session_id = session_id
        # coqidetop wants a path so it can set the top module name (‐topfile).
        # If the caller didn't give one, invent a throwaway that still looks
        # like a .v file so Rocq's module-name validation is happy.
        self.filename = filename or f"Top_{session_id}.v"
        self._coq_path = coq_path
        self._coq_prog = coq_prog
        self._extra_args = list(extra_args or [])
        # Rocq itself often writes harmless banners (e.g. "Welcome to Rocq") to
        # stderr; Coqtail treats unrecognized stderr output as fatal unless
        # this flag is on. For agent use, a warning is the right severity.
        self._stderr_is_warning = stderr_is_warning
        # Guard against the common failure where an agent points ``coq_prog``
        # at the wrong binary (e.g. ``coqtop`` on Rocq 9.x, which no longer
        # speaks the IDE protocol and will hang waiting for text input).
        self._init_timeout = init_timeout

        # Serialize all XML traffic for this session — the Coqtop driver is
        # not itself re-entrant and MCP servers can get concurrent tool calls.
        self._lock = threading.RLock()

        self.buffer: List[bytes] = _make_buffer(content)
        self.endpoints: List[Position] = []  # 0-indexed (line, col_after_dot)
        self.info_messages: List[str] = []

        self._coqtop = CT.Coqtop(add_info_callback=self._collect_info)
        self._started = False
        self.version_info: Optional[Mapping[str, Any]] = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> Mapping[str, Any]:
        with self._lock:
            if self._started:
                raise SessionError(f"session {self.session_id!r} already started")

            ver_or_err = self._coqtop.find_coq(self._coq_path, self._coq_prog)
            if isinstance(ver_or_err, str):
                raise SessionError(f"could not locate Rocq: {ver_or_err}")
            self.version_info = {
                "version": list(ver_or_err["version"]),
                "str_version": ver_or_err["str_version"],
                "latest": ver_or_err["latest"],
            }

            err, stderr = self._coqtop.start(
                self.filename,
                self._extra_args,
                use_dune=False,
                dune_compile_deps=False,
                timeout=self._init_timeout,
                stderr_is_warning=self._stderr_is_warning,
            )
            if err:
                # start() returned a non-None error string
                raise SessionError(
                    f"failed to start Rocq: {err}"
                    + (f"\nstderr: {stderr}" if stderr else "")
                )
            self._started = True
            return {
                "session_id": self.session_id,
                "filename": self.filename,
                "version": self.version_info,
                "startup_stderr": stderr,
            }

    def close(self) -> None:
        with self._lock:
            try:
                self._coqtop.stop()
            finally:
                self._started = False

    # ------------------------------------------------------------------ buffer
    def set_buffer(self, content: str) -> None:
        """Replace the session's source buffer.

        If part of what Rocq has already executed has now changed, we rewind
        to just before the first mismatched byte, mirroring Coqtail's
        :meth:`sync` logic.
        """
        with self._lock:
            newbuf = _make_buffer(content)
            if self.endpoints:
                diff = _diff_lines(self.buffer, newbuf, self.endpoints[-1])
                if diff is not None:
                    dline, dcol = diff
                    self._rewind_to(dline, dcol + 1)
            self.buffer = newbuf

    def buffer_text(self) -> str:
        return b"\n".join(self.buffer).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------- steps
    def step_to(
        self,
        line: int,
        col: Optional[int] = None,
        *,
        admit: bool = False,
    ) -> StepResult:
        """Advance or rewind so everything up to ``(line, col)`` has executed.

        ``line`` is 1-indexed. If ``col`` is ``None``, we target end-of-line
        (inclusive of any dot on that line). ``admit`` follows Coqtail's
        semantics: opaque proofs between ``Proof.`` and ``Qed.``/``Defined.``
        are replaced with a single ``Admitted.`` on the fly.
        """
        if not self._started:
            raise SessionError("session not started")

        with self._lock:
            tline, tcol = _resolve_target(self.buffer, line, col)
            target: Position = (tline, tcol)
            current_end = self.endpoints[-1] if self.endpoints else (0, 0)

            if target < current_end:
                # Coqtail's convention: rewind everything *strictly* after the
                # sentence that ends at ``target``. An endpoint is recorded as
                # ``(line, dot_col+1)``, so we want to pop endpoints where
                # ``(line, dot_col+1) > (tline, tcol+1)`` — equivalently,
                # ``>= (tline, tcol+2)``.
                rewound = self._rewind_to(tline, tcol + 2)
                return StepResult(
                    success=True,
                    endpoint=self._public_endpoint(),
                    sentences_applied=0,
                    sentences_rewound=rewound,
                )

            return self._advance_to(target, admit=admit)

    def _advance_to(self, target: Position, *, admit: bool) -> StepResult:
        to_send: List[Mapping[str, Position]] = []
        eline, ecol = self.endpoints[-1] if self.endpoints else (0, 0)

        unmatched_err: Optional[str] = None
        while True:
            try:
                msg_range = CTAIL._get_message_range(self.buffer, (eline, ecol))
            except CTAIL.NoDotError:
                break
            except CTAIL.UnmatchedError as e:
                if e.range[0] <= target:
                    unmatched_err = str(e)
                break
            if target < msg_range["stop"]:
                break
            to_send.append(msg_range)
            eline, ecol = msg_range["stop"]
            ecol += 1

        applied = 0
        all_msgs: List[str] = []
        all_stderr: List[str] = []
        err_text: Optional[str] = None
        err_range: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None
        self.info_messages = []

        admit_up_to: Optional[Mapping[str, Position]] = None

        for sentence in to_send:
            message = CTAIL._between(
                self.buffer, sentence["start"], sentence["stop"]
            )
            no_comments, _ = CTAIL._strip_comments(message)

            # Opt-in opaque-proof admitting, matching Coqtail's ``to_line(admit=True)``.
            if admit:
                if admit_up_to is None:
                    pstart = CTAIL.PROOF_START_PAT.match(no_comments)
                    if pstart is not None:
                        admit_up_to = CTAIL._find_opaque_proof_end(
                            self.buffer, _as_deque(to_send, after=sentence)
                        )
                elif admit_up_to["stop"] == sentence["stop"]:
                    message = no_comments = b"Admitted."
                    admit_up_to = None
                else:
                    continue

            try:
                ok, msg, err_loc, stderr = self._coqtop.dispatch(
                    message.decode("utf-8"),
                    no_comments.decode("utf-8"),
                    encoding="utf-8",
                    timeout=None,
                    stderr_is_warning=self._stderr_is_warning,
                )
            except CT.CoqtopError as e:
                err_text = str(e)
                break

            if msg:
                all_msgs.append(msg)
            if stderr:
                all_stderr.append(stderr)

            if ok:
                line_, col_ = sentence["stop"]
                self.endpoints.append((line_, col_ + 1))
                applied += 1
            else:
                err_text = msg or "Rocq reported a failure with no message."
                err_range = _derive_error_range(sentence, message, err_loc)
                break

        # Flush any info messages the Coqtop driver collected out-of-band.
        if self.info_messages:
            all_msgs.extend(self.info_messages)
            self.info_messages = []

        if err_text is None and unmatched_err is not None:
            err_text = unmatched_err

        return StepResult(
            success=err_text is None,
            endpoint=self._public_endpoint(),
            sentences_applied=applied,
            sentences_rewound=0,
            messages=all_msgs,
            error=err_text,
            error_range=err_range,
            stderr="\n".join(s for s in all_stderr if s),
        )

    def _rewind_to(self, line: int, col: int) -> int:
        """Rewind so every remaining endpoint is strictly before ``(line, col)``.

        Returns the number of sentences popped. Coqtop.rewind issues a single
        ``Edit_at`` under the hood.
        """
        steps = sum(1 for pos in self.endpoints if pos >= (line, col))
        if steps == 0:
            return 0

        ok, msg, extra_steps, stderr = self._coqtop.rewind(
            steps, stderr_is_warning=self._stderr_is_warning
        )
        if not ok:
            raise SessionError(f"rewind failed: {msg}\n{stderr}".strip())

        extra = extra_steps or 0
        cut = steps + extra
        if cut > 0:
            self.endpoints = self.endpoints[:-cut]
        return steps

    # ------------------------------------------------------------------ goals
    def goals_text(self) -> Tuple[Optional[Goals], str, str]:
        """Return ``(goals, rocq_msg, stderr)`` for the current state.

        ``rocq_msg`` is the Coqtail "info panel" content for this call — any
        warnings, notifications, or side messages Rocq attaches to the
        Subgoals response. Often empty, but never silently dropped.
        """
        if not self._started:
            raise SessionError("session not started")
        with self._lock:
            ok, msg, goals, stderr = self._coqtop.goals(
                timeout=None,
                stderr_is_warning=self._stderr_is_warning,
            )
            # ``ok`` only reflects whether Rocq accepted the Goal call —
            # a proof-less state returns ok=True with goals=None.
            if not ok:
                raise SessionError(f"Goal call failed: {msg}\n{stderr}".strip())
            return goals, msg, stderr

    # ------------------------------------------------------------------- query
    def query(self, cmd: str) -> QueryResult:
        if not self._started:
            raise SessionError("session not started")
        text = cmd.strip()
        if not text.endswith("."):
            text += "."
        with self._lock:
            try:
                ok, msg, _loc, stderr = self._coqtop.dispatch(
                    text,
                    in_script=False,
                    encoding="utf-8",
                    timeout=None,
                    stderr_is_warning=self._stderr_is_warning,
                )
            except CT.CoqtopError as e:
                return QueryResult(success=False, message=str(e), stderr="")
        return QueryResult(success=ok, message=msg, stderr=stderr)

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "filename": self.filename,
            "started": self._started,
            "version": self.version_info,
            "sentences_sent": len(self.endpoints),
            "endpoint": self._public_endpoint(),
            "buffer_lines": len(self.buffer),
            "coq_path": self._coq_path,
            "coq_prog": self._coq_prog,
            "extra_args": list(self._extra_args),
        }

    # ---------------------------------------------------------------- internals
    def _public_endpoint(self) -> Optional[Tuple[int, int]]:
        """Return the 1-indexed end-of-last-sentence, or ``None`` if nothing sent."""
        if not self.endpoints:
            return None
        line, col = self.endpoints[-1]
        return (line + 1, col)

    def _collect_info(self, msg: str) -> None:
        self.info_messages.append(msg)


# ------------------------------------------------------------------- helpers


def _make_buffer(content: str) -> List[bytes]:
    """Encode ``content`` into Coqtail's ``List[bytes]`` buffer format."""
    if content == "":
        return [b""]
    return content.encode("utf-8").split(b"\n")


def _resolve_target(buf: List[bytes], line: int, col: Optional[int]) -> Position:
    """Convert a user-facing (1-indexed line, optional 1-indexed col) to the
    0-indexed form Coqtail expects. When col is omitted, aim at end-of-line.
    """
    if line < 1:
        raise SessionError(f"line must be >= 1, got {line}")
    if line > len(buf):
        raise SessionError(
            f"line {line} is past the buffer (which has {len(buf)} lines)"
        )
    tline = line - 1
    if col is None:
        # End of the line — use (line, len(line)-1) so the terminator dot on
        # that line is included, or 0 if the line is empty.
        tcol = max(0, len(buf[tline]) - 1) if buf[tline] else 0
    else:
        if col < 1:
            raise SessionError(f"col must be >= 1, got {col}")
        tcol = col - 1
    return (tline, tcol)


def _derive_error_range(
    sentence: Mapping[str, Position],
    message: bytes,
    err_loc: Optional[Tuple[int, int]],
) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Translate a byte-offset error range into 1-indexed ``(line, col)`` pairs."""
    sline, scol = sentence["start"]
    eline, ecol = sentence["stop"]
    if err_loc is None or err_loc == (-1, -1):
        return ((sline + 1, scol + 1), (eline + 1, ecol + 1))
    loc_s, loc_e = err_loc
    sl, sc = CTAIL._pos_from_offset(scol, message, loc_s)
    el, ec = CTAIL._pos_from_offset(scol, message, loc_e)
    return ((sline + sl + 1, sc + 1), (sline + el + 1, ec + 1))


def _diff_lines(
    old: List[bytes],
    new: List[bytes],
    stop: Position,
) -> Optional[Position]:
    """Locate the first differing byte within the range Rocq has executed.

    Reimplements Coqtail's ``_diff_lines`` (only the behaviour we rely on).
    """
    eline, ecol = stop
    max_line = min(len(old), len(new), eline + 1)
    for lno in range(max_line):
        a = old[lno]
        b = new[lno]
        # On the last checked line, only compare up to the executed column.
        if lno == eline:
            a = a[:ecol]
            b = b[:ecol]
        if a != b:
            # Find the first differing column.
            common = 0
            for ca, cb in zip(a, b):
                if ca != cb:
                    break
                common += 1
            return (lno, common)
    # Line count changed before the executed prefix
    if len(old) != len(new) and min(len(old), len(new)) <= eline:
        return (min(len(old), len(new)), 0)
    return None


def _as_deque(to_send: List[Mapping[str, Position]], *, after: Mapping[str, Position]):
    """Return a deque of the sentences **after** ``after`` in ``to_send``.

    Coqtail's ``_find_opaque_proof_end`` expects a deque of remaining work.
    """
    from collections import deque

    idx = to_send.index(after)
    return deque(to_send[idx + 1 :])


# ---------------------------------------------------------------- registry


class SessionRegistry:
    """Thread-safe id → :class:`RocqSession` map."""

    def __init__(self) -> None:
        self._by_id: Dict[str, RocqSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> RocqSession:
        with self._lock:
            sid = session_id or uuid.uuid4().hex[:8]
            if sid in self._by_id:
                raise SessionError(f"session id {sid!r} already exists")
            session = RocqSession(sid, **kwargs)
            self._by_id[sid] = session
            return session

    def get(self, session_id: str) -> RocqSession:
        with self._lock:
            s = self._by_id.get(session_id)
        if s is None:
            raise SessionError(f"no such session: {session_id!r}")
        return s

    def drop(self, session_id: str) -> RocqSession:
        with self._lock:
            s = self._by_id.pop(session_id, None)
        if s is None:
            raise SessionError(f"no such session: {session_id!r}")
        return s

    def list_ids(self) -> List[str]:
        with self._lock:
            return list(self._by_id)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._by_id.values())
            self._by_id.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
