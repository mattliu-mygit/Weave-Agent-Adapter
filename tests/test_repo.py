"""`repo`: a session's repository as the origin remote's path, else the top-level directory's name."""
from __future__ import annotations

import subprocess

import pytest

from weave_agent_adapter.repo import remote_path, repo_identity


@pytest.mark.parametrize("url, expected", [
    ("git@github.com:wandb/core.git", "wandb/core"),
    ("github.com:wandb/core", "wandb/core"),
    ("ssh://git@github.com:2222/wandb/core.git", "wandb/core"),
    ("https://github.com/wandb/core", "wandb/core"),
    ("https://x-access-token:secret@github.com/wandb/core.git/", "wandb/core"),
    ("https://gitlab.com/group/sub/core.git", "group/sub/core"),
    ("/srv/git/core.git", None),
    ("../core", None),
    ("file:///srv/git/core.git", None),
])
def test_a_hosted_remote_names_its_path_and_a_local_one_names_nothing(url, expected):
    assert remote_path(url) == expected


@pytest.fixture
def git(tmp_path, monkeypatch):
    """Run git in `tmp_path` without the user's config, such as `url.insteadOf` rewrites."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    def run(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)
    return run


def test_a_repository_with_an_origin_is_named_by_the_remote_from_any_subdirectory(tmp_path, git):
    checkout = tmp_path / "my-checkout"
    (checkout / "src").mkdir(parents=True)
    git(checkout, "init", "-q")
    git(checkout, "remote", "add", "origin", "git@github.com:wandb/core.git")

    assert repo_identity(str(checkout / "src")) == "wandb/core"


def test_repo_is_lowercase_because_github_ignores_case(tmp_path, git):
    remote, plain = tmp_path / "remote", tmp_path / "Plain"
    for checkout in (remote, plain):
        checkout.mkdir()
        git(checkout, "init", "-q")
    git(remote, "remote", "add", "origin", "https://github.com/Wandb/Core.git")

    assert (repo_identity(str(remote)), repo_identity(str(plain))) == ("wandb/core", "plain")


def test_a_repository_without_a_hosted_origin_is_named_by_its_top_level_directory(tmp_path, git):
    plain, local = tmp_path / "plain", tmp_path / "local"
    for checkout in (plain, local):
        checkout.mkdir()
        git(checkout, "init", "-q")
    git(local, "remote", "add", "origin", "/srv/git/core.git")

    assert (repo_identity(str(plain)), repo_identity(str(local))) == ("plain", "local")


def test_a_directory_outside_a_repository_has_no_repo(tmp_path, git):
    assert repo_identity(str(tmp_path)) is None
    assert repo_identity(str(tmp_path / "missing")) is None
    assert repo_identity(None) is None
