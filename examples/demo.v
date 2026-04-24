(* A small example file used by the smoke tests and the README walkthrough. *)

Theorem plus_n_O : forall n : nat, n + 0 = n.
Proof.
  intros n.
  induction n as [| n' IH].
  - reflexivity.
  - simpl. rewrite IH. reflexivity.
Qed.

Lemma and_comm : forall P Q : Prop, P /\ Q -> Q /\ P.
Proof.
  intros P Q [HP HQ].
  split; assumption.
Qed.
