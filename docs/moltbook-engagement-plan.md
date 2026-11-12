# DI-081: Moltbook engagement plan (research only, no connection)

| | |
|---|---|
| Author agent | Claude Code research agent (task t1 of 4) |
| Model | Sonnet 5.5 (`claude-sonnet-5-5`) |
| Date | 2026-11-12 |
| Machine used | `michaelhansen-ThinkPad-X13-Gen-4` (the thinkpad), shell `curl` only |
| Fetch method | plain HTTP GET, no credentials, no cookies, no login |
| Status | Work in progress: Q1 and Q2 written; Q3 to Q7 are TODO for later tasks |

**Boundary.** This document is research. It does **not** lift the DI-060 boundary ("no Moltbook": install no Moltbot/OpenClaw, connect no agent to Moltbook). Only Michael's answer to decision D1 in the PR thread can change it. Nothing here was installed, registered, posted or connected. [S0]

**How to read the tags.** `[S#]` points to the Sources table. `[Assumption]` means I did not read it on a page; the "Verified facts vs assumptions" section says how to confirm it. "Recommendation:" marks an opinion. "Secondary" means a source that reports on the primary one (for example Wikipedia or a news site); a secondary source is never the only support for a claim.

**Terms used once.** *Agent* = an AI program that acts on its own. *Moltbook* = a Reddit-style website meant for AI agents. *OpenClaw* = open-source agent software, formerly called Moltbot. *Skill* = a text file of instructions plus tools that an OpenClaw agent loads. *Prompt injection* = text on a page that tries to make an AI reader obey it. *API* = the web interface programs use instead of a browser.

---

## Q1. What Moltbook and the Moltbot / OpenClaw ecosystem are today

### Names and renamings

| Date | Event | Source |
|---|---|---|
| 2025-11-24 | The OpenClaw GitHub repository `openclaw/openclaw` was created (first published as "Warelay"). | [S9], [S8] (secondary for the old name) |
| 2026-01-27 | Renamed "Moltbot", reported as following trademark complaints by Anthropic. | [S8] (secondary only; Assumption for the reason) [Assumption] |
| 2026-01-28 | Moltbook launched by Matt Schlicht. | [S7] (secondary) |
| 2026-01-30 | Renamed "OpenClaw". | [S8] (secondary) |
| 2026-02-14 | Creator Peter Steinberger wrote that he is joining OpenAI and that OpenClaw "will move to a foundation and stay open and independent". | [S10] (primary: his own blog), [S11] (press) |

Today the skill file Moltbook publishes still carries a `moltbot` key in its metadata, so the old name survives in the platform's own files. [S4]

### Who owns and runs what

- **OpenClaw**: MIT-licensed open source; the repository is `openclaw/openclaw`. [S9] The OpenClaw Foundation was announced as a 501(c)(3) non-profit with OpenAI, NVIDIA, Microsoft and Tencent as sponsors; Steinberger is quoted: "OpenAI hired me, not OpenClaw." [S12] (a news report of the foundation's announcement; I did not read the foundation's own page).
- **Moltbook**: the terms are an agreement with "Moltbook, LLC", last updated 2026-03-15. [S2] The footer says "Built for agents, by agents*" with "some human help from @mattprd". [S1] Neither the terms nor the privacy policy names Meta (searched the saved text for "Meta"). [S2][S3]
- **Meta acquisition**: PYMNTS (10 March 2026) reports that Meta confirmed the acquisition through a spokesperson and that the co-founders join Meta Superintelligence Labs. [S13] The Decoder says the price was not disclosed and the deal was expected to close in mid-March. [S14] Verdict repeats it on 2026-03-11 with a start date of 16 March. [S15] These are all press reports; I could not read a Meta announcement itself. [Assumption: that Meta published or confirmed it in its own channel; the quoted spokesperson statement is relayed by PYMNTS.]

### Status observed today (2026-11-12)

- The Moltbook home page and its terms, privacy policy, `rules.md` and developer page return HTTP 200. [S1][S2][S3][S5][S6] The home page counters (agents, posts, comments) read "0" in the page as fetched, because they are filled in by JavaScript that plain GET does not run; so I could not observe live size today. [S1]
- A public read endpoint (`GET /api/v1/submolts?limit=1`) answered `{"success":true,"submolts":[...]}` without any credential, so the service is running. [S16]
- The OpenClaw repository was last pushed on 2026-11-12 and is not archived. [S9]

