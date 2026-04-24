<!-- Copy this to AGENTS.md and CLAUDE.md -->

When working on Rocq/Coq proofs, use the `coqtail-mcp` workflow as the default proof-development loop.

First, read the `coqtail-mcp` skill/instructions before doing proof work. If your environment has a skill system, load/read the `coqtail-mcp` skill. Otherwise, read the local `SKILL.md` for `coqtail-mcp` if available, for example:
`/home/kingdoctor/Coqtail-mcp/skills/coqtail-mcp/SKILL.md`.

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
