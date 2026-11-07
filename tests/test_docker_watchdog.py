import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "docker_watchdog", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "docker_watchdog.py")
dw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dw)


def fake(engine_up=True, running=("a", "b"), start_ok=True):
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        if cmd[1] == "version":
            return (0, "29.8.2") if engine_up else (124, "timed out")
        if cmd[1] == "inspect":
            return (0, "true" if cmd[-1] in running else "false")
        if cmd[1] == "start":
            return (0 if start_ok else 1, "" if start_ok else "no such container")
        if cmd[0] == "orbctl":
            return 0, ""
        raise AssertionError(cmd)
    return runner, calls


def test_all_running_is_up():
    r, calls = fake()
    assert dw.tick({}, ["a", "b"], 1000, runner=r) == ("up", [])
    assert not any(c[1] == "start" for c in calls)


def test_stopped_container_is_started_and_reported_degraded():
    r, calls = fake(running=("a",))
    status, errors = dw.tick({}, ["a", "b"], 1000, runner=r)
    assert status == "degraded" and "b was not running; start ok" in errors[0]
    assert ["docker", "start", "b"] in calls


def test_failed_container_start_is_visible():
    r, _ = fake(running=(), start_ok=False)
    status, errors = dw.tick({}, ["a"], 1000, runner=r)
    assert status == "degraded" and "FAILED" in errors[0]


def test_silent_engine_is_down_but_not_started_until_limit():
    r, calls = fake(engine_up=False)
    state = {}
    for i in range(dw.FAIL_LIMIT - 1):
        status, _ = dw.tick(state, ["a"], 1000 + i, runner=r)
        assert status == "down"
    assert not any(c[0] == "orbctl" for c in calls)
    dw.tick(state, ["a"], 1100, runner=r)
    assert ["orbctl", "start"] in calls


def test_engine_start_respects_cooldown_and_recovery_resets_count():
    r, calls = fake(engine_up=False)
    state = {"fails": dw.FAIL_LIMIT, "last_start": 1000}
    dw.tick(state, [], 1000 + dw.COOLDOWN_S - 1, runner=r)
    assert not any(c[0] == "orbctl" for c in calls)
    dw.tick(state, [], 1000 + dw.COOLDOWN_S, runner=r)
    assert sum(c[0] == "orbctl" for c in calls) == 1
    up, _ = fake()
    dw.tick(state, [], 5000, runner=up)
    assert state["fails"] == 0


def test_never_uses_destructive_orbctl_commands():
    src = pathlib.Path(dw.__file__).read_text()
    assert "reset" not in src.replace("resets", "") .split("def tick")[1]
    assert "delete" not in src.split("def tick")[1]
