"""Immutable kit history with guarded, recoverable stable-name promotion."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from release_inventory import (require, read_json, write_json_atomic, sha256_file,
                               validate_zip, validate_qualification, relative_name,
                               load_inventory, validate_inventory, published_bundle, load_site_entries)

JOURNAL = "data/kit-revision-transaction.json"


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def archive_path(record):
    require(isinstance(record.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]),
            "invalid archive digest")
    return f"archive/{record['sha256']}/{relative_name(record['name'])}"


def archive_file(root, record, source):
    require(sha256_file(source) == record["sha256"], "archive source checksum mismatch")
    dest = Path(root) / "docs/downloads" / archive_path(record)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        require(sha256_file(dest) == record["sha256"], "immutable archive was changed")
    else:
        # Install only complete files under their immutable names.
        fd, name = tempfile.mkstemp(dir=dest.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output, Path(source).open("rb") as stream:
                shutil.copyfileobj(stream, output)
                output.flush()
                os.fsync(output.fileno())
            require(sha256_file(temporary) == record["sha256"], "archive copy checksum mismatch")
            try:
                os.link(temporary, dest)
            except FileExistsError:
                require(sha256_file(dest) == record["sha256"], "immutable archive was changed")
        finally:
            temporary.unlink(missing_ok=True)
    record["archive"] = archive_path(record)


def archive_version(root, item):
    if "revisions" in item:
        return
    for record in item["files"].values():
        archive_file(root, record, Path(root) / "docs/downloads" / record["name"])
    revision = copy.deepcopy(item)
    item.update(current_revision=1, revisions={"1": revision}, promotions=[{"revision": 1, "reason": "migration"}])


def validate_revisions(root, slug, version, item):
    revisions = item.get("revisions", {})
    current = item.get("current_revision")
    require(type(current) is int and str(current) in revisions, "missing selected kit revision")
    require(set(revisions) == {str(i) for i in range(1, len(revisions) + 1)}, "noncontiguous kit history")
    selected = revisions[str(current)]
    for field in ("files", "kind", "qualification", "date"):
        require(item.get(field) == selected.get(field), "selected revision/public metadata mismatch")
    events = item.get("promotions")
    require(isinstance(events, list) and events and events[-1].get("revision") == current,
            "missing kit promotion history")
    require(all(str(e.get("revision")) in revisions and e.get("reason") for e in events), "invalid promotion event")
    for number, revision in revisions.items():
        validate_qualification(slug, version, revision.get("qualification"))
        require(set(revision["files"]) == set(item["files"]), "revision download roles changed")
        for record in revision["files"].values():
            require(record.get("archive") == archive_path(record), "invalid immutable archive path")
            path = Path(root) / "docs/downloads" / record["archive"]
            require(path.is_file() and path.stat().st_size == record["size"]
                    and sha256_file(path) == record["sha256"], "immutable archive missing or changed")
            validate_zip(path)
        if int(number) > 1:
            require(isinstance(revision.get("game_contract_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", revision["game_contract_sha256"]), "invalid game contract")
            require(revision.get("game_contract_sha256") == item.get("game_contract_sha256"),
                    "kit revision game contract changed")


def migrate(root, inventory):
    result = copy.deepcopy(inventory)
    for release in result["releases"].values():
        for item in release["versions"].values():
            archive_version(root, item)
    result["schema"] = 2
    return result


def _replace(source, target):
    temporary = target.with_name(target.name + ".revision-tmp")
    require(not temporary.exists(), f"stale staging file: {temporary}")
    try:
        shutil.copyfile(source, temporary)
        require(sha256_file(temporary) == sha256_file(source), "public copy checksum mismatch")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def recover(root):
    root = Path(root)
    journal = root / JOURNAL
    state = read_json(journal)
    for record in state["restore"]:
        source = root / "docs/downloads" / archive_path(record)
        require(sha256_file(source) == record["sha256"], "recovery archive changed")
        target = root / "docs/downloads" / relative_name(record["name"])
        target.with_name(target.name + ".revision-tmp").unlink(missing_ok=True)
        _replace(source, target)
    inventory_path = root / "data/releases.json"
    inventory_path.with_name(inventory_path.name + ".tmp").unlink(missing_ok=True)
    write_json_atomic(inventory_path, state["previous"])
    validate_inventory(root, state["previous"])
    journal.unlink()


def promote(root, previous, updated, files, restore):
    root = Path(root)
    journal = root / JOURNAL
    # Atomically install a complete exclusive recovery record before any alias changes.
    fd, name = tempfile.mkstemp(dir=journal.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"previous": previous, "restore": restore}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, journal)
        except FileExistsError:
            require(False, "unfinished publication; run import_release.py --recover")
    finally:
        temporary.unlink(missing_ok=True)
    if read_json(root / "data/releases.json") != previous:
        journal.unlink()
        require(False, "publication changed during promotion")
    try:
        for record in files.values():
            _replace(root / "docs/downloads" / record["archive"], root / "docs/downloads" / record["name"])
        validate_inventory(root, updated)
        for entry in load_site_entries(root).values():
            if entry.get("version") and not entry.get("hidden"):
                published_bundle(root, entry, updated)
        write_json_atomic(root / "data/releases.json", updated)
    except BaseException:
        recover(root)
        raise
    journal.unlink()


def import_revision(root, ready_path, candidate, ready, release):
    root = Path(root)
    previous = load_inventory(root / "data/releases.json")
    validate_inventory(root, previous)
    plan = release["plan"]
    slug, version, number = plan["kit"], plan["version"], plan["kit_revision"]
    require(type(number) is int and number > 1, "tooling revision must be an integer greater than 1")
    require(slug in previous["releases"] and version in previous["releases"][slug]["versions"],
            "tooling revision needs an existing game release")
    old = previous["releases"][slug]["versions"][version]
    require(old["qualification"]["type"] in ("qualified-candidate", "unchanged-public-parity"),
            "tooling revision requires a qualified game baseline")
    require(number == len(old.get("revisions", {"1": old})) + 1, "kit revision already exists or skips history")
    evidence = ready.get("tooling_parity", {})
    require(evidence.get("status") == "passed" and evidence.get("release_sha256") == ready["release_sha256"],
            "missing or stale tooling parity")
    require(evidence.get("qa_sha256") == identity(ready["qa"]), "tooling parity is bound to another QA receipt")
    baseline = plan.get("tooling_baseline", {})
    baseline_files = {r: f["sha256"] for r, f in old["files"].items()}
    require(baseline.get("files") == baseline_files and evidence.get("baseline") == baseline,
            "tooling baseline differs from current publication")
    require(baseline.get("revision") == old.get("current_revision", 1), "stale baseline revision")
    contract = evidence.get("game_contract_sha256")
    require(isinstance(contract, str) and len(contract) == 64 and all(c in "0123456789abcdef" for c in contract),
            "missing game contract")
    require(not old.get("game_contract_sha256") or old["game_contract_sha256"] == contract,
            "game contract differs; use a new game version")
    require(set(release["downloads"]) == {r["name"] for r in old["files"].values()},
            "tooling revision must keep every public filename")
    updated = migrate(root, previous)
    item = updated["releases"][slug]["versions"][version]
    new = {"date": plan["date"], "kind": old["kind"], "files": {},
           "game_contract_sha256": contract, "tooling_baseline": baseline,
           "qualification": {"type": "qualified-candidate", "candidate": plan["candidate"],
                             "capcom_source_commit": plan["sources"]["capcom"],
                             "ready_record_sha256": sha256_file(ready_path),
                             "release_sha256": ready["release_sha256"]}}
    for role, old_record in old["files"].items():
        path = Path(candidate) / "downloads" / old_record["name"]
        record = {"name": path.name, "sha256": release["downloads"][path.name], "size": path.stat().st_size}
        archive_file(root, record, path)
        new["files"][role] = record
    item["revisions"][str(number)] = copy.deepcopy(new)
    item.update(new, current_revision=number)
    item["promotions"].append({"revision": number, "reason": "tooling update", "date": plan["date"]})
    promote(root, previous, updated, new["files"], list(old["files"].values()))
    return new


def rollback(root, slug, version, revision):
    root = Path(root)
    require(not (root / JOURNAL).exists(), "unfinished publication; run import_release.py --recover")
    previous = load_inventory(root / "data/releases.json")
    validate_inventory(root, previous)
    require(slug in previous["releases"] and version in previous["releases"][slug]["versions"], "unknown game release")
    updated = copy.deepcopy(previous)
    item = updated["releases"][slug]["versions"][version]
    require(str(revision) in item.get("revisions", {}), "unknown kit revision")
    restore = list(item["files"].values())
    selected = item["revisions"][str(revision)]
    for field in ("date", "kind", "files", "qualification"):
        item[field] = copy.deepcopy(selected[field])
    item["current_revision"] = revision
    item["promotions"].append({"revision": revision, "reason": "rollback"})
    promote(root, previous, updated, item["files"], restore)
