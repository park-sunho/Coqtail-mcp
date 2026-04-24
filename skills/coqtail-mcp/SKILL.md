---
name: coqtail-mcp
description: "Drive a live Rocq/Coq proof session through the coqtail-mcp MCP server — start a session from a .v file, step forward/backward to any line, inspect the current goal and hypothesis context, and run read-only queries (Check, Print, Search). Trigger when the user wants to step through a Rocq proof interactively, inspect proof state at a specific buffer position, run non-advancing queries, or otherwise drive coqidetop from an agent. Do NOT trigger for Lean 4, Agda, Isabelle, HOL4, Mizar, Idris, or other non-Rocq provers. Complements (does not replace) any general Rocq skill: use this for server-specific mechanics — session lifecycle, position semantics, rewind boundaries, error envelopes — not for tactic selection or proof strategy."
---

# coqtail-mcp

This skill teaches the agent how to use the `coqtail-mcp` MCP server,
which spawns `coqidetop` subprocesses and exposes a session-oriented view
over Coqtail's XML protocol. Each session is one live Rocq process; you
advance or rewind it by specifying buffer positions.

If the user has a *general* Rocq skill already loaded, that one covers
tactic selection, library search, axiom hygiene, etc. This skill is
strictly about **driving this particular MCP server correctly**.

## Tool summary

All tools accept JSON and return JSON. Rejected tool calls never raise —
they return `{ok: false, error: "brief reason"}`. Always check `ok` before
reading success-specific fields.

| Tool | Purpose |
|------|---------|
| `rocq_start` | Spawn a Rocq session. Pass either `file_path` OR `content`. |
| `rocq_close` | Terminate and forget a session. |
| `rocq_step_to` | Advance OR rewind so the state matches `(line, col)`. |
| `rocq_goals` | Current focused goal + hypotheses, as a structured summary. Optional `range`, `max_chars`, and `full_output_file` controls for large contexts. |
| `rocq_query` | Non-state-changing query (`Check`, `Print`, `Search`, …). Optional `max_chars` and `full_output_file` controls for large output. |
| `rocq_status` | Report whether one session is started. |
| `rocq_list` | List open session ids. |

## Session lifecycle (must be followed in order)

```
rocq_start ─► rocq_step_to* ─► rocq_goals / rocq_query (any order, any time)
                                        │
                                        └─► rocq_step_to (advance further or rewind)
                                                │
                                                ▼
                                          rocq_close
```

Every tool except `rocq_start` and `rocq_list` requires a `session_id` that
`rocq_start` returned. `rocq_close` is mandatory on exit — a leaked session
keeps a `coqidetop` subprocess alive.

## Position convention

- `line` and `col` at the tool boundary are **1-indexed** (line 1 is the
  first line of the file; col 1 is the first column).
- `line` values past the end of the buffer are accepted and clamped to EOF.
- `col` is optional on `rocq_step_to`; when omitted, the server targets
  end-of-line (inclusive of any terminating `.`).
- Coqtail's semantics are preserved on both sides: a sentence is kept iff
  its closing `.` is at or before `(line, col)`. On rewind, anything
  strictly after that boundary is popped via a single `Edit_at`.
- `endpoint` in responses is 1-indexed `[line, col_after_dot]` (i.e. one
  past the terminator), or `null` if nothing has been executed.

## Canonical workflows

### Open a file and inspect the goal mid-proof

```
rocq_start(session_id="t1", file_path="/abs/path/to/proof.v",
           coq_path="/abs/path/to/_opam/bin")
  → { ok: true, session_id: "t1" }

rocq_step_to(session_id="t1", line=12)
  → { ok: true, success: true, endpoint: [12, 8] }

rocq_goals(session_id="t1")
  → { ok: true,
      summary: { in_proof: true, fg: [{hypotheses: ["n : nat"],
                                       conclusion: "n + 0 = n"}], ... } }

rocq_goals(session_id="t1", range=[-5, -1])
  → returns only the final five hypotheses in each focused goal. Positive
    values are 1-indexed; negative values count from the bottom. Each focused
    goal also includes `hypothesis_count`.

rocq_goals(session_id="t1", range=[-5, -1], max_chars=500)
  → every string in the goal response is capped at 500 characters.

rocq_goals(session_id="t1", range=[-5, -1], max_chars=500,
           full_output_file="/tmp/t1-goals.json")
  → response stays capped/ranged, while the file receives the full JSON
    tool payload before range/max_chars are applied.

rocq_close(session_id="t1")
```

