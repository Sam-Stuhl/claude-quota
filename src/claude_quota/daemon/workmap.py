"""Name what a session was for, in the words Sam thinks in.

``classify`` says how a request got to Claude (a chat turn, a routine, a task
he started, a terminal). This adds what it was about: a project, and a kind of
work (school, ATLAS development, a role, background routines, interactive use).
Both come from ATLAS's own filing of the session, its folder
(``Projects/atlas/...``, ``School/Fall 2026/PHYS 121/...``) and repo, falling
back to the working directory when ATLAS has no record.
"""

from __future__ import annotations

import re

from . import classify

KINDS = {
    "assistant": "ATLAS assistant",
    "roles": "Roles",
    "routines": "Background routines",
    "school": "School",
    "atlas_dev": "ATLAS development",
    "interactive": "Interactive terminal",
    "projects": "Other projects",
    "life": "Life admin",
    "work": "Work (Lumasoft)",
    "cowork": "Cowork",
    "other": "Unclassified",
}


def _folder_parts(folder: str | None) -> list[str]:
    return [p for p in (folder or "").split("/") if p]


def project_name(place: classify.Place, conv: dict, atlas: dict) -> str:
    """The project a session belongs to: a repo, a course, or an area of life."""
    parts = _folder_parts(atlas.get("folder"))
    if parts:
        top = parts[0]
        if top == "School":
            course = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
            return f"School: {course}" if course else "School"
        if top == "Projects" and len(parts) > 1:
            return parts[1]
        if top == "Network" and len(parts) > 1 and atlas.get("repo"):
            return atlas["repo"]
        if top in ("Network", "Life", "Work"):
            return {"Work": "Work (Lumasoft)"}.get(top, top)
    if atlas.get("repo"):
        return atlas["repo"]
    cwd = conv.get("cwd") or ""
    if place.category in ("atlas_chat", "atlas_voice"):
        return "ATLAS assistant"
    role = classify._role(cwd)
    if role:
        return f"role: {role}"
    if re.search(r"/school/|/School/", cwd):
        m = re.search(r"/(study|advisor)(/|$)", cwd)
        return f"School: {m.group(1)}" if m else "School"
    name = classify.repo_name(cwd)
    if name in ("(unknown folder)", "", "~") or re.match(r"^[a-z]+$", name) and name in ("tmp", "private"):
        return "(no repo)"
    return name


def kind_of(place: classify.Place, project: str, conv: dict, atlas: dict) -> str:
    cat = place.category
    if cat in ("atlas_chat", "atlas_voice"):
        return "assistant"
    if cat == "roles":
        return "roles"
    if cat in ("scheduled", "helpers", "event"):
        return "routines"
    if cat == "cowork":
        return "cowork"
    parts = _folder_parts(atlas.get("folder"))
    if (parts and parts[0] == "School") or project.startswith("School"):
        return "school"
    if parts and parts[0] == "Life":
        return "life"
    if parts and parts[0] == "Work":
        return "work"
    if project == "atlas" or project.startswith("atlas-"):
        return "atlas_dev"
    if cat == "terminal":
        return "interactive"
    if cat == "other":
        # classify found no ATLAS record, role, helper or launcher for it.
        return "other"
    if project and project not in ("(no repo)",):
        return "projects"
    return "other"


def host_name(device: str | None) -> str:
    d = (device or "").strip()
    if not d:
        return "(no host recorded)"
    return re.sub(r"^sams-", "", d)
