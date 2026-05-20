"""MCP server exposing :class:`RocqSession` to AI agents over stdio.

Tools
-----
``rocq_start``   — spawn a coqidetop subprocess and associate it with a session id
``rocq_close``   — terminate the subprocess and forget the session
``rocq_step_to`` — advance or rewind so the state matches a given line/col
``rocq_goals``   — return the current goal and hypothesis context
``rocq_query``   — run a non-state-changing query (``Check``, ``Print``, …)
``rocq_status``  — report whether one session is started
``rocq_list``    — list active sessions

All positions passed to and from these tools are **1-indexed** (line numbers
start at 1, column numbers start at 1). ``rocq_step_to(line=-1)`` targets EOF.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .formatting import summarize_goals, truncate_strings
from .session import SessionError, SessionRegistry

_registry = SessionRegistry()
atexit.register(_registry.close_all)

mcp = FastMCP("coqtail-mcp")

DEFAULT_STEP_TIMEOUT_SECONDS = 30
STEP_TIMEOUT_ENV = "COQTAIL_MCP_STEP_TIMEOUT"
DEFAULT_QUERY_TIMEOUT_SECONDS = 30
QUERY_TIMEOUT_ENV = "COQTAIL_MCP_QUERY_TIMEOUT"


def _brief_error(exc: BaseException, *, limit: int = 300) -> str:
    """Return a compact, single-line diagnostic for rejected tool calls."""
    text = str(exc).strip() or exc.__class__.__name__
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _err(exc: Optional[BaseException] = None) -> Dict[str, Any]:
    """Compact envelope for rejected tool calls."""
    data: Dict[str, Any] = {"ok": False}
    if exc is not None:
        data["error"] = _brief_error(exc)
    return data


def _omit_empty_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop optional response fields when they do not carry information."""
    empty_optional_fields = {
        "error",
        "error_range",
        "full_output_written_to",
        "stderr",
        "startup_stderr",
        "timed_out",
        "timeout_seconds",
    }
    return {
        key: value
        for key, value in data.items()
        if key not in empty_optional_fields or (value is not None and value != "")
    }


def _validate_max_chars(max_chars: Optional[int]) -> None:
    if max_chars is None:
        return
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise SessionError("max_chars must be a positive integer")
    if max_chars < 1:
        raise SessionError("max_chars must be a positive integer")


def _validate_non_negative_int(name: str, value: Optional[int]) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise SessionError(f"{name} must be a non-negative integer")
    if value < 0:
        raise SessionError(f"{name} must be a non-negative integer")


def _resolve_step_timeout(step_timeout: Optional[int]) -> int:
    """Return the per-sentence timeout, honoring the process default."""
    return _resolve_timeout(
        "step_timeout",
        step_timeout,
        STEP_TIMEOUT_ENV,
        DEFAULT_STEP_TIMEOUT_SECONDS,
    )


def _resolve_query_timeout(query_timeout: Optional[int]) -> int:
    """Return the per-query timeout, honoring the process default."""
    return _resolve_timeout(
        "query_timeout",
        query_timeout,
        QUERY_TIMEOUT_ENV,
        DEFAULT_QUERY_TIMEOUT_SECONDS,
    )


def _resolve_timeout(
    arg_name: str,
    override: Optional[int],
    env_name: str,
    default: int,
) -> int:
    if override is not None:
        _validate_non_negative_int(arg_name, override)
        return override

    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise SessionError(f"{env_name} must be a non-negative integer") from exc
    _validate_non_negative_int(env_name, value)
    return value


