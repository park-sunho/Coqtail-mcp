# Proof recipes for coqtail-mcp

Higher-level guidance for *doing proofs* through this MCP server. The
[SKILL.md](../SKILL.md) and [tools.md](tools.md) cover the call protocol
itself — this file is about how to use those calls effectively.

The recipes assume one open session opened with `file_path` (so
`reload_from_file=true` works). Adapt to inline-`content` sessions by
reopening when the source changes.

---

## 1. Search before you prove

Most mathematical facts already exist in the Coq stdlib, MathComp, or
another installed package. Searching the environment is much cheaper
than writing a multi-line tactic that ends up duplicating
`Nat.add_comm`.

```
rocq_query(session_id="s", query="Search (_ + 0 = _).")
rocq_query(session_id="s", query="Search \"add\" \"comm\".")
rocq_query(session_id="s", query="SearchPattern (_ * _ = _ * _).")
rocq_query(session_id="s", query="Check Nat.add_comm.")
rocq_query(session_id="s", query="About Nat.add_comm.")
rocq_query(session_id="s", query="Print Nat.add.")
rocq_query(session_id="s", query="Locate \"+\".")
```

`rocq_query` does not consume state — issue as many as you need before
committing to a tactic. Cap large outputs with `max_chars` (and write
the full payload to disk via `full_output_file`) when scanning broad
search results.

**Heuristic:** if a goal looks like a one-line algebraic identity, do
*at least* one `Search` call before you write any `intros`. The most
common failure mode for agents is reproving a stdlib lemma badly.

---

## 2. Filling an `Admitted` (full loop)

The inner loop. The file on disk is the source of truth — tactics are
tested by editing the buffer and asking the server to reload it.

```
# (1) Open and step to just before the Admitted.
rocq_start(session_id="fill", file_path="/abs/F.v",
           coq_path="/.../_opam/bin")
rocq_step_to(session_id="fill", line=PROOF_FIRST_LINE - 1)

# (2) Read the goal.
rocq_goals(session_id="fill")
# inspect summary.fg[0].conclusion + hypotheses

# (3) Search for relevant lemmas.
rocq_query(session_id="fill", query="Search (...goal pattern...).")

# (4) Edit the .v file on disk: replace `Admitted.` with a candidate
#     proof body (intros / apply / exact / lia / ...).

# (5) Step through it. reload_from_file diffs the in-memory buffer
#     against the new file contents and rewinds only what's needed.
r = rocq_step_to(session_id="fill", line=PROOF_LAST_LINE,
                 reload_from_file=true)
if r["success"]:
    # If a sentence after Admitted now fails to compile, see (6).
    rocq_step_to(session_id="fill", line=-1, reload_from_file=true)
else:
    # r["error"] / r["error_range"] tell you what failed and where.
    # Edit the file, repeat from (5). reload_from_file rewinds the
    # session automatically based on the diff.
```

**Step 6 — when later sentences break.** If step (5) succeeds but
the EOF step fails, the new proof is locally well-typed but something
downstream broke. Most common cause: another `Admitted` further down
in the file that we never reached before. Inspect `error` /
`error_range` from the EOF step, then handle the new failure the
same way (search → edit → reload → step). Keep the original proof
opaque (`Qed.`, not `Defined.`) unless you have a specific reason
to expose the body — `Admitted` is opaque, so changing to `Defined.`
silently alters definitional equality for any downstream definition
that was relying on the placeholder being opaque.

**Constraints worth respecting** (carried over from common Coq
practice; not server-enforced):

- No statement changes (theorem / lemma / definition headers and
  signatures are off-limits without explicit user approval).
- No new global `Axiom` / `Parameter` / `Conjecture` declarations.
- ≤ ~80 lines of proof body added per attempt.
- Stop and reconsider after 2–3 failures with the same approach
  rather than escalating tactic complexity.

---

## 3. Tactic cascade

A useful default when the goal doesn't suggest a specific approach.
Try in order, stop on the first one that closes the goal (or makes
the most progress).

```
reflexivity. → assumption. → trivial. → auto. →
ring. / field. → lia. / lra. / nia. / nra. →
tauto. → intuition. → firstorder. → eauto. → decide.
```

Imports needed:
- `lia` / `nia`: `Require Import Lia.`
- `lra` / `nra`: `Require Import Lra.`
- `ring`: `Require Import Ring.` (or `Setoid` for setoid-rings)
- `field`: `Require Import Field.`

To run the cascade, edit the candidate tactic into the `.v` file and
call `rocq_step_to(..., reload_from_file=true)` once per candidate.
For one-shot candidates this is fast — most compile in sub-second.
When a cascade is slow because the preamble is heavy, or you want to
explore several substantive proof shapes in parallel, dispatch
subagents instead — each owns its own session. See
[subagents.md](subagents.md).

---

## 4. Goal-pattern → tactic mapping

| Goal shape                              | First thing to try                          |
| --------------------------------------- | ------------------------------------------- |
| `X = X`                                 | `reflexivity.`                              |
| `X = Y` over `nat` / `Z`                | `lia.` then `ring.`                         |
| `X = Y` over `R`                        | `lra.` then `ring.` / `field.`              |
| `X = Y` needing rewriting               | `rewrite H.` or `unfold f; simpl.`          |
| `f x = f y`                             | `f_equal.` (then prove `x = y`)             |
| `P /\ Q`                                | `split.`                                    |
| `P \/ Q` (P easy)                       | `left.`                                     |
| `exists x, P x`                         | `exists witness.`                           |
| `forall x, P x`                         | `intros x.`                                 |
| `P -> Q`                                | `intros H.`                                 |
| `~ P`                                   | `intros H.` then derive `False`             |
| `True`                                  | `exact I.` or `trivial.`                    |
| `False` from contradicting hyps         | `discriminate.` / `congruence.` / `lia.`    |
| nat / `Z` inequality                    | `lia.`                                      |
| `R` inequality                          | `lra.` / `nra.`                             |
| Decidable equality                      | `decide equality.` or `Nat.eq_dec`          |
| Finite case split on a hypothesis       | `destruct H as [...].`                      |
| Inductive proof on `n : nat` / `l : list` | `induction n.` / `induction l.`           |
| Pattern match incomplete / non-exhaustive | inversion, or add the missing case        |

