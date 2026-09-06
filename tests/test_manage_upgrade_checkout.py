"""Exercise upgrade checkout preparation with real local Git repositories.

Only the function-definition prefix is loaded: no apt, pip, systemd or live paths.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "manage.sh").read_text()
PREFIX = SOURCE.split("# Check if we're running in an interactive terminal")[0]
# bash -c has no BASH_SOURCE; supply only path constants through positional $0.
PREFIX = "\n".join(
    'readonly SCRIPT_PATH="$0"'
    if line.startswith("readonly SCRIPT_PATH=")
    else 'readonly SCRIPT_DIR="$(dirname -- "$0")"'
    if line.startswith("readonly SCRIPT_DIR=")
    else line
    for line in PREFIX.splitlines()
)


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="Test Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Test Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        ),
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "dev")
    (remote / "pyproject.toml").write_text("# old source\n")
    # Safe replacement body makes re-execution observable without upgrading a host.
    (remote / "manage.sh").write_text(PREFIX + '\nprintf "OLD:%s\\n" "$*"\n')
    git(remote, "add", ".")
    git(remote, "commit", "-m", "initial fixture")
    local = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(local))
    return remote, local


def run_refresh(local, cwd=None):
    # Bash positional $0 supplies BASH_SOURCE for the real prefix constants.
    return subprocess.run(
        [
            "bash",
            "-c",
            PREFIX + '\nrefresh_upgrade_checkout true || exit $?\nprintf "CONTINUED\\n"\n',
            str(local / "manage.sh"),
        ],
        cwd=cwd or local,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_refresh_pulls_script_directory_not_callers_directory(checkout, tmp_path):
    remote, local = checkout
    (remote / "pyproject.toml").write_text("# new source\n")
    git(remote, "commit", "-am", "update package")
    result = run_refresh(local, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (local / "pyproject.toml").read_text() == "# new source\n"
    assert "CONTINUED" in result.stdout


def test_updated_script_runs_in_same_invocation(checkout):
    remote, local = checkout
    (remote / "manage.sh").write_text('#!/bin/bash\nprintf "NEW:%s\\n" "$*"\n')
    git(remote, "commit", "-am", "new installer logic")
    result = run_refresh(local)
    assert result.returncode == 0, result.stderr
    assert "NEW:upgrade --silent" in result.stdout
    assert "CONTINUED" not in result.stdout


@pytest.mark.parametrize("problem", ["dirty", "detached", "diverged", "no-upstream", "offline"])
def test_unsafe_or_failed_pull_aborts_before_upgrade(checkout, problem):
    remote, local = checkout
    if problem == "dirty":
        (local / "pyproject.toml").write_text("# user changes\n")
    elif problem == "detached":
        git(local, "checkout", "--detach")
    elif problem == "no-upstream":
        git(local, "branch", "--unset-upstream")
    elif problem == "offline":
        git(local, "remote", "set-url", "origin", str(remote / "missing"))
    else:
        (local / "local.txt").write_text("local\n")
        git(local, "add", ".")
        git(local, "commit", "-m", "local divergence")
        (remote / "remote.txt").write_text("remote\n")
        git(remote, "add", ".")
        git(remote, "commit", "-m", "remote divergence")
    before = git(local, "rev-parse", "HEAD")
    result = run_refresh(local)
    assert result.returncode != 0
    assert "CONTINUED" not in result.stdout
    assert git(local, "rev-parse", "HEAD") == before
    if problem == "dirty":
        assert (local / "pyproject.toml").read_text() == "# user changes\n"


def test_standalone_script_skips_pull(tmp_path):
    result = run_refresh(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "CONTINUED" in result.stdout


def test_linked_worktree_is_updated(checkout, tmp_path):
    remote, local = checkout
    linked = tmp_path / "linked"
    git(local, "worktree", "add", "-b", "linked", str(linked), "origin/dev")
    (remote / "pyproject.toml").write_text("# updated worktree\n")
    git(remote, "commit", "-am", "advance")
    result = run_refresh(linked)
    assert result.returncode == 0, result.stderr
    assert (linked / "pyproject.toml").read_text() == "# updated worktree\n"


def test_refresh_precedes_package_selection_and_mutations():
    upgrade = SOURCE.split("upgrade_repeater() {", 1)[1].split("# Radio Configuration function")[0]
    refresh = upgrade.index('refresh_upgrade_checkout "$silent" || return 1')
    assert refresh < upgrade.index('package_source="$(determine_package_source')
    assert refresh < upgrade.index("migrate_legacy_paths")


def test_reexec_releases_existing_lock(checkout, tmp_path):
    remote, local = checkout
    lock = tmp_path / "manage.lock"
    (remote / "manage.sh").write_text(
        "#!/bin/bash\nexec 9>"
        + shlex.quote(str(lock))
        + "\nflock -n 9 || exit 73\nprintf 'LOCKED\\n'\n"
    )
    git(remote, "commit", "-am", "updated manager acquires lock")
    result = subprocess.run(
        [
            "bash",
            "-c",
            PREFIX + '\nexec 9>"$1"\nflock -n 9\nrefresh_upgrade_checkout true\n',
            str(local / "manage.sh"),
            str(lock),
        ],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "LOCKED" in result.stdout