def _write_full_output_file(
    full_output_file: Optional[str],
    data: Dict[str, Any],
) -> Optional[str]:
    """Write an untruncated UTF-8 JSON payload when requested."""
    if full_output_file is None:
        return None
    if not isinstance(full_output_file, str) or full_output_file.strip() == "":
        raise SessionError("full_output_file must be a non-empty file path")

    path = Path(full_output_file.strip()).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(path)


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
        return _omit_empty_fields(
            {
                "ok": True,
                "session_id": info["session_id"],
                "startup_stderr": info["startup_stderr"],
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    description=(
        "Close a session. The underlying coqidetop subprocess is terminated\n"
        "and its state is dropped. Safe to call on an unknown id (returns\n"
        "`ok: false` with an `error` reason)."
    )
)
def rocq_close(session_id: str) -> Dict[str, Any]:
    try:
        session = _registry.drop(session_id)
        session.close()
        return {"ok": True, "session_id": session_id, "closed": True}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    description=(
        "Advance or rewind the session so its state matches a position in\n"
        "the buffer. `line` is 1-indexed; pass `line=-1` to target EOF\n"
        "(ignoring `col`).\n"
        "Values past the end of the buffer are also clamped to EOF. `col`\n"
        "is optional (defaults to end-of-line).\n\n"
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
        "proofs.\n\n"
        "`step_timeout` caps each individual Rocq sentence, not the whole\n"
        "step_to batch. The default is 30 seconds, or the value of\n"
        "`COQTAIL_MCP_STEP_TIMEOUT`; pass 0 to disable. On timeout, the\n"
        "server interrupts Rocq, leaves the endpoint at the last successful\n"
        "sentence, and returns `timed_out: true` so callers can retry and\n"
        "compare endpoint progress."
    )
)
def rocq_step_to(
    session_id: str,
    line: int,
    col: Optional[int] = None,
    reload_from_file: bool = False,
    admit: bool = False,
    step_timeout: Optional[int] = None,
) -> Dict[str, Any]:
    try:
        resolved_step_timeout = _resolve_step_timeout(step_timeout)
        session = _registry.get(session_id)
        if reload_from_file:
            session.reload_buffer_from_file()
        result = session.step_to(
            line,
            col,
            admit=admit,
            step_timeout=resolved_step_timeout,
        )
        return _omit_empty_fields(
            {
                "ok": True,
                "success": result.success,
                "endpoint": result.endpoint,
                "error": result.error,
                "error_range": result.error_range,
                "stderr": result.stderr,
                "timed_out": result.timed_out or None,
                "timeout_seconds": result.timeout_seconds,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    description=(
        "Return the current proof goal and hypothesis context.\n\n"
        "`summary` gives a structured view: list of focused goals, each with\n"
        "hypotheses and conclusion, plus counts of background/shelved/admitted\n"
        "goals.\n\n"
        "Pass `range=[start, end]` to return only an inclusive range of\n"
        "hypothesis entries for each focused goal. Positive indexes are\n"
        "1-indexed; negative indexes count from the bottom, so `[-5, -1]`\n"
        "returns the last five hypotheses.\n\n"
        "Pass `max_chars=N` to cap every string field in the response to at\n"
        "most N characters, applied separately to each hypothesis,\n"
        "conclusion, name, and stderr string.\n\n"
        "Pass `full_output_file=PATH` to write the complete tool payload as\n"
        "UTF-8 JSON before `range` or `max_chars` are applied. The response\n"
        "then includes `full_output_written_to` with the resolved file path.\n\n"
        "If no proof is in progress, `summary.in_proof` is false."
    )
)
def rocq_goals(
    session_id: str,
    range: Optional[List[int]] = None,
    max_chars: Optional[int] = None,
    full_output_file: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        _validate_max_chars(max_chars)
        session = _registry.get(session_id)
        goals, _message, stderr = session.goals_text()
        response_summary = summarize_goals(goals, hypothesis_range=range)
        full_output_written_to = _write_full_output_file(
            full_output_file,
            _omit_empty_fields(
                {
                    "ok": True,
                    "summary": summarize_goals(goals),
                    "stderr": stderr,
                }
            ),
        )
        return _omit_empty_fields(
            {
                "ok": True,
                "summary": truncate_strings(response_summary, max_chars),
                "stderr": truncate_strings(stderr, max_chars),
                "full_output_written_to": full_output_written_to,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    description=(
        "Run a non-state-changing query (`Check`, `Print`, `Search`, `About`,\n"
        "`Locate`, `Compute`, …). The trailing `.` is optional — it's added\n"
        "if missing.\n\n"
        "Does NOT advance the session. Returns the text Rocq would print.\n\n"
        "Pass `max_chars=N` to cap every string field in the response to at\n"
        "most N characters, applied separately to `message` and `stderr`.\n\n"
        "Pass `full_output_file=PATH` to write the complete tool payload as\n"
        "UTF-8 JSON before `max_chars` is applied. The response then includes\n"
        "`full_output_written_to` with the resolved file path.\n\n"
        "`query_timeout` caps this single query. The default is 30 seconds,\n"
        "or the value of `COQTAIL_MCP_QUERY_TIMEOUT`; pass 0 to disable. On\n"
        "timeout, the server interrupts Rocq and returns `timed_out: true`.\n"
        "Queries do not advance the session state."
    )
)
def rocq_query(
    session_id: str,
    query: str,
    max_chars: Optional[int] = None,
    full_output_file: Optional[str] = None,
    query_timeout: Optional[int] = None,
) -> Dict[str, Any]:
    try:
        _validate_max_chars(max_chars)
        resolved_query_timeout = _resolve_query_timeout(query_timeout)
        session = _registry.get(session_id)
        res = session.query(query, query_timeout=resolved_query_timeout)
        full_output_written_to = _write_full_output_file(
            full_output_file,
            _omit_empty_fields(
                {
                    "ok": True,
                    "success": res.success,
                    "message": res.message,
                    "stderr": res.stderr,
                    "timed_out": res.timed_out or None,
                    "timeout_seconds": res.timeout_seconds,
                }
            ),
        )
        return _omit_empty_fields(
            {
                "ok": True,
                "success": res.success,
                "message": truncate_strings(res.message, max_chars),
                "stderr": truncate_strings(res.stderr, max_chars),
                "full_output_written_to": full_output_written_to,
                "timed_out": res.timed_out or None,
                "timeout_seconds": res.timeout_seconds,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(description="Report whether one session is started.")
def rocq_status(session_id: str) -> Dict[str, Any]:
    try:
        session = _registry.get(session_id)
        return {"ok": True, "started": session.status()["started"]}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


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
