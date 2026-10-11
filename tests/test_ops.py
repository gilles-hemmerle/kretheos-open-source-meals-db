"""Tests of the script cron runs on a server hosting the service from a clone.

`ops/export-and-push.sh` is run for real, with the git of the machine, in a
clone whose remote is a bare repository on the file system. The one thing
replaced is Docker: a stand-in named `docker` on the PATH runs the export the
container would run (`food-data export-layer`, from the same source), so that
what is tested is what the script does around it: bringing the clone up to
date, committing the file alone, pushing, failing when the push fails and
pushing what waits the next time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import REPOSITORY
from service_tools import PRODUCT, Running, start_service
from test_service_maintenance import git, write_through_the_api

from kratheos_food_data import kratheos
from kratheos_food_data.build import Workspace

SCRIPT = "ops/export-and-push.sh"
FALLBACK_AUTHOR = "kratheos-food-data <kratheos-food-data@localhost>"


@pytest.fixture
def service(built_repository: Workspace, tmp_path: Path) -> Iterator[Running]:
    """Give a running service over a copy of the miniature repository."""
    running = start_service(built_repository, tmp_path)
    yield running
    running.close()


class Server:
    """A clone the service is hosted from, its remote, and the script run in it.

    Attributes:
        remote: The bare repository the clone pushes to.
        clone: The clone on the "server".
        environment: Environment of the script: no git identity of the
            machine, and the stand-in for Docker first on the PATH.
    """

    def __init__(self, base: Path, live: Path) -> None:
        """Lay out a remote holding the script and an empty layer, and clone it.

        Args:
            base: Directory of the test.
            live: The live database the export reads.
        """
        seed = base / "seed"
        (seed / "ops").mkdir(parents=True)
        shutil.copy(REPOSITORY / SCRIPT, seed / SCRIPT)
        kratheos.create_database(seed / "kratheos.sqlite")
        (seed / "notes.txt").write_text("first\n", encoding="utf-8")
        git(seed, "init", "--quiet", "--initial-branch", "main")
        git(seed, "add", "ops", "kratheos.sqlite", "notes.txt")
        git(seed, "-c", "user.name=A", "-c", "user.email=a@example.org", "commit", "-qm", "1")
        self.remote = base / "remote.git"
        self.clone = base / "clone"
        git(seed, "clone", "--quiet", "--bare", str(seed), str(self.remote))
        git(seed, "clone", "--quiet", str(self.remote), str(self.clone))

        # `docker compose run --rm -T maintenance export-layer`, without Docker.
        tools = base / "bin"
        tools.mkdir()
        stand_in = tools / "docker"
        stand_in.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" -m kratheos_food_data.cli'
            f' --root "{self.clone}" export-layer --live "{live}"\n',
            encoding="utf-8",
        )
        stand_in.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def run(self) -> subprocess.CompletedProcess:
        """Run the script as cron would.

        Returns:
            The finished process, with what it printed.
        """
        return subprocess.run(  # noqa: S603 - the script of the repository
            ["sh", str(self.clone / SCRIPT)],  # noqa: S607
            env=self.environment,
            cwd="/",
            capture_output=True,
            text=True,
            check=False,
        )


def test_weekly_export_commits_the_layer_alone_and_pushes_it(
    service: Running, tmp_path: Path
) -> None:
    """Silent and pushed when all goes well; loud, and caught up later, when the remote is away."""
    server = Server(tmp_path / "server", service.settings.live)
    remote, clone = server.remote, server.clone
    write_through_the_api(service)
    # Something else is modified in the clone, and even staged: it must stay out.
    (clone / "notes.txt").write_text("changed by hand\n", encoding="utf-8")
    git(clone, "add", "notes.txt")

    done = server.run()
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert git(remote, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")
    assert git(remote, "rev-list", "--count", "main") == "2"
    assert git(remote, "show", "--name-only", "--format=", "main") == "kratheos.sqlite"
    assert git(remote, "log", "-1", "--format=%an <%ae>", "main") == FALLBACK_AUTHOR
    subject, _, body = git(remote, "log", "-1", "--format=%s%n%b", "main").partition("\n")
    assert subject.startswith("data: the Kratheos layer exported on 20")
    assert "2 products, 1 versions, 7 votes on 5 versions, 1 names" in body
    assert git(clone, "status", "--porcelain") == "M  notes.txt"
    assert kratheos.read_layer(clone / "kratheos.sqlite").rows == 9

    # A second run has nothing to do, and says nothing.
    done = server.run()
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert git(remote, "rev-list", "--count", "main") == "2"

    # The remote is away: status 1 and a message for cron, the commit stays.
    away = remote.with_name("away.git")
    remote.rename(away)
    service.post("/v1/products", dict(PRODUCT, names={"fr": "Un produit de plus"}))
    done = server.run()
    assert done.returncode == 1 and "git push failed: 1 commit(s) stay" in done.stderr
    assert git(clone, "rev-list", "--count", "HEAD") == "3"
    # Still away and nothing new: it fails again instead of forgetting the commit.
    done = server.run()
    assert done.returncode == 1 and "git push failed: 1 commit(s) stay" in done.stderr
    assert git(clone, "rev-list", "--count", "HEAD") == "3"

    # Meanwhile the code moved on the remote; then the remote is back. The run
    # replays the waiting commit on top of it and pushes: nothing fights. (A
    # clone modified by hand would have to be cleaned first, and the run says so.)
    git(clone, "restore", "--staged", "--worktree", "notes.txt")
    other = tmp_path / "elsewhere"
    git(tmp_path, "clone", "--quiet", str(away), str(other))
    (other / "code.txt").write_text("a new version\n", encoding="utf-8")
    git(other, "add", "code.txt")
    git(other, "-c", "user.name=A", "-c", "user.email=a@example.org", "commit", "-qm", "code")
    git(other, "push", "--quiet")
    away.rename(remote)
    done = server.run()
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert git(remote, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")
    assert git(remote, "rev-list", "--count", "main") == "4"
    assert (clone / "code.txt").exists()
    assert git(clone, "status", "--porcelain") == ""
    assert not (clone / ".git" / "kratheos-export.lock").exists()


def test_weekly_export_that_fails_says_so_and_commits_nothing(
    service: Running, tmp_path: Path
) -> None:
    """An export the service refuses ends with status 1 and leaves the clone alone."""
    server = Server(tmp_path / "server", tmp_path / "no-such-live.sqlite")
    done = server.run()
    assert done.returncode == 1 and "the export failed" in done.stderr
    assert git(server.clone, "status", "--porcelain") == ""
    assert git(server.clone, "rev-list", "--count", "HEAD") == "1"
