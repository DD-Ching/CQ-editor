## CQ-Editor Agent Rules

- Keep the Codex generation flow stable and observable.
- Prefer single-pass generation when `Iterations = 1`.
- Only enable multi-stage review when the user explicitly asks for deeper iteration.
- Show clear status, failure reason, and next-step guidance in the UI.
- Always read the nearest `AGENTS.md` before changing CQ-editor files.
- In every final handoff, include one copy-paste terminal command block the user can run to verify the current result.
