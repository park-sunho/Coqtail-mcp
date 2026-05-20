---
name: coqtail-mcp
description: "Drive a live Rocq/Coq proof session through the coqtail-mcp MCP server — start a session from a .v file, step forward/backward to any line, inspect the current goal and hypothesis context, and run read-only queries (Check, Print, Search). Trigger when the user wants to step through a Rocq proof interactively, inspect proof state at a specific buffer position, run non-advancing queries, or otherwise drive coqidetop from an agent. Do NOT trigger for Lean 4, Agda, Isabelle, HOL4, Mizar, Idris, or other non-Rocq provers."
---

# coqtail-mcp

This skill teaches the agent how to use the `coqtail-mcp` MCP server,
which spawns `coqidetop` subprocesses and exposes a session-oriented view
over Coqtail's XML protocol. Each session is one live Rocq process; you
advance or rewind it by specifying buffer positions.

The skill covers two things: (1) **driving the server correctly** —
session lifecycle, position semantics, rewind boundaries, error
envelopes — and (2) **using it well for actual proof work** — the
search-first habit, tactic cascade, goal-pattern → tactic table,
common-error fixes, axiom hygiene, and patterns for delegating
parallel work to subagents.

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
- `line=-1` is an explicit EOF target that ignores `col`. Use it when you
  want to run through the whole buffer, especially for files with trailing
  blank lines.
- `line` values past the end of the buffer are accepted and clamped to EOF.
- `col` is optional on `rocq_step_to`; when omitted, the server targets
  end-of-line (inclusive of any terminating `.`).
- Coqtail's semantics are preserved on both sides: a sentence is kept iff
  its closing `.` is at or before `(line, col)`. On rewind, anything
  strictly after that boundary is popped via a single `Edit_at`.
- `endpoint` in responses is 1-indexed `[line, col_after_dot]` (i.e. one
  past the terminator), or `null` if nothing has been executed.

## Working principles

These are not server-enforced — they are practitioner conventions that
make the difference between using this server well and fighting it.

1. **Search before you prove.** `rocq_query("Search ...")` /
   `rocq_query("Check ...")` / `rocq_query("Print ...")` are free —
   they don't consume state. Run at least one search before writing
   any non-trivial tactic. The most common agent failure mode is
   reproving a stdlib lemma from scratch.
2. **Step, don't compile.** Inside the proof loop, prefer
   `rocq_step_to(line=-1, reload_from_file=true)` over `coqc` /
   `make` / `dune build`. Each external compile re-checks the entire
   file from scratch; stepping only re-checks the diff. (See
   `examples/AGENTS_CLAUDE.md` for the rationale; it's a project
   convention, not a server limit.)
