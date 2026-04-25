# Workflow recipes

Concrete patterns for common tasks. Each recipe shows the exact tool
calls an agent should make, with rationale for the unusual choices.
Pair with [tools.md](tools.md) for per-tool argument detail.

---

## 1. Audit a file: run to the end and report errors

Goal: determine whether a `.v` file type-checks, and if not, where.
Don't use `rocq_compile` for this — that's a different server. Here we
step to EOF and read the `error` field.

```
rocq_start(session_id="audit", file_path="/abs/f.v", coq_path="/.../_opam/bin")

r = rocq_step_to(session_id="audit", line=-1)

if r["success"]:
    # File type-checks; endpoint is the end of the last sentence.
    print("OK up to", r["endpoint"])
else:
    print("Failed at", r["error_range"], ":", r["error"])

rocq_close(session_id="audit")
```

**Why `line=-1`?** The response `endpoint` is the last executed sentence,
not a visual cursor. If a file ends with blank lines, a successful EOF step
can still report an endpoint on the final tactic line.

---

## 2. Bisect to the first failing sentence

The previous recipe tells you *where* the first failure is. If the
error range is too coarse (e.g. inside a long proof), narrow it by
stepping in halves:

```
# Invariant: step_to(low) succeeds, step_to(high) fails.
low, high = 1, FAILURE_LINE

while high - low > 1:
    mid = (low + high) // 2
    r = rocq_step_to(session_id="audit", line=mid)
    if r["success"]:
        low = mid
    else:
        high = mid

# high is the first line whose sentence fails.
```

`rocq_step_to` is idempotent — calling it with a line we've already
passed rewinds automatically, so the bisection loop is safe.

---

## 3. Iterate on a single tactic

Goal: the user is stuck on a tactic at line L and wants to try
variations. Keep the buffer on disk authoritative — edit it with
Edit/Write, then re-step:

```
rocq_start(session_id="iter", file_path="/abs/f.v", coq_path="/.../_opam/bin")

# Step to just *before* the tactic of interest, so the goal is visible.
rocq_step_to(session_id="iter", line=L - 1)
rocq_goals(session_id="iter")     # read goal, decide next tactic

# Edit /abs/f.v on disk to use the new tactic, then:
r = rocq_step_to(session_id="iter", line=L, reload_from_file=True)

if r["success"]:
    rocq_goals(session_id="iter")   # inspect goal after new tactic
else:
    # Tactic failed; undo the edit, try a different one.
    ...
```

**Why `reload_from_file`?** The session's internal buffer is independent
of the file on disk. When you edit the file, the session doesn't notice
until you ask it to re-read — `reload_from_file=True` tells the server
to pull fresh contents from the original `file_path`, diff against the
old buffer, and rewind only what's affected. It requires the session to
have been opened with `file_path` (not inline `content`).

---

## 4. Test many tactic candidates against a frozen goal

`rocq_step_to` is destructive (advances the session). For candidate
exploration, either:

**(a) Use a separate session per candidate** — safest but spawns
multiple `coqidetop` processes:

```
for i, cand in enumerate(candidates):
    sid = f"cand_{i}"
    # Build a buffer where the tactic at line L is replaced with `cand`.
    rocq_start(session_id=sid, content=buffer_with(cand), coq_path="...")
    r = rocq_step_to(session_id=sid, line=L)
    if r["success"]:
        winners.append((cand, r))
    rocq_close(session_id=sid)
```

**(b) Single session, rewind between each** — cheaper but serial:

```
rocq_start(session_id="probe", file_path="/abs/f.v", coq_path="...")
for cand in candidates:
    # Write buffer_with(cand) back to /abs/f.v (Edit/Write), then:
    rocq_step_to(session_id="probe", line=L - 1, reload_from_file=True)
    r = rocq_step_to(session_id="probe", line=L)
    if r["success"]:
        winners.append((cand, r))
    # Next iteration's reload_from_file diff will rewind automatically.
rocq_close(session_id="probe")
```

