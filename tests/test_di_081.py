"""DI-081: Moltbook engagement plan -- structure, citation discipline, fetch-log hygiene of docs/moltbook-engagement-plan.md."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "moltbook-engagement-plan.md"


@pytest.fixture(scope="module")
def text():
    assert DOC.exists(), "docs/moltbook-engagement-plan.md missing"
    return DOC.read_text()


def section(text, prefix):
    """Body of the '## <prefix>...' section up to the next '## ' heading."""
    m = re.search(r"^## " + re.escape(prefix) + r".*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, f"section {prefix} missing"
    return m.group(1)


def git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)


def test_ac1_only_the_doc_is_added():
    for base in ("origin/main", "main"):
        r = git("diff", "--name-only", f"{base}...HEAD")
        if r.returncode == 0:
            break
    else:
        pytest.skip("no main ref")
    files = [f for f in r.stdout.split("\n") if f and not f.startswith("tests/test_di_081")]
    assert files == ["docs/moltbook-engagement-plan.md"]


def test_ac2_header_and_section_order(text):
    head = text[:3000]
    for k in ("Author agent", "Model", "Date", "Machine used", "Fetch method"):
        assert k in head
    assert "plain HTTP GET, no credentials, no cookies, no login" in head
    assert re.search(r"\| Date \| \d{4}-\d{2}-\d{2} \|", head)
    heads = re.findall(r"^## (.+)$", text, re.M)
    want = ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Verified facts vs assumptions", "Decision list", "Sources", "Appendix A"]
    idx = []
    for w in want:
        hit = [i for i, h in enumerate(heads) if h.startswith(w)]
        assert hit, f"heading {w} missing"
        idx.append(hit[0])
    assert idx == sorted(idx)
    assert any(h.startswith("Appendix A: fetch log") for h in heads)


def sources_rows(text):
    body = section(text, "Sources")
    return [l for l in body.splitlines() if re.match(r"\| S\d+ \|", l)]


def test_ac3_sources_table(text):
    rows = sources_rows(text)
    assert len(rows) >= 10
    kinds = ("primary platform page", "platform repo", "reputable press", "security vendor", "archive copy", "other")
    ids = []
    for r in rows:
        c = [x.strip() for x in r.strip("|").split("|")]
        assert len(c) >= 5
        ids.append(c[0])
        assert re.search(r"https?://|local file", c[1]), r[:80]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", c[2]), r[:80]
        assert any(c[3].startswith(k) for k in kinds), c[3]
        assert c[4]
        if "web.archive.org" in c[1]:
            assert "archive copy" in c[3] and re.search(r"snapshot", r, re.I)
    assert len(ids) == len(set(ids))


FACT_SKIP = re.compile(r"^(\s*$|\||#|---|```|\*\*How to read|Recommendation|\*\*Recommendation)")


def test_ac4_factual_sentences_tagged(text):
    bad = []
    for q in "1234567":
        body = section(text, f"Q{q}.")
        in_code = False
        head_tagged = False
        for line in body.splitlines():
            if line.startswith("#"):
                head_tagged = bool(re.search(r"\[S\d+\]", line))
                continue
            if head_tagged:
                continue
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or FACT_SKIP.match(line) or re.match(r"\s*\d+\. ", line) or re.match(r"(I|My) ", line.strip().lstrip("-* ")):
                continue
            # a line-ending sentence must carry a tag, or be marked as opinion/question/heading-like
            s = line.strip().lstrip("-*0123456789. ").strip()
            if len(s) < 60 or "See the Decision list" in s:
                continue
            if re.search(r"\[S\d+\]|\[Assumption|Recommendation|\bI recommend\b|\?$|^\*\*[^*]+\*\*:?$", s):
                continue
            bad.append((q, s[:90]))
    assert not bad, bad[:10]


def test_ac4_recommendations_marked(text):
    assert text.count("Recommendation:") >= 5


def test_ac5_q1_content(text):
    q = section(text, "Q1.")
    for n in ("Moltbook", "Moltbot", "OpenClaw"):
        assert n in q
    assert re.search(r"20\d\d-\d\d-\d\d|\d{1,2} \w+ 20\d\d", q)
    assert "2026-11-12" in q
    assert re.search(r"Meta", q) and re.search(r"acqui", q, re.I)
    assert re.search(r"fund|investor|raised", q, re.I) or "not" in q
    assert "DI-060" in q and "2026-11-06" in q
    tail = q[q.index("DI-060 claim"):]
    assert re.search(r"confirmed|refuted|still unverified", tail, re.I)
    rows = [l for l in tail.splitlines() if l.startswith("|") and "---" not in l][1:]
    assert len(rows) >= 3
    for r in rows:
        assert re.search(r"confirmed|refuted|unverified|not verifiable", r, re.I), r[:80]
        assert re.search(r"\[?S\d+", r), r[:80]


def test_ac6_q2_content(text):
    q = section(text, "Q2.")
    for term in ("Terms of service", "API", "skill", "rate limit", "verification", "ban"):
        assert term.lower() in q.lower(), term
    assert re.search(r"authenticat", q, re.I)
    assert re.search(r"human", q, re.I)
    assert re.search(r"not published \[Assumption", q)
    last = [l for l in q.strip().splitlines() if l.strip()][-1] if not q.strip().endswith("---") else \
        [l for l in q.strip().splitlines() if l.strip() and l.strip() != "---"][-1]
    sec = q[q.index("Verdict for Q2"):].split("\n\n")[1]
    assert "\n" not in sec.strip()
    assert re.search(r"\b(permitted|forbidden|unclear)\b", sec, re.I)


def test_ac7_q3_threat_table(text):
    q = section(text, "Q3.")
    rows = [l for l in q.splitlines() if l.startswith("|") and "---" not in l]
    header = rows[0].lower()
    for col in ("threat", "evidence", "likelihood", "impact", "control", "fleet"):
        assert col in header
    body = rows[1:]
    assert len(body) >= 7
    blob = "\n".join(body).lower()
    for k in ("prompt injection", "hidden", "skill", "clawhub", "exfiltrat", "impersonat", "spam", "legal", "privacy", "cost"):
        assert k in blob, k
    assert re.search(r"20\d\d-\d\d-\d\d", blob)
    assert re.search(r"(agents|config|scripts|bridge|[a-z_]+)/[\w./-]+\.(py|yaml|sh)", blob)
    for r in body:
        assert re.search(r"\[S\d+\]", r), r[:60]
        assert re.search(r"\|\s*\**[LMH]\**\s*\|\s*\**[LMH]\**\s*\|", r), r[:80]


def test_ac8_q4_controls(text):
    q = section(text, "Q4.").lower()
    for k in ("spend cap", "tools_off", "post_draft", "reply_draft", "do_nothing", "whatsapp", "rate limit", "kill switch",
              "audit log", "di-058", "di-059", "di-060", "di-031", "d-006", "degraded", "red-team", "no shell"):
        assert k in q, k
    m = re.search(r"agents/[\w./]+\.py:\d+", q)
    assert m, "tools_off file:line missing"
    path, line = m.group(0).rsplit(":", 1)
    f = ROOT / path
    assert f.exists()
    src = f.read_text().splitlines()
    ln = int(line)
    assert ln <= len(src)
    assert "tools_off" in "\n".join(src[max(0, ln - 6):ln + 6])
    fx = q[q.index("red-team fixture suite"):]
    items = re.findall(r"^\d+\. ", fx, re.M)
    assert len(items) >= 8
    assert re.search(r"no state change|only .?do_nothing", fx)


def test_ac9_q5_value(text):
    q = section(text, "Q5.")
    for k in ("research", "botblog", "budgetapp.vizneo.com", "exposure", "learning", "token", "hosting", "review time", "risk"):
        assert k in q.lower(), k
    verdicts = re.findall(r"Verdict on Q5[^:]*:\s*\**\s*(DO NOW|LATER|NEVER)", q)
    assert len(verdicts) == 1
    if verdicts[0] == "LATER":
        assert re.search(r"trigger", q, re.I)
    tail = q[q.index("Verdict on Q5"):]
    assert 3 <= len(set(re.findall(r"\[S\d+\]", tail))) <= 5


def test_ac10_q6_relations(text):
    q = section(text, "Q6.")
    for k in ("DI-060", "DI-009", "DI-010", "simtrader"):
        assert k in q
    paras = [p for p in q.split("\n\n") if p.strip().startswith("**")]
    assert len(paras) >= 5
    assert re.search(r"overlap", q, re.I) and re.search(r"reuse", q, re.I) and re.search(r"conflict", q, re.I)
    assert re.search(r"Mastodon[^\n]*\?\s*(Yes|No)\b", q)


def test_ac11_q7_phases(text):
    q = section(text, "Q7.")
    for p in ("P0", "P1", "P2", "P3"):
        assert p in q
    rows = [l for l in q.splitlines() if re.match(r"\| \*\*P[0-3]", l)]
    assert len(rows) == 4
    for r in rows:
        c = [x.strip() for x in r.strip("|").split("|")]
        assert c[2] in {"1", "2", "3", "5", "8"}, c[2]
        assert all(c[3:6])
    assert re.search(r"do_nothing", q) and re.search(r"cap", q) and re.search(r"ToS|terms", q, re.I)
    assert re.search(r"\d", rows[1].split("|")[-2])


def test_ac12_decisions(text):
    d = section(text, "Decision list")
    for n in range(1, 7):
        row = re.search(rf"^\| \*\*D{n}\*\* \|(.*)$", d, re.M)
        assert row, f"D{n}"
        c = [x.strip() for x in row.group(1).split("|")]
        assert len(c) >= 4 and c[1] and c[2], f"D{n} lacks options/default"
    assert re.search(r"D1\*\* \| Lift or keep", d)
    assert "no Moltbook" in d
    assert re.search(r"D2\*\* \| Spend cap", d) and re.search(r"D3\*\* \| Account name", d)
    assert re.search(r"D6\*\* \| Mastodon", d)


def test_ac13_fetch_log_is_get_only(text):
    a = text[text.index("## Appendix A"):]
    assert not re.search(r"\b(POST|PUT|DELETE|PATCH)\b", a.replace("no POST", ""), re.I) or "GET" in a
    urls = re.findall(r"https?://[^\s|)`]+", a)
    assert len(urls) >= 20
    for u in [u for u in urls if "moltbook.com" in u]:
        assert not re.search(r"/(register|claim|login|signin|post|posts)(/|\?|$)", u, re.I), u
        assert not re.search(r"\.(zip|tar|tgz|exe|sh|whl)$", u), u
    rows = [l for l in a.splitlines() if re.match(r"\| \d+ \|", l)]
    assert rows
    for r in rows:
        assert re.search(r"\|\s*(\d{3})\s*\|", r), r[:80]
    assert re.search(r"\bGET\b", a)
    assert not re.search(r"curl[^\n]*(-X\s*(POST|PUT|DELETE)|-d\s|--data)", a)
    assert not re.search(r"(?i)\b(npm|pip|clawhub)\s+install\b", a)


def test_ac14_citation_check_recorded_in_doc(text):
    # the PR body is not testable offline; the document records the check too
    assert re.search(r"seed \d+", text)
    assert re.search(r"12 checked", text)
    assert re.search(r"match|mismatch|unreachable", text)
