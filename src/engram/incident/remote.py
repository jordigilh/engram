"""Authenticated, allowlisted GitHub log and artifact ingestion for the PoC."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from dataclasses import asdict

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
MAX_EXTRACTED_FILES = 10_000
_JOB_URL_RE = re.compile(r"^/([^/]+)/([^/]+)/actions/runs/(\d+)/job/(\d+)")
_ARTIFACT_URL_RE = re.compile(r"^/([^/]+)/([^/]+)/actions/runs/(\d+)/artifacts/(\d+)")
_API_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/actions/(jobs|artifacts)/(\d+)")


def github_api_url(url: str) -> tuple[str, str]:
    """Resolve a GitHub Actions job/artifact page to an API download URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {"github.com", "api.github.com"}:
        raise ValueError("only https://github.com or https://api.github.com URLs are supported")
    match = _JOB_URL_RE.match(parsed.path) if parsed.netloc == "github.com" else _API_RE.match(parsed.path)
    if match and (parsed.netloc == "github.com" or match.group(3) == "jobs"):
        owner, repo, job_id = (match.group(1), match.group(2), match.group(4) if parsed.netloc == "github.com" else match.group(4))
        return f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", "job_logs"
    match = _ARTIFACT_URL_RE.match(parsed.path) if parsed.netloc == "github.com" else _API_RE.match(parsed.path)
    if match and (parsed.netloc == "github.com" or match.group(3) == "artifacts"):
        owner, repo, artifact_id = (match.group(1), match.group(2), match.group(4) if parsed.netloc == "github.com" else match.group(4))
        return f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip", "artifact"
    raise ValueError("URL must identify a GitHub Actions job log or artifact")


def _download(url: str) -> bytes:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "engram-kubernaut-rca"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    response = opener.open(request, timeout=60)
    if 300 <= response.status < 400:
        redirect_url = response.headers.get("Location")
        if not redirect_url:
            raise ValueError("GitHub download redirect did not include a location")
        response.close()
        # Signed artifact/job-log URLs are independently authorized. Never
        # forward the GitHub API bearer token to the storage host.
        response = urllib.request.urlopen(
            urllib.request.Request(redirect_url, headers={"User-Agent": "engram-kubernaut-rca"}),
            timeout=60,
        )
    with response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"download exceeds {MAX_DOWNLOAD_BYTES} byte limit")
    return data


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, newurl, *args):
        return None

    def http_error_302(self, request, response, code, msg, headers):
        return response


def _safe_member(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep):
        raise ValueError(f"archive member escapes extraction root: {name}")
    return target


def _extract_zip(data: bytes, root: Path) -> int:
    count = 0
    extracted_bytes = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if count >= MAX_EXTRACTED_FILES or extracted_bytes + member.file_size > MAX_EXTRACTED_BYTES:
                raise ValueError("archive expansion exceeds safety limits")
            target = _safe_member(root, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            count += 1
            extracted_bytes += member.file_size
    return count


def _extract_tar(data: bytes, root: Path) -> int:
    count = 0
    extracted_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"archive links are not allowed: {member.name}")
            target = _safe_member(root, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            if count >= MAX_EXTRACTED_FILES or extracted_bytes + member.size > MAX_EXTRACTED_BYTES:
                raise ValueError("archive expansion exceeds safety limits")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            count += 1
            extracted_bytes += member.size
    return count


def _extract_archive(data: bytes, root: Path) -> int:
    if data[:2] == b"PK":
        return _extract_zip(data, root)
    return _extract_tar(data, root)


def _expand_nested_archives(root: Path) -> int:
    count = 0
    for archive_path in sorted(root.rglob("*")):
        if not archive_path.is_file() or archive_path.suffixes[-2:] not in ([".tar", ".gz"], [".tgz"]):
            continue
        data = archive_path.read_bytes()
        count += _extract_tar(data, archive_path.parent)
    return count


def ingest_urls(test_log_url: str, must_gather_url: str, destination: Path | None = None) -> dict:
    """Download and safely extract one CI log plus one must-gather artifact."""
    root = destination or Path(tempfile.mkdtemp(prefix="engram-kubernaut-rca-"))
    root.mkdir(parents=True, exist_ok=True)
    log_api_url, log_kind = github_api_url(test_log_url)
    artifact_api_url, artifact_kind = github_api_url(must_gather_url)
    if log_kind != "job_logs" or artifact_kind != "artifact":
        raise ValueError("test_log_url must be a job URL and must_gather_url must be an artifact URL")
    log_data = _download(log_api_url)
    log_path = root / "ci" / "job.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(log_data)
    artifact_root = root / "must-gather"
    artifact_root.mkdir(parents=True, exist_ok=True)
    files = _extract_archive(_download(artifact_api_url), artifact_root)
    files += _expand_nested_archives(artifact_root)
    from .normalize import iter_evidence

    index_path = root / ".engram" / "evidence-index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    indexed = 0
    with index_path.open("w", encoding="utf-8") as index:
        for evidence in iter_evidence(root):
            index.write(json.dumps(asdict(evidence), default=str, sort_keys=True) + "\n")
            indexed += 1
    return {
        "root": str(root),
        "job_log": str(log_path.relative_to(root)),
        "artifact_files": files,
        "indexed_evidence": indexed,
        "index_file": str(index_path.relative_to(root)),
        "sources": {"test_log_url": test_log_url, "must_gather_url": must_gather_url},
    }
