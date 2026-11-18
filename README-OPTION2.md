# Option 2 draft (not deployed)

Local branch `option2-silent-pine`, based on `origin/main` at
`466f58e` (the commit currently serving production). This worktree is
`/workspace/iml-mindump/option2-draft`. Upstream was unset; this branch
has not been pushed.

The new behavior is behind `ELEVENLABS_DIRECT_ANSWER` (default off).
When on, `/api/voice` rings `FORWARD_TO_NUMBER` with no Polly line, and a
missed dial hands off silently to Pine. Existing press-1 consent is
unchanged until that flag is turned on.

See `/workspace/iml-mindump/2026-11-18-option2-plan.md` for the plan,
approval steps, rollback, and compliance notes.
