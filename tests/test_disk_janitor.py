import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "disk_janitor.py"
spec = importlib.util.spec_from_file_location("disk_janitor", SCRIPT)
dj = importlib.util.module_from_spec(spec)
sys.modules["disk_janitor"] = dj          # dataclasses look the module up by name
spec.loader.exec_module(dj)

NOW = 2_000_000_000.0
DAY = 86400


def age(path, seconds_ago):
    """Make a path (and everything in it) look `seconds_ago` old."""
    t = NOW - seconds_ago
    for dirpath, dirs, files in os.walk(path, topdown=False):
        for n in files + dirs:
            os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
    os.utime(path, (t, t), follow_symlinks=False)


def mk(path, size=10, old=None):
    path.mkdir(parents=True, exist_ok=True)
    (path / "f").write_bytes(b"x" * size)
    if old is not None:
        age(str(path), old)
    return path


# --------------------------------------------------------------------------- monitor

def mount(pct, free_gb, ram=False):
    """A mount whose used/(used+free) is exactly `pct` percent with `free_gb` GB free (what df shows)."""
    free = int(free_gb * dj.GB)
    used = int(free * pct / (100 - pct))
    return dj.Mount("/", "root", used + free, used, free, ram)


TH = {"warn_pct": 85, "crit_pct": 92, "warn_free": 50 * dj.GB, "crit_free": 20 * dj.GB}


def test_roomy_disk_is_up():
    assert dj.evaluate(mount(60, 200), TH) == ("up", None)


def test_percentage_warns_then_goes_critical():
    assert dj.evaluate(mount(86, 200), TH)[0] == "degraded"
    assert dj.evaluate(mount(93, 200), TH)[0] == "critical"


def test_low_free_space_warns_even_when_percentage_is_fine():
    # a 4 TB disk at 70% is still 1.2 TB free; a small disk with 15 GB left is in trouble at any percentage
    assert dj.evaluate(mount(70, 40), TH)[0] == "degraded"   # under 50 GB free, over 20
    status, msg = dj.evaluate(mount(70, 15), TH)
    assert status == "critical" and "15.0 GB free" in msg


def test_a_ram_backed_tmp_ignores_free_space_but_not_percentage():
    assert dj.evaluate(mount(10, 2, ram=True), TH)[0] == "up"
    assert dj.evaluate(mount(95, 2, ram=True), TH)[0] == "critical"


def test_environment_overrides_thresholds(monkeypatch):
    monkeypatch.setenv("JANITOR_WARN_PCT", "70")
    monkeypatch.setenv("JANITOR_CRIT_FREE_GB", "abc")      # junk falls back to the default
    th = dj.thresholds()
    assert th["warn_pct"] == 70 and th["crit_free"] == 20 * dj.GB


# --------------------------------------------------------------------------- the cron bug

