"""scripts/run-with-env.sh: loads .env without executing it, never overrides, refuses loose permissions."""
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run-with-env.sh"


def run(env_file: Path, *cmd: str, extra_env=None):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **(extra_env or {})}
    return subprocess.run(["bash", str(SCRIPT), "--env-file", str(env_file), "--", *cmd],
                          env=env, capture_output=True, text=True)


def envfile(tmp_path, body: str, mode=0o600) -> Path:
    f = tmp_path / "x.env"
    f.write_text(body)
    f.chmod(mode)
    return f


def test_loads_plain_quoted_export_and_trailing_comment(tmp_path):
    f = envfile(tmp_path, 'A=1\nB="two words"\nC=\'q\'\n# comment\n\nexport D=4\nE=5 # note\n')
    r = run(f, "bash", "-c", 'echo "$A|$B|$C|$D|$E"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1|two words|q|4|5"


def test_environment_wins_over_the_file(tmp_path):
    f = envfile(tmp_path, "A=file\n")
    r = run(f, "bash", "-c", 'echo "$A"', extra_env={"A": "env"})
    assert r.stdout.strip() == "env"


def test_values_are_never_executed(tmp_path):
    marker = tmp_path / "pwned"
    f = envfile(tmp_path, f"X=$(touch {marker})\nY=`touch {marker}`\n")
    r = run(f, "bash", "-c", 'echo "$X"')
    assert r.returncode == 0
    assert not marker.exists()
    assert "touch" in r.stdout          # kept as literal text


def test_refuses_a_group_or_world_readable_file(tmp_path):
    f = envfile(tmp_path, "A=1\n", mode=0o644)
    r = run(f, "true")
    assert r.returncode != 0
    assert "chmod 600" in r.stderr
    assert "A=1" not in r.stderr          # never echoes contents


def test_missing_file_falls_back_to_process_environment(tmp_path):
    r = run(tmp_path / "absent.env", "bash", "-c", 'echo "$A"', extra_env={"A": "env"})
    assert r.returncode == 0 and r.stdout.strip() == "env"
    assert "no env file" in r.stderr


def test_invalid_keys_are_skipped(tmp_path):
    f = envfile(tmp_path, "1BAD=x\nGOOD=y\nA B=z\n")
    r = run(f, "bash", "-c", 'echo "$GOOD"')
    assert r.stdout.strip() == "y"


def test_quoted_value_followed_by_a_comment(tmp_path):
    f = envfile(tmp_path, 'A="two words" # note\nB=\'x y\'   # note\nC="has # inside" # note\nD="plain"\nE=\'q\'\n')
    r = run(f, "bash", "-c", 'echo "$A|$B|$C|$D|$E"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "two words|x y|has # inside|plain|q"


def test_refuses_a_symlinked_env_file(tmp_path):
    real = envfile(tmp_path, "A=1\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    r = run(link, "bash", "-c", 'echo "[$A]"')
    assert r.returncode != 0
    assert "is a symlink; refusing" in r.stderr
    assert r.stdout == ""            # the command never ran
