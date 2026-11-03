"""Documentation that cannot rot.

A README is the first thing a visitor reads, and it is also the first thing to go
stale: a link to a page that moved, a setting nobody documented, a translated
version that was forgotten. These tests keep the documentation honest without
anyone having to remember to check it.
"""
import re
import struct
from pathlib import Path

import pytest

import settings_store

ROOT = Path(__file__).resolve().parent.parent

# Every Markdown document a visitor can reach from the repository.
DOCUMENTS = [ROOT / "README.md", ROOT / "README.fa.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
DOCUMENTS += sorted((ROOT / "docs").glob("*.md"))

LINK = re.compile(r"\]\(([^)\s]+)\)")
IMAGE_TAG = re.compile(r"<img[^>]*\ssrc=\"([^\"]+)\"", re.IGNORECASE)


def _targets(text: str):
    """Every local file a document points at, from Markdown and from HTML."""
    candidates = LINK.findall(text) + IMAGE_TAG.findall(text)
    for target in candidates:
        if target.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        path = target.split("#", 1)[0]
        if path:  # a pure in-page anchor has nothing to resolve
            yield target, path


def _relative_links(document: Path):
    """Links and images that should point at a file in this repository."""
    yield from _targets(document.read_text(encoding="utf-8"))


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


# --- brand assets -----------------------------------------------------------

BRAND = ROOT / "assets" / "brand"


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height straight from the PNG header; no image library needed."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_the_brand_assets_exist_in_both_formats():
    for name in ("logo", "mark", "banner"):
        assert (BRAND / f"{name}.svg").is_file(), f"{name}.svg is missing"
        assert (BRAND / f"{name}.png").is_file(), f"{name}.png is missing"


def test_the_logo_is_square_and_the_banner_is_wide():
    assert _png_size(BRAND / "logo.png") == (512, 512)
    assert _png_size(BRAND / "logo-192.png") == (192, 192)
    assert _png_size(BRAND / "logo-64.png") == (64, 64)
    assert _png_size(BRAND / "banner.png") == (1200, 400)


def test_the_readmes_show_the_banner():
    for name in ("README.md", "README.fa.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "assets/brand/banner.png" in text, f"{name} does not show the banner"


def test_the_brand_page_documents_every_asset():
    """The palette page is where a contributor looks before redrawing anything."""
    text = (BRAND / "README.md").read_text(encoding="utf-8")
    for name in ("logo.svg", "mark.svg", "banner.svg", "logo-192.png", "logo-64.png"):
        assert name in text, f"{name} is not described in assets/brand/README.md"


def _sections(document: Path) -> int:
    return sum(1 for line in document.read_text(encoding="utf-8").splitlines() if line.startswith("## "))


def test_the_persian_readme_keeps_the_same_section_shape():
    """The translation may phrase things differently, but must not lose topics."""
    english = _sections(ROOT / "README.md")
    persian = _sections(ROOT / "README.fa.md")
    assert english >= 8, "README.md lost its structure"
    assert english == persian, f"README.fa.md has {persian} sections, README.md has {english}"
