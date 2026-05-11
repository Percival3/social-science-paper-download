"""
Reporting and maintenance utilities for Paper Harvester.
"""
import csv
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from tqdm import tqdm

from .database import Database, Download, Paper
from .paths import build_pdf_path
from .scihub import SciHubClient


FILE_EXISTS_SKIPPED = "File already exists (skipped)"
COLLISION_INVALIDATED_ERROR = "path collision invalidated; redownload required"


def _format_timestamp(value) -> str:
    """Format SQLite or datetime timestamp values for reports."""
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def generate_report(
    db: Database,
    output_format: str,
    output_file: Path,
    by_platform: bool = False,
    by_year: bool = False,
    by_journal: bool = False,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate download report.
    
    Args:
        db: Database instance
        output_format: 'csv' or 'markdown'
        output_file: Output file path
        by_platform: Group by platform
        by_year: Group by year
        by_journal: Group by journal
        status: Filter by status
        
    Returns:
        Statistics dictionary
    """
    # Get all downloads with paper info
    downloads = db.list_downloads(status=status, limit=100000)
    
    stats = {
        'total': len(downloads),
        'success': sum(1 for d, _ in downloads if d.status == 'success'),
        'failed': sum(1 for d, _ in downloads if d.status == 'failed'),
        'skipped': sum(1 for d, _ in downloads if d.status == 'skipped'),
        'pending': sum(1 for d, _ in downloads if d.status == 'pending'),
    }
    
    if output_format == 'csv':
        _generate_csv_report(downloads, output_file)
    elif output_format == 'markdown':
        _generate_markdown_report(db, downloads, output_file, by_platform, by_year, by_journal)
    
    return stats


def _generate_csv_report(downloads: List[tuple], output_file: Path) -> None:
    """Generate CSV report."""
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'DOI', 'Title', 'Journal', 'Year', 'Status',
            'Mirror', 'File Path', 'File Size', 'SHA256',
            'Downloaded At', 'Error'
        ])
        
        for download, paper in downloads:
            writer.writerow([
                paper.doi,
                paper.title or '',
                paper.journal_id or '',
                paper.published_year or '',
                download.status,
                download.mirror or '',
                download.file_path or '',
                download.file_size or '',
                download.sha256 or '',
                _format_timestamp(download.completed_at),
                download.error_message or '',
            ])


def _generate_markdown_report(
    db: Database,
    downloads: List[tuple],
    output_file: Path,
    by_platform: bool,
    by_year: bool,
    by_journal: bool,
) -> None:
    """Generate Markdown report."""
    lines = [
        "# Paper Harvester Download Report",
        f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "\n## Summary",
    ]
    
    # Overall stats
    total = len(downloads)
    success = sum(1 for d, _ in downloads if d.status == 'success')
    failed = sum(1 for d, _ in downloads if d.status == 'failed')
    skipped = sum(1 for d, _ in downloads if d.status == 'skipped')
    
    lines.extend([
        f"\n- **Total Downloads**: {total}",
        f"- **Successful**: {success} ({success/total*100:.1f}%)" if total else "- **Successful**: 0 (0.0%)",
        f"- **Failed**: {failed} ({failed/total*100:.1f}%)" if total else "- **Failed**: 0 (0.0%)",
        f"- **Skipped**: {skipped} ({skipped/total*100:.1f}%)" if total else "- **Skipped**: 0 (0.0%)",
    ])
    
    # By platform
    if by_platform:
        lines.append("\n## By Platform")
        lines.append("\n| Platform | Total | Success | Failed | Coverage |")
        lines.append("|----------|-------|---------|--------|----------|")
        
        platform_stats = {}
        for download, paper in downloads:
            # Get journal to find platform
            journal = db.get_journal(paper.journal_id) if paper.journal_id else None
            platform = journal.platform if journal else 'Unknown'
            
            if platform not in platform_stats:
                platform_stats[platform] = {'total': 0, 'success': 0, 'failed': 0}
            platform_stats[platform]['total'] += 1
            if download.status == 'success':
                platform_stats[platform]['success'] += 1
            elif download.status == 'failed':
                platform_stats[platform]['failed'] += 1
        
        for platform, stats in sorted(platform_stats.items(), key=lambda x: -x[1]['total']):
            coverage = stats['success'] / stats['total'] * 100 if stats['total'] > 0 else 0
            lines.append(f"| {platform} | {stats['total']} | {stats['success']} | {stats['failed']} | {coverage:.1f}% |")
    
    # By year
    if by_year:
        lines.append("\n## By Year")
        lines.append("\n| Year | Total | Success | Failed | Coverage |")
        lines.append("|------|-------|---------|--------|----------|")
        
        year_stats = {}
        for download, paper in downloads:
            year = paper.published_year or 'Unknown'
            if year not in year_stats:
                year_stats[year] = {'total': 0, 'success': 0, 'failed': 0}
            year_stats[year]['total'] += 1
            if download.status == 'success':
                year_stats[year]['success'] += 1
            elif download.status == 'failed':
                year_stats[year]['failed'] += 1
        
        for year in sorted(year_stats.keys(), reverse=True):
            stats = year_stats[year]
            coverage = stats['success'] / stats['total'] * 100 if stats['total'] > 0 else 0
            lines.append(f"| {year} | {stats['total']} | {stats['success']} | {stats['failed']} | {coverage:.1f}% |")
    
    # By journal
    if by_journal:
        lines.append("\n## By Journal")
        lines.append("\n| Journal | Total | Success | Failed | Coverage |")
        lines.append("|----------|-------|---------|--------|----------|")
        
        journal_stats = {}
        for download, paper in downloads:
            journal_id = paper.journal_id or 'Unknown'
            if journal_id not in journal_stats:
                journal_stats[journal_id] = {'total': 0, 'success': 0, 'failed': 0}
            journal_stats[journal_id]['total'] += 1
            if download.status == 'success':
                journal_stats[journal_id]['success'] += 1
            elif download.status == 'failed':
                journal_stats[journal_id]['failed'] += 1
        
        for journal_id, stats in sorted(journal_stats.items(), key=lambda x: -x[1]['total']):
            coverage = stats['success'] / stats['total'] * 100 if stats['total'] > 0 else 0
            lines.append(f"| {journal_id} | {stats['total']} | {stats['success']} | {stats['failed']} | {coverage:.1f}% |")
    
    # Failed downloads
    failed_downloads = [(d, p) for d, p in downloads if d.status == 'failed']
    if failed_downloads:
        lines.append("\n## Failed Downloads")
        lines.append(f"\nTotal failed: {len(failed_downloads)}\n")
        lines.append("\n| DOI | Journal | Error |")
        lines.append("|-----|---------|-------|")
        for download, paper in failed_downloads[:100]:  # Limit to first 100
            error = (download.error_message or 'Unknown')[:50]
            lines.append(f"| {paper.doi} | {paper.journal_id or 'N/A'} | {error} |")
        
        if len(failed_downloads) > 100:
            lines.append(f"\n... and {len(failed_downloads) - 100} more")
    
    # Write to file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def export_doi_map(db: Database, output_file: Path) -> int:
    """
    Export DOI to file path mapping.
    
    Args:
        db: Database instance
        output_file: Output JSON file path
        
    Returns:
        Number of mappings exported
    """
    downloads = db.list_downloads(status='success', limit=100000)
    
    mapping = {}
    for download, paper in downloads:
        if download.file_path:
            mapping[paper.doi] = {
                'file_path': download.file_path,
                'title': paper.title,
                'journal': paper.journal_id,
                'year': paper.published_year,
                'sha256': download.sha256,
            }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
    
    return len(mapping)


def _resolve_download_path(data_dir: Path, file_path: str) -> Path:
    """Resolve downloads.file_path against DATA_DIR when it is relative."""
    path = Path(file_path)
    return path if path.is_absolute() else data_dir / path


def _relative_to_data_dir(data_dir: Path, file_path: Path) -> str:
    """Return the storage path format used by downloads.file_path."""
    try:
        return str(file_path.relative_to(data_dir))
    except ValueError:
        return str(file_path)


def _calculate_file_sha256(file_path: Path) -> str:
    """Calculate SHA256 for a local file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _file_metadata(file_path: Path) -> tuple[int, str]:
    """Return file size and SHA256."""
    return file_path.stat().st_size, _calculate_file_sha256(file_path)


def _download_time_key(download: Download) -> tuple[str, int]:
    """Sort downloads by their recorded time, then ID."""
    timestamp = download.completed_at or download.started_at or ""
    return str(timestamp), download.download_id or 0


def _is_skipped_success(download: Download) -> bool:
    """Return whether a success row came from a path-only existing-file skip."""
    return download.error_message == FILE_EXISTS_SKIPPED


def _download_manifest_entry(download: Download, paper: Paper) -> Dict[str, Any]:
    """Serialize the fields needed to audit a path migration decision."""
    return {
        "download_id": download.download_id,
        "doi": download.doi,
        "title": paper.title,
        "journal_id": paper.journal_id,
        "year": paper.published_year,
        "file_path": download.file_path,
        "file_size": download.file_size,
        "sha256": download.sha256,
        "mirror": download.mirror,
        "error_message": download.error_message,
        "started_at": _format_timestamp(download.started_at),
        "completed_at": _format_timestamp(download.completed_at),
    }


def _choose_collision_owner(records: List[tuple[Download, Paper]]) -> Optional[tuple[Download, Paper]]:
    """Pick the one record most likely to own a shared historical PDF path."""
    non_skipped = [
        (download, paper)
        for download, paper in records
        if not _is_skipped_success(download)
    ]
    if not non_skipped:
        return None

    return min(non_skipped, key=lambda item: _download_time_key(item[0]))


def _append_manifest(manifest: Dict[str, Any], key: str, entry: Dict[str, Any]) -> None:
    """Append a manifest entry and keep summary counts in sync."""
    manifest[key].append(entry)
    manifest["summary"][key] += 1


def _update_success_records_for_path(
    db: Database,
    doi: str,
    old_file_path: str,
    new_file_path: str,
    file_size: int,
    sha256: str,
    apply: bool,
) -> List[int]:
    """Update successful rows for one DOI that still point at the old path."""
    updated_ids: List[int] = []
    for record in db.list_downloads_for_doi(doi, status="success"):
        if record.file_path != old_file_path:
            continue

        updated_ids.append(record.download_id)
        if apply:
            record.file_path = new_file_path
            record.file_size = file_size
            record.sha256 = sha256
            db.update_download(record)

    return updated_ids


def _invalidate_success_records_for_path(
    db: Database,
    doi: str,
    old_file_path: str,
    apply: bool,
) -> List[int]:
    """Mark path-collision success rows as failed so they can be redownloaded."""
    invalidated_ids: List[int] = []
    for record in db.list_downloads_for_doi(doi, status="success"):
        if record.file_path != old_file_path:
            continue

        invalidated_ids.append(record.download_id)
        if apply:
            record.status = "failed"
            record.file_path = None
            record.file_size = None
            record.sha256 = None
            record.completed_at = None
            record.error_message = COLLISION_INVALIDATED_ERROR
            db.update_download(record)

    return invalidated_ids


def migrate_pdf_paths(
    db: Database,
    data_dir: Path,
    pdf_dir: Path,
    manifest_path: Path,
    apply: bool = False,
) -> Dict[str, Any]:
    """
    Migrate successful download records to the current collision-safe path rule.

    Dry-run mode writes the same manifest without moving files or updating rows.
    """
    manifest: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry-run",
        "data_dir": str(data_dir),
        "pdf_dir": str(pdf_dir),
        "summary": {
            "moved": 0,
            "unchanged": 0,
            "invalidated": 0,
            "missing": 0,
            "conflict": 0,
            "unresolved": 0,
        },
        "moved": [],
        "unchanged": [],
        "invalidated": [],
        "missing": [],
        "conflict": [],
        "unresolved": [],
    }

    grouped_downloads = db.list_latest_success_downloads_by_file_path()

    for old_file_path, downloads in sorted(grouped_downloads.items()):
        records: List[tuple[Download, Paper]] = []
        for download in downloads:
            if not download.doi:
                _append_manifest(
                    manifest,
                    "unresolved",
                    {
                        "reason": "Download record has no DOI",
                        "download": _download_manifest_entry(download, Paper(doi="")),
                    },
                )
                continue

            paper = db.get_paper(download.doi) or Paper(doi=download.doi)
            records.append((download, paper))

        if not records:
            continue

        source_path = _resolve_download_path(data_dir, old_file_path)
        if not source_path.exists() or not source_path.is_file():
            for download, paper in records:
                _append_manifest(
                    manifest,
                    "missing",
                    {
                        "reason": "Source PDF is missing",
                        "source_path": str(source_path),
                        "download": _download_manifest_entry(download, paper),
                    },
                )
            continue

        owner = records[0] if len(records) == 1 else _choose_collision_owner(records)
        if owner is None:
            _append_manifest(
                manifest,
                "unresolved",
                {
                    "reason": "Shared path has no non-skipped success record to infer ownership",
                    "source_path": str(source_path),
                    "downloads": [
                        _download_manifest_entry(download, paper)
                        for download, paper in records
                    ],
                },
            )
            continue

        owner_download, owner_paper = owner
        target_path = build_pdf_path(db, pdf_dir, owner_paper)
        target_file_path = _relative_to_data_dir(data_dir, target_path)
        source_size, source_sha256 = _file_metadata(source_path)
        source_matches_target = source_path.resolve() == target_path.resolve()

        if not source_matches_target and target_path.exists():
            target_size, target_sha256 = _file_metadata(target_path)
            if target_sha256.lower() != source_sha256.lower():
                _append_manifest(
                    manifest,
                    "conflict",
                    {
                        "reason": "Target path already exists with different SHA256",
                        "source_path": str(source_path),
                        "target_path": str(target_path),
                        "source_size": source_size,
                        "source_sha256": source_sha256,
                        "target_size": target_size,
                        "target_sha256": target_sha256,
                        "owner": _download_manifest_entry(owner_download, owner_paper),
                        "downloads": [
                            _download_manifest_entry(download, paper)
                            for download, paper in records
                        ],
                    },
                )
                continue

        try:
            copied = False
            removed_source = False
            moved_entry: Optional[Dict[str, Any]] = None

            if not source_matches_target:
                if apply and not target_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target_path)
                    copied = True

                if apply:
                    target_size, target_sha256 = _file_metadata(target_path)
                    if target_sha256.lower() != source_sha256.lower():
                        _append_manifest(
                            manifest,
                            "conflict",
                            {
                                "reason": "Copied target SHA256 does not match source",
                                "source_path": str(source_path),
                                "target_path": str(target_path),
                                "source_sha256": source_sha256,
                                "target_sha256": target_sha256,
                                "owner": _download_manifest_entry(owner_download, owner_paper),
                            },
                        )
                        continue
                else:
                    target_size = source_size
                    target_sha256 = source_sha256

                updated_ids = _update_success_records_for_path(
                    db,
                    owner_paper.doi,
                    old_file_path,
                    target_file_path,
                    target_size,
                    target_sha256,
                    apply,
                )

                moved_entry = {
                    "source_path": str(source_path),
                    "target_path": str(target_path),
                    "old_file_path": old_file_path,
                    "new_file_path": target_file_path,
                    "copied": copied,
                    "removed_source": removed_source,
                    "updated_download_ids": updated_ids,
                    "owner": _download_manifest_entry(owner_download, owner_paper),
                }
                _append_manifest(manifest, "moved", moved_entry)
            else:
                _append_manifest(
                    manifest,
                    "unchanged",
                    {
                        "path": str(source_path),
                        "file_path": old_file_path,
                        "owner": _download_manifest_entry(owner_download, owner_paper),
                    },
                )

            for download, paper in records:
                if download.download_id == owner_download.download_id:
                    continue

                invalidated_ids = _invalidate_success_records_for_path(
                    db,
                    paper.doi,
                    old_file_path,
                    apply,
                )
                _append_manifest(
                    manifest,
                    "invalidated",
                    {
                        "reason": COLLISION_INVALIDATED_ERROR,
                        "old_file_path": old_file_path,
                        "invalidated_download_ids": invalidated_ids,
                        "download": _download_manifest_entry(download, paper),
                    },
                )

            if (
                apply
                and moved_entry is not None
                and not db.list_downloads_by_file_path(old_file_path)
            ):
                source_path.unlink(missing_ok=True)
                moved_entry["removed_source"] = True
        except Exception as exc:  # noqa: BLE001 - keep migration resumable per path group
            _append_manifest(
                manifest,
                "unresolved",
                {
                    "reason": str(exc),
                    "source_path": str(source_path),
                    "downloads": [
                        _download_manifest_entry(download, paper)
                        for download, paper in records
                    ],
                },
            )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


