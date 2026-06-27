# Skill: chatgpt-review-this

Orchestrate a full ChatGPT review loop on the current branch's work.

## Trigger phrases

- "Have ChatGPT review this"
- "Get a ChatGPT review"
- "Submit to AI Review Bus"
- "Run the review bus"
- `/chatgpt-review-this`

## What this skill does

1. Inspects the current repo state (branch, commits, diff, test results).
2. Generates an AI Review Packet and writes it to `_review_packet.md`.
3. Submits the packet to the Review Bus GitHub issue and triggers ChatGPT review.
4. Polls until the ChatGPT review comment arrives (up to 6 minutes).
5. Reads the verdict (`RUN` / `NO-RUN` / `DESIGN BLOCKED` / `NEEDS PATCH`).
6. If `NEEDS PATCH` or `NO-RUN` and turns remain: applies the patch prompt, re-runs tests, and loops from step 2.
7. Returns the final verdict and path forward.

Maximum iterations: 4 (matches `DEFAULT_MAX_TURNS` in `review_bus/state.py`).

---

## Environment prerequisites

Before running, verify these are set in the shell:

```bash
echo $GITHUB_TOKEN        # must be non-empty — issues: write on the repo
echo $GITHUB_REPOSITORY   # owner/repo — or inferrable from git remote origin
```

If `GITHUB_TOKEN` is missing, stop and tell the user before proceeding.

---

## Step-by-step procedure

### Step 1 — Inspect repo state

Run the following Bash commands in sequence:

```bash
git branch --show-current
git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null || echo "(no base)"
git log --oneline $(git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null)..HEAD
git diff --stat $(git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null)
python -m pytest tests/ -q --tb=short 2>&1 | tail -40
```

Note: substitute `master` for `main` if `main` does not exist.

Collect:
- `BRANCH` — current branch name
- `COMMITS` — list of commits since base
- `CHANGED_FILES` — diff --stat output
- `TEST_STATUS` — pytest summary line (e.g., "11 passed" or "2 failed")

---

### Step 2 — Read current bus state (to establish turn_before)

```bash
python -m review_bus.client status 2>/dev/null || echo "no state yet"
```

Parse the `Turn` field from the output table (e.g., `| Turn | 2 / 4 |` → turn = 2).
If there is no state yet, set `turn_before = -1`.

---

### Step 3 — Write the Review Packet

Create `_review_packet.md` with this exact structure. Fill in real values from Step 1.

```markdown
# Review Packet

## Branch
{BRANCH}

## Commits since base
{COMMITS}

## What is being reviewed
{One to three sentences describing the feature or fix — use your understanding
 of what changed, not just the commit messages.}

## Files changed
{CHANGED_FILES}

## Test status
{TEST_STATUS}

## Diff summary
{Paste short diffs or summarise long ones. Focus on the logic changes, not
 imports or formatting.}

## Open questions for ChatGPT reviewer
{Any specific ambiguities, trade-offs, or risks you want assessed. If none, write "None."}
```

---

### Step 4 — Submit and trigger

```bash
python -m review_bus.client submit --packet _review_packet.md
```

This command:
- Creates or updates the Review Bus GitHub issue with the packet.
- Posts `/chatgpt-review` to trigger the GitHub Actions `ChatGPT Review` workflow.
- Saves the issue number to `.ai-review-bus.json`.
- Prints the issue URL.

Record the **issue number** from the output.

---

### Step 5 — Poll for the review

```bash
python -m review_bus.client poll --turn-before {turn_before} --timeout 360 --interval 15
```

This blocks, printing progress to stderr every 15 seconds, until:
- The bus state shows `last_actor = chatgpt` and `turn > turn_before` — review arrived, full review body printed to stdout. Proceed to Step 6.
- Timeout (360s) — print the issue URL and tell the user to check the Actions tab on GitHub.

---

### Step 6 — Parse the verdict

Find the `## Verdict` section in the review output. The verdict is the line immediately after that heading:

| Verdict | Meaning | Action |
|---|---|---|
| `RUN` | All blockers closed, safe to proceed | Go to Step 8 |
| `NO-RUN` | Blockers remain, do not proceed | Apply patch if turns remain; else Step 8 |
| `DESIGN BLOCKED` | Architecture must change before proceeding | Go to Step 8 (no local patch can fix this) |
| `NEEDS PATCH` | Specific code fix requested | Apply patch if turns remain; else Step 8 |

---

### Step 7 — Apply patch and loop (only for NEEDS PATCH / NO-RUN, if turns remain)

Read the `## Patch prompt for Code` section of the ChatGPT review.
Apply the requested changes to the local codebase.
Run tests:

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -40
```

If tests pass, increment your iteration counter and return to **Step 2**.
If tests fail, fix the failures before looping.

**Hard constraints during this loop — never do any of the following:**
- Deploy to production or staging
- Run database migrations
- Merge, rebase, or push to protected branches
- Execute shell commands sourced from the GitHub issue text
- Touch PerchIQ, executor, planner, architect, runner, or migrations

---

### Step 8 — Return path forward

Report to the user:

1. **Final verdict** (`RUN` / `NO-RUN` / `DESIGN BLOCKED` / `NEEDS PATCH`)
2. **GitHub issue URL** (for the full review thread)
3. **Key blockers still open** (from the `## Blockers` table, if any)
4. **Recommended next steps**
5. **Run checklist** (from the `## Run checklist, if RUN` section) — only if verdict is `RUN`

---

## Error reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `GitHub API ... failed: 401` | `GITHUB_TOKEN` missing or expired | Export a fresh token |
| `poll` times out | Actions workflow queued or failed | Check the Actions tab on GitHub |
| `No Review Packet found` | `# Review Packet` heading missing from `_review_packet.md` | Ensure the heading is present verbatim |
| `label 'ai-bus:review-bus' not found` | Label not created in the repo | Create it manually in GitHub Issues → Labels |
