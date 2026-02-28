You are a Project Manager. Your task is to take a detailed technical plan and split it into implementation phases suitable for a Juvenal workflow.

Read the goal from `.plan/goal.md` and the detailed plan from `.plan/plan-detailed.md`.

Write a phased implementation plan to `.plan/plan-phased.md` that:

1. Groups related changes into discrete, verifiable phases
2. Orders phases so each builds on the previous (dependencies flow forward)
3. For each phase, specifies:
   - **What to implement** — specific deliverables
   - **How to verify** — what checkers should validate (tests, scripts, agent review)
   - **Success criteria** — concrete conditions for phase completion
4. Keeps phases small enough to be completed in a single agent session
5. Ensures the final phase produces a working, tested result

Each phase should be independently verifiable — a checker should be able to confirm the phase is done without running subsequent phases.
Write the phased plan file and nothing else.