`summary.fg[i]` gives the hypothesis list and conclusion as separate
strings if you need to reason about them programmatically.
If the full context might be large, first consider calling
`rocq_goals(..., range=[-20, -1])` or another narrow hypothesis range to save
context. Add `max_chars` when individual hypotheses or conclusions may be
large. Use `full_output_file` when the complete context may be useful but too
large for the tool response.

### Stream through a proof one tactic at a time

Do not send a full `Add` for each keystroke — instead, write each new
tactic into the `.v` file on disk and ask `rocq_step_to` to catch up
with `reload_from_file=true`. The server re-reads the `file_path` that
was supplied to `rocq_start`, diffs it against the in-memory buffer, and
rewinds only what's necessary.

```
# ...edit the .v file on disk...
rocq_step_to(session_id="t1", line=15, reload_from_file=true)
rocq_goals(session_id="t1")
# decide next tactic based on the goal, edit file, repeat
```

This requires the session to have been opened with `file_path` (not
inline `content`). For inline-content sessions, open a fresh session
when the source changes.

### Query without disturbing state

`rocq_query` does not advance the session. Use it for `Check`, `Print`,
`Search`, `Locate`, `About`, `Compute`, etc.

When you need to find a specific hypothesis or fact in a large context,
prefer a targeted `rocq_query` (`Search`, `Check`, `About`, etc.) before
asking `rocq_goals` for the full context.
Use `max_chars` on broad queries such as `Search` to guarantee the `message`
string stays within a fixed character budget. Use `full_output_file` to keep
the in-band response small while writing the complete query result to disk.

```
rocq_query(session_id="t1", query="Search (_ + 0 = _).")
rocq_query(session_id="t1", query="Check plus_n_O")   # trailing dot optional
rocq_query(session_id="t1", query="Search (_ + 0 = _).", max_chars=1000,
           full_output_file="/tmp/t1-search.json")
```

### Skip past an opaque proof you don't want to re-check

`rocq_step_to` takes `admit=true`, which rewrites any opaque
(`Qed.`/`Admitted.`) proof encountered during the advance into a single
`Admitted.`. Useful when stepping far into a file that has expensive
proofs earlier.

```
rocq_step_to(session_id="t1", line=200, admit=true)
```

## Compact responses

The MCP tools intentionally return small envelopes:

- `rocq_start`: `ok`, `session_id`, `startup_stderr`
- `rocq_step_to`: `ok`, `success`, `endpoint`, `error`, `error_range`, `stderr`
- `rocq_goals`: `ok`, `summary`, `stderr`, `full_output_written_to`
- `rocq_query`: `ok`, `success`, `message`, `stderr`, `full_output_written_to`
- `rocq_status`: `ok`, `started`
- Rejected tool calls: `ok`, `error`

Optional fields are omitted when empty or absent. For example, successful
steps omit `error`, `error_range`, and empty `stderr`; `rocq_start` omits empty
`startup_stderr`; unnamed goals omit `name`.
If `ok` is `false`, no other fields are returned.
For `rocq_goals` and `rocq_query`, `max_chars` is a positive integer that caps
each emitted string value independently. Truncated strings end with `...`,
with the suffix included inside the character limit.
Pass `full_output_file` to write the complete tool payload as UTF-8 JSON
before `range` or `max_chars` are applied. The response includes
`full_output_written_to` with the resolved side-file path.

Per-sentence info-panel messages are kept internally by the session layer but
are not exposed through the MCP tool response.

Example:

