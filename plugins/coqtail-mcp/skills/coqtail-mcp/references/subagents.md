# Subagents

Each coqtail-mcp session is one `coqidetop` subprocess; each session
is single-threaded. Inside the main agent, work is serial: one step,
one query, one buffer reload at a time. When you need parallelism or
a fresh context for a hard task, delegate to subagents — each gets
its own context window and can open its own session.

There are two kinds of dispatch: **named subagents** that ship with
this plugin (specialized for common Rocq workflows) and **generic
subagents** (`general-purpose`, `Explore`, `Plan`) for cross-cutting
patterns. Use named ones first when the task fits; fall back to the
patterns below when it doesn't.

## Named subagents (this plugin)

When this plugin is installed, four specialized subagents are
available. They show up in the agent picker as
`coqtail-mcp:<name>`. Reach for them spontaneously — that's their
job.

| Agent | Model | Use when |
|-------|-------|----------|
| `coqtail-mcp:proof-repair` | opus | A single tactic or sentence fails to step (`type_mismatch`, `unable_to_unify`, `unknown_ident`, `unsolved_goals`, `synth_instance`, `timeout`, `syntax`). Two-stage budget (6 fast / 18 strategic). Outputs a unified diff. |
| `coqtail-mcp:admitted-filler-deep` | opus | A stubborn `Admitted` resists 3+ candidate proofs, or the goal needs a helper lemma / multi-step structuring / careful library exploration. Plans before editing; may add helpers in the same file. |
| `coqtail-mcp:axiom-auditor` | opus | Verify proof hygiene before a checkpoint, or after a long session that may have introduced axioms. Two modes: `audit` (read-only report) and `eliminate` (remove non-standard axioms). |
| `coqtail-mcp:proof-golfer` | opus | A file already type-checks end-to-end (no `Admitted`) and you want to shorten / direct-ify proofs. Reverts immediately on any verification failure. |

Triggers in plain language:

- **Hit a tactic error?** → `coqtail-mcp:proof-repair`.
- **Stuck on an Admitted after 3 honest attempts?** →
  `coqtail-mcp:admitted-filler-deep`.
- **About to checkpoint or claim a proof is done?** →
  `coqtail-mcp:axiom-auditor` (audit mode).
- **Want to clean up a passing file?** →
  `coqtail-mcp:proof-golfer`.

Each named agent has its own pre-flight context spec — see the
agent's own `description` and the file under `agents/` for what to
include in the dispatch prompt.

## When the named agents don't fit

Use the generic patterns below when the task is:

- Naturally parallel across many independent units (Pattern A, C).
- Long-running and unblockable (Pattern B).
- Context-heavy in raw output (Pattern D).
- A strategic re-plan rather than a focused execution (Pattern E).

## When to delegate

Delegate when the task is:

- **Naturally parallel** — N independent attempts at the same goal,
  N files to audit independently.
- **Independently bounded** — finishes on its own without needing your
  judgement mid-flight.
- **Context-heavy in output** — the raw results would dominate your
  conversation but you only need a digest (e.g. searching a large
  project for usages of a name).
- **Long-running and unblockable** — a full-project compile,
  multi-file lint, or PDF ingestion that the inner proof loop
  shouldn't have to wait for.

Keep in the main thread when the task:

- Needs goal-by-goal feedback to decide the next move.
- Involves a single small experiment.
- Requires conversational ambiguity resolution ("ask the user").
- Will inform the very next tool call you make.

## Which subagent type

This skill assumes only the generic agents are available:

| Type              | Best for                                              |
| ----------------- | ----------------------------------------------------- |
| `Explore`         | Static file/code inspection, grep-style searches, repository mapping. No edits. |
| `general-purpose` | Anything that needs to call MCP tools, edit files, or run shell commands. Use this for parallel candidate testing. |
| `Plan`            | Drafting an implementation strategy (e.g. how to fill a hard Admitted) without writing code. |

Pick `Explore` first when the answer is purely "find this in the
codebase". Pick `general-purpose` when the subagent needs to open its
own coqtail-mcp session, run `rocq_step_to`, etc. Pick `Plan` for
strategic decisions you want a second pass on.

## Pattern A — Parallel candidate testing

The single highest-value use of subagents in this server. The
main-thread alternatives are: (a) test candidates one at a time via
edit-and-reload, or (b) spawn N coqidetop sessions sequentially in
the main thread. Subagents let you actually run them concurrently.

**Setup.** Decide N candidate proof bodies for a single Admitted at
`/abs/F.v` line `L`. For each, dispatch one `general-purpose`
subagent in a single message (so they run concurrently):

```
Agent(description="Test candidate 1",
      subagent_type="general-purpose",
      prompt="""
      Test whether this proof body closes the Admitted at
      /abs/F.v line L. Open a coqtail-mcp session with a unique
      session_id, write the candidate body into a copy of the file
      in /tmp (or use content= directly), step to the end of the
      proof, and report exactly:

        OK | endpoint=[..]   <-- if rocq_step_to succeeded
        FAIL | error=...     <-- otherwise

      Candidate body:
      ---
      intros n. induction n; simpl; auto.
      ---

      Close the session before exiting. Do not edit /abs/F.v.
      """)
Agent(description="Test candidate 2", subagent_type="general-purpose",
      prompt="...candidate 2...")
Agent(description="Test candidate 3", subagent_type="general-purpose",
      prompt="...candidate 3...")
```

