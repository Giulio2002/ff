"""Git plumbing: one worktree and branch per agent run, never the main checkout."""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True, input: str | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}) failed:\n{p.stdout}{p.stderr}")
    return p.stdout.strip()


def sha(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref)


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=repo).returncode == 0


def show(repo: Path, ref: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


class Workspaces:
    def __init__(self, repo: Path, root: Path, main: str):
        self.repo, self.root, self.main = repo, root, main
        self.root.mkdir(parents=True, exist_ok=True)

    def branch_of(self, run_id: str) -> str:
        return f"ff/{run_id}"

    def create(self, run_id: str, base: str | None = None) -> tuple[Path, str]:
        path = self.root / run_id
        branch = self.branch_of(run_id)
        git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), base or self.main)
        # agents' scratch output must never be committed by accident
        exclude = Path(git(path, "rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else path / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n.ff/\n")
        return path, branch

    def remove(self, path: Path, delete_branch: str | None = None) -> None:
        git(self.repo, "worktree", "remove", "--force", str(path), check=False)
        if delete_branch:
            git(self.repo, "branch", "-D", delete_branch, check=False)

    def commit_pending(self, path: Path, message: str) -> bool:
        """Commit whatever the agent left uncommitted. Returns True if a commit was made."""
        if not git(path, "status", "--porcelain"):
            return False
        git(path, "add", "-A")
        git(path, "-c", "user.name=formal-factory", "-c", "user.email=factory@localhost",
            "commit", "-q", "-m", message)
        return True

    def has_new_commits(self, path: Path) -> bool:
        return git(path, "rev-list", "--count", f"{self.main}..HEAD") != "0"

    def rebase_on_main(self, path: Path) -> bool:
        if is_ancestor(path, self.main, "HEAD"):
            return True
        p = subprocess.run(["git", "-c", "user.name=formal-factory", "-c", "user.email=factory@localhost",
                            "rebase", "-q", self.main], cwd=path, capture_output=True, text=True)
        if p.returncode != 0:
            git(path, "rebase", "--abort", check=False)
            return False
        return True

    def diff_stat(self, path: Path) -> str:
        return git(path, "diff", "--stat", f"{self.main}...HEAD", check=False)
