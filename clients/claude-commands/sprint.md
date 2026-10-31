---
description: Start a sprint for a project, step by step — or resume the proposal you were reviewing
allowed-tools: Bash(dis-agent:*)
model: haiku
---
You are walking the human product owner through starting a sprint. The first word of $ARGUMENTS is the project; the rest (if any) is what the sprint should deliver. Go one step at a time and wait for his answer between steps.

0. Run `dis-agent next -p <project>`. If it reports a sprint proposal waiting for him, DO NOT start a new one (a new Product Owner round costs Opus money and replaces the proposal): show him the stories from that proposal (`dis-agent backlog -p <project>` for context; the proposal itself is in the output of the last `po` run) and continue at step 3 with that sprint number.
1. Otherwise run `dis-agent backlog -p <project>` and show the open work in two lines. If no goal was given, propose a goal in one sentence from the in-flight and planned stories and ask him to confirm or change it.
2. The Product Owner uses Opus (capped, about $0.3–0.6 a call). Say so, and on his yes run `dis-agent po -p <project> "<goal>"`. Do not pass -s: it uses the next free sprint number; note the number it prints and use it as <n> from now on. Show the stories, points, concerns and which agents were briefed.
3. Ask for his feedback. For each round run `dis-agent po -p <project> -s <n> --revise "<his feedback, verbatim>"` (earlier rounds are kept automatically). Repeat until he accepts.
4. For each story he wants built run `dis-agent plan -p <project> -s <n> --story <story id>` and show the table (task, role, agent, machine). Nothing is queued by this. Stories marked human-only are steps for him, not for agents: list them as his to-do.
5. When he says to approve (for example "approve S-1" or "approve and go"), run `dis-agent approve <plan file>` and then `dis-agent go <plan file>`, running `go` in the background because it takes minutes. Tell him how to follow it: `/review`, or Vantage at http://100.76.214.54:8055/scrum.html. If the project is urbanlife, stop: it is isolated and needs his explicit approval in this chat first.
6. When the run finishes show `dis-agent review -p <project> -s <n>`: validation per criterion, review findings, PRs.

Never pass --force. Closing a story is the separate `/close` command and only on his explicit ask.
