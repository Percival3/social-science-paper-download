"""
PDF storage path and code generation rules.
"""
import html
import re
from pathlib import Path
from typing import Optional

from .database import Database, Journal, Paper


WINDOWS_RESERVED_CHARS = r'<>:"/\|?*'
NON_DOWNLOAD_MAIN_TITLES = {
    "acknowledgements",
    "acknowledgments",
    "addendum",
    "appendix",
    "appendix a",
    "back matter",
    "bibliography",
    "book review",
    "book reviews",
    "conclusion",
    "conclusions",
    "contents",
    "contributors",
    "correction",
    "correction to",
    "corrigendum",
    "erratum",
    "front matter",
    "frontmatter",
    "index",
    "introduction",
    "list of abbreviations",
    "notes",
    "preface",
    "references",
    "table of contents",
}


def sanitize_path_part(value: Optional[str], fallback: str = "unknown") -> str:
    """Make journal titles and other metadata safe for Windows folder names."""
    text = str(value).strip() if value else fallback
    text = "".join("_" if char in WINDOWS_RESERVED_CHARS else char for char in text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or fallback


def numeric_code(value: Optional[object], width: int) -> str:
    """Extract a numeric code and left-pad it; missing values become zero codes."""
    if value is None:
        return "0" * width

    text = str(value).strip()
    if not text:
        return "0" * width

    match = re.search(r"\d+", text)
    if not match:
        return "0" * width

    number = int(match.group(0))
    return f"{number:0{width}d}" if number < 10 ** width else str(number)


def main_title(value: Optional[str], fallback: str = "untitled") -> str:
    """Return the filename-safe main title before the first colon."""
    text = html.unescape(str(value).strip()) if value else fallback
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    for separator in (":", "："):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
            break

    text = "".join("_" if char in WINDOWS_RESERVED_CHARS else char for char in text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:180]


def normalized_main_title(value: Optional[str]) -> str:
    """Normalize the main title for classification and collision checks."""
    return main_title(value).casefold()


def is_non_downloadable_title(value: Optional[str]) -> bool:
    """Return whether a title represents front matter, reviews, or corrections."""
    title = normalized_main_title(value)
    title_without_space = re.sub(r"\s+", "", title)

    return (
        title in NON_DOWNLOAD_MAIN_TITLES
        or title_without_space in {"frontmatter", "backmatter"}
        or title.startswith("appendix")
        or bool(re.fullmatch(r"\d+\s+introduction", title))
    )


def journal_code(db: Database, journal_id: Optional[str]) -> str:
    """Return the three-digit journal code from the merged journal-list ID."""
    if not journal_id:
        return "000"

    journal = db.get_journal(journal_id)
    if journal and journal.source_id:
        return f"{journal.source_id:03d}" if journal.source_id < 1000 else str(journal.source_id)

    return "000"


def issue_paper_id(db: Database, paper: Paper) -> str:
    """Number papers within the same journal-year-volume-issue by DOI order."""
    if not paper.doi:
        return "01"

    papers = db.list_papers(
        journal_id=paper.journal_id,
        year=paper.published_year,
        limit=1_000_000,
    )

    volume = numeric_code(paper.volume, 3)
    issue = numeric_code(paper.issue, 2)
    issue_dois = {
        candidate.doi.lower()
        for candidate in papers
        if numeric_code(candidate.volume, 3) == volume
        and numeric_code(candidate.issue, 2) == issue
        and candidate.doi
    }
    issue_dois.add(paper.doi.lower())

    sorted_dois = sorted(issue_dois)
    paper_index = sorted_dois.index(paper.doi.lower()) + 1
    return f"{paper_index:02d}"


def paper_code(db: Database, paper: Paper) -> str:
    """
    Build the PDF filename code:
    journal source ID(3) + volume(4) + issue(3).
    """
    volume = numeric_code(paper.volume, 4)
    issue = numeric_code(paper.issue, 3)
    return f"{journal_code(db, paper.journal_id)}{volume}{issue}"


def journal_folder(db: Database, paper: Paper) -> str:
    """Build the top-level journal folder name."""
    journal: Optional[Journal] = db.get_journal(paper.journal_id) if paper.journal_id else None
    return sanitize_path_part(journal.title if journal else None, "Unknown Journal")


def build_pdf_path(db: Database, pdf_dir: Path, paper: Paper) -> Path:
    """Build the managed PDF path for a paper using the project code rule."""
    year = numeric_code(paper.published_year, 4)
    issue = numeric_code(paper.issue, 3)
    return (
        pdf_dir
        / journal_folder(db, paper)
        / year
        / issue
        / f"{paper_code(db, paper)}_{main_title(paper.title)}.pdf"
    )
