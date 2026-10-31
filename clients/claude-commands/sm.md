---
description: Ask the Scrum Master (Sonnet) about a project — breakdown, order, who should do what
allowed-tools: Bash(dis-agent:*)
model: haiku
---
The first word of $ARGUMENTS is the project; the rest is my question for the Scrum Master. Run `dis-agent ask --role scrum_master -p <project> "<the question>"` with Bash and relay the answer. The Scrum Master answers from what you send, so include the relevant `dis-agent backlog -p <project>` output in the question when it helps. For a real sprint plan tell me to use `dis-agent plan` in a terminal.
