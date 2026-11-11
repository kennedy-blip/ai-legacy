"""Git plumbing: safe subprocess execution, cloning, history and blame extraction.

Security posture
----------------
* Git is always executed with an argv list (never a shell) and a timeout.
* Only ``https://`` and ``ssh://``/``git@`` remotes are accepted for cloning; the
  ``file``/``ext`` transports are disabled so a crafted URL cannot make the server
  read arbitrary local paths or run helper programs.
* An access token is passed through ``GIT_CONFIG_*`` environment variables as an
  HTTP header. It never appears in argv (visible in ``ps``), in the cloned
  repository's ``.git/config`` or in error messages.
* Prompts are disabled, so a private repository without credentials fails fast
  instead of hanging a worker.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_ALLOWED_URL_RE: Final = re.compile(r"^(?:https://|ssh://|git@[\w.-]+:)[\w.@:/~+%-]+$")
_SCP_LIKE_RE: Final = re.compile(r"^git@([\w.-]+):(.+)$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_BLAME_HEADER_RE: Final = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$")
_RENAME_BRACES_RE: Final = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")
_PR_RES: Final = (
    re.compile(r"Merge pull request #(\d+)"),
    re.compile(r"\(#(\d+)\)\s*$"),
    re.compile(r"\b(?:PR|pull request|pull)[ /#]+(\d+)\b", re.IGNORECASE),
    re.compile(r"See merge request \S*!(\d+)"),
)
_TICKET_RE: Final = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
_NOT_TICKETS: Final = frozenset(
    {"UTF-8", "UTF-16", "UTF-32", "ISO-8859", "SHA-1", "SHA-256", "SHA-512", "CVE-0"}
)
_ZERO_HASH: Final = "0" * 40

_RECORD: Final = "\x1e"
_FIELD: Final = "\x1f"
_HEADER_END: Final = "\x1d"
_LOG_FORMAT: Final = (
    f"{_RECORD}%H{_FIELD}%P{_FIELD}%aN{_FIELD}%aE{_FIELD}%aI{_FIELD}%s{_FIELD}%b{_HEADER_END}"
)


class GitError(RuntimeError):
    """A git command failed. The message never contains credentials."""


@dataclass(slots=True)
class FileChange:
    path: str
    insertions: int
    deletions: int
    #: Previous path when the commit renamed the file.
    old_path: str | None = None


@dataclass(slots=True)
class CommitInfo:
    hash: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    #: ISO-8601 author date with offset, e.g. ``2014-03-09T11:02:17+01:00``.
    authored_at: str
    subject: str
    body: str = ""
    files: list[FileChange] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def pr_number(self) -> int | None:
        for pattern in _PR_RES:
            found = pattern.search(self.subject) or pattern.search(self.body)
            if found:
                return int(found.group(1))
        return None

    @property
    def tickets(self) -> list[str]:
        """Issue-tracker keys (``BILL-1432``) mentioned in the message."""
        found = dict.fromkeys(_TICKET_RE.findall(f"{self.subject}\n{self.body}"))
        return [
            ticket
            for ticket in found
            if ticket not in _NOT_TICKETS and not ticket.startswith(("UTF-", "ISO-"))
        ]


def git_available() -> bool:
    return shutil.which("git") is not None


def validate_remote_url(url: str) -> str:
    """Return ``url`` if it is an acceptable clone source, else raise ``ValueError``."""
    candidate = url.strip()
    if not _ALLOWED_URL_RE.match(candidate) or ".." in candidate or candidate.startswith("-"):
        raise ValueError("only https:// and ssh:// (git@host:path) repository URLs are accepted")
    return candidate


def _auth_environment(url: str, token: str | None) -> dict[str, str]:
    """Environment that injects ``token`` as an HTTP header for this one command."""
    if not token or not url.startswith("https://"):
        return {}
    host = url.split("/", 3)[2]
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


def run_git(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = 120.0,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> str:
    """Run ``git`` with a sanitised environment and return its stdout."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in ("GIT_SSH_COMMAND", "GIT_SSL_CAINFO")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "true",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    if env:
        environment.update(env)
    command = [
        "git",
        "-c", "core.quotePath=false",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        "-c", "advice.detachedHead=false",
    ]  # fmt: skip
    if cwd is not None:
        # A mounted repository is usually owned by another user; reading it is intended.
        command += ["-c", f"safe.directory={Path(cwd).resolve()}"]
    command += args
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=environment,
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {args[0]} timed out after {timeout:.0f}s") from error
    except OSError as error:
        raise GitError(f"git could not be executed: {error}") from error
    stdout = completed.stdout.decode("utf-8", "replace")
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git {args[0]} failed ({completed.returncode}): {' | '.join(detail[-3:])[:500]}")
    return stdout


def is_git_repository(path: str | Path) -> bool:
    if not git_available() or not Path(path).is_dir():
        return False
    try:
        return run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=15).strip() == "true"
    except GitError:
        return False


def clone(
    url: str,
    destination: str | Path,
    *,
    branch: str | None = None,
    token: str | None = None,
    depth: int | None = None,
    timeout: float = 900.0,
) -> None:
    """Clone ``url`` into ``destination`` (which must not exist yet)."""
    url = validate_remote_url(url)
    args = ["clone", "--no-tags", "--single-branch", "--quiet"]
    if branch:
        if not re.fullmatch(r"[\w./-]{1,200}", branch) or branch.startswith("-"):
            raise ValueError("invalid branch name")
        args += ["--branch", branch]
    if depth:
        args += ["--depth", str(int(depth))]
    args += ["--", url, str(destination)]
    run_git(args, timeout=timeout, env=_auth_environment(url, token))