```
rocq_step_to(session_id="t1", line=5)   # file has: idtac "checkpoint reached".
  → {
      ok: true, success: true,
      endpoint: [5, 35]
    }

rocq_query(session_id="t1", query="Check no_such_name")
  → {
      ok: true, success: false,
      message: "The reference no_such_name was not found …"
    }
```

## Error handling

`ok: false` means the MCP tool itself refused the call (unknown session,
bad argument, …). The response includes a compact `error` string with the
reason.

`ok: true` with `success: false` means Rocq *accepted* the call but
*rejected* the input — e.g. a tactic failure or a syntax error. In that
case `error` holds Rocq's message and `error_range` points at the byte
range in the buffer (1-indexed `[[sl, sc], [el, ec]]`). Do not retry
blindly; inspect `error` and adjust the buffer or tactic before calling
`rocq_step_to` again.

```
{ ok: true, success: false,
  error: "The reference foo was not found in the current environment.",
  error_range: [[42, 1], [42, 12]],
  endpoint: [41, 14] }
```

If a `rocq_step_to` fails mid-batch, the endpoint reflects the last
**successful** sentence — the Rocq state is consistent with that
endpoint, and subsequent `rocq_goals` / `rocq_query` calls will work
against it.

## Gotchas

1. **`coq_prog` should almost always be left blank.** On Rocq ≥ 8.9,
   plain `coqtop` no longer speaks the IDE protocol and will silently
   hang. The server's auto-detection picks `coqidetop` on those
   versions. Pass `coq_prog` only if you know the binary name differs.
   `init_timeout` (default 60s) protects against that mistake.

2. **`file_path` must be absolute and must exist** — the server refuses
   relative paths (they can be ambiguous in an MCP client whose CWD is
   not the user's working directory). If you only have a relative path,
   resolve it first with Read/LS or pass `content` directly.

3. **Provide `file_path` OR `content`, not both.** Rocq uses the path
   only to set the top-module name via `-topfile`; it does not re-read
   the file from disk. If you pass `content`, the server writes it to
   an internal buffer and makes up a safe module name.

4. **One session per file (and per Rocq process).** Reusing a
   `session_id` across files gives you Rocq state that thinks it's
   inside the first file's module. Open a fresh session for a different
   `.v` file.

5. **The session's buffer is independent from the agent's Read cache.**
   After you edit a file on disk with Edit/Write, subsequent
   `rocq_step_to` calls still see the buffer the server loaded. Pass
   `reload_from_file=true` on `rocq_step_to` (or open a new session) to
   push changes. `reload_from_file` only works when the session was
   started with `file_path`; inline-content sessions must be reopened.

6. **Project settings are auto-detected for `file_path` sessions.**
   The default `build_system="prefer-coqproject"` uses `_CoqProject`
   or `_RocqProject` files when found, otherwise it falls back to Dune.
   Project-file search first checks `.` and `./theories` relative to
   the current working directory, then searches upward from the file:
   ```
   rocq_start(..., file_path="/abs/proj/src/foo.v")
   ```
   Use `build_system="prefer-dune"`, `"dune"`, or `"coqproject"` to
   control precedence. Pass `extra_args` for final overrides.

7. **Rocq output strings are plain text.** Richpp tags (syntax
   highlighting spans) are stripped from conclusions, hypotheses, query
   messages, and errors.

## When this skill is NOT the right answer

- **One-shot file compilation** (`coqc` is faster and simpler) — only
  use this server when you need interactive state.
- **Parsing `.v` files statically** — use Grep/Read directly; do not
  spawn a session just to read the buffer back.
- **Non-Rocq provers** — do not attempt to drive Lean, Agda, Isabelle,
  etc. through this server.

## Capability check

Before using the skill's guidance, verify the server is reachable. The
cheapest probe is a no-op that always returns quickly:

```
rocq_list()  → { ok: true, session_ids: [...] }
```

If this call fails at the MCP level, the server isn't registered. Tell
the user to register it (see `examples/mcp_config.json` in the
Coqtail-mcp repo) and stop — do not fall back to silently trying
other tools.

## References

- [tools](references/tools.md) — per-tool argument and response details
- [workflows](references/workflows.md) — extended recipes beyond the
  canonical examples above
