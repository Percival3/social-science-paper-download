"""
Local PDF import and text extraction utilities.
"""
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import fitz

from .database import Database, Download, Paper
from .paths import build_pdf_path


def safe_doi_filename(doi: str) -> str:
    """Convert a DOI into the same safe filename used by the downloader."""
    return doi.replace("/", "_").replace("\\", "_")


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 for a local file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _paper_lookup_by_safe_filename(db: Database) -> Dict[str, Paper]:
    """Build a lookup from downloader-style PDF stem to known paper metadata."""
    papers = db.list_papers(limit=1_000_000)
    return {safe_doi_filename(paper.doi).lower(): paper for paper in papers}


def _relative_to_data_dir(file_path: Path, pdf_dir: Path) -> str:
    """Return the path format used in downloads.file_path."""
    data_dir = pdf_dir.parent.parent
    return str(file_path.relative_to(data_dir))


def import_pdf_files(
    db: Database,
    input_dir: Path,
    pdf_dir: Path,
    journal_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, int]:
    """
    Import local PDF files into the managed PDF directory.

    Files are matched by DOI-style filename, using the same convention as the
    downloader: DOI slashes are replaced with underscores.
    """
    stats = {
        "found": 0,
        "imported": 0,
        "skipped": 0,
        "unmatched": 0,
        "failed": 0,
    }
    known_papers = _paper_lookup_by_safe_filename(db)

    for source_path in input_dir.rglob("*.pdf"):
        stats["found"] += 1
        paper = known_papers.get(source_path.stem.lower())

        if not paper:
            stats["unmatched"] += 1
            continue

        if journal_id and paper.journal_id != journal_id:
            stats["skipped"] += 1
            continue

        target_path = build_pdf_path(db, pdf_dir, paper)
        existing = db.get_download_by_doi(paper.doi)

        if target_path.exists() and not force and existing and existing.status == "success":
            stats["skipped"] += 1
            continue

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            should_copy = force or not target_path.exists()
            if should_copy and source_path.resolve() != target_path.resolve():
                shutil.copy2(source_path, target_path)

            now = datetime.now()
            db.insert_download(
                Download(
                    doi=paper.doi,
                    file_path=_relative_to_data_dir(target_path, pdf_dir),
                    file_size=target_path.stat().st_size,
                    sha256=calculate_sha256(target_path),
                    mirror="local-file",
                    status="success",
                    attempts=1,
                    started_at=now,
                    completed_at=now,
                )
            )
            stats["imported"] += 1
        except Exception:
            stats["failed"] += 1

    return stats


def extract_text_from_pdfs(
    input_dir: Path,
    output_dir: Path,
    force: bool = False,
) -> Dict[str, int]:
    """Extract plain text from PDFs into a parallel .txt directory tree."""
    stats = {
        "found": 0,
        "extracted": 0,
        "skipped": 0,
        "failed": 0,
    }

    for pdf_path in input_dir.rglob("*.pdf"):
        stats["found"] += 1
        relative_path = pdf_path.relative_to(input_dir).with_suffix(".txt")
        output_path = output_dir / relative_path

        if output_path.exists() and not force:
            stats["skipped"] += 1
            continue

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with fitz.open(str(pdf_path)) as doc:
                text = "\n".join(page.get_text() for page in doc)
            output_path.write_text(text, encoding="utf-8")
            stats["extracted"] += 1
        except Exception:
            stats["failed"] += 1

    return stats
