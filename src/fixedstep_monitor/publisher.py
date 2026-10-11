from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishResult:
    changed: bool
    committed: bool
    pushed: bool


class GitPublisher:
    """Stage generated public artifacts and optionally publish them.

    A network push is impossible unless ``allow_push=True`` is supplied by an
    explicit caller configuration. The helper never modifies Git settings or
    credentials.
    """

    def __init__(
        self,
        repository: str | Path,
        *,
        generated_path: str = "docs/ai",
        allow_push: bool = False,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.generated_path = Path(generated_path)
        if self.generated_path.is_absolute() or ".." in self.generated_path.parts:
            raise ValueError("generated_path must stay inside the repository")
        self.allow_push = allow_push

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "git operation failed").strip()
            raise PublishError(detail)
        return result

    def stage(self) -> bool:
        if not (self.repository / ".git").exists():
            raise PublishError("publication repository is not initialized")
        self._git("add", "--", self.generated_path.as_posix())
        status = self._git("diff", "--cached", "--quiet", "--", self.generated_path.as_posix(), check=False)
        return status.returncode == 1

    def commit(
        self,
        message: str = "Update FixedStep0.5 monitoring snapshot",
        *,
        rolling_snapshot: bool = False,
    ) -> bool:
        staged = self._git("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return False
        if rolling_snapshot:
            self._git("rev-parse", "--verify", "HEAD")
            self._git("commit", "--amend", "--no-edit", "--", self.generated_path.as_posix())
        else:
            self._git("commit", "-m", message, "--", self.generated_path.as_posix())
        return True

    def push(
        self,
        *,
        remote: str = "origin",
        branch: str | None = None,
        force_with_lease: bool = False,
    ) -> None:
        if not self.allow_push:
            raise PublishError("git push is disabled; enable it explicitly in publisher configuration")
        arguments = ["push"]
        if force_with_lease:
            if not branch:
                raise PublishError("a branch is required for rolling-snapshot publication")
            arguments.extend(["--force-with-lease", remote, f"HEAD:{branch}"])
        else:
            arguments.append(remote)
            if branch:
                arguments.append(branch)
        self._git(*arguments)

    def publish(
        self,
        *,
        commit: bool = False,
        push: bool = False,
        message: str = "Update FixedStep0.5 monitoring snapshot",
        remote: str = "origin",
        branch: str | None = None,
        rolling_snapshot: bool = False,
    ) -> PublishResult:
        if rolling_snapshot:
            if not branch:
                raise PublishError("a branch is required for rolling-snapshot publication")
            current = self._git("branch", "--show-current").stdout.strip()
            if current != branch:
                raise PublishError(
                    f"rolling-snapshot publication requires the checked-out branch to be {branch!r}"
                )
        changed = self.stage()
        committed = self.commit(message, rolling_snapshot=rolling_snapshot) if commit and changed else False
        pushed = False
        if push and committed:
            self.push(remote=remote, branch=branch, force_with_lease=rolling_snapshot)
            pushed = True
        return PublishResult(changed=changed, committed=committed, pushed=pushed)
