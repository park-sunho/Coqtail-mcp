"""MCP server exposing :class:`RocqSession` to AI agents over stdio.

Tools
-----
``rocq_start``   — spawn a coqidetop subprocess and associate it with a session id
``rocq_close``   — terminate the subprocess and forget the session
``rocq_step_to`` — advance or rewind so the state matches a given line/col
``rocq_goals``   — return the current goal and hypothesis context
``rocq_query``   — run a non-state-changing query (``Check``, ``Print``, …)
``rocq_status``  — inspect one session (bonus, zero-cost helper)
``rocq_list``    — list active sessions

All positions passed to and from these tools are **1-indexed** (line numbers
start at 1, column numbers start at 1).
"""

from __future__ import annotations

import atexit
import os
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .formatting import format_goals, summarize_goals
from .session import SessionError, SessionRegistry

_registry = SessionRegistry()
atexit.register(_registry.close_all)

mcp = FastMCP("coqtail-mcp")


def _err(e: Exception) -> Dict[str, Any]:
    """Standard error envelope. We return structured errors instead of raising
    so the agent gets a JSON result rather than an MCP transport error."""
    return {"ok": False, "error": str(e), "error_type": type(e).__name__}


def _join_info(
    messages: Optional[List[str]],
    stderr: Optional[str] = "",
    error: Optional[str] = None,
) -> str:
    """Roll up everything that would appear in Coqtail's 'info' panel.

    Concatenates per-sentence Rocq response messages, the stderr stream, and
    (on failure) the final error — dropping empties and prefixing stderr so
    it's distinguishable from Rocq's own output, mirroring Coqtail's
    ``print_stderr``.
    """
    parts: List[str] = []
    for m in messages or []:
        if m:
            parts.append(m)
    if stderr:
        parts.append(f"From stderr:\n{stderr}")
    if error:
        parts.append(error)
    return "\n\n".join(parts)


def _resolve_content(
    file_path: Optional[str],
    content: Optional[str],
) -> tuple[str, Optional[str]]:
    """Return ``(buffer_content, resolved_absolute_path)`` from the tool args."""
    if file_path and content is not None:
        raise SessionError("provide either file_path or content, not both")
    if file_path:
        p = Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise SessionError(f"file_path does not exist: {p}")
        return p.read_text(encoding="utf-8"), str(p)
    return (content or ""), None


# ---------------------------------------------------------------- tools


