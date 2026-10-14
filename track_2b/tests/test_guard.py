"""Garde-fou : aucun autre nom de modèle dans le dépôt, aucune adresse ni clé en dur."""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Les mots interdits, écrits à l'envers pour que ce fichier ne les contienne pas lui-même.
FORBIDDEN = tuple(w[::-1] for w in ("newq", "amall", "lartsim", "tpg", "edualc"))
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "runtime"}
TEXT_EXT = {".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".json", ".html", ".js", ".css", ".svg",
            ".sh", ".cfg", ".ini", "", ".env.example", ".dockerfile"}


def _text_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in TEXT_EXT or name in ("Dockerfile", "Makefile"):
                yield os.path.join(dirpath, name)


def test_no_other_model_name_in_repo():
    hits = []
    for path in _text_files():
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for n, line in enumerate(fh, 1):
                    low = line.lower()
                    for word in FORBIDDEN:
                        if word in low:
                            hits.append(f"{os.path.relpath(path, ROOT)}:{n}: {word}")
        except OSError:
            continue
    assert not hits, "forbidden model names found:\n" + "\n".join(hits)


def test_no_hardcoded_endpoint_or_key_in_sources():
    """Le code source ne contient ni https:// vers une API ni une clé qui ressemble à un secret."""
    src = os.path.join(ROOT, "track_2b", "src")
    bad = []
    key_like = re.compile(r"(sk|key|token)[-_][A-Za-z0-9]{24,}")
    for dirpath, _, filenames in os.walk(src):
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if "https://api." in line or key_like.search(line):
                        bad.append(f"{os.path.relpath(path, ROOT)}:{n}")
    assert not bad, "hard-coded endpoint or key:\n" + "\n".join(bad)


@pytest.mark.parametrize("word", FORBIDDEN)
def test_forbidden_list_is_lowercase(word):
    assert word == word.lower()
