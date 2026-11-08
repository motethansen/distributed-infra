"""DI-071: one secret into a project's .env.deploy, checked first, never printed."""
import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy_secret", ROOT / "scripts" / "deploy_secret.py")
ds = importlib.util.module_from_spec(spec)
sys.modules["deploy_secret"] = ds
spec.loader.exec_module(ds)


def run_remote(path, var, value):
    """Execute the very script that is sent to the machines, against a temp file."""
    ns = {}
    sys.stdin = io.StringIO(json.dumps({"path": str(path), "var": var, "value": value}))
    try:
        exec(compile(ds.REMOTE, "remote", "exec"), ns)
    finally:
        sys.stdin = sys.__stdin__


def test_the_written_file_keeps_other_lines_replaces_in_place_and_is_mode_600(tmp_path, capsys):
    f = tmp_path / "x" / ".env.deploy"
    f.parent.mkdir()
    f.write_text("A=1\nCLOUDFLARE_ACCOUNT_ID=acc\n")
    run_remote(f, "TOKEN_X", "secretvalue")
    assert f.read_text().split("\n")[:3] == ["A=1", "CLOUDFLARE_ACCOUNT_ID=acc", "TOKEN_X=secretvalue"]
    run_remote(f, "TOKEN_X", "newvalue")
    assert f.read_text().count("TOKEN_X=") == 1 and "TOKEN_X=newvalue" in f.read_text()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    run_remote(f, "TOKEN_X", None)
    assert "TOKEN_X" not in f.read_text() and "A=1" in f.read_text()


def test_get_var_strips_quotes(tmp_path):
    f = tmp_path / ".env.deploy"
    f.write_text("CLOUDFLARE_ACCOUNT_ID='abc'\nOTHER=\"d\"\n")
    assert ds.get_var(f, "CLOUDFLARE_ACCOUNT_ID") == "abc" and ds.get_var(f, "OTHER") == "d" and ds.get_var(f, "NOPE") == ""


def fake_cf(monkeypatch, verify_ok=True, pages=("budgetapp-web",), pages_ok=True):
    def cf(path, token):
        if "tokens/verify" in path:
            return {"success": verify_ok, "result": {"status": "active"} if verify_ok else None, "errors": [] if verify_ok else [{"message": "Invalid API Token"}]}
        if path.endswith("/pages/projects"):
            return {"success": pages_ok, "result": [{"name": n} for n in pages], "errors": [] if pages_ok else [{"message": "Authentication error"}]}
        return {"success": False}
    monkeypatch.setattr(ds, "cf", cf)


def test_a_good_token_has_no_problems(monkeypatch):
    fake_cf(monkeypatch)
    assert ds.verify_cloudflare("t", "acc", "budgetapp-web") == []


def test_an_inactive_token_is_refused(monkeypatch):
    fake_cf(monkeypatch, verify_ok=False)
    assert "not active" in ds.verify_cloudflare("t", "acc", "budgetapp-web")[0]


def test_a_token_without_the_pages_permission_says_what_it_needs(monkeypatch):
    fake_cf(monkeypatch, pages_ok=False)
    p = ds.verify_cloudflare("t", "acc", "budgetapp-web")
    assert "Cloudflare Pages, Edit" in p[0]


def test_a_token_for_an_account_without_that_project_says_so(monkeypatch):
    fake_cf(monkeypatch, pages=("other",))
    assert "budgetapp-web" in ds.verify_cloudflare("t", "acc", "budgetapp-web")[0]


@pytest.mark.parametrize("text", ["", "short", "has a space in the middle of it 1234567890", "TOKEN=abcdefghijklmnopqrstuvwxyz"])
def test_input_that_is_not_one_secret_is_refused_before_anything_is_written(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    called = []
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit) as e:
        ds.main(["budgetapp", "CLOUDFLARE_API_TOKEN"])
    assert "nothing changed" in str(e.value) and called == []


def test_a_bad_cloudflare_token_is_refused_before_anything_is_written(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 40))
    fake_cf(monkeypatch, verify_ok=False)
    called = []
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit) as e:
        ds.main(["budgetapp", "CLOUDFLARE_API_TOKEN"])
    assert "nothing changed" in str(e.value) and called == []


def test_the_secret_is_never_printed_or_on_a_command_line(monkeypatch, capsys):
    secret = "s3cr3t-" + "z" * 40
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    fake_cf(monkeypatch)
    seen = []

    def run(cmd, input=None, **k):
        seen.append((cmd, input))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
    monkeypatch.setattr(ds.subprocess, "run", run)
    monkeypatch.setattr(ds, "worker_hosts", lambda: {"w": "10.0.0.1"})
    assert ds.main(["budgetapp", "CLOUDFLARE_API_TOKEN"]) == 0
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    assert all(secret not in " ".join(cmd) for cmd, _ in seen) and all(secret in (i or "") for _, i in seen)