def head_commit(repo: str | Path) -> str | None:
    try:
        value = run_git(["rev-parse", "HEAD"], cwd=repo, timeout=15).strip()
    except GitError:
        return None
    return value if _HASH_RE.match(value) else None


def current_branch(repo: str | Path) -> str | None:
    try:
        value = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=15).strip()
    except GitError:
        return None
    return value or None


def remote_url(repo: str | Path) -> str | None:
    try:
        value = run_git(["config", "--get", "remote.origin.url"], cwd=repo, timeout=15, check=False).strip()
    except GitError:
        return None
    return value or None


def github_slug(url: str | None) -> tuple[str, str] | None:
    """``(owner, repository)`` when ``url`` points at github.com."""
    if not url:
        return None
    scp = _SCP_LIKE_RE.match(url)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        found = re.match(r"^(?:https|ssh)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
        if not found:
            return None
        host, path = found.group(1), found.group(2)
    if host.lower() != "github.com":
        return None
    parts = path.removesuffix(".git").strip("/").split("/")
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


def tracked_files(repo: str | Path) -> list[str] | None:
    """Paths tracked at HEAD, or ``None`` when ``repo`` is not a git work tree."""
    try:
        output = run_git(["ls-files", "-z"], cwd=repo, timeout=120)
    except GitError:
        return None
    return [path for path in output.split("\0") if path]


def _split_rename(path: str) -> tuple[str, str | None]:
    """Decode numstat rename notation: ``a/{old => new}/f`` or ``old => new``."""
    braces = _RENAME_BRACES_RE.match(path)
    if braces:
        prefix, old, new, suffix = braces.groups()
        return (
            f"{prefix}{new}{suffix}".replace("//", "/"),
            f"{prefix}{old}{suffix}".replace("//", "/"),
        )
    if " => " in path:
        old, new = path.split(" => ", 1)
        return new, old
    return path, None


def read_history(repo: str | Path, *, max_commits: int = 20000, timeout: float = 600.0) -> list[CommitInfo]:
    """Commits reachable from HEAD, newest first, with per-file line statistics."""
    args = ["log", "--no-color", "--numstat", "-M", f"--format={_LOG_FORMAT}", "-n", str(max_commits), "HEAD"]
    try:
        output = run_git(args, cwd=repo, timeout=timeout)
    except GitError as error:
        if "does not have any commits" in str(error) or "unknown revision" in str(error):
            return []
        raise
    commits: list[CommitInfo] = []
    for record in output.split(_RECORD)[1:]:
        header, _, stats = record.partition(_HEADER_END)
        fields = header.split(_FIELD)
        if len(fields) < 7 or not _HASH_RE.match(fields[0]):
            continue
        commit = CommitInfo(
            hash=fields[0],
            parents=tuple(fields[1].split()),
            author_name=fields[2].strip() or "unknown",
            author_email=fields[3].strip().lower(),
            authored_at=fields[4].strip(),
            subject=fields[5].strip(),
            body=_FIELD.join(fields[6:]).strip(),
        )
        for line in stats.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, raw_path = parts
            path, old_path = _split_rename(raw_path)
            commit.files.append(
                FileChange(
                    path=path,
                    insertions=int(added) if added.isdigit() else 0,
                    deletions=int(removed) if removed.isdigit() else 0,
                    old_path=old_path,
                )
            )
        commits.append(commit)
    return commits


def blame_lines(repo: str | Path, path: str, *, timeout: float = 30.0) -> dict[int, str] | None:
    """Map each line number of ``path`` at HEAD to the hash of the commit that last changed it.

    Whitespace-only changes (``-w``) and lines moved within the file (``-M``) are
    attributed to the commit that originally wrote them. Returns ``None`` when the
    file cannot be blamed (untracked, binary, timeout).
    """
    try:
        output = run_git(["blame", "--porcelain", "-w", "-M", "HEAD", "--", path], cwd=repo, timeout=timeout)
    except GitError:
        return None
    lines: dict[int, str] = {}
    current: tuple[str, int] | None = None
    for line in output.split("\n"):
        if line.startswith("\t"):
            if current is not None and current[0] != _ZERO_HASH:
                lines[current[1]] = current[0]
            current = None
            continue
        header = _BLAME_HEADER_RE.match(line)
        if header:
            current = (header.group(1), int(header.group(2)))
    return lines


def iter_current_paths(commits: Sequence[CommitInfo]) -> Iterator[tuple[CommitInfo, FileChange, str]]:
    """Yield ``(commit, change, path today)`` following renames back through history.

    ``commits`` must be ordered newest first. A change recorded under a file's old
    name is attributed to the path the file has at HEAD.
    """
    alias: dict[str, str] = {}
    for commit in commits:
        renames: list[tuple[str, str]] = []
        for change in commit.files:
            today = alias.get(change.path, change.path)
            yield commit, change, today
            if change.old_path:
                renames.append((change.old_path, today))
        for old_path, today in renames:
            alias[old_path] = today
