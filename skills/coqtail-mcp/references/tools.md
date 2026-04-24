# Tool reference

Per-tool inputs, outputs, and failure modes. Paired with
[SKILL.md](../SKILL.md), which covers the higher-level workflow.

All tools return a JSON object with an `ok` boolean. If `ok` is `false`,
the call itself was rejected; only `error` and `error_type` are
meaningful. If `ok` is `true`, the remaining fields are populated.

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
| `extra_args`   | string[] | no       | Passed through to `coqidetop`; typically `-Q`, `-R`, `-I`, `-w`, etc. |
| `strict_stderr`| bool     | no       | Default `false`. When `true`, any unrecognized output on stderr (including Rocq's welcome banner) becomes a fatal error. |
| `init_timeout` | int      | no       | Seconds to wait for the initial handshake (default 60). Pass 0 or a very large number to disable. |

**Returns**

```json
{
  "ok": true,
  "session_id": "t1",
  "filename": "/abs/path/to/proof.v",
  "version": { "version": [9,1,1], "str_version": "9.1.1", "latest": null },
  "startup_stderr": ""
}
```

**Common failures**

- `file_path does not exist: /...` — path is relative or wrong
- `provide either file_path or content, not both` — passed both
- `could not locate Rocq: ...` — no binary at `coq_path`/`$PATH`
- `failed to start Rocq: Rocq timed out ...` — wrong binary or a dependency build is hanging; rerun with `init_timeout` bumped if you're sure it's slow-but-working

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

Closing an unknown session is safe (just reports the error). Always
close sessions you opened; abandoned sessions keep `coqidetop`
subprocesses alive until server shutdown.

---

## `rocq_step_to`

Advance or rewind the session so the Rocq state matches a buffer
position. This is the only tool that changes session state (besides
admitted proofs triggered by `admit=true`).

**Arguments**

| Name          | Type   | Required | Notes |
|---------------|--------|----------|-------|
| `session_id`  | string | yes      | |
| `line`        | int    | yes      | 1-indexed. Must be within the buffer. |
| `col`         | int    | no       | 1-indexed. Defaults to end-of-line (inclusive of any terminator on that line). |
| `new_content` | string | no       | Replace the session's buffer before stepping. Only the sentences affected by the diff are rewound. |
| `admit`       | bool   | no       | Default `false`. When `true`, opaque proofs (`Qed.`/`Admitted.`) encountered during the advance are replaced with `Admitted.`. |

**Returns**

```json
{
  "ok": true,
  "session_id": "t1",
  "success": true,
  "endpoint": [12, 8],
  "sentences_applied": 3,
  "sentences_rewound": 0,
  "messages": [],
  "error": null,
  "error_range": null,
  "stderr": "",
  "info": ""
}
```

- `success` is `false` if Rocq rejected a sentence. `endpoint` still
  reflects the last **successful** position; the session is fully
  consistent with that state.
- `error_range` is 1-indexed `[[start_line, start_col], [end_line, end_col]]`.
- `messages` is one string per sentence — `idtac` output, warnings,
  notifications, `Fail` directive diagnostics, etc.
- `info` is the rolled-up *Coqtail-info-panel* equivalent:
  `messages ++ stderr ++ error`, newline-separated. Convenient when you
  just want to show the user everything Rocq said that wasn't the goal.

**Semantics**

- A sentence is included iff its closing `.` is at or before `(line, col)`.
- `step_to(L, C)` from a further-along state rewinds to the greatest
  endpoint that still satisfies the above rule.
- The top of a file is reached with `step_to(line=1, col=1)` — it
  rewinds everything.

**Common failures at the envelope level**

- `session not started` — you skipped `rocq_start`
- `line N is past the buffer (which has M lines)` — off-the-end
- `line must be >= 1` / `col must be >= 1` — 0-indexed mistake

---

## `rocq_goals`

Fetch the current proof goal and hypothesis context at the session's
current endpoint.

**Arguments**

| Name         | Type   | Required |
|--------------|--------|----------|
| `session_id` | string | yes      |

**Returns**

```json
{
  "ok": true,
  "session_id": "t1",
  "text": "1 subgoal\n\nn : nat\n\n========================= (1 / 1)\n\nn + 0 = n\n",
  "summary": {
    "in_proof": true,
    "fg": [
      { "name": null,
        "hypotheses": ["n : nat"],
        "conclusion": "n + 0 = n" }
    ],
    "bg_count": 0,
    "shelved": 0,
    "given_up": 0
  },
  "message": "",
  "stderr": "",
  "info": ""
}
```

- `text` is a rendered view close to what `coqtop` prints; preserve line
  breaks if you show it to the user.
- `message` is the `Subgoals` RPC's side-message channel — normally
  empty, but Rocq sometimes attaches diagnostics (e.g. when proof diffs
  are on). Mirrored into `info`.
- `info` rolls up `message` + `stderr` like the other tools, so a single
  field shows anything Rocq printed while answering the Goal call.
- `summary.fg` lists the **focused** goals — those the user's next tactic
  will act on. `summary.bg_count`, `shelved`, `given_up` give counts
  without details (use the MathComp/stdlib tactics `unshelve`, etc. to
  bring shelved goals into focus if needed).
- When no proof is in progress: `text = "No proof in progress."`,
  `summary.in_proof = false`.

---

## `rocq_query`

Run a query that does not change the proof state.

**Arguments**

| Name         | Type   | Required | Notes |
|--------------|--------|----------|-------|
| `session_id` | string | yes      | |
| `query`      | string | yes      | A `Check …`, `Print …`, `Search …`, `About …`, `Locate …`, `Compute …`, etc. The trailing `.` is added if missing. |

**Returns**

```json
{
  "ok": true,
  "session_id": "t1",
  "success": true,
  "message": "nat\n     : Set",
  "stderr": "",
  "info": "nat\n     : Set"
}
```

When Rocq rejects the query (e.g. unknown identifier), `success` is
`false` and `message` contains Rocq's error text. `info` is the
Coqtail-panel rollup — for `rocq_query` it is essentially `message`
plus any stderr, since the query response *is* the info content.

**Important**: `rocq_query` does not consume a state_id — it runs
relative to the current position without advancing. Repeated queries
are free.

---

## `rocq_status`

Inspect metadata about a session without modifying it.

**Returns**

```json
{
  "ok": true,
  "session_id": "t1",
  "filename": "/abs/path/to/proof.v",
  "started": true,
  "version": { "version": [9,1,1], "str_version": "9.1.1", "latest": null },
  "sentences_sent": 3,
  "endpoint": [12, 8],
  "buffer_lines": 42,
  "coq_path": "/home/u/.opam/my-switch/bin",
  "coq_prog": null,
  "extra_args": ["-Q", "/abs/theory/", "MyLib"]
}
```

Useful for debugging — e.g. to confirm the buffer has the expected
number of lines, or to figure out why `rocq_step_to(line=50)` rejects
with "past the buffer".

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