Prefer (b) for a dozen candidates; switch to (a) if the proof prelude is
very large (so re-running it per candidate would dominate). For (b) the
file on disk must be the current candidate before each reload — the
server re-reads from the path supplied to `rocq_start`, so stage the
edit first.

---

## 5. Walk a proof to teach the user what each step does

```
rocq_start(session_id="teach", file_path="/abs/proof.v", coq_path="...")

# Where does the proof start?
r = rocq_query(session_id="teach",
               query="Locate \"Theorem plus_n_O\".")

for line in range(PROOF_START_LINE, PROOF_END_LINE + 1):
    rocq_step_to(session_id="teach", line=line)
    g = rocq_goals(session_id="teach")
    narrate(line, g["text"])       # show the user each step

rocq_close(session_id="teach")
```

---

## 6. Explore the environment without touching the session

`rocq_query` runs against the current state but doesn't change it. Use
this to answer "what's available here?" type questions:

```
rocq_query(session_id="teach", query="Search (_ + 0 = _).")
rocq_query(session_id="teach", query="Print Nat.add.")
rocq_query(session_id="teach", query="Check eq_trans.")
rocq_query(session_id="teach", query="About Nat.")
```

These calls are idempotent and can be issued as many times as needed.

---

## 7. Step past expensive earlier proofs

Long Rocq files often have early proofs that take seconds to re-check.
If your task lives near the bottom of the file, admit the upstream
proofs:

```
rocq_start(session_id="fast", file_path="/abs/big.v", coq_path="...")

# Admit every Qed-closed proof we hit on the way to line 500.
rocq_step_to(session_id="fast", line=500, admit=true)

# From here on, leave admit=false so the proof we actually care about
# is real.
rocq_step_to(session_id="fast", line=520)
rocq_goals(session_id="fast")
```

**Caveat:** `admit=true` changes semantics — any definition relying on
the omitted proofs for *computation* (not just type-checking) will
compute differently. Don't use this mode when the proof body matters
for evaluation.

---

## 8. Handle project settings

For `file_path` sessions, `rocq_start` automatically detects project settings.
The default `build_system="prefer-coqproject"` uses `_CoqProject` or
`_RocqProject` files when found; otherwise it falls back to Dune. Project-file
search first checks `.` and `./theories` relative to the current working
directory, then searches upward from the file. Detected project files pass
their `-Q`/`-R`/`-I`/`-include`/`-arg` options to Rocq.

```
rocq_start(session_id="proj", file_path="/abs/proj/src/foo.v",
           coq_path="/.../_opam/bin")
```

Use these knobs when the default is not what you want:
- `build_system="prefer-dune"` uses Dune when a `dune-project` is found,
  otherwise falls back to project files.
- `build_system="dune"` or `"coqproject"` forces one mode.
- `project_names=["_CoqProject", "_CoqProject.local"]` customizes project
  files.
- `project_search_dirs=[".", "./vendor"]` customizes directories checked
  before upward search.
- `extra_args=[...]` appends final overrides after detected args.

---

## 9. Clean shutdown after an error

Whatever goes wrong, close the session — otherwise a `coqidetop`
process lingers. Wrap the entire flow in a try/finally (or whatever
your harness provides):

```
sid = "work"
rocq_start(session_id=sid, file_path="/abs/f.v", coq_path="...")
try:
    ... do the work ...
finally:
    rocq_close(session_id=sid)
```

If you're uncertain what's still open, `rocq_list()` is cheap; iterate
and close what you find.

---

## 10. When to open multiple concurrent sessions

Legitimate reasons:
- Comparing the same lemma statement under two different `_CoqProject`
  configurations.
- Running a slow proof in one session while exploring a scratch buffer
  in another.
- Parallelising candidate tactics as in recipe 4(a).

Each session is its own `coqidetop` subprocess — expect ~30–100 MB of
resident memory per live session and a few hundred ms startup overhead.
Don't spin up one session per tactic candidate if recipe 4(b) would do.