def test_runs_with_the_minimal_environment_cron_gives_it(tmp_path):
    """The shell version died under cron because $USER is unset there. Run the real script with
    only what cron provides."""
    env = {"HOME": str(tmp_path), "LOGNAME": "x", "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh",
           "JANITOR_STATE_DIR": str(tmp_path / "state")}
    p = subprocess.run([sys.executable, str(SCRIPT), "monitor"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode in (0, 1), p.stderr
    assert "% used" in p.stdout and "Traceback" not in p.stderr


def test_unknown_command_prints_usage(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path))
    assert dj.main(["bogus"]) == 2


# --------------------------------------------------------------------------- tmp cleanup

def test_stale_tempdirs_go_and_fresh_or_unrelated_ones_stay(tmp_path):
    mk(tmp_path / "tmpabc123", old=3 * 3600)               # python tempdir, idle 3 h: remove
    mk(tmp_path / "tmpfresh01", old=60)                    # touched a minute ago: keep
    mk(tmp_path / "vantage-venv", old=30 * DAY)            # a venv looks idle but is in use: never by age
    mk(tmp_path / "at230-shots", old=30 * DAY)             # somebody's scratch: recommendation only
    got = {os.path.basename(c.path) for c in dj.tmp_candidates(NOW, False, [str(tmp_path)])}
    assert got == {"tmpabc123"}


def test_a_dir_with_one_fresh_file_deep_inside_is_not_stale(tmp_path):
    d = mk(tmp_path / "tmpdeep001", old=5 * 3600)
    inner = d / "a" / "b"
    inner.mkdir(parents=True)
    (inner / "live.log").write_bytes(b"x")
    os.utime(inner / "live.log", (NOW - 30, NOW - 30))
    assert dj.tmp_candidates(NOW, False, [str(tmp_path)]) == []


def test_test_artefacts_wait_for_running_tests(tmp_path):
    mk(tmp_path / "flutter_tools.AbC", old=5 * 3600)
    runs = tmp_path / "pytest-of-me"
    for i, ago in enumerate((10 * 3600, 9 * 3600, 8 * 3600, 7 * 3600, 6 * 3600)):
        mk(runs / f"pytest-{i}", old=ago)
    busy = dj.tmp_candidates(NOW, True, [str(tmp_path)])
    assert busy == []
    idle = dj.tmp_candidates(NOW, False, [str(tmp_path)])
    names = sorted(os.path.basename(c.path) for c in idle)
    # the 3 newest pytest runs (2,3,4) are kept; the two oldest go; the flutter dir goes
    assert names == ["flutter_tools.AbC", "pytest-0", "pytest-1"]


def test_node_compile_cache_uses_a_three_day_rule(tmp_path):
    mk(tmp_path / "node-compile-cache", old=2 * DAY)
    assert dj.tmp_candidates(NOW, False, [str(tmp_path)]) == []
    age(str(tmp_path / "node-compile-cache"), 4 * DAY)
    assert [c.rule for c in dj.tmp_candidates(NOW, False, [str(tmp_path)])] == ["tmp-node-cache"]


# --------------------------------------------------------------------------- safety net

def test_safety_check_refuses_root_outside_and_protected_paths(tmp_path):
    root = tmp_path / "root"
    inside = mk(root / "a")
    outside = mk(tmp_path / "other")
    assert dj.safe_to_delete(str(inside), str(root))
    assert not dj.safe_to_delete(str(root), str(root))               # the root itself
    assert not dj.safe_to_delete(str(outside), str(root))            # outside the root
    assert not dj.safe_to_delete(str(root / "a" / ".."), str(root))
    assert not dj.safe_to_delete(dj.HOME, "/")                       # protected directory
    assert not dj.safe_to_delete("/tmp", "/")


def test_safety_check_refuses_a_root_reached_through_a_symlink(tmp_path):
    real = mk(tmp_path / "real" / "victim")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path / "real")
    assert not dj.safe_to_delete(str(root / "link" / "victim"), str(root))
    assert real.exists()


def test_deleting_a_symlink_removes_the_link_not_the_target(tmp_path):
    target = mk(tmp_path / "precious")
    root = tmp_path / "root"
    root.mkdir()
    (root / "tmplink").symlink_to(target)
    res = dj.clean(False, NOW, [dj.Candidate("t", str(root / "tmplink"), str(root))], with_docker=False)
    assert not (root / "tmplink").exists() and (target / "f").exists()
    assert res.errors == []


def test_clean_dry_run_deletes_nothing_and_reports_the_size(tmp_path):
    d = mk(tmp_path / "tmpold0001", size=200_000, old=5 * 3600)
    res = dj.clean(True, NOW, dj.tmp_candidates(NOW, False, [str(tmp_path)]), with_docker=False)
    assert d.exists()
    assert res.freed >= 200_000 and res.removed[0]["dry"] is True


def test_clean_really_deletes_and_counts_the_bytes(tmp_path):
    d = mk(tmp_path / "tmpold0002", size=200_000, old=5 * 3600)
    res = dj.clean(False, NOW, dj.tmp_candidates(NOW, False, [str(tmp_path)]), with_docker=False)
    assert not d.exists()
    assert res.freed >= 200_000 and res.errors == []


def test_a_candidate_failing_the_safety_check_is_skipped_not_deleted(tmp_path):
    keep = mk(tmp_path / "keepme")
    res = dj.clean(False, NOW, [dj.Candidate("t", str(keep), str(tmp_path / "elsewhere"))], with_docker=False)
    assert keep.exists() and "refused" in res.skipped[0]


# --------------------------------------------------------------------------- test-runner detection

