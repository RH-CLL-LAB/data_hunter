"""Audit and restore the public starter collection without executing its contents.

The lock contains previously acquired public files only. New/controlled sources
belong in the acquisition backlog, never in this unattended restore command.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import re
import subprocess
import tarfile
import tempfile
import zipfile

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root
PROJECT = workspace_root(default=CODE_ROOT)
COLLECTION = PROJECT / "data/raw/public_reference_collection"
DATA = COLLECTION / "downloaded_data"
LOCK = PROJECT / "configs/public_resources.lock.json"
REPORTS = PROJECT / "data/interim/public_resource_audit"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained(root, name):
    parts = PurePosixPath(name)
    if not name or parts.is_absolute() or ".." in parts.parts or "\\" in name:
        raise ValueError(f"Unsafe relative path: {name!r}")
    path = Path(root) / name
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"Path escapes destination: {name!r}")
    return path


def repository_tree(path):
    """Hash names/content, ignoring GitHub's ref-dependent ZIP root directory."""
    entries = []
    with zipfile.ZipFile(path) as archive:
        commit = archive.comment.decode("ascii")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("GitHub ZIP has no commit identity")
        if (len(archive.infolist()) > 100000
                or sum(i.file_size for i in archive.infolist()) > 1024 ** 3):
            raise ValueError("Repository archive exceeds inspection limits")
        roots = set()
        for info in archive.infolist():
            contained(Path("/tmp/archive-inspection"), info.filename)
            roots.add(PurePosixPath(info.filename).parts[0])
            if info.is_dir():
                continue
            name = info.filename.split("/", 1)[1]
            with archive.open(info) as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append((name, info.external_attr >> 16, sha))
        if len(roots) != 1 or len({x[0] for x in entries}) != len(entries):
            raise ValueError("Unexpected ZIP roots or duplicate members")
    payload = json.dumps(sorted(entries), separators=(",", ":")).encode()
    return commit, hashlib.sha256(payload).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        temp = Path(stream.name)
    temp.replace(path)


def collection_file(name):
    """Resolve a historical top-level file after documented housekeeping."""
    ledger = COLLECTION / 'organization.json'
    relocations = json.loads(ledger.read_text()).get('relocations', {}) if ledger.exists() else {}
    return contained(COLLECTION, relocations.get(name, name))


def create_lock():
    if LOCK.exists():
        raise ValueError("Lock already exists; do not silently replace acquisition identity")
    manifest = json.loads((DATA / "download_manifest.json").read_text())
    files = []
    for item in manifest["files"]:
        path = contained(DATA, item["path"])
        if digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError(f"Original acquisition differs: {item['path']}")
        record = {k: item[k] for k in ("path", "url", "sha256", "bytes")}
        record["verification"] = "sha256"
        record["hash_authority"] = "original_local_acquisition_manifest"
        if item["url"].startswith("https://codeload.github.com/"):
            commit, tree = repository_tree(path)
            record.update(original_url=item["url"], commit=commit,
                          tree_sha256=tree, verification="github_commit_and_tree")
            record["url"] = item["url"].rsplit("/", 1)[0] + "/" + commit
        files.append(record)
    write_json(LOCK, {"schema_version": 1, "scope": "existing_public_starter_only",
                     "files": files})
    return {"locked_files": len(files)}


def check_file(path, item):
    if not path.exists():
        return {"state": "missing"}
    actual = digest(path)
    if actual == item["sha256"] and path.stat().st_size == item["bytes"]:
        return {"state": "verified_original_bytes", "sha256": actual}
    if item["verification"] == "github_commit_and_tree":
        try:
            commit, tree = repository_tree(path)
            if commit == item["commit"] and tree == item["tree_sha256"]:
                return {"state": "verified_pinned_tree", "sha256": actual}
        except (ValueError, zipfile.BadZipFile, UnicodeError, OSError):
            pass
    return {"state": "mismatch", "sha256": actual}


def archive_check(path):
    if path.suffix in (".zip", ".xlsx"):
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"ZIP CRC failure: {bad}")
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for info in archive:
                if info.isfile():
                    with archive.extractfile(info) as stream:
                        while stream.read(1024 * 1024):
                            pass
    elif path.suffix == ".gz":
        with gzip.open(path, "rb") as stream:
            while stream.read(1024 * 1024):
                pass


