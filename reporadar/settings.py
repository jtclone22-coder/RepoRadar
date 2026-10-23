"""The folders to scan, kept in %APPDATA%\\RepoRadar\\settings.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .radar import default_roots


def home() -> Path:
    base = os.environ.get("REPORADAR_HOME") or (
        os.path.join(os.environ["APPDATA"], "RepoRadar") if os.environ.get("APPDATA")
        else os.path.join(os.path.expanduser("~"), ".reporadar"))
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base)


def load() -> dict:
    try:
        data = json.loads((home() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("roots", default_roots())
    data.setdefault("depth", 6)
    return data


def save(data: dict) -> None:
    path = home() / "settings.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def add_root(folder: str) -> list[str]:
    data = load()
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"{folder} isn't a folder.")
    if os.path.normcase(folder) not in {os.path.normcase(r) for r in data["roots"]}:
        data["roots"].append(folder)
        save(data)
    return data["roots"]


def risky(folder: str) -> str:
    """A warning when a folder is somewhere downloaded code lands: git there can run a repository's own filters."""
    target = os.path.normcase(os.path.abspath(folder))
    places = [os.path.join(os.path.expanduser("~"), "Downloads"), os.environ.get("TEMP", ""), os.environ.get("TMP", "")]
    for place in filter(None, places):
        place = os.path.normcase(os.path.abspath(place))
        if target == place or target.startswith(place + os.sep):
            return (f"{folder} is where downloads land. Scanning runs git status, which can run programs a "
                    "repository's own settings name; only scan repositories you trust.")
    return ""


def remove_root(folder: str) -> list[str]:
    data = load()
    target = os.path.normcase(os.path.abspath(folder))
    kept = [r for r in data["roots"] if os.path.normcase(r) != target]
    if len(kept) == len(data["roots"]):
        raise ValueError(f"{folder} isn't in the list.")
    data["roots"] = kept
    save(data)
    return kept
