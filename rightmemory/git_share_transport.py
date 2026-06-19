from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .share_models import ShareRelationship, validate_share_id


GIT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class GitShareReference:
    repo_url: str
    share_id: str
    branch: str | None = None


def parse_git_share_url(value: str) -> GitShareReference:
    text = str(value).strip()
    parsed = urlsplit(text)
    fragment = parse_qs(parsed.fragment, keep_blank_values=False)
    raw_share = _single_fragment_value(fragment, "share")
    if not raw_share:
        raise ValueError("Git share URL must include #share=<share-id>")
    branch = _single_fragment_value(fragment, "branch")
    repo_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    if not repo_url:
        raise ValueError("Git share URL must include a repository URL")
    return GitShareReference(
        repo_url=repo_url,
        share_id=validate_share_id(raw_share),
        branch=branch.strip() if branch and branch.strip() else None,
    )


def git_join_url(repo_url: str, share_id: str, branch: str | None = None) -> str:
    clean_repo_url = _strip_fragment(repo_url)
    fragment: dict[str, str] = {"share": validate_share_id(share_id)}
    if branch and branch.strip():
        fragment["branch"] = branch.strip()
    separator = "&" if "#" in clean_repo_url else "#"
    return f"{clean_repo_url}{separator}{urlencode(fragment)}"


def checkout_path(memory_root: Path, repo_url: str, branch: str | None = None) -> Path:
    root = Path(memory_root).expanduser()
    key = f"{_strip_fragment(repo_url)}\0{(branch or '').strip()}"
    digest = sha256(key.encode("utf-8")).hexdigest()[:16]
    return root / ".runtime" / "git_shares" / digest


def ensure_checkout(memory_root: Path, repo_url: str, branch: str | None = None, *, writable: bool = False) -> Path:
    checkout = checkout_path(memory_root, repo_url, branch)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    clean_repo_url = _strip_fragment(repo_url)
    if not checkout.exists():
        command = ["clone"]
        if branch and branch.strip():
            command.extend(["--branch", branch.strip()])
        command.extend([clean_repo_url, str(checkout)])
        _run_git(checkout.parent, *command)
    else:
        _run_git(checkout, "fetch", "--all", "--prune")
    if branch and branch.strip():
        _checkout_branch(checkout, branch.strip())
    if _has_head(checkout):
        _run_git(checkout, "pull", "--ff-only")
    elif branch and branch.strip():
        _run_git(checkout, "checkout", "-B", branch.strip())
    if not writable:
        _run_git(checkout, "status", "--short")
    return checkout


def publish_git_share_package(
    memory_root: Path,
    share: ShareRelationship,
    package_root: Path,
    *,
    push: bool = True,
) -> str:
    repo_url = _required_text(share.git_url, "git_url")
    checkout = ensure_checkout(memory_root, repo_url, share.git_branch, writable=True)
    share_dir = checkout / "shares" / validate_share_id(share.share_id)
    package_dir = share_dir / "package"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    share_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(package_root), package_dir)
    (share_dir / "share.toml").write_text(_render_share_toml(share), encoding="utf-8")
    relative_share_dir = share_dir.relative_to(checkout).as_posix()
    _run_git(checkout, "add", relative_share_dir)
    status = _run_git(checkout, "status", "--short", "--", relative_share_dir).stdout.strip()
    if status:
        _run_git(
            checkout,
            "-c",
            "user.name=RightMemory",
            "-c",
            "user.email=rightmemory@example.invalid",
            "commit",
            "-m",
            f"Publish RightMemory share {share.share_id}",
        )
    if push:
        if share.git_branch:
            _run_git(checkout, "push", "-u", "origin", share.git_branch)
        else:
            _run_git(checkout, "push", "-u", "origin", "HEAD")
    return git_join_url(repo_url, share.share_id, share.git_branch)


def _single_fragment_value(fragment: dict[str, list[str]], key: str) -> str | None:
    values = fragment.get(key) or []
    if not values:
        return None
    return values[0]


def _strip_fragment(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _checkout_branch(checkout: Path, branch: str) -> None:
    result = _run_git(checkout, "checkout", branch, check=False)
    if result.returncode == 0:
        return
    _run_git(checkout, "checkout", "-B", branch, f"origin/{branch}")


def _has_head(checkout: Path) -> bool:
    return _run_git(checkout, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def _render_share_toml(share: ShareRelationship) -> str:
    file_part = share.file
    if file_part is None or not file_part.view_id:
        raise ValueError(f"Git share {share.share_id} requires a file part")
    lines = [
        "version = 1",
        f"share_id = {_toml_string(share.share_id)}",
        f"title = {_toml_string(share.title)}",
        f"provider_id = {_toml_string(share.provider_id or '')}",
        'transport = "git"',
        'parts = ["file"]',
        "",
        "[file]",
        f"view_id = {_toml_string(file_part.view_id)}",
        f"title = {_toml_string(f'{share.title} Files')}",
        f"description = {_toml_string(file_part.intent or '')}",
        'path = "package"',
        'manifest = "package/dist/manifest.toml"',
        "",
    ]
    return "\n".join(lines)


def _required_text(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Git share {label} is required")
    return value.strip()


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    result = subprocess.run(
        ["git", *args],
        cwd=Path(cwd),
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result
