# Adversarial Verification Protocol

This project handles live trading logic. Mistakes cost real money.
Every non-trivial change (anything touching engine/, more than a
pure deletion/rename) MUST go through the two-pass protocol below
before being presented to the user as "done" or "ready for Act Mode."

## Pass 1 — Proposal
Produce the plan, diff, or analysis as normal.

## Pass 2 — Adversarial Self-Review (MANDATORY, before showing the user)
Before presenting Pass 1's output, re-read it wearing a DIFFERENT hat:
assume the proposal is WRONG and your job is to find out how.

Specifically check for:
1. Unverified claims stated as fact. Anywhere you wrote "X is
   unused" / "Y is dead code" / "this works because Z" — did you
   actually grep/run/trace it, or did you infer it from reading
   nearby code? If you inferred it, say so explicitly and mark it
   UNVERIFIED, don't present it as confirmed.
2. Internal contradictions. Does any later section of your own
   answer contradict an earlier section? Read your own output twice,
   specifically hunting for this.
3. Silent scope narrowing. Did you say you'd check 5 things and
   only actually check 3? Call out what you skipped and why.
4. Mechanism honesty. If this is a refactor, state the EXACT
   mechanism (e.g. "mixin inheritance" vs "delegator functions") and
   confirm no ambiguity (e.g. MRO conflicts, shared state ordering,
   naming collisions between files and packages) remains. Do not
   describe a mechanism you haven't traced end-to-end with actual
   command output.
5. Test coverage gaps. Does the change touch any function with
   zero existing test coverage? Say so explicitly rather than
   assuming "tests pass" means "this is safe."
6. Reversibility. Can this change be cleanly reverted if it's
   wrong (git, backup, re-export shim)? If not, flag it as
   higher-risk and suggest a safer incremental path.

Write out Pass 2 findings EXPLICITLY as a "Self-Review" section before
the final summary — even if you find nothing wrong, state what you
checked and why it holds up. Do not skip this section or merge it
invisibly into Pass 1's narrative.

## When in doubt
If Pass 2 finds ANY unresolved contradiction or unverified claim,
do NOT proceed to execution / Act Mode. Present the unresolved item
to the user explicitly and wait for guidance.

## Financial-logic-specific rules (this repo)
- Never describe a fix as "implemented" without showing the actual
  current file contents (grep/view), not a paraphrase of what you
  intended to write.
- Never bundle a logic change with a structural move (file split,
  rename) in the same step. Structural moves must be byte-for-byte.
- Any claim of "N tests passing" must include the actual pytest
  output, not a summarized count from memory of an earlier run.
- Before claiming a file rename/move resolves a naming collision,
  run `ls` and `python -c "import X; print(X.__file__)"` and show
  the raw output — do not describe the fix without proof it works.

## Multi-Model Committee Mode

### Role: Proposer
Any available model. Produces Pass 1 (plan/diff/analysis) as normal.

### Role: Mechanical Verifier (free/cheap models OK — Hy3, Nemotron,
### or similar)
Strictly execute-and-report. Runs every grep/test/diff/import-check
the Proposer claims to have run, pastes RAW unedited output only.
Never judges correctness, safety, or proportionality — if asked to,
must refuse and redirect to the Adversarial Reviewer role.

### Role: Adversarial Reviewer (Sonnet 4.6 or Gemini Flash 3.5+ ONLY —
### never a free/weak model, regardless of quota availability)
Performs the full Pass 2 checklist in this file. Responsible for
catching: unproven attribution logic, disproportionate remediation,
summaries that omit bad results from their own evidence, and any
claim of "implemented"/"tested"/"verified" not backed by raw output
shown in the same message.

### Model Rotation Note
[TEMPORARY — remove this subsection after July 21, 2026]
One or more free-tier models used for the Mechanical Verifier role
may become paid/unavailable after July 21, 2026. If this happens,
do not attempt to fill the Mechanical Verifier role with the
Adversarial Reviewer's model to save cost, and do not downgrade the
Adversarial Reviewer role to a free model to compensate. If only one
paid reviewer model remains available, the committee still functions
with a single Proposer + single Adversarial Reviewer — the
Mechanical Verifier role becomes "run it yourself and paste output"
if no second model is available at all.

### Switching Mechanism — CONFIRM BEFORE RELYING ON THIS
This file describes roles, but does not itself switch the active
model. Before treating this committee structure as functional, confirm
explicitly: can Cline actually invoke a second/third model
automatically within one task (a real sub-agent or multi-model
feature), or does the user have to manually switch the model dropdown
between passes? State the answer plainly — do not assume automatic
routing works just because this file describes roles.

### Model Assignment Log
Every committee-reviewed change must state in its final summary:
"Proposer: <model>. Mechanical Verifier: <model>. Adversarial
Reviewer: <model>." Mandatory, not optional.
