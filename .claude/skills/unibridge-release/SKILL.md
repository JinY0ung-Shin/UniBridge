---
name: unibridge-release
description: Release workflow for /home/jinyoung/UniBridge. Use when Claude Code needs to prepare, locally validate, tag, and publish a UniBridge GitHub release without relying on GitHub Actions; includes local frontend/backend/converter/script checks, tag selection, main push, and gh release creation.
---

# UniBridge Release

Use this skill only for `/home/jinyoung/UniBridge`.

## Workflow

1. Start from repo root and inspect state:
   - `git status --short --branch`
   - `git remote -v`
   - `gh auth status`
   - `gh release list --limit 10`
   - `git tag --sort=-version:refname | sed -n '1,20p'`
2. Keep unrelated user files out of the release commit. In this repo, root-level untracked `package.json` and `package-lock.json` have appeared before; ignore them unless the user explicitly includes them.
3. Run local checks with `scripts/run-local-release-checks.sh` from this skill:
   - Pass the repo path as the first argument if not already in `/home/jinyoung/UniBridge`.
   - Set `RUN_LIVE_E2E=1` only when `LLM_API_KEY` and a live deployment are configured.
4. If checks fail, fix or report the exact failing command. Do not tag or publish.
5. Ensure `main` is current:
   - `git fetch origin main`
   - `git rev-list --left-right --count main...origin/main`
   - push only after local `main` is not behind `origin/main`.
6. Commit intended release workflow or code changes with explicit paths only.
7. Choose the tag:
   - Use the user's requested tag if provided.
   - Otherwise use date format `vYYYY.MM.DD`.
   - If that tag exists, append `.1`, `.2`, etc.
8. Push `main`, create an annotated tag, push the tag, then create the GitHub Release:
   - `git push origin main`
   - `git tag -a <tag> -m "UniBridge <tag>"`
   - `git push origin <tag>`
   - `gh release create <tag> --target main --title "UniBridge <tag>" --notes "<summary>"`
9. Verify:
   - `git rev-parse HEAD origin/main`
   - `gh release view <tag>`

## Local Check Policy

Treat local checks as the release gate. GitHub Actions may be absent or disabled for this repo, so do not wait on Actions unless the user explicitly asks.

The bundled check script mirrors the removed CI surface:

```bash
/home/jinyoung/UniBridge/.claude/skills/unibridge-release/scripts/run-local-release-checks.sh /home/jinyoung/UniBridge
```

For live E2E:

```bash
RUN_LIVE_E2E=1 LLM_API_KEY=... /home/jinyoung/UniBridge/.claude/skills/unibridge-release/scripts/run-local-release-checks.sh /home/jinyoung/UniBridge
```

If `RUN_LIVE_E2E=1` is unset, live E2E is checked only for skip health and should be reported as skipped, not passed.