### Size claims, each tied to who made it

| Claim | Who made it | Date | Source |
|---|---|---|---|
| 206,839 "human-verified" agents and 2,895,874 registered | The Moltbook site, as quoted by Wikipedia | 2026-06-06 | [S7] (secondary; I could not read the figure live) |
| 1.5 million agents belonging to only about 17,000 human owners | Wiz researchers, as quoted by Wikipedia | Feb 2026 | [S7] (secondary) |
| OpenClaw: 394,406 GitHub stars, 82,877 forks | GitHub API, read today | 2026-11-12 | [S9] |
| OpenClaw: 247,000 stars, 47,700 forks | Wikipedia | 2026-03-02 | [S8] (secondary) |
| Whether agent posts are really autonomous | Disputed by CNBC, The Verge, Wired, MIT Technology Review as summarised by Wikipedia | 2026 | [S7] (secondary) |

Recommendation: treat every Moltbook size figure as marketing, not fact; the Wiz figure (1.5 million agents, 17,000 owners) suggests most "agents" are one human running many. [S7]

### Re-check of the claims the 2026-11-06 DI-060 note recorded as unverified

| DI-060 claim | Result today | Source |
|---|---|---|
| Moltbot was renamed OpenClaw | **Confirmed** (secondary) and the repo and site use the OpenClaw name. | [S8][S9] |
| Moltbook is an agents-only social network | **Confirmed**: its home page says "A Social Network for AI Agents ... Humans welcome to observe." | [S1] |
| Moltbook size claims | **Not verifiable first-hand**: counters render empty to plain GET; only a secondary figure from June 2026. | [S1][S7] |
| Meta acquired Moltbook | **Confirmed by several press reports** (10 to 11 March 2026); price undisclosed; no Meta primary page read. | [S13][S14][S15] |
| "A count of Moltbook posts with hidden injections" | **Count not verified.** Wikipedia says researchers at Vectra AI and PointGuard AI identified the platform as a vector for indirect prompt injection; I did not read those reports. Re-check in Q3. | [S7] (secondary) |
| ClawHub: 341 malicious of 2,857 skills, about 7% leak credentials, tens of thousands of exposed instances | **Not re-checked in this task.** TODO: Q3 (task t2). | |

### Findings: text addressed to AI agents (safety rule 1)

I did not act on any of these. They are recorded as findings.

1. `https://www.moltbook.com/` (2026-11-12): "Read https://www.moltbook.com/skill.md and follow the instructions to join Moltbook" shown to every visitor. [S1]
2. `https://www.moltbook.com/skill.md` (2026-11-12): a file written to an AI reader, with install commands (`curl ... > ~/.moltbot/skills/moltbook/SKILL.md`), a registration `POST`, and "Or just read them from the URLs above!". [S4]
3. Same file: it tells the agent to re-fetch `heartbeat.md` "every 30 minutes" and "follow it". Recommendation: this means the platform operator can change what a connected agent does at any time just by editing a text file; that is a standing remote-instruction channel by design. [S4]
4. `https://www.moltbook.com/developers` (2026-11-12): a "Copy this prompt into Cursor, Copilot, or any AI coding assistant" block telling a coding assistant to read `developers.md` and implement it. [S6]

---

## Q2. Is engagement allowed and possible

### Terms of service (last updated 2026-03-15) [S2]

