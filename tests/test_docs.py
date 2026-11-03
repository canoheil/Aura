"""Documentation that cannot rot.

A README is the first thing a visitor reads, and it is also the first thing to go
stale: a link to a page that moved, a setting nobody documented, a translated
version that was forgotten. These tests keep the documentation honest without
anyone having to remember to check it.
"""
import re
from pathlib import Path

import pytest

import settings_store

ROOT = Path(__file__).resolve().parent.parent

# Every Markdown document a visitor can reach from the repository.
DOCUMENTS = [ROOT / "README.md", ROOT / "README.fa.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
DOCUMENTS += sorted((ROOT / "docs").glob("*.md"))

LINK = re.compile(r"\]\(([^)\s]+)\)")


def _relative_links(document: Path):
    """Links that should point at a file in this repository."""
    text = document.read_text(encoding="utf-8")
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:  # a pure in-page anchor
            continue
        yield target, path


def test_every_document_exists():
    assert DOCUMENTS, "no documentation found"
    for document in DOCUMENTS:
        assert document.is_file(), f"{document} is missing"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_relative_links_resolve(document):
    broken = [
        target
        for target, path in _relative_links(document)
        if not (document.parent / path).exists()
    ]
    assert not broken, f"{document.name} links to files that do not exist: {broken}"


def test_every_docs_page_is_reachable_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    orphans = [
        page.name
        for page in sorted((ROOT / "docs").glob("*.md"))
        if f"docs/{page.name}" not in readme
    ]
    assert not orphans, f"these pages are not linked from README.md: {orphans}"


def test_the_two_readmes_point_at_each_other():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    persian = (ROOT / "README.fa.md").read_text(encoding="utf-8")
    assert "README.fa.md" in english
    assert "README.md" in persian


def test_every_setting_is_documented_in_the_environment_template():
    """A setting nobody can discover is a setting that does not exist."""
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = [spec.env for spec in settings_store.SPECS if spec.env and spec.env not in template]
    assert not missing, f"settings missing from .env.example: {missing}"


def test_the_environment_template_is_not_a_stub():
    """Guard against a template that was emptied to make the check above pass."""
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assigned = [line for line in template.splitlines() if "=" in line and not line.startswith("#")]
    assert len(assigned) >= 50


def _sections(document: Path) -> int:
    return sum(1 for line in document.read_text(encoding="utf-8").splitlines() if line.startswith("## "))


def test_the_persian_readme_keeps_the_same_section_shape():
    """The translation may phrase things differently, but must not lose topics."""
    english = _sections(ROOT / "README.md")
    persian = _sections(ROOT / "README.fa.md")
    assert english >= 8, "README.md lost its structure"
    assert english == persian, f"README.fa.md has {persian} sections, README.md has {english}"