Send all the `Agent` calls in a single message — that's how the
harness runs them in parallel. Collect the OK/FAIL reports and pick
the winner.

**Hygiene.** Each subagent must use a unique `session_id` and call
`rocq_close` before exiting. Never share a session id across
subagents — the registry rejects duplicates anyway, but more
importantly, two writers on one session would produce nonsense.

**When to do this rather than a serial cascade.** Use parallel
subagents when:

- The proof preamble is heavy and `reload_from_file` would still
  re-check several seconds of work each pass, or
- You want to compare a few substantively different proof shapes
  (induction vs. case analysis vs. existing-lemma-application)
  rather than blast through the [tactic cascade](proof-recipes.md#3-tactic-cascade).

Stick with serial reload-and-step in the main thread when candidates
are cheap or there are only two of them.

## Pattern B — Background final compile

You're confident a proof is finished but want a `coqc` / `make` final
check without blocking your interactive session.

```
Agent(description="Project compile gate",
      subagent_type="general-purpose",
      run_in_background=true,
      prompt="""
      cd /abs/proj && coq_makefile -f _CoqProject -o CoqMakefile &&
      make -f CoqMakefile -j 4

      Report: PASS or FAIL <first error line>. Under 80 words.
      """)
```

`run_in_background=true` is the important flag — you'll be notified
when the agent finishes and can keep working in the meantime. Use
this only after you've already confirmed success interactively via
`rocq_step_to(line=-1)`; it's a confirmation, not a debugging tool.

## Pattern C — Multi-file audit

Audit every `.v` file in a directory: open a session per file, step
to EOF, run `Print Assumptions` on the public theorems, report.
Independently bounded per file, so it parallelises naturally.

```
Agent(description="Audit theories/",
      subagent_type="general-purpose",
      prompt="""
      For every .v file under /abs/proj/theories/:
        1. Open a coqtail-mcp session with a unique session_id.
        2. rocq_step_to(line=-1, reload_from_file=true).
        3. For each public Theorem/Lemma in the file (Grep
           '^(Theorem|Lemma|Definition)\\s+\\w+'), run
           rocq_query("Print Assumptions <name>.").
        4. Close the session.

      Report a single table:
        File | Theorems | Std-axiom | Custom-axiom | Build-OK

      Custom axioms = anything outside Classical_Prop,
      FunctionalExtensionality, PropExtensionality,
      ProofIrrelevance, JMeq, Rdefinitions/Raxioms.
      Under 300 words.
      """)
```

You get back a concise audit table without polluting your context
with hundreds of `Print Assumptions` outputs.

## Pattern D — Search delegation

When you need to grep-style search a project for usages, lemma
candidates, or all `Admitted` occurrences with surrounding context,
use `Explore`:

```
Agent(description="Find Admitteds in theories/",
      subagent_type="Explore",
      prompt="""
      Find every `Admitted.` and `admit.` occurrence under
      /abs/proj/theories/. For each, report:
        file:line | enclosing Theorem/Lemma name | 5 lines of
        surrounding context

      Skip occurrences inside comments. Plain text, one block per
      occurrence.
      """)
```

This keeps the raw search output out of your context — you get a
filtered, structured digest.

## Pattern E — Strategy second-opinion

When stuck on a hard Admitted, ask `Plan` for an independent
strategy without committing tool calls:

```
Agent(description="Replan stuck Admitted",
      subagent_type="Plan",
      prompt="""
      Stuck filling Admitted at /abs/F.v line 142.

      Goal: <paste rocq_goals output>

      Tried so far (all failed):
        1. <tactic> → <error>
        2. <tactic> → <error>
        3. <tactic> → <error>

      Searches done:
        rocq_query("Search ...") → <top hits>

      Surrounding hypotheses include <H : ...>.

      Propose 2–3 substantively different strategies (not
      variations of what's been tried). For each, say what library
      lemma or proof shape it would lean on. Do not write tactics —
      this is for an independent strategic read.
      """)
```

The point is to break out of a local optimum. The plan you get back
becomes input to your next round of candidates, which you can run
either in the main thread or via Pattern A.

## Pre-flight context to include in any dispatch

Subagents start with no memory of your conversation. Always include:

- **Absolute file paths.** Subagents don't share your CWD intuitions.
- **The current goal state.** If they need to know what to prove,
  paste `rocq_goals` output verbatim.
- **What you've already tried.** Otherwise they'll repeat your work.
- **Output format you want.** "Under 200 words." "One line per
  candidate: OK or FAIL plus reason." Without this they over-narrate.
- **Cleanup expectations.** Tell them to call `rocq_close` and not
  to mutate the working file.

A terse one-line dispatch produces shallow work; a self-contained
brief produces something usable.

## Anti-patterns

- **Don't dispatch one subagent per single tactic.** The dispatch
  overhead dominates. Use serial reload-and-step or batch several
  candidates per subagent.
- **Don't delegate the proof inner loop.** Each step depends on the
  previous goal — that's what the main thread is for.
- **Don't share `session_id`s across subagents.** Use distinct ids
  per dispatch; subagents that need to communicate should do so via
  their reports, not by sharing live Rocq state.
- **Don't forget `rocq_close` in the subagent's instructions.**
  Leaked sessions keep `coqidetop` processes alive across the whole
  server lifetime.
- **Don't run a `coqc` / `make` background subagent during active
  proof iteration.** The interactive session is faster and more
  precise. Reserve background compilation for *final* gating.