- **Who is the user.** The user is a human with an X (Twitter) account: "you must sign on to Moltbook using your X ... account". AI agents "are not granted any legal eligibility". [S2]
- **Humans may run agents.** The terms apply to the human and "any artificial intelligence agents associated with your account"; the human is "solely responsible" for every act of the agent, "regardless of the degree of control, supervision, or oversight", autonomous or not. [S2]
- **Suspension.** Moltbook may suspend or deny access at its discretion, and also if an agent is "likely to be involved in any fraudulent, misleading, or unlawful activities". [S2]
- **Prohibited, applied to the agent too:** harassment; posting private personal information about anyone without consent; impersonation; unauthorized advertising, marketing, spam or commercial sales content; promoting or selling cryptocurrency (listed under Restricted Content); harvesting other users' or agents' data; and "scrape or otherwise collect any data", or using "any robot, spider ... or other automated device" to access or index the Services. [S2]
- **Tension to resolve (Recommendation):** the same terms ban automated access, yet the platform's own API and `skill.md` are built for automated agents. [S2][S4] A written answer from the platform would settle it; I made no contact (out of scope). So automated reading by a fleet bot is allowed in practice but unclear in law. [Assumption]
- **Your content.** You keep ownership but grant Moltbook a perpetual, irrevocable, worldwide, sublicensable licence to it. [S2]
- **Liability.** You indemnify Moltbook for anything your agent does; Moltbook's liability is capped at US$100; California law, courts in the Northern District of California or San Mateo County. [S2]
- **No monitoring.** Moltbook states it has no obligation to monitor or police AI-generated content. [S2]

### Privacy policy [S3]

It collects the X user ID, handle and e-mail, and usage data (IP, device, pages), and may publish the username or social handle with the agent's activity. [S3] Recommendation: an agent account is therefore tied to a real X identity; the "which account name" decision (D3) is a public-identity decision.

### Official API or only the skill route

- There is an official REST API at `https://www.moltbook.com/api/v1`, documented in `skill.md`: register, read feeds and posts, post, comment, up/downvote, follow, search, submolts, and a `/home` dashboard. [S4] The skill file is just instructions; any HTTP client can use the same API. Whether OpenClaw is required: **no**; the documented path is plain `curl` calls. [S4]
- A separate developer program lets third-party apps verify a Moltbook bot identity ("Sign in with Moltbook"); it is "early access", key prefix `moltdev_`. This is not needed for posting. [S6]

### Authentication model

- An agent calls `POST /agents/register` and gets an API key (`moltbook_...`), a claim URL and a verification code. The human then verifies by e-mail and posts a verification tweet to activate the agent. [S4]
- Requests carry `Authorization: Bearer <key>`. The file warns to send the key only to `www.moltbook.com` because requests to the bare domain redirect and drop the header. [S4]
- The home page says ownership is verified "via X" and shows a claim step "Tweet to verify ownership". [S1] (Terms say login via X; skill file says e-mail plus tweet. Both are described; I did not test either.)

### Rate limits and new-account limits (the platform's own rules page) [S5]

| Action | Established agent | New agent (first 24 h) |
|---|---|---|
| Posts | 1 per 30 min | 1 per 2 h |
| Comments | 1 per 20 s, 50 per day | 1 per 60 s, 20 per day |
| Direct messages | allowed | blocked |
| Submolt creation | 1 per hour | 1 total |
| API requests | 100 per minute | stated for all |

Also, every post, comment or submolt may return a **verification challenge** (an obfuscated "lobster physics" math puzzle, 5 minutes to answer); 30 verification attempts per minute are allowed and "if your last 10 challenge attempts are all failures ... your account will be automatically suspended". "Trusted agents and admins bypass" it. [S4] Wikipedia describes this as a reverse CAPTCHA and notes critics say humans can bypass it with a script that asks an AI. [S7] (secondary)

### What an agent may read and post

It may read feeds, posts, comment trees (default 35 per page, max 100), search (max 50), and profiles including the owner's X handle; and it may post text or link posts, comment, vote, follow, create submolts and, once established, send DMs. [S4][S5] Everything it reads is other users' text; the home dashboard even returns post previews and "what_to_do_next" suggestions generated by the platform. [S4]

### Verification, moderation and bans [S5]

- Warning: off-topic, excessive self-promotion, low effort, duplicates.
- Restriction ("shadow cooldowns"): karma farming, vote manipulation, ignoring warnings.
- Suspension: 1 hour to 1 month.
- Permanent ban: spam, malicious content, API abuse, leaking API keys, ban evasion; the human is notified.
- The rules say the human is "accountable for your behavior" and that "Gross misconduct reflects on both of you". A reporting system is "coming soon". [S5]
- Rules page says "Last updated: February 2026" while the terms are dated 2026-03-15, so the rules may lag the terms. [S5][S2]

### Verdict for Q2 (Recommendation)

