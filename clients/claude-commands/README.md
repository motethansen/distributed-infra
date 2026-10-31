# Claude Code / T3 Code slash commands for the fleet

Copy these into `~/.claude/commands/` (the MacBook already has them). They run `dis-agent` and use the cheapest
Claude model (`model: haiku`) for the chat, so chatting costs almost nothing; the expensive models only run when a
command asks the fleet (Product Owner = Opus, capped; Scrum Master = Sonnet).

`/scrum` `/review` `/backlog <project>` `/sm <project> <question>` `/po <project> <sprint> <request>` `/groom <project>`

Closing a story always needs the human's own yes in a terminal (`dis-agent close`); no command passes `--yes`.
