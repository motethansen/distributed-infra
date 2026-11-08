---
description: What should I do next? Ranked suggestions, each with the exact command
allowed-tools: Bash(dis-agent:*)
model: sonnet
---
Run `dis-agent next` with Bash (add `-p <project>` if $ARGUMENTS names a project). If the output starts with "WAITING ON YOU FIRST", that block leads your answer, before anything ranked. Say how many answers I have recorded on the board that nobody has applied, and that a Confirm on the board records my answer and does NOT accept a story. Then, in this order: the stories I confirmed that I must check and accept (give the exact `dis-agent accept ... --yes` line, or say "use the Accept button on the board"), the team work that becomes a story (`queue-story`), my comments (only I can say what to do), and finally the finished stories ready to accept that I have not confirmed. Group long lists by project, do not read out every line. The count of confirms on already-closed stories is one sentence.

Then talk to me like a colleague, not a report:

- Lead with the one thing you would do first, in one plain sentence, and why.
- Then list up to two alternatives, one line each.
- For every item give the exact command or slash command to copy, and say in a few words whether it only reads, queues agent work, runs agents for minutes, or changes the backlog, and what it costs.
- Finish with one question: which should we do? If I say yes to something you are allowed to run, run it.
