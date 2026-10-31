---
description: Scrum of scrums across all projects — review queue, decisions, stalls, adjustments
allowed-tools: Bash(dis-agent:*)
model: haiku
---
Run `dis-agent scrum` with Bash. Then tell me in at most 8 lines: how many stories wait for my review (and which projects), what needs a decision, what has stalled, and the one adjustment you would make first. Quote story IDs. Do not invent anything that is not in the output. If I ask for the Scrum Master's own briefing, run `dis-agent scrum --narrate` instead.
