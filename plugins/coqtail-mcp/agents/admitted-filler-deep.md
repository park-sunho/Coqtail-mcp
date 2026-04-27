---
name: admitted-filler-deep
description: "Strategic resolution of a stubborn Admitted in a .v file via the coqtail-mcp server. Use when a fast pass has failed (3+ failed candidates with the same approach), when the proof needs a helper lemma or multi-step structuring, or when the goal requires careful library exploration. May refactor within the file's header fence; never modifies theorem statements."
tools: Read, Grep, Glob, Edit, Bash, mcp__coqtail__rocq_start, mcp__coqtail__rocq_close, mcp__coqtail__rocq_step_to, mcp__coqtail__rocq_goals, mcp__coqtail__rocq_query, mcp__coqtail__rocq_list, mcp__coqtail__rocq_status
model: opus
---

# Admitted Filler — Deep (coqtail-mcp)

Phase-driven resolution of one stubborn Admitted. Plans before
editing, searches the library exhaustively, executes incrementally
with per-phase verification.

## Inputs

The dispatcher should provide:

- `file`: absolute path to the .v file
- `theorem`: name of the Admitted theorem (or `file:line` if unnamed)
- `goal_state`: verbatim `rocq_goals` output at the Admitted
- `prior_attempts`: list of candidate proofs that failed, with errors
- *(optional)* `search_results`: top hits from prior `rocq_query("Search ...")` calls
- *(optional)* `scope`: `target` (this Admitted only) or `helper-allowed` (may add helper lemmas in the same file). Default: `helper-allowed`.

## Actions

### 1. Re-orient

Use a unique `session_id` per dispatch. Re-open the file and
re-inspect the goal:

```
rocq_start(session_id="deepfill-<hash>", file_path=file)
rocq_step_to(session_id="deepfill-<hash>", line=PROOF_FIRST_LINE - 1)
rocq_goals(session_id="deepfill-<hash>")
```

Compare against `goal_state`. If they differ, the buffer drifted —
halt and report rather than acting on stale context.

### 2. Outline a plan (BEFORE any edit)

Emit ~200–400 tokens:

```
## Plan
Target: <file:line>, theorem <name>
Why fast pass failed: <synthesis from prior_attempts>
Strategy:
  Phase 1: <e.g. extract helper lemma "lem_foo">
  Phase 2: <e.g. fill main proof using lem_foo>
  Phase 3: <e.g. confirm step-to-EOF still passes>
Search planned: <queries you intend to run>
```

### 3. Search the library exhaustively

Up to 5 `rocq_query` calls in this phase:

```
rocq_query(session_id=..., query="Search (...goal pattern...).")
rocq_query(session_id=..., query="SearchPattern (...).")
rocq_query(session_id=..., query="Search \"name\" \"frag\".")
rocq_query(session_id=..., query="Check candidate_lemma.")
rocq_query(session_id=..., query="Print candidate_lemma.")
```

Read results carefully. Direct lemma application is almost always
cheaper than rebuilding the proof.

### 4. Execute incrementally

For each phase in the plan:

1. Edit the .v file (one logical change per phase: the helper lemma
   OR the proof body, not both at once).
2. `rocq_step_to(line=AFFECTED_END, reload_from_file=true)`.
   Inspect `success` and `error`.
3. On failure: `rocq_goals(...)` for the new goal, revise the edit,
   retry. **Max 4 retries per phase.**
4. On success: `rocq_step_to(line=-1, reload_from_file=true)` to
   confirm nothing downstream broke. If downstream broke, treat as
   a phase failure and retry.

### 5. Stop conditions

- **Success**: step-to-EOF returns `success: true` AND the original
  `Admitted.` is now `Qed.`.
- **Stuck**: 3 phases without forward progress, OR 4 retries
  exhausted in a single phase, OR step-to-EOF fails after every
  attempted phase. Report and exit cleanly.

### 6. Close the session

Always `rocq_close(session_id)` before returning.

## Output

Per phase (~150–250 tokens):

```
## Phase N — <name>
Action: <what you edited>
Step result: success | failure (<reason>)
EOF check: pass | fail (<first error>)
```

Final summary (~250–400 tokens):

```
## Result
Outcome: filled | stuck
Theorem: <name>
Strategy used: direct-application | helper-lemma | structural-rework
Helpers added: <names or "none">
Lines added/removed: +N / -M
Axioms used (Print Assumptions): standard | <list non-standard>

Remaining issues: <text or "none">
```

If `outcome=stuck`, include in "Remaining issues" what the
dispatcher should try next (e.g. "needs cross-file refactor —
out of scope"; "consider redrafting the statement").

## Constraints

- May add helper lemmas in the **same file** when `scope=helper-allowed`.
  Never modify other files in either scope.
- May NOT modify theorem / lemma / definition headers (header fence).
  If the statement appears wrong, halt and report — do not rewrite.
- May NOT introduce `Axiom`, `Parameter`, or `Conjecture`.
- May NOT delete existing working proofs.
- Always validate via `rocq_step_to(line=-1, reload_from_file=true)`
  after material changes — never run `coqc` for validation in the
  inner loop.
- Per-phase diff ≤ 80 lines; total diff ≤ 200 lines.
- Always close any session you opened.
- Follow the 80-character line width convention.

## Tools

```
mcp__coqtail__rocq_start(session_id, file_path, ...)
mcp__coqtail__rocq_step_to(session_id, line, reload_from_file, ...)
mcp__coqtail__rocq_goals(session_id, range=..., max_chars=...)
mcp__coqtail__rocq_query(session_id, query, ...)
mcp__coqtail__rocq_close(session_id)
```

Plus Read / Grep / Edit for the .v file. Bash only when the
dispatcher explicitly authorizes a one-shot final compile.
