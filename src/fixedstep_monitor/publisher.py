from __future__ import annotations

import math
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_GIT_TIMEOUT_SECONDS = 120.0
MAX_GIT_TIMEOUT_SECONDS = 3600.0
GIT_TIMEOUT_ENVIRONMENT_VARIABLE = "FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS"
MAX_GIT_ERROR_CHARACTERS = 512

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?:[^/@\s]+@)", re.IGNORECASE
)
_SECRET_VALUE = re.compile(
    r"(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}|\bgh[opusr]_[A-Za-z0-9]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,})"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api_?key|credential)\s*[=:]\s*\S+"
)
_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Z])(?:[A-Z]:[\\/]|\\\\)[^\r\n\t]+")
_POSIX_PRIVATE_PATH = re.compile(r"/(?:home|Users|var|etc|opt|srv|tmp)/[^\s'\"]+")


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishResult:
    changed: bool
    committed: bool
    pushed: bool


@dataclass(frozen=True)
class PublishWorkerSnapshot:
    requested: int
    started: int
    completed: int
    coalesced: int
    failures: int
    running: bool
    pending: bool
    stopping: bool
    last_error: str | None


class CoalescingPublishWorker:
    """Run publication off the collector thread with at most one pending run.

    A single worker thread guarantees that Git commands never overlap.  While
    one publication is running, any number of requests collapse into one
    trailing run so a slow network cannot build an unbounded queue.
    """

    def __init__(
        self,
        publish: Callable[[], PublishResult],
        *,
        on_complete: Callable[[PublishResult | None, str | None], None] | None = None,
    ) -> None:
        self._publish = publish
        self._on_complete = on_complete
        self._condition = threading.Condition()
        self._pending = False
        self._running = False
        self._stopping = False
        self._drain = True
        self._requested = 0
        self._started = 0
        self._completed = 0
        self._coalesced = 0
        self._failures = 0
        self._last_error: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="fixedstep-git-publisher",
            daemon=True,
        )
        self._thread.start()

    def request(self) -> bool:
        """Queue a publication without waiting; return false when coalesced."""

        with self._condition:
            if self._stopping:
                return False
            self._requested += 1
            if self._pending:
                self._coalesced += 1
                return False
            self._pending = True
            self._condition.notify()
            return True

    @property
    def snapshot(self) -> PublishWorkerSnapshot:
        with self._condition:
            return PublishWorkerSnapshot(
                requested=self._requested,
                started=self._started,
                completed=self._completed,
                coalesced=self._coalesced,
                failures=self._failures,
                running=self._running,
                pending=self._pending,
                stopping=self._stopping,
                last_error=self._last_error,
            )

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def shutdown(self, *, drain: bool = True, timeout: float | None = None) -> bool:
        """Stop accepting work and join the worker after any active Git call."""

        with self._condition:
            self._stopping = True
            self._drain = bool(drain)
            if not self._drain:
                self._pending = False
            self._condition.notify_all()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._stopping)
                if self._stopping and (not self._drain or not self._pending):
                    return
                self._pending = False
                self._running = True
                self._started += 1

            result: PublishResult | None = None
            error: str | None = None
            try:
                result = self._publish()
            except PublishError as exc:
                error = str(exc)
            except Exception as exc:
                # Arbitrary callback errors may include paths or endpoints in
                # their messages.  Keep diagnostics useful without exposing it.
                error = f"{type(exc).__name__}: publication failed"

            with self._condition:
                self._running = False
                self._completed += 1
                if error is not None:
                    self._failures += 1
                self._last_error = error
                self._condition.notify_all()

            if self._on_complete is not None:
                try:
                    self._on_complete(result, error)
                except Exception:
                    pass


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
        git_timeout_seconds: float | None = None,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.generated_path = Path(generated_path)
        if self.generated_path.is_absolute() or ".." in self.generated_path.parts:
            raise ValueError("generated_path must stay inside the repository")
        self.allow_push = allow_push
        configured_timeout: float | str = (
            os.environ.get(
                GIT_TIMEOUT_ENVIRONMENT_VARIABLE,
                str(DEFAULT_GIT_TIMEOUT_SECONDS),
            )
            if git_timeout_seconds is None
            else git_timeout_seconds
        )
        try:
            timeout = float(configured_timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("git_timeout_seconds must be a number") from exc
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_GIT_TIMEOUT_SECONDS:
            raise ValueError(
                f"git_timeout_seconds must be greater than zero and no more than "
                f"{MAX_GIT_TIMEOUT_SECONDS:g}"
            )
        self.git_timeout_seconds = timeout

    @staticmethod
    def _safe_error(detail: object) -> str:
        """Return a single-line, credential-safe, bounded diagnostic."""

        text = _ANSI_ESCAPE.sub("", str(detail))
        text = _URL_CREDENTIALS.sub(r"\g<scheme><redacted>@", text)
        text = _SECRET_VALUE.sub("<redacted>", text)
        text = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", text)
        text = _WINDOWS_PATH.sub("<path>", text)
        text = _POSIX_PRIVATE_PATH.sub("<path>", text)
        text = " ".join(text.split()) or "git operation failed"
        if len(text) > MAX_GIT_ERROR_CHARACTERS:
            text = text[: MAX_GIT_ERROR_CHARACTERS - 3].rstrip() + "..."
        return text

    def _raise_for_result(self, result: subprocess.CompletedProcess[str]) -> None:
        detail = result.stderr or result.stdout or "git operation failed"
        raise PublishError(self._safe_error(detail))

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
            }
        )
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=self.repository,
                text=True,
                capture_output=True,
                check=False,
                env=environment,
                timeout=self.git_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise PublishError(
                f"git operation timed out after {self.git_timeout_seconds:g} seconds"
            ) from None
        except OSError as exc:
            raise PublishError(self._safe_error(exc)) from None
        if check and result.returncode != 0:
            self._raise_for_result(result)
        return result

    def stage(self) -> bool:
        if not (self.repository / ".git").exists():
            raise PublishError("publication repository is not initialized")
        self._git("add", "--", self.generated_path.as_posix())
        status = self._git("diff", "--cached", "--quiet", "--", self.generated_path.as_posix(), check=False)
        if status.returncode not in (0, 1):
            self._raise_for_result(status)
        return status.returncode == 1

    def commit(
        self,
        message: str = "Update FixedStep0.5 monitoring snapshot",
        *,
        rolling_snapshot: bool = False,
    ) -> bool:
        staged = self._git("diff", "--cached", "--quiet", check=False)
        if staged.returncode not in (0, 1):
            self._raise_for_result(staged)
        if staged.returncode == 0:
            return False
        staged_paths = self._git(
            "diff", "--cached", "--name-only", "-z"
        ).stdout.split("\0")
        generated_prefix = self.generated_path.as_posix().rstrip("/") + "/"
        if any(
            path
            and path != self.generated_path.as_posix()
            and not path.startswith(generated_prefix)
            for path in staged_paths
        ):
            raise PublishError(
                "publication index contains staged changes outside the generated path"
            )
        if rolling_snapshot:
            self._git("rev-parse", "--verify", "HEAD")
            self._git("commit", "--amend", "--no-edit")
        else:
            self._git("commit", "-m", message)
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
        self.validate_rolling_snapshot(
            branch=branch,
            rolling_snapshot=rolling_snapshot,
        )
        changed = self.stage()
        return self._finish_staged(
            changed=changed,
            commit=commit,
            push=push,
            message=message,
            remote=remote,
            branch=branch,
            rolling_snapshot=rolling_snapshot,
        )

    def validate_rolling_snapshot(
        self, *, branch: str | None, rolling_snapshot: bool
    ) -> None:
        if rolling_snapshot:
            if not branch:
                raise PublishError("a branch is required for rolling-snapshot publication")
            current = self._git("branch", "--show-current").stdout.strip()
            if current != branch:
                raise PublishError(
                    f"rolling-snapshot publication requires the checked-out branch to be {branch!r}"
                )

    def finish_staged(
        self,
        *,
        changed: bool,
        commit: bool = False,
        push: bool = False,
        message: str = "Update FixedStep0.5 monitoring snapshot",
        remote: str = "origin",
        branch: str | None = None,
        rolling_snapshot: bool = False,
    ) -> PublishResult:
        """Commit and push a snapshot already captured in the Git index."""

        self.validate_rolling_snapshot(
            branch=branch,
            rolling_snapshot=rolling_snapshot,
        )
        return self._finish_staged(
            changed=changed,
            commit=commit,
            push=push,
            message=message,
            remote=remote,
            branch=branch,
            rolling_snapshot=rolling_snapshot,
        )

    def _finish_staged(
        self,
        *,
        changed: bool,
        commit: bool,
        push: bool,
        message: str,
        remote: str,
        branch: str | None,
        rolling_snapshot: bool,
    ) -> PublishResult:
        committed = self.commit(message, rolling_snapshot=rolling_snapshot) if commit and changed else False
        pushed = False
        if push and committed:
            self.push(remote=remote, branch=branch, force_with_lease=rolling_snapshot)
            pushed = True
        return PublishResult(changed=changed, committed=committed, pushed=pushed)