@mcp.tool(
    description=(
        "Start a new Rocq session.\n\n"
        "Provide either `file_path` (loads the file) or `content` (use inline\n"
        "text). Either way, nothing is sent to Rocq yet — use `rocq_step_to`\n"
        "to execute sentences.\n\n"
        "`coq_path` is the directory containing the Rocq binaries. Leave\n"
        "`coq_prog` blank so the server auto-selects `coqidetop` on Rocq\n"
        "≥ 8.9 (plain `coqtop` no longer speaks the IDE protocol on those\n"
        "versions and will hang).\n\n"
        "Project settings are auto-detected for `file_path` sessions. By\n"
        "default, `build_system='prefer-coqproject'` uses project files when\n"
        "found, otherwise it falls back to Dune. Project file search first\n"
        "checks `.` and `./theories`, then searches upward for `_CoqProject`\n"
        "and `_RocqProject`. `extra_args` are appended last and passed through to\n"
        "the Rocq process. `init_timeout` caps the initial\n"
        "handshake in seconds (default 60); pass 0 or a large number to\n"
        "disable it."
    )
)
def rocq_start(
    session_id: Optional[str] = None,
    file_path: Optional[str] = None,
    content: Optional[str] = None,
    coq_path: Optional[str] = None,
    coq_prog: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    build_system: str = "prefer-coqproject",
    project_names: Optional[List[str]] = None,
    project_search_dirs: Optional[List[str]] = None,
    dune_compile_deps: bool = False,
    strict_stderr: bool = False,
    init_timeout: Optional[int] = 60,
) -> Dict[str, Any]:
    try:
        buffer, resolved = _resolve_content(file_path, content)
        session = _registry.create(
            session_id=session_id,
            filename=resolved,
            content=buffer,
            coq_path=coq_path,
            coq_prog=coq_prog,
            extra_args=extra_args,
            build_system=build_system,
            project_names=project_names,
            project_search_dirs=project_search_dirs,
            dune_compile_deps=dune_compile_deps,
            stderr_is_warning=not strict_stderr,
            init_timeout=init_timeout,
        )
        try:
            info = session.start()
        except Exception:
            _registry.drop(session.session_id).close()
            raise
        return {"ok": True, **info}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    description=(
        "Close a session. The underlying coqidetop subprocess is terminated\n"
        "and its state is dropped. Safe to call on an unknown id (returns\n"
        "`ok: false` with a descriptive error)."
    )
)
def rocq_close(session_id: str) -> Dict[str, Any]:
    try:
        session = _registry.drop(session_id)
        session.close()
        return {"ok": True, "session_id": session_id, "closed": True}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    description=(
        "Advance or rewind the session so its state matches a position in\n"
        "the buffer. `line` is 1-indexed; `col` is optional (defaults to\n"
        "end-of-line).\n\n"
        "Semantics match Coqtail's `to_line`: every sentence whose terminator\n"
        "is at or before `(line, col)` will have been executed; everything\n"
        "after it will have been rewound via `Edit_at`.\n\n"
        "If `reload_from_file` is true, the server re-reads the `file_path`\n"
        "originally supplied to `rocq_start` and replaces the buffer first\n"
        "(rewinding the minimum necessary to stay consistent), then applies\n"
        "the step. Fails when the session was opened with inline `content`\n"
        "instead of a `file_path`, or when the file no longer exists.\n\n"
        "If `admit` is true, opaque proofs encountered while advancing are\n"
        "replaced with `Admitted.` — useful for jumping past uninteresting\n"
        "proofs."
    )
)
def rocq_step_to(
    session_id: str,
    line: int,
    col: Optional[int] = None,
    reload_from_file: bool = False,
    admit: bool = False,
) -> Dict[str, Any]:
    try:
        session = _registry.get(session_id)
        if reload_from_file:
            session.reload_buffer_from_file()
        result = session.step_to(line, col, admit=admit)
        return {
            "ok": True,
            "session_id": session_id,
            "success": result.success,
            "endpoint": result.endpoint,
            "sentences_applied": result.sentences_applied,
            "sentences_rewound": result.sentences_rewound,
            "messages": result.messages,
            "error": result.error,
            "error_range": result.error_range,
            "stderr": result.stderr,
            "info": _join_info(result.messages, result.stderr, result.error),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    description=(
        "Return the current proof goal and hypothesis context.\n\n"
        "`text` is a pretty-printed block suitable for display to the user\n"
        "(similar to what `coqtop` would print). `summary` gives a structured\n"
        "view: list of focused goals, each with hypotheses and conclusion,\n"
        "plus counts of background/shelved/admitted goals.\n\n"
        "If no proof is in progress, `text` says so and `summary.in_proof` is\n"
        "false."
    )
)
def rocq_goals(session_id: str) -> Dict[str, Any]:
    try:
        session = _registry.get(session_id)
        goals, message, stderr = session.goals_text()
        return {
            "ok": True,
            "session_id": session_id,
            "text": format_goals(goals),
            "summary": summarize_goals(goals),
            "message": message,
            "stderr": stderr,
            "info": _join_info([message], stderr),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    description=(
        "Run a non-state-changing query (`Check`, `Print`, `Search`, `About`,\n"
        "`Locate`, `Compute`, …). The trailing `.` is optional — it's added\n"
        "if missing.\n\n"
        "Does NOT advance the session. Returns the text Rocq would print."
    )
)
def rocq_query(session_id: str, query: str) -> Dict[str, Any]:
    try:
        session = _registry.get(session_id)
        res = session.query(query)
        return {
            "ok": True,
            "session_id": session_id,
            "success": res.success,
            "message": res.message,
            "stderr": res.stderr,
            "info": _join_info([res.message], res.stderr),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Inspect one session (version, sentence count, endpoint, …).")
def rocq_status(session_id: str) -> Dict[str, Any]:
    try:
        session = _registry.get(session_id)
        return {"ok": True, **session.status()}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="List the ids of all currently-open sessions.")
def rocq_list() -> Dict[str, Any]:
    return {"ok": True, "session_ids": _registry.list_ids()}


# ---------------------------------------------------------------- entrypoint


def _install_signal_handlers() -> None:
    def _handler(signum: int, _frame: Any) -> None:  # pragma: no cover
        _registry.close_all()
        # Re-raise the default behaviour so the process actually exits.
        os.kill(os.getpid(), signal.SIGKILL if signum == signal.SIGKILL else signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Some environments don't let MCP servers install signal handlers
            # (e.g. when running inside an event loop on a non-main thread).
            pass


def main() -> None:
    """CLI entry point — runs the MCP server over stdio."""
    _install_signal_handlers()
    mcp.run()


if __name__ == "__main__":
    main()
