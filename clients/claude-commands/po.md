---
description: Ask the Product Owner (Opus, capped) to propose stories — uses Opus, so use it sparingly
allowed-tools: Bash(dis-agent:*)
model: haiku
---
This calls Claude Opus through the Product Owner role (monthly cap applies). The arguments are: <project> <sprint> then the request. Before running, repeat the request back in one line and ask me to confirm. Then run `dis-agent po -p <project> -s <sprint> "<request>"` with Bash. The PO reads the architecture notes and the open backlog itself. Show the stories, points, concerns and which agents it briefed. Nothing is queued by this command. For feedback rounds tell me to use `dis-agent planning` in a terminal, or `dis-agent po ... --revise "<feedback>"`.
