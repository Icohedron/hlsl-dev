---
name: grilling
description: Grill a plan, decision, or idea. Use to stress-test HLSL feature and compiler work, or when the user uses a 'grill' trigger phrase.
---

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

For HLSL, DirectX, DXIL IR, Vulkan or SPIR-V decisions, read [HLSL feature planning](../../../docs/agents/hlsl-feature-planning.md) before the first round. Use its source-backed requirements and baseline-first workflow to build the design tree and its handoff to specs or tickets.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

Format a round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Word each question so "yes" accepts your recommended answer.

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding **facts** is your job. Look up repository and specification facts before asking questions; delegate independent investigations when warranted. A running investigation is an unsettled prerequisite: only its downstream questions wait, while the rest of the frontier can proceed. Put unresolved **decisions** to the user and wait for their answers.

The session is done when the frontier is empty: every applicable branch is settled or explicitly out of scope, with source-backed requirements and verification decisions recorded for the handoff. Wait for the user's confirmation of that shared understanding before implementation.
