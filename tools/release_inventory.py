#!/usr/bin/env python3
"""Tracked, append-only publication inventory for release downloads."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath


SCHEMA = 1
ROLES = ("ips", "mra", "chd", "kit")
QUALIFICATION_TYPES = (
    "qualified-candidate",
    "unchanged-public-parity",
    "historical-publication",
    "audio-publication-unqualified",
)


class ReleaseInventoryError(Exception):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ReleaseInventoryError(message)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseInventoryError(f"cannot read JSON {path}: {exc}") from exc
    require(isinstance(value, dict), f"expected a JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: dict) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    require(not temporary.exists(), f"temporary inventory already exists: {temporary}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def token(value: object, label: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value),
            f"invalid {label}: {value!r}")
    return value


def relative_name(value: object) -> str:
    require(isinstance(value, str), f"invalid download name: {value!r}")
    path = PurePosixPath(value)
    require(value and not path.is_absolute() and len(path.parts) == 1
            and value not in (".", "..") and "\\" not in value,
            f"download must be a plain filename: {value!r}")
    return value


def file_role(slug: str, version: str, name: str) -> str:
    prefix = f"{slug}-{version}-"
    require(name.startswith(prefix) and name.endswith(".zip"),
            f"download filename must start with {prefix!r} and end in .zip: {name}")
    role = name[len(prefix):-4]
    require(role in ROLES, f"unsupported download role {role!r}: {name}")
    return role


def validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            require(len(names) == len(set(names)), f"duplicate ZIP member in {path}")
            for info in infos:
                name = info.filename.rstrip("/")
                if not name:
                    continue
                member = PurePosixPath(name)
                require(not member.is_absolute() and ".." not in member.parts
                        and "\\" not in name,
                        f"unsafe ZIP member {info.filename!r} in {path}")
                require((info.external_attr >> 16) & 0o170000 != 0o120000,
                        f"ZIP symlink is forbidden: {info.filename} in {path}")
            bad = archive.testzip()
            require(bad is None, f"corrupt ZIP member {bad!r} in {path}")
    except zipfile.BadZipFile as exc:
        raise ReleaseInventoryError(f"invalid ZIP {path}: {exc}") from exc


def read_zip_json(path: Path, name: str) -> dict:
    validate_zip(path)
    try:
        with zipfile.ZipFile(path) as archive:
            value = json.loads(archive.read(name))
    except KeyError as exc:
        raise ReleaseInventoryError(f"{path.name} has no {name}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseInventoryError(f"invalid {name} in {path.name}: {exc}") from exc
    require(isinstance(value, dict), f"expected object in {path.name}:{name}")
    return value


def empty_inventory() -> dict:
    return {"schema": SCHEMA, "releases": {}}


def load_inventory(path: Path) -> dict:
    inventory = read_json(path)
    require(inventory.get("schema") == SCHEMA, "unsupported release inventory schema")
    require(isinstance(inventory.get("releases"), dict), "release inventory has no releases map")
    return inventory


def load_site_entries(root: Path) -> dict[str, dict]:
    config = read_json(Path(root) / "data" / "patches.json")
    result = {}
    for group in ("patches", "projects"):
        values = config.get(group, [])
        require(isinstance(values, list), f"data/patches.json {group} must be a list")
        for entry in values:
            require(isinstance(entry, dict), f"{group} entry must be an object")
            slug = token(entry.get("slug"), f"{group} slug")
            require(slug not in result, f"duplicate site slug: {slug}")
            result[slug] = entry
    return result


def validate_file_record(root: Path, slug: str, version: str, role: str,
                         record: dict) -> dict:
    require(isinstance(record, dict), f"invalid {slug} {version} {role} file record")
    name = relative_name(record.get("name"))
    # Migrated releases retain their historical public filenames.  New
    # imports use file_role()'s canonical slug-version-role convention, while
    # old names such as cpsplus-kit-1.3b.zip and final-fight-cd-rc1-kit.zip
    # remain valid because their explicit role and digest are inventoried.
    sha = record.get("sha256")
    size = record.get("size")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha),
            f"invalid SHA-256 for {name}")
    require(isinstance(size, int) and size >= 0, f"invalid size for {name}")
    path = Path(root) / "docs" / "downloads" / name
    require(path.is_file(), f"published download is missing: {path}")
    require(path.stat().st_size == size, f"published download size changed: {name}")
    require(sha256_file(path) == sha, f"published download hash changed: {name}")
    validate_zip(path)
    return {"zipname": name, "size": size, "sha256": sha}


def validate_qualification(slug: str, version: str, qualification: object) -> None:
    require(isinstance(qualification, dict), f"missing qualification: {slug} {version}")
    kind = qualification.get("type")
    require(kind in QUALIFICATION_TYPES,
            f"invalid qualification type for {slug} {version}: {kind!r}")
    if kind in ("historical-publication", "audio-publication-unqualified"):
        require(isinstance(qualification.get("note"), str) and qualification["note"],
                f"unqualified publication needs a note: {slug} {version}")
        return
    for field in ("candidate", "capcom_source_commit", "ready_record_sha256", "release_sha256"):
        require(isinstance(qualification.get(field), str) and qualification[field],
                f"qualified release lacks {field}: {slug} {version}")
    require(re.fullmatch(r"[0-9a-f]{40}", qualification["capcom_source_commit"]) is not None,
            f"invalid Capcom commit: {slug} {version}")
    for field in ("ready_record_sha256", "release_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", qualification[field]) is not None,
                f"invalid {field}: {slug} {version}")


def validate_inventory(root: Path, inventory: dict, *, require_complete: bool = True,
                       require_page_current: bool = True) -> None:
    require(inventory.get("schema") == SCHEMA, "unsupported release inventory schema")
    releases = inventory.get("releases")
    require(isinstance(releases, dict), "release inventory has no releases map")
    entries = load_site_entries(root)
    active = {slug for slug, entry in entries.items()
              if not entry.get("hidden") and entry.get("version")}
    if require_complete:
        require(set(releases) == active,
                f"inventory must cover active patches exactly; missing={sorted(active-set(releases))}, "
                f"extra={sorted(set(releases)-active)}")
    used_names: set[str] = set()
    for slug, release in sorted(releases.items()):
        token(slug, "release slug")
        require(slug in entries, f"inventory release has no site entry: {slug}")
        require(isinstance(release, dict), f"invalid release entry: {slug}")
        current = token(release.get("current"), f"{slug} current version")
        versions = release.get("versions")
        require(isinstance(versions, dict) and current in versions,
                f"{slug} current version is absent from its history")
        if slug in active and require_page_current:
            require(entries[slug]["version"] == current,
                    f"{slug} page version {entries[slug]['version']} != inventory current {current}")
        for version, item in sorted(versions.items()):
            token(version, f"{slug} version")
            require(isinstance(item, dict), f"invalid release version: {slug} {version}")
            validate_qualification(slug, version, item.get("qualification"))
            kind = item.get("kind")
            require(kind in ("rom", "chd", "kit"), f"invalid release kind: {slug} {version}")
            files = item.get("files")
            require(isinstance(files, dict) and files, f"release has no files: {slug} {version}")
            expected_roles = ({"ips", "mra"} if kind == "rom" and "mra" in files
                              else {"ips"} if kind == "rom"
                              else {kind})
            require(set(files) == expected_roles,
                    f"wrong file roles for {slug} {version}: {sorted(files)}")
            for role, record in files.items():
                info = validate_file_record(root, slug, version, role, record)
                require(info["zipname"] not in used_names,
                        f"download appears in more than one release: {info['zipname']}")
                used_names.add(info["zipname"])
    published_names = {path.name for path in (Path(root) / "docs" / "downloads").glob("*.zip")}
    require(used_names == published_names,
            f"download inventory must cover docs/downloads exactly; "
            f"missing={sorted(published_names-used_names)}, extra={sorted(used_names-published_names)}")


def _rom_bundle(root: Path, patch: dict, files: dict) -> dict:
    ips = validate_file_record(root, patch["slug"], patch["version"], "ips", files["ips"])
    manifest = read_zip_json(Path(root) / "docs" / "downloads" / ips["zipname"], "manifest.json")
    require(manifest.get("version") == patch["version"],
            f"embedded manifest version mismatch: {ips['zipname']}")
    result = {"kind": "rom", "ips": ips}
    variants = manifest.get("variants")
    if variants is not None:
        require(isinstance(variants, list) and variants, f"empty variants in {ips['zipname']}")
        configs = {item["key"]: item for item in (patch.get("artifact", {}).get("variants") or [])}
        rendered = []
        for item in variants:
            require(isinstance(item, dict) and item.get("key") in configs,
                    f"manifest variant does not match page configuration in {ips['zipname']}")
            config = configs[item["key"]]
            entry = {"key": item["key"], "label": item.get("label") or config.get("label"),
                     "members": item.get("members")}
            require(entry["label"] and isinstance(entry["members"], list),
                    f"incomplete variant in {ips['zipname']}")
            if item.get("hbmame"):
                entry["hbmame"] = item["hbmame"]
            if item.get("mra"):
                mra = config.get("mra")
                require(isinstance(mra, dict) and mra.get("setname") == item.get("mra_setname"),
                        f"MRA metadata mismatch for {item['key']} in {ips['zipname']}")
                entry["mra"] = mra
            rendered.append(entry)
        require(set(configs) == {item["key"] for item in rendered},
                f"published variants do not cover page configuration in {ips['zipname']}")
        result["variants"] = rendered
        result["members"] = rendered[0]["members"]
    else:
        require(isinstance(manifest.get("members"), list), f"no members in {ips['zipname']}")
        result["members"] = manifest["members"]
    if "mra" in files:
        result["mra"] = validate_file_record(
            root, patch["slug"], patch["version"], "mra", files["mra"])
    return result


def published_bundle(root: Path, patch: dict, inventory: dict) -> dict:
    slug = patch["slug"]
    require(slug in inventory["releases"], f"no published release inventory for {slug}")
    release = inventory["releases"][slug]
    version = release["current"]
    require(patch.get("version") == version,
            f"{slug} page version {patch.get('version')} != inventory current {version}")
    item = release["versions"][version]
    files = item["files"]
    if item["kind"] == "rom":
        return _rom_bundle(root, patch, files)
    role = item["kind"]
    info = validate_file_record(root, slug, version, role, files[role])
    if role == "chd":
        manifest = read_zip_json(Path(root) / "docs" / "downloads" / info["zipname"],
                                 "manifest.json")
        require(manifest.get("version") == version,
                f"embedded manifest version mismatch: {info['zipname']}")
        return {"kind": "chd", "chd": info, "manifest": manifest}
    return {"kind": "kit", **info, "regions": []}


def candidate_release(ready_path: Path, candidate: Path) -> tuple[dict, dict]:
    ready = read_json(ready_path)
    require(ready.get("schema") == SCHEMA and ready.get("status") == "ready_for_import",
            "release is not explicitly ready_for_import")
    release_path = Path(candidate) / "release.json"
    release = read_json(release_path)
    require(sha256_file(release_path) == ready.get("release_sha256"),
            "candidate release.json does not match readiness record")
    require(release == ready.get("release"),
            "candidate release.json content differs from readiness record")
    require(release.get("schema") == SCHEMA and release.get("status") == "candidate",
            "unsupported candidate release manifest")
    require(ready.get("reproduction", {}).get("status") == "clean_rebuild_identical",
            "candidate has no passing clean reproduction")
    require(ready.get("reproduction", {}).get("release_sha256") == ready.get("release_sha256"),
            "clean reproduction is bound to another candidate")
    require(ready.get("reproduction", {}).get("downloads") == release.get("downloads"),
            "clean reproduction covers another download set")
    require(ready.get("qa", {}).get("status") == "passed",
            "candidate QA did not pass")
    require(ready.get("qa", {}).get("release_sha256") == ready.get("release_sha256"),
            "candidate QA is bound to another release")
    downloads = release.get("downloads")
    require(isinstance(downloads, dict) and downloads, "candidate has no downloads")
    actual = {path.name for path in (Path(candidate) / "downloads").iterdir() if path.is_file()}
    require(actual == set(downloads), "candidate download directory differs from release.json")
    for name, expected in downloads.items():
        relative_name(name)
        path = Path(candidate) / "downloads" / name
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
                f"invalid candidate hash for {name}")
        require(sha256_file(path) == expected, f"candidate download hash mismatch: {name}")
        validate_zip(path)
    return ready, release


def import_candidate(root: Path, ready_path: Path, candidate: Path) -> dict:
    root = Path(root)
    inventory_path = root / "data" / "releases.json"
    inventory = load_inventory(inventory_path)
    # Release-page prose is prepared with the new version before import.  At
    # this point the tracked inventory still points at the old public version,
    # so validate its files and shape without requiring those two current
    # pointers to agree until the append succeeds.
    validate_inventory(root, inventory, require_complete=False,
                       require_page_current=False)
    ready, release = candidate_release(ready_path, candidate)
    plan = release.get("plan")
    require(isinstance(plan, dict), "candidate has no release plan")
    slug = token(plan.get("kit"), "candidate kit")
    version = token(plan.get("version"), "candidate version")
    patches = load_site_entries(root)
    require(slug in patches and not patches[slug].get("hidden"),
            f"candidate has no active site page: {slug}")
    require(patches[slug].get("version") == version,
            f"update {slug} page metadata to version {version} before importing")
    entry = inventory["releases"].setdefault(slug, {"current": version, "versions": {}})
    require(version not in entry["versions"],
            f"release already exists and cannot be replaced: {slug} {version}")

    files: dict[str, dict] = {}
    for name, expected in release["downloads"].items():
        role = file_role(slug, version, name)
        require(role not in files, f"duplicate {role} download for {slug} {version}")
        source = Path(candidate) / "downloads" / name
        files[role] = {"name": name, "sha256": expected, "size": source.stat().st_size}
    kind = "rom" if "ips" in files else "chd" if "chd" in files else "kit" if "kit" in files else None
    require(kind is not None, f"candidate has no primary download: {slug} {version}")
    expected_roles = ({"ips", "mra"} if kind == "rom" and "mra" in files
                      else {"ips"} if kind == "rom" else {kind})
    require(set(files) == expected_roles, f"unexpected candidate download roles: {sorted(files)}")

    destination = root / "docs" / "downloads"
    added: list[Path] = []
    try:
        for record in files.values():
            target = destination / record["name"]
            require(not target.exists(), f"download already exists and cannot be replaced: {target.name}")
            temporary = target.with_name(target.name + ".tmp")
            require(not temporary.exists(), f"temporary download already exists: {temporary.name}")
            shutil.copyfile(Path(candidate) / "downloads" / record["name"], temporary)
            require(sha256_file(temporary) == record["sha256"],
                    f"copied download hash mismatch: {record['name']}")
            temporary.replace(target)
            added.append(target)
        entry["versions"][version] = {
            "date": plan.get("date"),
            "files": files,
            "kind": kind,
            "qualification": {
                "type": "qualified-candidate",
                "candidate": plan.get("candidate"),
                "capcom_source_commit": plan.get("sources", {}).get("capcom"),
                "ready_record_sha256": sha256_file(ready_path),
                "release_sha256": ready["release_sha256"],
            },
        }
        entry["current"] = version
        validate_inventory(root, inventory)
        published_bundle(root, patches[slug], inventory)
        write_json_atomic(inventory_path, inventory)
    except Exception:
        for path in added:
            path.unlink(missing_ok=True)
        raise
    return entry["versions"][version]