def retry_failed_downloads(
    db: Database,
    client: SciHubClient,
    output_dir: Path,
    limit: int = 50,
) -> Dict[str, int]:
    """
    Retry failed downloads.
    
    Args:
        db: Database instance
        client: SciHub client
        output_dir: Output directory
        limit: Maximum number to retry
        
    Returns:
        Statistics dictionary
    """
    # Get failed downloads
    downloads = db.list_downloads(status='failed', limit=limit)
    
    stats = {
        'retried': len(downloads),
        'now_success': 0,
        'still_failed': 0,
    }
    
    for download, paper in tqdm(downloads, desc="Retrying"):
        output_path = build_pdf_path(db, output_dir, paper)
        
        # Retry download
        result = client.download(paper.doi, output_path, force=True)
        
        # Update database
        download.status = 'success' if result.success else 'failed'
        download.file_path = str(output_path.relative_to(client.config.data_dir)) if result.success and output_path.exists() else None
        download.file_size = result.file_size
        download.sha256 = result.sha256
        download.mirror = result.mirror
        download.completed_at = datetime.now() if result.success else None
        download.error_message = result.error_message
        download.response_time_ms = result.response_time_ms
        
        db.update_download(download)
        
        if result.success:
            stats['now_success'] += 1
        else:
            stats['still_failed'] += 1
    
    return stats


def verify_downloads(db: Database, pdf_dir: Path) -> Dict[str, int]:
    """
    Verify downloaded files against database records.
    
    Args:
        db: Database instance
        pdf_dir: PDF directory
        
    Returns:
        Statistics dictionary
    """
    downloads = db.list_downloads(status='success', limit=100000)
    
    stats = {
        'valid': 0,
        'corrupt': 0,
        'missing': 0,
    }
    
    for download, paper in tqdm(downloads, desc="Verifying"):
        if not download.file_path:
            continue
        
        file_path = pdf_dir.parent.parent / download.file_path
        
        if not file_path.exists():
            stats['missing'] += 1
            continue
        
        # Check file size
        file_size = file_path.stat().st_size
        if file_size != download.file_size:
            stats['corrupt'] += 1
            continue
        
        # Verify SHA256 if available
        if download.sha256:
            sha256_hash = hashlib.sha256()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256_hash.update(chunk)
            
            if sha256_hash.hexdigest() != download.sha256:
                stats['corrupt'] += 1
                continue
        
        # Check PDF header
        with open(file_path, 'rb') as f:
            header = f.read(4)
            if header != b'%PDF':
                stats['corrupt'] += 1
                continue
        
        stats['valid'] += 1
    
    return stats
