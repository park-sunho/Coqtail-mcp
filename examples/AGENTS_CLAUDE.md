When working on Rocq/Coq proofs, use the `coqtail-mcp` workflow as the default proof-development loop.

First, read the `coqtail-mcp` skill/instructions before doing proof work. If your environment has a skill system, load/read the `coqtail-mcp` skill. Otherwise, read the local `SKILL.md` for `coqtail-mcp` if available, for example:
`/home/kingdoctor/Coqtail-mcp/plugins/coqtail-mcp/skills/coqtail-mcp/SKILL.md`.

For Rocq proof development, do not use whole-file compilation as the way to find proof errors. In particular, during proof iteration, do not run commands such as `coqc`, `rocq compile`, `rocq_compile`, `rocq_compile_file`, `make`, `dune build`, or
`opam exec -- coqc` merely to discover the next failing tactic or proof state.

Instead, use `coqtail-mcp` interactively:
- Start a live session with `rocq_start`.
- Let `rocq_start(file_path=...)` auto-detect Dune or `_CoqProject` settings; use `build_system`, `project_names`, or `extra_args` only when overriding that default.
- Move through the proof with `rocq_step_to`.
- Inspect goals with `rocq_goals`.
- Use `rocq_query` for `Check`, `Search`, `Print`, `About`, etc.
- For very large goals or query results, pass `full_output_file` so the full JSON payload is written to disk while the tool response stays compact; then read the path returned in `full_output_written_to`.
- After editing a file that is already loaded in the session, use `rocq_step_to(..., reload_from_file=true)` or reopen the session so the live buffer reflects the current proof text.
- Use the live goal/error returned by Coqtail to guide each proof step.

Compilation is allowed only in these cases:
1. You are explicitly checking for possible whole-file/build compilation errors outside the local proof loop.
2. You are confident the proof is finished and want a final confirmation.
3. You need to build or refresh dependencies before the interactive session can work.

This restriction is important for efficiency: whole-file compilation is too slow for proof search and tactic iteration. Treat `coqtail-mcp` as the primary proof debugger, and compile only at the allowed checkpoints above.

## Specialized subagents (when the `coqtail-mcp` plugin is installed)

If the plugin is installed, four named subagents are available in the
agent picker. Reach for them spontaneously when their use conditions
match — that is what they exist for.

- **`coqtail-mcp:proof-repair`** — a single tactic or sentence fails
  to step (type mismatch, unification failure, missing reference,
  unsolved goals, syntax error, …). Fast two-stage budget. Outputs
  only a unified diff or `REPAIR FAILED`.
- **`coqtail-mcp:admitted-filler-deep`** — a stubborn `Admitted`
  resists 3+ honest candidate proofs, or the goal needs a helper
  lemma / multi-step structuring / careful library exploration.
  Plans before editing; may add helpers in the same file (header
  fence holds).
- **`coqtail-mcp:axiom-auditor`** — verify proof hygiene before a
  checkpoint, after a long proof session, or when claiming a theorem
  is "done". Two modes: `audit` (read-only `Print Assumptions` report)
  and `eliminate` (remove non-standard axioms by library search /
  compositional rewrite).
- **`coqtail-mcp:proof-golfer`** — a file already type-checks
  end-to-end (no `Admitted`) and you want to shorten / direct-ify
  proofs. Reverts immediately on any verification failure.

Each subagent gets a fresh context. Pre-flight context to include in
the dispatch prompt is documented in the agent's own definition file
(`agents/<name>.md` in the plugin) — at minimum, pass absolute paths,
the relevant `rocq_goals` output, and what you've already tried.

For cross-cutting patterns the named agents don't cover (parallel
candidate testing across many sessions, multi-file audits,
background final compile, search delegation, stuck-state strategy
second-opinion), see `references/subagents.md` in the skill.
