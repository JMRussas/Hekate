#  Orchestration Engine - Slug Utilities
#
#  Branch-safe slug generation for git branch names.
#
#  Depends on: (none)
#  Used by:    routes/projects.py, services/executor.py

import re


def slugify(name: str) -> str:
    """Convert a project name to a branch-safe slug.

    Strips non-alphanumeric characters, collapses runs to hyphens,
    and truncates to 50 chars. Returns 'project' for empty input.
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:50] or "project"