3. **Goal-driven, not guess-driven.** Read `rocq_goals` before
   choosing a tactic. The hypothesis list and conclusion shape almost
   always determine the right move. See [proof-recipes.md § 4](references/proof-recipes.md#4-goal-pattern--tactic-mapping)
   for a goal-pattern → tactic table.
4. **The file is the source of truth.** There's no "send a sentence"
   primitive. Tactics get tested by editing the `.v` file and asking
   the server to reload (`reload_from_file=true`). One implication:
   keep the file on disk in sync with what you want Rocq to see.
5. **Don't change theorem statements.** Headers, signatures, and doc
   comments are off-limits without explicit user approval. Same for
   introducing new global `Axiom` / `Parameter` / `Conjecture` —
   if a proof seems to need one, stop and ask.
6. **80-char line width.** Standard Coq/Rocq formatting.

For full proof-craft guidance (search-first protocol, tactic cascade,
error-pattern fixes, axiom hygiene, completion criteria), read
[references/proof-recipes.md](references/proof-recipes.md).

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
- `rocq_step_to` timeout fields: `timed_out`, `timeout_seconds` when a
  per-sentence `step_timeout` fires
- `rocq_goals`: `ok`, `summary`, `stderr`, `full_output_written_to`
- `rocq_query`: `ok`, `success`, `message`, `stderr`, `full_output_written_to`
- `rocq_query` timeout fields: `timed_out`, `timeout_seconds` when
  `query_timeout` fires
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

If `timed_out: true` on `rocq_step_to`, the server intentionally interrupted
the current Rocq sentence after `step_timeout` seconds. The endpoint is the
current cursor position. Retry the same `rocq_step_to` call once to test
progress; if the endpoint advances, continue, and if it stays fixed, inspect
the next sentence for a non-terminating tactic or rerun with a larger
`step_timeout`. If the timed-out sentence is `Qed.`, `Defined.`, or
`Admitted.`, prefer a larger timeout or `step_timeout=0`; proof closing often
takes a long time and is less likely to be an infinite tactic loop.

If `timed_out: true` on `rocq_query`, the query was interrupted after
`query_timeout` seconds. Queries do not advance state. Retry with a larger
`query_timeout`, use `query_timeout=0`, or narrow broad queries such as
`Search` / `Compute`.

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

2. **`step_timeout` is per sentence, not per file.** The default is 30s
   or `$COQTAIL_MCP_STEP_TIMEOUT`; pass `step_timeout=0` to disable. A
   timeout returns `timed_out: true` with the last successful `endpoint`.
   If this happens at `Qed.`, `Defined.`, or `Admitted.`, try a larger
   timeout before treating it as a loop.

3. **`query_timeout` is per query.** The default is 30s or
   `$COQTAIL_MCP_QUERY_TIMEOUT`; pass `query_timeout=0` to disable. Query
   timeouts do not change the session state.

4. **`file_path` must be absolute and must exist** — the server refuses
   relative paths (they can be ambiguous in an MCP client whose CWD is
   not the user's working directory). If you only have a relative path,
   resolve it first with Read/LS or pass `content` directly.

5. **Provide `file_path` OR `content`, not both.** Rocq uses the path
   only to set the top-module name via `-topfile`; it does not re-read
   the file from disk. If you pass `content`, the server writes it to
   an internal buffer and makes up a safe module name.

6. **One session per file (and per Rocq process).** Reusing a
   `session_id` across files gives you Rocq state that thinks it's
   inside the first file's module. Open a fresh session for a different
   `.v` file.

7. **The session's buffer is independent from the agent's Read cache.**
   After you edit a file on disk with Edit/Write, subsequent
   `rocq_step_to` calls still see the buffer the server loaded. Pass
   `reload_from_file=true` on `rocq_step_to` (or open a new session) to
   push changes. `reload_from_file` only works when the session was
   started with `file_path`; inline-content sessions must be reopened.

8. **Project settings are auto-detected for `file_path` sessions.**
   The default `build_system="prefer-coqproject"` uses `_CoqProject`
   or `_RocqProject` files when found, otherwise it falls back to Dune.
   Project-file search first checks `.` and `./theories` relative to
   the current working directory, then searches upward from the file:
   ```
   rocq_start(..., file_path="/abs/proj/src/foo.v")
   ```
   Use `build_system="prefer-dune"`, `"dune"`, or `"coqproject"` to
   control precedence. Pass `extra_args` for final overrides.

9. **Rocq output strings are plain text.** Richpp tags (syntax
   highlighting spans) are stripped from conclusions, hypotheses, query
   messages, and errors.

10. **The file on disk is the unit of work — there is no "send a
   sentence" primitive.** Tactics get tested by editing the `.v` file
   and asking the server to reload it (`rocq_step_to(...,
   reload_from_file=true)`). The server diffs the new buffer against
   the in-memory one and rewinds the minimum needed to stay
   consistent.
   - **Run a tactic at line L** → edit the file, then
     `rocq_step_to(line=L, reload_from_file=true)`.
   - **Backtrack** → step to an earlier `(line, col)`. Rewinds are
     position-based; `reload_from_file=true` handles diff-based
     rewinds automatically.
   - **Type-check the whole file interactively** →
     `rocq_step_to(line=-1, reload_from_file=true)`. Inside the proof
     loop this is preferred over `coqc` / `make` / `dune build`,
     which re-check from scratch every time and are catastrophic for
     tactic search. Reserve those for final/CI confirmation (see
     `examples/AGENTS_CLAUDE.md`).
   - **Verify a finished proof** → step to EOF, then
     `rocq_query("Print Assumptions name.")` to confirm only standard
     axioms are used.
   - **Test N tactic candidates "in parallel"** → see [subagents.md](references/subagents.md);
     each subagent owns its own session.

## Quality gate

A proof is complete (in this server) when **all** of the following
hold:

1. `rocq_step_to(session_id="...", line=-1, reload_from_file=true)`
   returns `success: true` on the file as written. The session *is*
   the type-checker — there's no separate compile step.
2. No `Admitted` / `admit` remains in the agreed scope. (The server
   doesn't track this; do a text scan with Grep.)
3. `rocq_query("Print Assumptions theorem_name.")` lists only
   standard axioms — namely:
   - `classic`, `NNPP` (`Coq.Logic.Classical_Prop`)
   - `functional_extensionality`
     (`Coq.Logic.FunctionalExtensionality`)
   - `propositional_extensionality`
     (`Coq.Logic.PropExtensionality`)
   - `proof_irrelevance` (`Coq.Logic.ProofIrrelevance`)
   - `JMeq_eq` (`Coq.Logic.JMeq`)
   - real-number axioms (`Coq.Reals.Rdefinitions` / `Raxioms`)

   Anything else — especially project-local `Axiom` / `Parameter` /
   `Conjecture` — should be surfaced to the user.
4. No theorem / lemma statement was modified without explicit
   approval.

For final CI verification *outside* the interactive session, `coqc`
or `make` / `dune build` is appropriate. Inside the loop, use
`rocq_step_to` — see the working principles above.

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

- [tools](references/tools.md) — per-tool argument and response
  details (envelope shapes, optional fields, common failures)
- [workflows](references/workflows.md) — extended tool-call recipes
  beyond the canonical examples above (audit, bisect, iterate,
  candidate testing, axiom audit, …)
- [proof-recipes](references/proof-recipes.md) — proof-craft
  guidance using only this server's primitives (search-first, tactic
  cascade, goal-pattern table, error fixes, axiom hygiene,
  completion criteria, house conventions)
- [subagents](references/subagents.md) — when and how to delegate
  to subagents to compensate for the server's serial primitives
  (parallel candidate testing, background compilation, multi-file
  audits, search delegation)