def csv_shape(path):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as table:
                reader = csv.reader(table)
                header = next(reader)
                rows = 0
                rectangular = True
                for row in reader:
                    rows += 1
                    rectangular &= len(row) == len(header)
            return rows, len(header), rectangular, encoding
        except UnicodeDecodeError:
            if encoding == "cp1252":
                raise


def audit():
    lock = json.loads(LOCK.read_text())
    files, failures = [], []
    for item in lock["files"]:
        path = contained(DATA, item["path"])
        result = {"path": item["path"], **check_file(path, item)}
        if result["state"].startswith("verified"):
            try:
                archive_check(path)
                result["container_integrity"] = "passed_or_not_applicable"
            except (ValueError, OSError, EOFError, zipfile.BadZipFile, tarfile.TarError) as error:
                result["container_integrity"] = "failed"
                failures.append({"path": item["path"], "error": str(error)})
        else:
            failures.append(result)
        files.append(result)
    provider, provider_absent = [], []
    for sums in sorted(DATA.glob("*/SHA256SUMS.txt")):
        for line in sums.read_text().splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line)
            if not match:
                raise ValueError(f"Unparsed checksum line in {sums}")
            sha, name = match.groups()
            path = contained(sums.parent, name)
            relative = str(path.relative_to(DATA))
            if not path.is_file():
                provider_absent.append(relative)
                continue
            result = {"path": relative, "match": digest(path) == sha.lower()}
            provider.append(result)
            if not result["match"]:
                failures.append(result)
    conversions = []
    with (DATA / "derived_csv/conversion_index.csv").open(newline="") as stream:
        for record in csv.DictReader(stream):
            path = contained(DATA / "derived_csv", record["csv"])
            source = contained(DATA, record["source"])
            try:
                rows, columns, rectangular, encoding = csv_shape(path)
                passed = (rows == int(record["rows"]) and columns == int(record["columns"])
                          and rectangular and source.is_file())
                result = {"path": str(path.relative_to(DATA)), "rows": rows,
                          "columns": columns, "source_exists": source.is_file(), "passed": passed,
                          "read_encoding": encoding}
            except (OSError, UnicodeError, csv.Error, StopIteration) as error:
                passed = False
                result = {"path": str(path.relative_to(DATA)), "passed": False, "error": str(error)}
            conversions.append(result)
            if not passed:
                failures.append(result)
    ledger_path = COLLECTION / 'organization.json'
    organization = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    bundle_path = collection_file("MedScrape_public_datasets.zip")
    archive_check(bundle_path)
    bundle_mismatches = []
    duplicate_mismatches = []
    duplicate = COLLECTION / "MedScrape_public_datasets"
    duplicate_removed = not duplicate.exists() and organization.get('removed_duplicate_delivery', {}).get('path') == duplicate.name
    with zipfile.ZipFile(bundle_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.split("/", 1)[1]
            path = contained(DATA, name)
            # The bundle's tools/catalog are beside downloaded_data in the original layout.
            if name.startswith("tools_and_catalog/"):
                path = collection_file(name.split("/", 1)[1])
            with archive.open(info) as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if not path.is_file() or digest(path) != sha:
                bundle_mismatches.append(name)
            copy = contained(duplicate, name)
            if not duplicate_removed and (not copy.is_file() or digest(copy) != sha):
                duplicate_mismatches.append(name)
    if bundle_mismatches:
        failures.append({"bundle_mismatches": bundle_mismatches})
    for path in sorted(duplicate.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        relative = path.relative_to(duplicate)
        original = DATA / relative
        if relative.parts[0] == "tools_and_catalog":
            original = collection_file('/'.join(relative.parts[1:]))
        if not original.is_file() or digest(original) != digest(path):
            if str(relative) not in duplicate_mismatches:
                duplicate_mismatches.append(str(relative))
    if duplicate_mismatches:
        failures.append({"duplicate_mismatches": duplicate_mismatches})
    cleanup_checks = []
    if duplicate_removed:
        for record in organization['removed_duplicate_delivery']['files']:
            path = contained(COLLECTION, record['retained'])
            passed = path.is_file() and digest(path) == record['sha256']
            cleanup_checks.append({'path': record['retained'], 'passed': passed})
            if not passed:
                failures.append({'cleanup_retained_file_mismatch': record['retained']})
    census = []
    for path in sorted(COLLECTION.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file():
            census.append({"path": str(path.relative_to(COLLECTION)),
                           "bytes": path.stat().st_size, "sha256": digest(path)})
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "collection": str(COLLECTION), "files": files,
              "original_states": dict(Counter(x["state"] for x in files)),
              "provider_checks": provider, "provider_listed_not_local": provider_absent,
              "conversions": conversions,
              "bundle": {"sha256": digest(bundle_path), "bytes": bundle_path.stat().st_size,
                         "crc": "passed", "member_mismatches": bundle_mismatches},
              "duplicate_mismatches": duplicate_mismatches,
              "duplicate_delivery_state": 'removed_after_verified_deduplication' if duplicate_removed else 'retained_and_checked',
              "cleanup_retained_checks": cleanup_checks,
              "census_files": len(census), "census_bytes": sum(x["bytes"] for x in census),
              "failures": failures}
    write_json(REPORTS / "audit.json", report)
    write_json(REPORTS / "census.json", census)
    return {k: report[k] for k in ("original_states", "census_files", "census_bytes")} | {
        "provider_checks": len(provider), "provider_listed_not_local": len(provider_absent),
        "conversions": len(conversions), "failures": len(failures)}


def restore(download=False, only=None):
    records = json.loads(LOCK.read_text())["files"]
    if only:
        records = [r for r in records if r["path"] == only]
        if not records:
            raise ValueError(f"Unknown locked file: {only}")
    results = []
    for item in records:
        path = contained(DATA, item["path"])
        checked = check_file(path, item)
        if checked["state"].startswith("verified"):
            results.append({"path": item["path"], "state": "already_verified"})
            continue
        if checked["state"] == "mismatch":
            results.append({"path": item["path"], "state": "mismatch_requires_review"})
            continue
        if not download:
            results.append({"path": item["path"], "state": "would_download", "url": item["url"]})
            continue
        if not item["url"].startswith("https://"):
            raise ValueError("Only HTTPS downloads are allowed")
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        if part.is_symlink():
            raise ValueError("Refusing a symlink download destination")
        cmd = ["curl", "--fail", "--location", "--silent", "--show-error",
               "--proto", "=https", "--proto-redir", "=https",
               "--retry", "0", "--connect-timeout", "20", "--max-time", "600",
               "--max-filesize", str(item["bytes"] + 1024 * 1024),
               "--continue-at", "-", "--output", str(part), item["url"]]
        # Re-invoke curl so every retry resumes from the current partial size.
        # Built-in retries may rewind to the offset at the initial invocation.
        for attempt in range(4):
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                break
        state = "download_failed"
        if result.returncode == 0:
            verification = check_file(part, item)
            if verification["state"].startswith("verified"):
                archive_check(part) if item["verification"] == "github_commit_and_tree" else None
                part.replace(path)
                state = verification["state"]
            else:
                state = "download_hash_mismatch"
        results.append({"path": item["path"], "state": state})
    write_json(REPORTS / ("restore.json" if download else "restore_plan.json"), results)
    return dict(Counter(x["state"] for x in results))


def main():
    global LOCK, DATA, REPORTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("lock", "audit", "plan", "sync"))
    parser.add_argument("--only", help="Select one exact relative path from the lock")
    parser.add_argument("--lock", type=Path, default=LOCK, help="File lock for plan/sync")
    parser.add_argument("--data-root", type=Path, default=DATA, help="Destination root for plan/sync")
    parser.add_argument("--report-dir", type=Path, default=REPORTS, help="Generated report directory")
    args = parser.parse_args()
    if args.action in ("lock", "audit") and (args.lock != LOCK or args.data_root != DATA):
        parser.error("Custom locks/destinations are supported by plan and sync only")
    LOCK, DATA, REPORTS = args.lock.resolve(), args.data_root.resolve(), args.report_dir.resolve()
    if args.action == "lock":
        result = create_lock()
    elif args.action == "audit":
        result = audit()
    else:
        result = restore(args.action == "sync", args.only)
    print(json.dumps(result, indent=2))
    bad = (result.get("failures", 0) or result.get("mismatch_requires_review", 0)
           or result.get("download_failed", 0) or result.get("download_hash_mismatch", 0))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