def test_tests_running_matches_the_program_not_a_stray_word():
    assert dj.tests_running(["/home/me/flutter/bin/cache/artifacts/engine/linux-x64/flutter_tester --foo"])
    assert dj.tests_running(["/home/me/.venv/bin/python /home/me/.venv/bin/pytest -x tests"])
    assert dj.tests_running(["/usr/bin/python3 -m pytest tests/"])
    assert dj.tests_running(["node /repo/node_modules/.bin/vitest run"])
    # an ssh command or an agent prompt that merely mentions the word must not block cleanup forever
    assert not dj.tests_running(["ssh host cd repo && run pytest and report"])
    assert not dj.tests_running(["claude -p please review the pytest failures"])
    assert not dj.tests_running([])


# --------------------------------------------------------------------------- other rules

def test_runner_cleanup_takes_old_logs_and_temp_but_never_the_checkouts(tmp_path):
    r = tmp_path / "actions-runner-demo"
    mk(r / "_diag" / "Worker_old.log", old=20 * DAY)
    mk(r / "_diag" / "Worker_new.log", old=1 * DAY)
    mk(r / "_work" / "_temp" / "job1", old=5 * DAY)
    mk(r / "_work" / "myrepo" / "myrepo", old=60 * DAY)         # a checkout, however old
    got = {os.path.relpath(c.path, r) for c in dj.runner_candidates(NOW, str(tmp_path))}
    assert got == {"_diag/Worker_old.log", "_work/_temp/job1"}


def test_linux_trash_goes_by_deletion_date_not_file_age(tmp_path, monkeypatch):
    monkeypatch.setattr(dj, "IS_MAC", False)
    base = tmp_path / ".local" / "share" / "Trash"
    (base / "info").mkdir(parents=True)
    (base / "files").mkdir()

    def trashed(name, days_ago):
        mk(base / "files" / name, old=500 * DAY)                # the file itself is ancient
        when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - days_ago * DAY))
        (base / "info" / f"{name}.trashinfo").write_text(f"[Trash Info]\nPath=/x/{name}\nDeletionDate={when}\n")

    trashed("long-gone", 45)
    trashed("just-deleted", 1)
    got = [os.path.basename(c.path) for c in dj.trash_candidates(NOW, str(tmp_path))]
    assert got == ["long-gone"]


def test_the_mac_trash_is_never_automatic(tmp_path, monkeypatch):
    monkeypatch.setattr(dj, "IS_MAC", True)
    mk(tmp_path / ".Trash" / "old-file", old=400 * DAY)
    assert dj.trash_candidates(NOW, str(tmp_path)) == []


def test_docker_engine_down_skips_without_pruning():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return (124, "")

    removed, errors = dj.docker_clean(False, runner=runner, docker="docker")
    assert removed == [] and "not answering" in errors[0]
    assert len(calls) == 1                                       # only the probe


def test_docker_only_prunes_dangling_images_and_week_old_build_cache():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        if cmd[1] == "version":
            return 0, "29.8"
        return 0, "Total reclaimed space: 1.5GB"

    removed, errors = dj.docker_clean(False, runner=runner, docker="docker")
    assert errors == [] and sum(r["bytes"] for r in removed) == 2 * 1_500_000_000
    pruned = [c for c in calls if c[1] != "version"]
    assert ["docker", "image", "prune", "-f"] in pruned
    assert ["docker", "builder", "prune", "-f", "--filter", "until=168h"] in pruned
    assert not any("-a" in c or "--all" in c or "volume" in c or "system" in c for c in pruned)


def test_docker_dry_run_prunes_nothing():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return 0, "29.8"

    removed, _ = dj.docker_clean(True, runner=runner, docker="docker")
    assert len(calls) == 1 and all(r.get("dry") for r in removed)


def test_size_helpers():
    assert dj.parse_size("1.5GB") == 1_500_000_000
    assert dj.parse_size("340 MB") == 340_000_000
    assert dj.parse_size("garbage") == 0
    assert dj.fmt(1536) == "1.5 KB" and dj.fmt(None) == "?"


# --------------------------------------------------------------------------- worktree advice

