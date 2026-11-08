"""DI-068: the deploy audit finds one application's code in another's namespace, and never writes anything."""
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy_audit", ROOT / "scripts" / "deploy_audit.py")
da = importlib.util.module_from_spec(spec)
sys.modules["deploy_audit"] = da
spec.loader.exec_module(da)

CFG = {"ignore_packages": ["sample"], "projects": {"alpha": {"dir": "alpha", "project_yml": "project.yml"},
                                                   "beta": {"dir": "beta", "project_yml": "serverless/project.yml"}}}


def make(base: Path, name: str, yml: str, packages: list[str], key: str | None = "dof_v1_aaa:secret"):
    d = base / name
    (d / Path(yml).parent).mkdir(parents=True, exist_ok=True)
    (d / yml).write_text(yaml.safe_dump({"packages": [{"name": p, "functions": []} for p in packages]}))
    if key:
        (d / ".env.deploy").write_text(f"CLOUDFLARE_ACCOUNT_ID=x\nDO_FUNCTIONS_ACCESS_KEY={key}\n")


def fake(namespaces: dict[str, tuple[str, list[tuple[str, str]]]]):
    """key -> (namespace id, [(package, action), ...])"""
    def fetch(key, path, timeout=30):
        if key not in namespaces:
            raise urllib.error.HTTPError("u", 401, "no", {}, None)
        ns, acts = namespaces[key]
        if path.endswith("/namespaces"):
            return [ns]
        return [{"name": a, "namespace": f"{ns}/{p}" if p else ns} for p, a in acts]
    return fetch


@pytest.fixture
def base(tmp_path):
    make(tmp_path, "alpha", "project.yml", ["v1"], key="K1")
    make(tmp_path, "beta", "serverless/project.yml", ["cheers"], key="K2")
    return tmp_path


def test_clean_namespaces_are_ok_and_the_default_sample_is_ignored(base):
    r = da.audit(CFG, fetch=fake({"K1": ("fn-1", [("v1", "events"), ("sample", "hello")]), "K2": ("fn-2", [("cheers", "auth")])}), base=base)
    assert r["alpha"]["status"] == "ok" and r["beta"]["status"] == "ok" and r["alpha"]["foreign"] == {}


def test_another_applications_package_is_foreign_and_attributed(base):
    r = da.audit(CFG, fetch=fake({"K1": ("fn-1", [("v1", "events"), ("cheers", "auth"), ("cheers", "ai")]), "K2": ("fn-2", [("cheers", "auth")])}), base=base)
    a = r["alpha"]
    assert a["status"] == "FOREIGN CODE" and a["foreign"]["cheers"]["belongs_to"] == ["beta"] and len(a["foreign"]["cheers"]["actions"]) == 2
    assert "FOREIGN package cheers" in da.render(r) and "declared by beta" in da.render(r)


def test_a_package_nobody_declares_is_foreign_too(base):
    r = da.audit(CFG, fetch=fake({"K1": ("fn-1", [("v1", "e"), ("mystery", "x")]), "K2": ("fn-2", [("cheers", "a")])}), base=base)
    assert r["alpha"]["foreign"]["mystery"]["belongs_to"] == ["nobody in the config"]


def test_declared_but_not_deployed_is_reported_as_missing(base):
    r = da.audit(CFG, fetch=fake({"K1": ("fn-1", []), "K2": ("fn-2", [("cheers", "a")])}), base=base)
    assert r["alpha"]["missing"] == ["v1"] and r["alpha"]["status"] == "ok"


def test_two_projects_reaching_the_same_namespace_is_flagged(base):
    r = da.audit(CFG, fetch=fake({"K1": ("fn-1", [("v1", "e")]), "K2": ("fn-1", [("cheers", "a")])}), base=base)
    assert r["alpha"]["status"].startswith("SHARED NAMESPACE") and r["beta"]["status"].startswith("SHARED NAMESPACE")


def test_a_rejected_or_missing_key_is_reported_not_raised(base, tmp_path):
    make(tmp_path, "alpha", "project.yml", ["v1"], key="BAD")
    (base / "beta" / ".env.deploy").unlink()
    r = da.audit(CFG, fetch=fake({}), base=base)
    assert r["alpha"]["status"] == "key rejected (HTTP 401)" and r["beta"]["status"] == "no key"


def test_only_selects_projects_but_foreignness_still_knows_every_owner(base):
    r = da.audit(CFG, only=["alpha"], fetch=fake({"K1": ("fn-1", [("v1", "e"), ("cheers", "a")])}), base=base)
    assert list(r) == ["alpha"] and r["alpha"]["foreign"]["cheers"]["belongs_to"] == ["beta"]


def test_strict_exits_1_only_when_something_is_wrong(base, monkeypatch):
    monkeypatch.setattr(da, "load_config", lambda *a, **k: CFG)
    real = da.audit
    clean = fake({"K1": ("fn-1", [("v1", "e")]), "K2": ("fn-2", [("cheers", "a")])})
    monkeypatch.setattr(da, "audit", lambda cfg, only=None: real(cfg, only, clean, base))
    assert da.main(["--strict"]) == 0
    dirty = fake({"K1": ("fn-1", [("v1", "e"), ("cheers", "a")]), "K2": ("fn-2", [("cheers", "a")])})
    monkeypatch.setattr(da, "audit", lambda cfg, only=None: real(cfg, only, dirty, base))
    assert da.main(["--strict"]) == 1 and da.main([]) == 0           # without --strict it only reports


def test_the_script_only_reads():
    src = (ROOT / "scripts" / "deploy_audit.py").read_text()
    assert "method=" not in src and "urlopen(req" in src and "DELETE" not in src and ".write_text" not in src
