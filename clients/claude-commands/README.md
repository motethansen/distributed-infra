# Claude Code / T3 Code slash commands for the fleet

Copy these into `~/.claude/commands/` (the MacBook already has them). They run `dis-agent` and use Sonnet
(`model: sonnet`) for the chat. Haiku was tried first and lost the thread when one message held several requests
(2026-11-08). The expensive models only run when a
command asks the fleet (Product Owner = Opus, capped; Scrum Master = Sonnet).

`/scrum` `/review` `/backlog <project>` `/sm <project> <question>` `/po <project> <sprint> <request>` `/groom <project>`
`/next [project]` what to do next, with exact commands · `/desk` what you can ask · `/sprint <project> [goal]` start a sprint step by step · `/close <project> <sprint> <story>` close a story you have reviewed

The human decides every state change. A command may approve, run or close only when he asks for it in that chat message, and `/close` shows the evidence first and relies on the CLI's own gates (no review, failed validation or unfinished tasks refuse). No command ever passes `--force`.
