"""Shape checks for every page in data/patches.json, hidden ones included.

A hidden page is skipped by the site build, so a malformed field would
otherwise surface only at import, when the page is unhidden.  build.py runs
this before rendering anything.
"""
from __future__ import annotations

import datetime

REQUIRED = ("slug", "title", "subtitle", "status", "version", "date", "game",
            "hardware", "summary")


class PageSchemaError(ValueError):
    pass


def _date(value) -> bool:
    try:
        datetime.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def problems(entry: dict, kind: str = "page") -> list[str]:
    slug = entry.get("slug", "?")
    out = []
    say = lambda message: out.append(f"{slug}: {message}")
    if kind == "page":
        # An in-development page has no release yet, so no version or date.
        unreleased = entry.get("status") == "in-development"
        for key in REQUIRED:
            if unreleased and key in ("version", "date") and entry.get(key) is None:
                continue
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                say(f"missing {key}")
        if not _strings(entry.get("description")) or not entry.get("description"):
            say("description must be a non-empty list of paragraphs")
        if not _strings(entry.get("changes")):
            say("changes must be a list of strings")
        shots = entry.get("screenshots")
        if not isinstance(shots, list):
            say("screenshots must be a list")
        else:
            for n, shot in enumerate(shots, 1):
                if not isinstance(shot, dict) or not shot.get("caption"):
                    say(f"screenshot {n} needs a caption")
                elif not (shot.get("single") or (shot.get("before") and shot.get("after"))):
                    say(f"screenshot {n} needs single, or before and after")
            if not shots and not entry.get("hidden"):
                say("a visible page needs screenshots")
    if entry.get("date") is not None and not _date(entry["date"]):
        say(f"date {entry['date']!r} is not YYYY-MM-DD")
    history = entry.get("release_history")
    if history is not None:
        if not isinstance(history, list) or not history:
            say("release_history must be a non-empty list")
            return out
        for rel in history:
            if not isinstance(rel, dict) or set(rel) != {"version", "date", "items"}:
                keys = sorted(rel) if isinstance(rel, dict) else rel
                say(f"release_history entries have exactly version, date and items, not {keys}")
                continue
            if not _date(rel["date"]):
                say(f"release_history {rel['version']} date {rel['date']!r} is not YYYY-MM-DD")
            if not _strings(rel["items"]) or not rel["items"]:
                say(f"release_history {rel['version']} items must be a non-empty list of strings")
        top = history[0]
        if isinstance(top, dict) and kind == "page" and (top.get("version"), top.get("date")) != (
                entry.get("version"), entry.get("date")):
            say("the newest release_history entry must match the page's version and date")
    return out


def validate(config: dict) -> None:
    found = [p for page in config.get("patches", []) for p in problems(page)]
    found += [p for project in config.get("projects", []) for p in problems(project, "project")]
    if found:
        raise PageSchemaError("data/patches.json:\n  " + "\n  ".join(found))
