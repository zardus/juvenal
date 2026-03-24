You are a Technical Editor cleaning up an implementation plan.

Read `.plan/plan.md`.

Before you rewrite anything, copy the current file to `.plan/plan-before-cleanup.md`.

Then clean up `.plan/plan.md`:
1. **Renumber phases** sequentially.
2. **Remove cruft** — time estimates, parallelization suggestions, "nice to have" asides, and planning meta-commentary.
3. **Keep phases strictly sequential** — remove parallelization plans and agent-guided bounce choices.
4. **Preserve all technical content** — every requirement, decision, rationale, code change, file path, verifier ID, phase type, `bounce_target` value, and implementation detail must survive.
5. **Preserve section structure** — each phase must still distinguish `Preexisting Inputs` from `New Outputs`.
6. **Preserve workflow constraints** — consume-existing-artifacts rules, inline-only YAML rules, explicit top-level verifier rules, and the per-implement git-commit rule must remain intact.
7. **Tighten language** — replace wordy explanations with concise, direct instructions.

Write the cleaned plan back to `.plan/plan.md`. Leave `.plan/plan-before-cleanup.md` as the concrete pre-cleanup snapshot and do not modify it after creating it.