If the cascade and the table both fail, the proof is non-trivial: do a
`Search` pass, then build a multi-step proof.

---

## 5. Common errors and quick fixes

When `rocq_step_to` returns `success: false`, the `error` and
`error_range` fields tell you what to fix. The table below is for the
most common Rocq error families.

| Error fragment                                              | Likely cause                | Quick fix                                                     |
| ----------------------------------------------------------- | --------------------------- | ------------------------------------------------------------- |
| `has type "A" while it is expected to have type "B"`        | Type mismatch               | `change`, type annotation, coercion (`Z.of_nat`), `refine`    |
| `Unable to unify "A" with "B"`                              | Definitional inequality     | `unfold f.`, `simpl.`, `change A with B.`, then retry         |
| `The reference X was not found`                             | Missing import / typo       | `Require Import Module.` (see import map below); fix name     |
| `No matching clauses for match`                             | Incomplete pattern match    | Add missing case; or `destruct ... eqn:?`                     |
| `Cannot guess decreasing argument`                          | Non-structural recursion    | `{struct n}`, `Function`, `Program Fixpoint {measure ...}`    |
| `Universe inconsistency`                                    | Universe levels collide     | `Set Universe Polymorphism.`; explicit `Type@{u}`             |
| `Tactic failure: ... (level N)`                             | Tactic didn't apply         | Pick a different tactic; check goal with `rocq_goals` first   |
| `The command has not enough arguments` / `unexpected token` | Syntax error nearby         | Check 5–10 lines *before* `error_range` for missing `.`       |
| `Found no subterm matching X in the current goal`           | `rewrite` target absent     | `rewrite ... in H.` / `rewrite ... at N.` / unfold first      |

**Import map for failed-tactic errors:**

| Tactic           | Import                  |
| ---------------- | ----------------------- |
| `lia` / `nia`    | `Require Import Lia.`   |
| `lra` / `nra`    | `Require Import Lra.`   |
| `ring`           | `Require Import Ring.`  |
| `field`          | `Require Import Field.` |
| `psatz`          | `Require Import Psatz.` |
| `omega`          | (deprecated — use `lia`) |

**Locating the real error.** Rocq error locations occasionally point
at the *next* sentence (when the previous one is missing its closing
`.`). If `error_range` looks wrong, check the line above. The session
endpoint after a failure reflects the last successful sentence — the
state is consistent with that endpoint, so `rocq_goals` /
`rocq_query` continue to work for diagnosis.

---

## 6. Axiom hygiene

A proof is only as trustworthy as the axioms it depends on. After
finishing a theorem, audit its dependencies with:

```
rocq_query(session_id="s", query="Print Assumptions theorem_name.")
```

The output lists every axiom (and `Parameter` / `Variable`) the term
depends on, transitively.

**Standard axioms** that are widely accepted and not normally flagged:

| Axiom                            | Module                                  |
| -------------------------------- | --------------------------------------- |
| `classic`, `NNPP`                | `Coq.Logic.Classical_Prop`              |
| `functional_extensionality`      | `Coq.Logic.FunctionalExtensionality`    |
| `propositional_extensionality`   | `Coq.Logic.PropExtensionality`          |
| `proof_irrelevance`              | `Coq.Logic.ProofIrrelevance`            |
| `JMeq_eq`                        | `Coq.Logic.JMeq`                        |
| Real-number axioms (`Rplus_comm` etc.) | `Coq.Reals.Rdefinitions` / `Raxioms` |

Anything else — especially `Axiom`, `Parameter`, or `Conjecture`
declared in the project itself — should be either justified, replaced
with a real proof, or surfaced to the user. Do not silently introduce
new axioms while filling an `Admitted`; if the proof apparently
needs one, stop and discuss.

---

## 7. Completion criteria

A proof is complete (in this server) when **all** of the following
hold:

1. `rocq_step_to(session_id="...", line=-1)` returns `success: true`
   on the file as written. There is no separate "compile" step — the
   session *is* the type-checker.
2. There are no remaining `Admitted` or `admit` in the agreed scope.
3. `rocq_query("Print Assumptions theorem_name.")` lists only
   standard axioms (see §6).
4. No theorem / lemma statement was modified without explicit user
   approval.

For final CI confirmation outside the interactive session, `coqc` (or
`make` / `dune build`) is appropriate. Inside the proof loop, prefer
`rocq_step_to` — see the [project AGENTS_CLAUDE.md](../../../examples/AGENTS_CLAUDE.md)
for the rationale.

---

## 8. House conventions

- **80-character line width.** Standard Coq/Rocq formatting.
- **Don't change theorem statements.** Headers, signatures, and doc
  comments are off-limits unless the user explicitly asks.
- **Don't introduce new global axioms.** If a proof seems to need one,
  stop and surface the question.
- **Edit the buffer, then `reload_from_file`.** This server has no
  "send a sentence" primitive; the file is the unit of work.
- **One session per file, per Rocq process.** Reusing a `session_id`
  across files leaves Rocq with the wrong `-topfile`.

These are practitioner conventions, not server enforcement — the MCP
server itself will accept any well-formed call.
