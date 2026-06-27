# AI Review Bus Protocol

The AI Review Bus is a GitHub Issue-based handoff channel for AI-to-AI review loops.

It is intentionally isolated from the existing executor pipeline. In this first version, it can only read GitHub issues, call OpenAI, and post GitHub issue comments. It cannot run migrations, push patches, merge pull requests, deploy, or execute commands from issue text.

## Goal

Stop using a human as the copy/paste middleman between AI systems.

The desired loop:

```text
Review Packet in GitHub issue
→ comment /chatgpt-review
→ GitHub Action runs
→ OpenAI reviews packet
→ ChatGPT Review comment is posted back to the issue
```

## Use cases

Use this for cleanroom review loops involving:

- schema design
- migration run/no-run decisions
- concurrency and lease/fencing reviews
- append-only stores
- shared/global catalogs or registries
- security-definer RPC review
- RLS/trust-boundary review

Do not use it for routine UI tweaks or one-off local tasks.

## Hard safety boundaries

The broker must not:

- run shell commands from issue text
- run database migrations
- call Supabase
- merge PRs
- deploy
- push commits
- trigger the existing Executor role
- read secrets from issue comments

It only writes GitHub issue comments.

## Command

Trigger a review by commenting exactly:

```text
/chatgpt-review
```

The script rejects any other command text, even if the GitHub Action starts.

## Review Packet format

Use the issue template at:

```text
.github/ISSUE_TEMPLATE/ai-review-packet.md
```

Packet shape:

```markdown
# Review Packet — <task name>

## Decision requested
RUN / NO-RUN / DESIGN REVIEW / PATCH REVIEW

## Current state
- Repository:
- Branch:
- Commit:
- Migration run? yes/no
- Feature flag state:

## Scope
Plain-English description.

## Load-bearing SQL / code
Literal snippets.

## Tests and proof
- TypeScript:
- Unit tests:
- Integration/SQL fixtures:
- Two-session/concurrency tests:
- Build/CI:

## Known risks / open questions
- Identity-key correctness:
- Immutability:
- Concurrency / race / fencing:
- Trust boundary:
- Time / scale:
- Migration safety:

## Specific questions for ChatGPT
1.
2.
3.

## Proposed next action
What should happen next?
```

## ChatGPT response format

The broker asks ChatGPT to return:

```markdown
# ChatGPT Review

## Verdict
RUN / NO-RUN / DESIGN BLOCKED / NEEDS PATCH

## Summary
One paragraph.

## Conformance check
Did the packet show that the implementation matches the requested design?

## How does this break?
- Identity-key correctness:
- Immutability:
- Concurrency / race / fencing:
- Trust boundary:
- Time / scale:
- Migration safety:

## Blockers
| # | Category | Problem | Required fix |
|---|---|---|---|

## Patch prompt for Code
```text
...
```

## Run checklist, if RUN
1.
2.
3.
```

## Cleanroom test

Create an issue with the included AI Review Packet template and a fake risky migration. Then comment:

```text
/chatgpt-review
```

Expected result: ChatGPT posts a NO-RUN review explaining the flaw.

## Current implementation

Files:

```text
.github/workflows/chatgpt-review.yml
.github/ISSUE_TEMPLATE/ai-review-packet.md
review_bus/chatgpt_review_issue.py
review_bus/github_issue.py
review_bus/packet.py
review_bus/prompts.py
```

Required GitHub Actions secret:

```text
OPENAI_API_KEY
```

Optional env:

```text
REVIEW_OPENAI_MODEL=gpt-4o
MAX_RECENT_COMMENTS=12
```