def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", str(origin), str(work), cwd=tmp_path)
    (work / "a.txt").write_text("a")
    git("add", ".", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("branch", "-M", "main", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return work


def test_worktrees_are_sorted_into_safe_review_and_keep(repo, tmp_path):
    merged = tmp_path / "wt-merged"
    dirty = tmp_path / "wt-dirty"
    ahead = tmp_path / "wt-ahead"
    git("worktree", "add", "-b", "m", str(merged), cwd=repo)
    git("worktree", "add", "-b", "d", str(dirty), cwd=repo)
    git("worktree", "add", "-b", "a", str(ahead), cwd=repo)
    (dirty / "wip.txt").write_text("unsaved work")
    (ahead / "new.txt").write_text("n")
    git("add", ".", cwd=ahead)
    git("commit", "-m", "work", cwd=ahead)

    assert dj.classify_worktree(str(merged)).confidence == "safe"
    d = dj.classify_worktree(str(dirty))
    assert d.confidence == "keep" and "uncommitted" in d.detail
    a = dj.classify_worktree(str(ahead))
    assert a.confidence == "review" and "not in origin/main" in a.detail
    assert "worktree remove" in dj.classify_worktree(str(merged)).action
    assert dj.classify_worktree(str(repo)) is None               # the main checkout is not a linked worktree


def test_dormant_artefacts_need_age_and_size(repo, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text("{}")
    nm = proj / "node_modules"
    nm.mkdir()
    (nm / "big").write_bytes(b"x" * 3_000_000)
    for p in (proj, nm, nm / "big", proj / "package.json"):
        os.utime(p, (NOW - 200 * DAY, NOW - 200 * DAY))
    got = dj.rec_dormant_artefacts(str(tmp_path), days=60, min_bytes=1_000_000, now=NOW)
    assert [r.title for r in got] == [str(nm)] and got[0].confidence == "safe"
    # recent edit in the project: leave it alone
    os.utime(proj / "package.json", (NOW - 3600, NOW - 3600))
    assert dj.rec_dormant_artefacts(str(tmp_path), days=60, min_bytes=1_000_000, now=NOW) == []
    # too small to matter
    os.utime(proj / "package.json", (NOW - 200 * DAY, NOW - 200 * DAY))
    assert dj.rec_dormant_artefacts(str(tmp_path), days=60, min_bytes=10_000_000, now=NOW) == []


# --------------------------------------------------------------------------- report + tick

def test_markdown_lists_every_section_and_totals_only_the_safe_items():
    mon = {"mounts": [{"path": "/", "label": "root", "total": 100 * dj.GB, "used": 80 * dj.GB,
                       "free": 20 * dj.GB, "pct": 80.0, "status": "up"}]}
    rec = {"errors": [], "sections": {
        "Worktrees": [{"id": "w", "title": "~/wt/a", "bytes": 2 * dj.GB, "detail": "merged", "action": "x",
                       "sudo": False, "confidence": "safe"},
                      {"id": "w", "title": "~/wt/b", "bytes": 5 * dj.GB, "detail": "dirty", "action": "keep",
                       "sudo": False, "confidence": "keep"}],
        "Empty": []}}
    md = dj.render_markdown("tp", mon, rec, {"freed": dj.GB, "removed": [1, 2]})
    assert "## Worktrees" in md and "## Empty" not in md
    assert "about 2.0 GB" in md                                  # the 'keep' item is not counted
    assert "freed 1.0 GB" in md


def test_tick_writes_report_and_pushes_to_vantage_only_for_real_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VANTAGE_URL", "http://vantage.test")
    monkeypatch.setenv("JANITOR_COMPONENT", "tp-disk")
    mon = {"status": "degraded", "errors": ["root 88% used, 40.0 GB free"],
           "mounts": [{"path": "/", "label": "root", "total": 10, "used": 9, "free": 1, "pct": 88.0, "status": "degraded"}]}
    monkeypatch.setattr(dj, "monitor", lambda: mon)
    monkeypatch.setattr(dj, "clean", lambda dry, *a, **k: dj.CleanResult(freed=5 * dj.GB))
    monkeypatch.setattr(dj, "recommend", lambda: {"ts": 1, "errors": [], "sections": {"X": [
        {"id": "i", "title": "t", "bytes": 3 * dj.GB, "detail": "d", "action": "a", "sudo": False, "confidence": "safe"}]}})
    pushed = []
    monkeypatch.setattr(dj, "push_vantage", lambda url, key, comp, st, errs, checks: pushed.append((comp, st, errs)) or True)

    dj.tick(dry=False)
    state = tmp_path / "state"
    rep = json.loads((state / "report.json").read_text())
    assert rep["cleanup"]["freed"] == 5 * dj.GB and rep["monitor"]["status"] == "degraded"
    assert (state / "recommendations.md").exists() and (state / "janitor.log").exists()
    comp, status, errs = pushed[0]
    assert comp == "tp-disk" and status == "degraded"
    assert errs[0].startswith("root 88%") and any("3.0 GB could be reclaimed" in e for e in errs)

    pushed.clear()
    dj.tick(dry=True)
    assert pushed == []                                          # a dry run reports nothing to Vantage


def test_cleanup_problems_make_the_report_degraded_but_not_down(tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VANTAGE_URL", "http://vantage.test")
    monkeypatch.setattr(dj, "monitor", lambda: {"status": "up", "errors": [], "mounts": []})
    monkeypatch.setattr(dj, "clean", lambda dry, *a, **k: dj.CleanResult(errors=["pip-cache: /x: Permission denied"]))
    monkeypatch.setattr(dj, "recommend", lambda: {"ts": 1, "errors": [], "sections": {}})
    pushed = []
    monkeypatch.setattr(dj, "push_vantage", lambda url, key, comp, st, errs, checks: pushed.append((st, errs, checks)) or True)
    dj.tick(dry=False)
    status, errs, checks = pushed[0]
    assert status == "degraded" and "Permission denied" in errs[0] and checks["cleanup"] == "degraded"


def test_recommendations_are_refreshed_once_a_day_not_every_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VANTAGE_URL", raising=False)
    monkeypatch.setattr(dj, "monitor", lambda: {"status": "up", "errors": [], "mounts": []})
    monkeypatch.setattr(dj, "clean", lambda dry, *a, **k: dj.CleanResult())
    scans = []
    monkeypatch.setattr(dj, "recommend", lambda: scans.append(1) or {"ts": int(time.time()), "errors": [], "sections": {}})
    dj.tick(dry=False)
    dj.tick(dry=False)
    assert len(scans) == 1
    dj.tick(dry=False, force_recommend=True)
    assert len(scans) == 2


def test_one_broken_scanner_does_not_hide_the_others(monkeypatch):
    def boom():
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(dj, "rec_worktrees", boom)
    for name in ("rec_heavy_dirs", "rec_user_caches", "rec_app_data", "rec_dormant_artefacts", "rec_big_old_files",
                 "rec_old_tmp", "rec_linux_system", "rec_apps"):
        monkeypatch.setattr(dj, name, lambda: [])
    out = dj.recommend()
    assert out["errors"] and "scanner exploded" in out["errors"][0]
    assert "Applications" in out["sections"]


def test_a_squash_merged_branch_is_safe_but_one_main_has_since_changed_is_not(repo, tmp_path):
    # PR merged by squash: the branch's commits are NOT in main's history, but its content is
    wt = tmp_path / "wt-squashed"
    git("worktree", "add", "-b", "feat", str(wt), cwd=repo)
    (wt / "b.txt").write_text("feature")
    git("add", ".", cwd=wt)
    git("commit", "-m", "feature work", cwd=wt)
    git("merge", "--squash", "feat", cwd=repo)
    git("commit", "-m", "feature (squash)", cwd=repo)
    git("push", "origin", "main", cwd=repo)
    git("fetch", "origin", cwd=wt)
    rec = dj.classify_worktree(str(wt))
    assert rec.confidence == "safe" and "squash" in rec.detail

    # another worktree whose own work never reached main stays 'review'
    other = tmp_path / "wt-unmerged"
    git("worktree", "add", "-b", "lost", str(other), cwd=repo)
    (other / "c.txt").write_text("never merged")
    git("add", ".", cwd=other)
    git("commit", "-m", "unmerged work", cwd=other)
    assert dj.classify_worktree(str(other)).confidence == "review"

    # main later rewrote the same file: the branch content is no longer what is in main
    (repo / "b.txt").write_text("changed again after the merge")
    git("commit", "-am", "later change", cwd=repo)
    git("push", "origin", "main", cwd=repo)
    git("fetch", "origin", cwd=wt)
    assert dj.classify_worktree(str(wt)).confidence == "review"


def test_runtime_snaps_are_not_listed_as_applications():
    for name in ("core22", "snapd", "gnome-42-2204", "gtk-common-themes", "mesa-2404", "kf6-core24", "kde-frameworks-6-core24"):
        assert dj.SNAP_RUNTIME.match(name), name
    for name in ("android-studio", "libreoffice", "blender", "code", "zoom-client"):
        assert not dj.SNAP_RUNTIME.match(name), name


def test_downloads_and_emulators_are_never_called_safe():
    conf = {rel: c for rel, _, _, c in dj.HEAVY_DIRS}
    assert conf[".android/avd"] == "review" and conf[".lmstudio"] == "review"
    assert conf["Library/Developer/Xcode/Archives"] == "keep"
    assert conf[".gradle/caches"] == "safe"


def test_app_usage_falls_back_to_preference_files_when_spotlight_has_no_date(tmp_path, monkeypatch):
    prefs = tmp_path / "Library" / "Preferences"
    prefs.mkdir(parents=True)
    plist = prefs / "com.example.app.plist"
    plist.write_text("x")
    ten_days = time.time() - 10 * DAY
    os.utime(plist, (ten_days, ten_days))

    def fake_run(cmd, timeout):
        if cmd[0] == "mdls":
            return 0, "(null)"
        if cmd[0] == "plutil":
            return 0, "com.example.app"
        raise AssertionError(cmd)

    monkeypatch.setattr(dj, "run", fake_run)
    got = dj.mac_app_usage("/Applications/Example.app", home=str(tmp_path))
    assert abs(got - ten_days) < 2
    # nothing at all on disk: genuinely unknown, not "used"
    assert dj.mac_app_usage("/Applications/Ghost.app", home=str(tmp_path / "empty")) is None


def test_spotlights_date_wins_when_it_exists(monkeypatch):
    monkeypatch.setattr(dj, "run", lambda cmd, timeout: (0, "2026-10-01 12:00:00 +0000"))
    assert dj.mac_app_usage("/Applications/Example.app") == 1_790_856_000.0


def test_build_tooling_is_never_suggested_for_removal():
    for app in ("Xcode.app", "OrbStack.app", "Android Studio.app"):
        assert app in dj.KEEP_APPS


def test_cache_advice_uses_the_tools_own_command_for_the_big_ones():
    assert "brew cleanup" in dj.CACHE_COMMANDS["Homebrew"] and "pip cache purge" in dj.CACHE_COMMANDS["pip"]


def test_heartbeat_reports_without_cleaning_or_scanning(tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VANTAGE_URL", "http://vantage.test")
    monkeypatch.setenv("JANITOR_COMPONENT", "tp-disk")
    monkeypatch.setattr(dj, "monitor", lambda: {"status": "up", "errors": [], "mounts": []})
    monkeypatch.setattr(dj, "clean", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not clean")))
    monkeypatch.setattr(dj, "recommend", lambda: (_ for _ in ()).throw(AssertionError("must not scan")))
    pushed = []
    monkeypatch.setattr(dj, "push_vantage", lambda url, key, comp, st, errs, checks: pushed.append((comp, st, errs, checks)) or True)
    dj.heartbeat()
    assert pushed == [("tp-disk", "up", [], {"disk": "up", "cleanup": "up"})]


def test_heartbeat_keeps_a_failing_cleanup_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VANTAGE_URL", "http://vantage.test")
    (tmp_path / "report.json").write_text(json.dumps({"cleanup": {"errors": ["pip-cache: /x: Permission denied"]}}))
    monkeypatch.setattr(dj, "monitor", lambda: {"status": "up", "errors": [], "mounts": []})
    pushed = []
    monkeypatch.setattr(dj, "push_vantage", lambda url, key, comp, st, errs, checks: pushed.append((st, errs, checks)) or True)
    dj.heartbeat()
    st, errs, checks = pushed[0]
    assert st == "degraded" and "Permission denied" in errs[0] and checks["cleanup"] == "degraded"


def test_heartbeat_runs_while_another_run_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("JANITOR_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("VANTAGE_URL", raising=False)
    monkeypatch.setattr(dj, "monitor", lambda: {"status": "up", "errors": [], "mounts": []})
    holder = dj.lock_or_exit()                       # a long scan is running
    try:
        assert dj.main(["heartbeat"]) == 0
    finally:
        holder.close()
