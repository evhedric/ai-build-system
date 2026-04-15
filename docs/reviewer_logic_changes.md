# Reviewer Logic Changes

## Overview

This document describes the updates made to the reviewer logic as part of the initiative to enforce strict, production-quality standards. The reviewer no longer operates with lenient or implicit approval criteria. Every task submission must satisfy a defined set of checks before it is approved. If any check fails, the reviewer returns a `revise` decision accompanied by specific, actionable instructions.

---

## Motivation

The previous reviewer logic was prone to approving tasks that were incomplete, ambiguous, or lacking critical context. This created downstream quality issues and required additional remediation cycles. The updated logic enforces explicit gates at review time to prevent low-quality outputs from passing through the pipeline.

---

## Quality Standards

The reviewer now enforces four mandatory quality dimensions:

| Dimension        | Definition |
|------------------|------------|
| **Completeness** | The task output addresses all stated goals, covers all required sections, and leaves no specified requirement unaddressed. |
| **Clarity**      | The content is unambiguous, logically structured, and written at the appropriate technical level for the intended audience. |
| **Differentiation** | The output clearly distinguishes between concepts, components, or options where applicable. Edge cases and distinct behaviors are explicitly called out. |
| **Limitations**  | Known constraints, assumptions, caveats, or out-of-scope items are explicitly documented. The output does not overstate capabilities or applicability. |

All four dimensions must be satisfied for a task to receive an `approve` decision. A deficiency in any single dimension triggers a `revise` decision.

---

## Checks Implemented

The following checks were added to the reviewer logic:

### 1. Completeness Check

- Verifies that all goals listed in the task definition are addressed in the output.
- Confirms that all required sections (as defined by the task type and plan step) are present.
- Flags outputs that respond to only a subset of the stated requirements.

**Failure condition:** One or more goals or required sections are absent or only partially addressed.

### 2. Clarity Check

- Evaluates whether the output is free of ambiguous language, undefined acronyms, or contradictory statements.
- Confirms the document structure is logical and easy to follow.
- Checks that code, configuration, or instructions can be understood and acted upon without external clarification.

**Failure condition:** Ambiguous phrasing, missing context, or structural issues that impede understanding.

### 3. Differentiation Check

- Ensures that distinct concepts, behaviors, or components are explicitly distinguished from one another.
- Verifies that comparisons include concrete criteria rather than vague qualitative statements.
- Checks that edge cases and conditional behaviors are enumerated where relevant.

**Failure condition:** Concepts or behaviors that are meaningfully different are conflated or treated as equivalent without justification.

### 4. Limitations Check

- Confirms that the output explicitly states any assumptions made during its creation.
- Verifies that known constraints, out-of-scope items, or caveats are documented.
- Flags outputs that present conclusions as universally applicable when they are context-dependent.

**Failure condition:** No limitations section or equivalent content is present, or material assumptions are left implicit.

---

## Decision Logic

The reviewer evaluates all four checks sequentially. The decision is determined as follows:

```
if completeness_check FAILS
    OR clarity_check FAILS
    OR differentiation_check FAILS
    OR limitations_check FAILS:
    decision = "revise"
    feedback = [specific instructions for each failed check]
else:
    decision = "approve"
```

The reviewer does **not** apply partial credit. A task that passes three out of four checks still receives a `revise` decision.

---

## Feedback Mechanism

When the reviewer returns a `revise` decision, the feedback payload must include:

1. **Identification of the failed check(s):** Clearly state which dimension(s) were not satisfied.
2. **Description of the deficiency:** Explain specifically what is missing, weak, or incorrect.
3. **Revision instructions:** Provide concrete, actionable guidance on how to address the deficiency.

The feedback must not be generic. Phrases such as "improve the content" or "add more detail" are not acceptable on their own. Each instruction must reference the specific section, statement, or omission that triggered the failure.

---

## Example Feedback

### Example 1 — Completeness Failure

**Task:** Document the API authentication flow.

**Deficiency:** The output describes the token issuance process but does not address token refresh or revocation, both of which were listed as required goals.

**Feedback returned:**

```
REVISION REQUIRED

Check failed: Completeness

The following required sections are missing from the output:
  - Token refresh flow: Describe the conditions under which a token is refreshed,
    the endpoint used, the request/response schema, and error handling behavior.
  - Token revocation flow: Describe how tokens are invalidated, including the
    revocation endpoint, expected response codes, and downstream effects on
    active sessions.

Add these sections before resubmitting.
```

---

### Example 2 — Limitations Failure

**Task:** Provide configuration recommendations for a production database.

**Deficiency:** The output presents connection pool settings as universally optimal without disclosing that the values are calibrated for a specific workload profile.

**Feedback returned:**

```
REVISION REQUIRED

Check failed: Limitations

The recommended connection pool values (min: 5, max: 50) are presented without
qualification. These values are workload-dependent and may degrade performance
under different traffic patterns.

Add a Limitations or Assumptions section that:
  - States the workload profile (e.g., request rate, average query duration) under
    which these values were derived.
  - Notes that operators should benchmark and adjust these values for their
    specific environment.
  - Lists any database engine version constraints that affect applicability.
```

---

### Example 3 — Differentiation Failure

**Task:** Compare synchronous and asynchronous task processing models.

**Deficiency:** The output describes both models but does not explicitly state the conditions under which one is preferred over the other.

**Feedback returned:**

```
REVISION REQUIRED

Check failed: Differentiation

The comparison section lists characteristics of each model but does not provide
decision criteria for selecting between them.

Add a subsection titled "Selection Criteria" that explicitly states:
  - Conditions under which synchronous processing is appropriate (e.g., tasks
    requiring immediate response, low-latency requirements, sequential dependencies).
  - Conditions under which asynchronous processing is appropriate (e.g., long-running
    tasks, decoupled producers and consumers, tolerance for eventual consistency).
  - Any scenarios where the two models may be combined and the implications of
    doing so.
```

---

## Summary of Changes

| Area | Before | After |
|------|--------|-------|
| Approval criteria | Implicit; lenient | Explicit; four mandatory checks |
| Feedback on rejection | Generic or absent | Specific, check-referenced, actionable |
| Partial pass handling | Could result in approval | Always results in `revise` |
| Limitations requirement | Optional | Mandatory |
| Differentiation requirement | Not evaluated | Explicitly checked |

---

## Related Files

- `path/to/reviewer_logic_file.ext` — Core reviewer logic implementation containing the updated check functions and decision flow.
- Test suite for reviewer logic — Covers approval paths, single-check failures, and multi-check failures to validate correct behavior across all scenarios.