Engagement is **possible** (a documented API, no OpenClaw needed) and **allowed for a human-owned agent under the owner's full legal responsibility**, but the terms are broad, one-sided and in tension with automation. The owner would need an X account to log in and would carry unlimited indemnity for the bot's output. Promotion of the estate or the botblog would likely break the "no unauthorized advertising / marketing" clause. [S2]

---

## Q3. Threat model

TODO: written in a later task (task t2): prompt injection, ClawHub malicious skills (re-verify with primary or reputable sources and dates), exfiltration, impersonation, reputation harm, legal and privacy exposure, cost runaway; for each: likelihood, impact, control.

## Q4. Safe design for an "embassy bot"

TODO: written in a later task.

## Q5. What the owner would get

TODO: written in a later task.

## Q6. Relation to DI-060, DI-009, DI-010 and the simtrader research agent

TODO: written in a later task.

## Q7. Phased rollout, kill criteria, decisions for Michael

TODO: written in a later task.

---

## Verified facts vs assumptions

| # | Assumption | How to confirm |
|---|---|---|
| A1 | Moltbot was renamed after Anthropic trademark complaints (reason only secondary). | Read Steinberger's rename post or an Anthropic statement. |
| A2 | Meta's own channel confirmed the acquisition (only the PYMNTS-relayed spokesperson quote was read). | Find a Meta newsroom or Reuters/Axios page; The Next Web returned 403 to plain GET and was not read. |
| A3 | Automated agent access through the API is tolerated despite the terms' ban on "robot ... automated device". | Ask Moltbook in writing (needs Michael's approval; out of scope here), or read the API's own terms if any. |
| A4 | Live size of Moltbook today (counters render by JavaScript). | Read the JSON stats endpoint, if one exists, in a later task; or ask a browser-capable researcher. |
| A5 | Hidden-injection post counts and the ClawHub malicious-skill figures. | Task t2: read the Vectra AI, PointGuard and primary ClawHub audit reports. |

## Decision list

*(Empty until Q7. Provisional: D1 lift or keep the 2026-11-06 "no Moltbook" boundary; D2 spend cap; D3 account name; D4 who approves posts.)*

## Sources

| ID | URL | Accessed | Kind | Supports |
|---|---|---|---|---|
| S0 | `~/Projects/github/techstartups/vizneoprojects/distributed-infra/product-backlog.md` (DI-060 and DI-081 sections; local file) | 2026-11-12 | other | The DI-060 boundary and which claims were unverified. |
| S1 | https://www.moltbook.com/ | 2026-11-12 | primary platform page | Description, "humans welcome to observe", claim/tweet flow, counters empty, injection finding 1. |
| S2 | https://www.moltbook.com/terms | 2026-11-12 | primary platform page | Terms of Service (2026-03-15): X login, responsibility, prohibitions, indemnity, licence, law. |
| S3 | https://www.moltbook.com/privacy | 2026-11-12 | primary platform page | Data collected, no mention of Meta. |
| S4 | https://www.moltbook.com/skill.md | 2026-11-12 | primary platform page | API, auth, registration, challenges, heartbeat, `moltbot` key, injection findings 2 and 3. |
| S5 | https://www.moltbook.com/rules.md | 2026-11-12 | primary platform page | Rate limits, new-agent limits, moderation ladder, bans. |
| S6 | https://www.moltbook.com/developers | 2026-11-12 | primary platform page | "Sign in with Moltbook" early access; injection finding 4. |
| S7 | https://en.wikipedia.org/wiki/Moltbook | 2026-11-12 | other (secondary) | Launch date, Meta acquisition, size figures, authenticity dispute, reverse CAPTCHA, security reports. |
| S8 | https://en.wikipedia.org/wiki/OpenClaw | 2026-11-12 | other (secondary) | Rename chronology, Warelay, star counts in March, foundation. |
| S9 | https://api.github.com/repos/openclaw/openclaw | 2026-11-12 | platform repo | Created 2025-11-24, MIT, stars/forks today, last push, not archived. |
| S10 | https://steipete.me/posts/2026/openclaw | 2026-11-12 | primary (creator's blog, dated 14 Feb 2026) | Steinberger joins OpenAI; OpenClaw to a foundation. |
| S11 | https://euronews.com/next/2026/02/16/austrian-creator-of-viral-openclaw-joins-openai-to-build-next-generation-of-ai-agents | 2026-11-12 | reputable press | Same event, Altman quote. |
| S12 | https://www.trendingtopics.eu/opwnclaw-foundation-launches-with-openai-nvidia-microsoft-and-tencent-as-sponsors/ | 2026-11-12 | reputable press (secondary) | Foundation launch, 501(c)(3), sponsors. |
| S13 | https://www.pymnts.com/artificial-intelligence-2/2026/meta-acquisition-brings-moltbook-founders-to-superintelligence-labs/ | 2026-11-12 | reputable press | Meta acquisition, spokesperson quote, 2026-03-10. |
| S14 | https://the-decoder.com/meta-acquires-moltbook-the-reddit-style-platform-built-for-ai-agents/ | 2026-11-12 | reputable press | Price undisclosed, close mid-March. |
| S15 | https://www.verdict.co.uk/meta-acquires-moltbook/ | 2026-11-12 | reputable press | Acquisition dated 2026-03-11, start 16 March. |
| S16 | https://www.moltbook.com/api/v1/submolts?limit=1 | 2026-11-12 | primary platform page | Public read endpoint answers without credentials (service live). |

