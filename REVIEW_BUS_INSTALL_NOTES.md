# Review Bus install notes

Drop these files into the root of `evhedric/ai-build-system`.

Then add the required GitHub Actions secret:

```text
OPENAI_API_KEY
```

Optional repo variable:

```text
REVIEW_OPENAI_MODEL=gpt-4o
```

The workflow runs only when a GitHub issue comment is exactly:

```text
/chatgpt-review
```

The broker only posts issue comments. It does not invoke the existing executor pipeline.
