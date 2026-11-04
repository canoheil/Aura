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


ANCHOR_LINK = re.compile(r"\]\(([^)\s]*\.md#[^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def _github_slug(heading: str) -> str:
    """Approximate GitHub's heading anchors, including Persian headings."""
    text = heading.strip().lower()
    text = re.sub(r"[`*\[\]()]", "", text)
    text = re.sub(r"[^\w\s\u200c-]", "", text, flags=re.UNICODE)
    text = text.replace("\u200c", "")
    return re.sub(r"\s+", "-", text).strip("-")


def _anchors(document: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for match in HEADING.finditer(document.read_text(encoding="utf-8")):
        base = _github_slug(match.group(2))
        count = seen.get(base, 0)
        anchors.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return anchors


def test_internal_anchors_still_point_at_a_heading():
    """Renaming a heading silently breaks every cross-reference to it.

    A file that exists is not enough: `docs/business.md#some-section` is a promise
    about a heading, and a public contributor follows it.
    """
    documents = [ROOT / "README.md", ROOT / "README.fa.md", *sorted((ROOT / "docs").glob("*.md"))]
    broken = []
    for document in documents:
        for target in ANCHOR_LINK.findall(document.read_text(encoding="utf-8")):
            path_part, anchor = target.split("#", 1)
            target_path = (document.parent / path_part).resolve()
            if not target_path.is_file():
                broken.append(f"{document.name} -> {target} (no such file)")
            elif anchor not in _anchors(target_path):
                broken.append(f"{document.name} -> {target} (no such heading)")
    assert not broken, f"broken anchors: {broken}"


GOVERNANCE = ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "LICENSE", "NOTICE")


def test_every_governance_file_is_reachable_from_the_front_page():
    """A code of conduct nobody links to is a file, not a policy."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    orphans = [name for name in GOVERNANCE if name not in readme]
    assert not orphans, f"not linked from README.md: {orphans}"


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


# --- the environment reference ----------------------------------------------

ENV_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]+)=", re.MULTILINE)


def _template_variables() -> list[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return ENV_ASSIGNMENT.findall(text)


def test_every_template_variable_is_explained_in_the_docs():
    """`.env.example` is where operators look first; every line must lead somewhere.

    A variable that appears in the template but in no document is a question with
    no answer — and the reason this test exists is that twenty-two of them once
    had none.
    """
    documented = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "docs").glob("*.md"))
    )
    documented += (ROOT / "README.md").read_text(encoding="utf-8")
    missing = [name for name in _template_variables() if name not in documented]
    assert not missing, f"documented nowhere in docs/ or README.md: {missing}"


def test_every_template_variable_is_actually_read_by_the_code():
    """A knob nothing reads is a lie in the template, not a feature."""
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(ROOT.glob("*.py"))
    )
    unread = [name for name in _template_variables() if name not in source]
    assert not unread, f"in .env.example but never read by any module: {unread}"


# --- licensing and schema truth --------------------------------------------

def _runtime_requirements() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    names = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def test_every_runtime_dependency_is_named_in_the_notice():
    """A public repository has to attribute what it depends on.

    NOTICE is prose, so nothing would notice a new import shipping without its
    licence line; this does.
    """
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    missing = [name for name in _runtime_requirements() if name not in notice]
    assert not missing, f"runtime dependencies missing from NOTICE: {missing}"


def test_the_notice_names_the_bundled_font_licence():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Vazirmatn" in notice
    assert "SIL Open Font License" in notice
    assert (ROOT / "assets" / "fonts" / "OFL.txt").is_file()


def test_the_privacy_page_states_the_schema_version_that_ships():
    """The example in the docs must match migrations.py, not last year's release."""
    import migrations

    text = (ROOT / "docs" / "data-and-privacy.md").read_text(encoding="utf-8")
    version = migrations.LATEST_VERSION
    assert f"schema v{version}/{version}" in text, (
        f"docs/data-and-privacy.md does not mention schema v{version}/{version}; "
        "update the example after adding a migration"
    )


def test_the_security_policy_covers_the_features_that_touch_other_people():
    """A public security policy that omits its riskiest surface invites bad reports."""
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8").lower()
    for topic in ("secretary mode", "context crossing conversations", "prompt-injection"):
        assert topic in text, f"SECURITY.md does not mention {topic!r}"


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