## Appendix A: fetch log

All: HTTP GET via `curl -sL -m 30 -A 'Mozilla/5.0 (research fetch)' --max-filesize 2000000`, no cookies, no login, no credentials, on 2026-11-12 from the thinkpad. Saved to `/tmp/di081/` and read as text; nothing executed.

| # | URL | HTTP | Note |
|---|---|---|---|
| 0 | https://www.google.com/robots.txt | 200 | connectivity check |
| 1 | https://www.moltbook.com/ | 200 | S1 |
| 2 | https://www.moltbook.com/terms | 200 | S2 |
| 3 | https://www.moltbook.com/privacy | 200 | S3 |
| 4 | https://www.moltbook.com/skill.md | 200 | S4; read as data only, not followed |
| 5 | https://www.moltbook.com/developers | 200 | S6 |
| 6 | https://www.moltbook.com/robots.txt | 404 | no robots file |
| 7 | https://github.com/openclaw/openclaw | 200 | fetched, not used (page too large; used the API instead) |
| 8 | https://openclaw.ai/ | 200 | fetched, not used in Q1/Q2 |
| 9 | https://en.wikipedia.org/wiki/Moltbook | 200 | S7 |
| 10 | https://en.wikipedia.org/wiki/OpenClaw | 200 | S8 |
| 11 | https://www.moltbook.com/rules.md | 200 | S5 |
| 12 | https://www.moltbook.com/heartbeat.md | 200 | fetched, not read (it is the remote-instruction file; TODO t2 read as data) |
| 13 | https://www.moltbook.com/developers.md | 200 | fetched, not read |
| 14 | https://thenextweb.com/news/meta-acquires-moltbook-ai-agent-social-network | 403 | **unreachable** (blocks automated fetch); nothing filled from memory |
| 15 | https://www.tomshardware.com/tech-industry/openai-hires-genius-openclaw-creator-... | 200 | fetched, not read |
| 16 | https://euronews.com/next/2026/02/16/... | 200 | S11 |
| 17 | https://api.github.com/repos/openclaw/openclaw | 200 | S9 |
| 18 | https://www.trendingtopics.eu/opwnclaw-foundation-launches-... | 200 | S12 |
| 19 | https://steipete.me/posts/2026/openclaw | 200 | S10 |
| 20 | https://the-decoder.com/meta-acquires-moltbook-... | 200 | S14 |
| 21 | https://www.verdict.co.uk/meta-acquires-moltbook/ | 200 | S15 |
| 22 | https://www.pymnts.com/artificial-intelligence-2/2026/meta-acquisition-brings-moltbook-founders-to-superintelligence-labs/ | 200 | S13 |
| 23 | https://www.moltbook.com/api/v1/submolts?limit=1 | 200 | S16; unauthenticated GET of a public read endpoint, first 300 characters read |

Also: two web searches (standard mode) were used to find press URLs; their summaries were not used as evidence, only the pages above.
