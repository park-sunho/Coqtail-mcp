# Tool reference

Per-tool inputs, outputs, and failure modes. Paired with
[SKILL.md](../SKILL.md), which covers the higher-level workflow.

All tools return a JSON object with an `ok` boolean. The primary tools expose
compact envelopes so agents do not spend context on metadata they did not ask
for. Optional fields are omitted when empty or absent; for example empty
`stderr`, empty `startup_stderr`, `error: null`, `error_range: null`, and
unnamed-goal `name: null` are not returned.
When `ok` is `false`, the response includes a compact `error` string with the
reason.

---

## `rocq_start`

Spawn a `coqidetop` subprocess and create a new session.

**Arguments**

| Name           | Type     | Required | Notes |
|----------------|----------|----------|-------|
| `session_id`   | string   | no       | Provide a stable id so follow-up calls can reference it. A random 8-char id is generated if omitted. Must be unique — duplicates fail. |
| `file_path`    | string   | one of   | Absolute path to a `.v` file. Contents are loaded into the session buffer. |
| `content`      | string   | one of   | Inline `.v` source as a string. Mutually exclusive with `file_path`. |
| `coq_path`     | string   | no       | Directory containing the Rocq binaries (e.g. `/home/u/.opam/my-switch/bin`). If omitted, uses `$PATH`. |
| `coq_prog`     | string   | no       | Name of the binary to launch. Leave blank for auto-detection — **setting this wrong causes a hang on Rocq ≥ 8.9**. |
| `extra_args`   | string[] | no       | Appended last to the detected project args and passed through to `coqidetop`; typically `-Q`, `-R`, `-I`, `-w`, etc. |
| `build_system` | string   | no       | Default `prefer-coqproject`. One of `prefer-dune`, `prefer-coqproject`, `dune`, `coqproject`. Controls Dune vs `_CoqProject` precedence for `file_path` sessions. |
| `project_names`| string[] | no       | Project filenames to search for. Defaults to `_CoqProject` and `_RocqProject`. |
| `project_search_dirs` | string[] | no | Directories checked before upward search. Defaults to `.` and `./theories`, resolved relative to the current working directory. |
| `dune_compile_deps` | bool | no       | Default `false`. When using Dune, asks Dune to compile dependencies before launching the toplevel. |
| `strict_stderr`| bool     | no       | Default `false`. When `true`, any unrecognized output on stderr (including Rocq's welcome banner) becomes a fatal error. |
| `init_timeout` | int      | no       | Seconds to wait for the initial handshake (default 60). Pass 0 or a very large number to disable. |

**Returns**

```json
{
  "ok": true,
  "session_id": "t1"
}
```

`startup_stderr` is included only when Rocq writes something meaningful while
starting.

**Common failures**

- `file_path does not exist: /...` — path is relative or wrong
- `provide either file_path or content, not both` — passed both
- `could not locate Rocq: ...` — no binary at `coq_path`/`$PATH`
- `failed to start Rocq: Rocq timed out ...` — wrong binary or a dependency build is hanging; rerun with `init_timeout` bumped if you're sure it's slow-but-working

These rejected tool calls return `{ "ok": false, "error": "..." }`.

---

## `rocq_close`

Terminate the subprocess and drop the session from the registry.

**Arguments**

| Name         | Type   | Required | Notes |
|--------------|--------|----------|-------|
| `session_id` | string | yes      | The id returned by `rocq_start`. |

**Returns**

```json
{ "ok": true, "session_id": "t1", "closed": true }
```

**Common failures**

- `no such session: 't1'` — already closed, or never opened

Closing an unknown session is safe and returns `{ "ok": false, "error": "..." }`.
Always close sessions you opened; abandoned sessions keep `coqidetop`
subprocesses alive until server shutdown.

---

## `rocq_step_to`

Advance or rewind the session so the Rocq state matches a buffer
position. This is the only tool that changes session state (besides
admitted proofs triggered by `admit=true`).

**Arguments**

| Name               | Type   | Required | Notes |
|--------------------|--------|----------|-------|
| `session_id`       | string | yes      | |
| `line`             | int    | yes      | 1-indexed. Values past the end of the buffer are clamped to EOF. |
| `col`              | int    | no       | 1-indexed. Defaults to end-of-line (inclusive of any terminator on that line). |
| `reload_from_file` | bool   | no       | Default `false`. When `true`, the server re-reads the `file_path` supplied to `rocq_start` and replaces the buffer before stepping. Only the sentences affected by the diff are rewound. Fails when the session was opened with inline `content` or when the file no longer exists. |
| `admit`            | bool   | no       | Default `false`. When `true`, opaque proofs (`Qed.`/`Admitted.`) encountered during the advance are replaced with `Admitted.`. |

**Returns**

```json
{
  "ok": true,
  "success": true,
  "endpoint": [12, 8]
}
```

- `success` is `false` if Rocq rejected a sentence. `endpoint` still
  reflects the last **successful** position; the session is fully
  consistent with that state.
- On failure, `error` contains Rocq's message and `error_range` is 1-indexed
  `[[start_line, start_col], [end_line, end_col]]`.
- `stderr` is included only when non-empty.

**Semantics**

- A sentence is included iff its closing `.` is at or before `(line, col)`.
- `step_to(L, C)` from a further-along state rewinds to the greatest
  endpoint that still satisfies the above rule.
- The top of a file is reached with `step_to(line=1, col=1)` — it
  rewinds everything.

**Common failures at the envelope level**

- `session not started` — you skipped `rocq_start`
- `line must be >= 1` / `col must be >= 1` — 0-indexed mistake

These rejected tool calls return `{ "ok": false, "error": "..." }`.

---

## `rocq_goals`

Fetch the current proof goal and hypothesis context at the session's
current endpoint.

**Arguments**

| Name         | Type           | Required | Notes |
|--------------|----------------|----------|-------|
| `session_id` | string         | yes      | |
| `range`      | array of 2 ints | no      | Inclusive hypothesis-entry range for each focused goal. Positive values are 1-indexed; negative values count from the bottom, so `[-5, -1]` returns the last five hypotheses. Zero is invalid. |
| `max_chars`  | int            | no       | Positive integer. Caps every emitted string value independently to at most this many characters. Truncated strings end with `...`, included inside the limit. |
| `full_output_file` | string   | no       | File path. Writes the complete tool payload as UTF-8 JSON before `range` or `max_chars` are applied. |

**Returns**

```json
{
  "ok": true,
  "summary": {
    "in_proof": true,
    "fg": [
      { "hypotheses": ["n : nat"],
        "conclusion": "n + 0 = n" }
    ],
    "bg_count": 0,
    "shelved": 0,
    "given_up": 0
  }
}
```

- `range=[start, end]` slices `summary.fg[*].hypotheses` only. Conclusions
  and goal counts are still returned. When `range` is used, each focused goal
  also includes `hypothesis_count`, so agents can tell whether more hypotheses
  existed than were returned.
- `max_chars=N` applies to each string value separately, including each
  hypothesis entry, `conclusion`, goal `name`, and `stderr`.
- `full_output_file=PATH` writes the complete JSON tool payload to `PATH`
  before `range` or `max_chars` are applied. The in-band response still honors
  `range` and `max_chars` and includes `full_output_written_to` with the
  resolved side-file path.
- `name` is included only for named goals.
- `stderr` is included only when non-empty.
- `summary.fg` lists the **focused** goals — those the user's next tactic
  will act on. `summary.bg_count`, `shelved`, `given_up` give counts
  without details (use the MathComp/stdlib tactics `unshelve`, etc. to
  bring shelved goals into focus if needed).
- When no proof is in progress: `summary.in_proof = false`.

---

## `rocq_query`

Run a query that does not change the proof state.

**Arguments**

| Name         | Type   | Required | Notes |
|--------------|--------|----------|-------|
| `session_id` | string | yes      | |
| `query`      | string | yes      | A `Check …`, `Print …`, `Search …`, `About …`, `Locate …`, `Compute …`, etc. The trailing `.` is added if missing. |
| `max_chars`  | int    | no       | Positive integer. Caps every emitted string value independently to at most this many characters. Truncated strings end with `...`, included inside the limit. |
| `full_output_file` | string | no | File path. Writes the complete tool payload as UTF-8 JSON before `max_chars` is applied. |

**Returns**

```json
{
  "ok": true,
  "success": true,
  "message": "nat\n     : Set"
}
```

When Rocq rejects the query (e.g. unknown identifier), `success` is
`false` and `message` contains Rocq's error text. `stderr` is included only
when non-empty.
`max_chars=N` applies separately to `message` and `stderr`.
`full_output_file=PATH` writes the complete JSON tool payload to `PATH`
before `max_chars` is applied. The in-band response still honors `max_chars`
and includes `full_output_written_to` with the resolved side-file path.

**Important**: `rocq_query` does not consume a state_id — it runs
relative to the current position without advancing. Repeated queries
are free.

---

## `rocq_status`

Report whether a session is currently started without modifying it.

**Returns**

```json
{
  "ok": true,
  "started": true
}
```

Useful as a cheap session liveness check.

---

## `rocq_list`

Return the session ids currently tracked by the server.

**Returns**

```json
{ "ok": true, "session_ids": ["t1", "scratch"] }
```

Cheap to call — use it as a health probe before committing to a
longer workflow, or to clean up stale sessions at the start of a new
task:

```
r = rocq_list()
for sid in r["session_ids"]:
    rocq_close(session_id=sid)
```
