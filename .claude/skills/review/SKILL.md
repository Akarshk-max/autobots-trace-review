---
name: review
description: Review code changes for quality, security, and adherence to project patterns. Use /review for staged changes, /review path/to/file.py for a specific file.
user-invocable: true
context: fork
agent: code-reviewer
allowed-tools: "Bash(git diff *) Bash(git log *)"
---

# Code Review

Review code for quality, security, async correctness, and adherence to the Autobots service folder pattern.

If arguments provided, review specific files: read and analyze $ARGUMENTS
If no arguments, review staged/unstaged changes:

```!
git diff --stat
```

```!
git diff
```

Provide a structured review with issues ranked by severity.
