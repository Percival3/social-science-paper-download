"""
Command-line interface for Paper Harvester.
"""
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from tqdm import tqdm

from .config import load_config, validate_config, Config
from .database import Database
from .files import import_pdf_files, extract_text_from_pdfs
from .journals import (
    import_journals_from_directory,
    import_journals_from_file,
    list_journals,
    get_journal_stats,
)
from .crossref import CrossrefClient, discover_papers_for_journal, discover_papers_for_platform
from .scihub import SciHubClient, download_papers
from .reports import (
    generate_report,
    export_doi_map,
    retry_failed_downloads,
    verify_downloads,
    migrate_pdf_paths,
    cleanup_non_paper_downloads,
)


# =============================================================================
# Helper Functions
# =============================================================================

def get_db_and_config(ctx) -> tuple[Database, Config]:
    """Get database and config from context."""
    config = ctx.obj['config']
    db = Database(config.db_path)
    return db, config


# =============================================================================
# Main CLI Group
# =============================================================================

@click.group()
@click.option('--env-file', type=click.Path(exists=True), help='Path to .env file')
@click.pass_context
def cli(ctx, env_file):
    """Paper Harvester - Sci-Hub Edition
    
    Batch download academic papers from Sci-Hub for meta-analysis research.
    """
    # Ensure context object exists
    ctx.ensure_object(dict)
    
    # Load configuration
    config = load_config(env_file)
    errors = validate_config(config)
    
    if errors:
        click.echo("Configuration errors:", err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        click.echo("\nPlease check your .env file or create one from .env.example", err=True)
        sys.exit(1)
    
    # Ensure directories exist
    config.ensure_directories()
    
    ctx.obj['config'] = config


# =============================================================================
# Journals Commands
# =============================================================================

@cli.group()
def journals():
    """Manage journal list."""
    pass


@journals.command('import')
@click.option(
    '--input',
    '-i',
    'input_path',
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=True),
    help='Directory of Excel lists, or one .xlsx/.xls file',
)
@click.pass_context
def import_journals_cmd(ctx, input_path):
    """Import journals from Excel files (one file or a whole directory)."""
    db, config = get_db_and_config(ctx)
    input_path = Path(input_path)

    click.echo(f"Importing journals from: {input_path}")
    if input_path.is_file():
        if input_path.suffix.lower() not in ('.xlsx', '.xls'):
            click.echo("Error: --input must be a .xlsx or .xls file when importing a single file", err=True)
            sys.exit(1)
        stats = import_journals_from_file(db, input_path, verbose=True)
    elif input_path.is_dir():
        stats = import_journals_from_directory(db, input_path, verbose=True)
    else:
        click.echo("Error: --input is not a file or directory", err=True)
        sys.exit(1)
    
    click.echo(f"\n✓ Imported {stats['journals_imported']} new journal(s)")
    click.echo(f"✓ Updated {stats['journals_updated']} existing journal(s)")
    if stats['files_skipped'] > 0:
        click.echo(f"⚠ Skipped {stats['files_skipped']} file(s) with errors")


@journals.command('list')
@click.option('--platform', '-p', help='Filter by platform')
@click.option('--discipline', '-d', help='Filter by discipline')
@click.pass_context
def list_journals_cmd(ctx, platform, discipline):
    """List imported journals."""
    db, config = get_db_and_config(ctx)
    
    journal_list = list_journals(db, platform=platform, discipline=discipline)
    
    if not journal_list:
        click.echo("No journals found. Run 'journals import' first.")
        return
    
    # Get stats
    stats = get_journal_stats(db)
    click.echo(f"Total journals: {stats['total_journals']}\n")
    
    # Table header
    click.echo(f"{'ID':<30} {'Title':<50} {'Platform':<15} {'Discipline':<15} {'ISSN':<10}")
    click.echo("-" * 122)
    
    for journal in journal_list:
        title = (journal.title[:47] + '...') if len(journal.title) > 50 else journal.title
        discipline_value = journal.discipline or 'N/A'
        click.echo(
            f"{journal.journal_id:<30} {title:<50} "
            f"{journal.platform or 'N/A':<15} {discipline_value:<15} {journal.issn or 'N/A':<10}"
        )


# =============================================================================
# Discover Commands
# =============================================================================

@cli.command()
@click.option('--journal-id', '-j', help='Discover papers for specific journal')
@click.option('--platform', '-p', help='Discover papers for all journals on platform')
@click.option('--all', 'discover_all', is_flag=True, help='Discover for all journals')
@click.option('--from-year', type=int, required=True, help='Start year')
@click.option('--until-year', type=int, required=True, help='End year')
@click.option('--dry-run', is_flag=True, help='Count papers without saving')
@click.pass_context
def discover(ctx, journal_id, platform, discover_all, from_year, until_year, dry_run):
    """Discover papers via Crossref API."""
    db, config = get_db_and_config(ctx)
    client = CrossrefClient(config)
    
    if dry_run:
        click.echo("DRY RUN MODE - No data will be saved")
    
    if journal_id:
        # Single journal
        journal = db.get_journal(journal_id)
        if not journal:
            click.echo(f"Error: Journal not found: {journal_id}", err=True)
            sys.exit(1)
        
        click.echo(f"Discovering papers for: {journal.title} ({from_year}-{until_year})")
        
        stats = discover_papers_for_journal(
            db, client, journal_id, from_year, until_year, dry_run=dry_run
        )
        
        if dry_run:
            click.echo(f"\nEstimated papers to discover: {stats['found']}")
        else:
            click.echo(f"\n✓ Found: {stats['found']}")
            click.echo(f"✓ Imported: {stats['imported']}")
            click.echo(f"✓ Updated: {stats['updated']}")
            if stats['failed'] > 0:
                click.echo(f"⚠ Failed: {stats['failed']}")
    
    elif platform:
        # Platform
        click.echo(f"Discovering papers for platform: {platform} ({from_year}-{until_year})")
        
        results = discover_papers_for_platform(
            db, client, platform, from_year, until_year, dry_run=dry_run
        )
        
        click.echo(f"\nJournals processed: {results['journals_processed']}")
        if results['journals_skipped'] > 0:
            click.echo(f"Journals skipped: {results['journals_skipped']}")
        
        if dry_run:
            click.echo(f"Estimated total papers: {results['total_found']}")
        else:
            click.echo(f"✓ Total found: {results['total_found']}")
            click.echo(f"✓ Total imported: {results['total_imported']}")
    
    elif discover_all:
        # All journals
        click.echo(f"Discovering papers for ALL journals ({from_year}-{until_year})")
        click.echo("This may take a while...")
        
        journals_list = db.list_journals()
        total_found = 0
        total_imported = 0
        
        for journal in tqdm(journals_list, desc="Journals"):
            stats = discover_papers_for_journal(
                db, client, journal.journal_id, from_year, until_year, dry_run=dry_run
            )
            total_found += stats['found']
            total_imported += stats['imported']
        
        if dry_run:
            click.echo(f"\nEstimated total papers: {total_found}")
        else:
            click.echo(f"\n✓ Total found: {total_found}")
            click.echo(f"✓ Total imported: {total_imported}")
    
    else:
        click.echo("Error: Must specify --journal-id, --platform, or --all", err=True)
        sys.exit(1)


# =============================================================================
# Download Commands
# =============================================================================

@cli.command()
@click.option('--journal-id', '-j', help='Download papers for specific journal')
@click.option('--platform', '-p', help='Download papers for all journals on platform')
@click.option('--doi', 'single_doi', help='Download single DOI')
@click.option('--file', 'doi_file', type=click.Path(exists=True), help='File with DOI list (one per line)')
@click.option('--from-year', type=int, help='Start year (for journal/platform)')
@click.option('--until-year', type=int, help='End year (for journal/platform)')
@click.option('--limit', '-l', type=int, help='Limit number of downloads')
@click.option('--mirror', '-m', help='Use specific mirror URL')
@click.option('--force', is_flag=True, help='Re-download existing files')
@click.option('--official-only', is_flag=True, help='Use official source rules only; do not fall back to Sci-Hub')
@click.pass_context
def download(ctx, journal_id, platform, single_doi, doi_file, from_year, until_year, limit, mirror, force, official_only):
    """Download papers from Sci-Hub."""
    db, config = get_db_and_config(ctx)
    
    # Determine DOIs to download
    dois = []
    skip_year_after_failed_volume = False
    
    if single_doi:
        dois = [single_doi]
    
    elif doi_file:
        with open(doi_file, 'r') as f:
            dois = [line.strip() for line in f if line.strip()]
    
    elif journal_id:
        if not from_year or not until_year:
            click.echo("Error: --from-year and --until-year required for journal download", err=True)
            sys.exit(1)
        
        papers = db.list_papers(
            journal_id=journal_id,
            from_year=from_year,
            until_year=until_year,
            limit=limit,
        )
        dois = [p.doi for p in papers]
        skip_year_after_failed_volume = True
    
    elif platform:
        if not from_year or not until_year:
            click.echo("Error: --from-year and --until-year required for platform download", err=True)
            sys.exit(1)
        
        papers = db.list_papers(
            from_year=from_year,
            until_year=until_year,
        )
        # Filter by platform
        platform_journals = {j.journal_id for j in db.list_journals(platform=platform)}
        dois = [p.doi for p in papers if p.journal_id in platform_journals]
        
        if limit:
            dois = dois[:limit]
        skip_year_after_failed_volume = True
    
    else:
        click.echo("Error: Must specify --doi, --file, --journal-id, or --platform", err=True)
        sys.exit(1)
    
    if not dois:
        click.echo("No papers to download. Run 'discover' first or check filters.")
        return
    
    click.echo(f"Preparing to download {len(dois)} paper(s)...")
    
    # Initialize client
    client = SciHubClient(config, db)
    
    # Download with progress bar
    with tqdm(total=len(dois), desc="Downloading") as pbar:
        def progress_callback(current, total, doi, success, message=None):
            status = "✓" if success else "✗"
            msg = f" {message}" if message else ""
            pbar.set_description(f"{status} {doi[:40]}...{msg}")
            pbar.update(1)
        
        stats = download_papers(
            db, client, dois, config.pdf_dir,
            force=force,
            preferred_mirror=mirror,
            skip_year_after_failed_volume=skip_year_after_failed_volume,
            official_only=official_only,
            progress_callback=progress_callback,
        )
    
    click.echo(f"\n✓ Success: {stats['success']}")
    click.echo(f"✓ Skipped: {stats['skipped']}")
    if stats.get('skipped_unavailable_year'):
        click.echo(f"✓ Skipped by unavailable-year rule: {stats['skipped_unavailable_year']}")
    if stats.get('years_skipped_after_failed_volume'):
        click.echo(f"✓ Years skipped after failed volume: {stats['years_skipped_after_failed_volume']}")
    click.echo(f"✗ Failed: {stats['failed']}")


# =============================================================================
# Local PDF Commands
# =============================================================================

@cli.command('import-files')
@click.option('--input', '-i', 'input_dir', required=True, type=click.Path(exists=True, file_okay=False),
              help='Directory containing local PDF files')
@click.option('--match-by', default='doi', show_default=True, type=click.Choice(['doi']),
              help='How to match files to known papers')
@click.option('--journal-id', '-j', help='Only import PDFs matching this journal')
@click.option('--force', is_flag=True, help='Overwrite copied PDFs and add a fresh download record')
@click.pass_context
def import_files(ctx, input_dir, match_by, journal_id, force):
    """Import local PDF files into the managed PDF directory."""
    db, config = get_db_and_config(ctx)
    
    if match_by != 'doi':
        click.echo("Error: only --match-by doi is currently supported", err=True)
        sys.exit(1)
    
    stats = import_pdf_files(
        db=db,
        input_dir=Path(input_dir),
        pdf_dir=config.pdf_dir,
        journal_id=journal_id,
        force=force,
    )
    
    click.echo(f"PDF files found: {stats['found']}")
    click.echo(f"✓ Imported: {stats['imported']}")
    click.echo(f"✓ Skipped: {stats['skipped']}")
    click.echo(f"⚠ Unmatched: {stats['unmatched']}")
    click.echo(f"✗ Failed: {stats['failed']}")


@cli.command('extract-text')
@click.option('--input', '-i', 'input_dir', type=click.Path(exists=True, file_okay=False),
              help='PDF input directory (defaults to DATA_DIR/fulltext/pdf)')
@click.option('--output', '-o', 'output_dir',
              help='Text output directory (defaults to DATA_DIR/fulltext/txt)')
@click.option('--force', is_flag=True, help='Overwrite existing text files')
@click.pass_context
def extract_text(ctx, input_dir, output_dir, force):
    """Extract plain text from downloaded or imported PDFs."""
    _, config = get_db_and_config(ctx)
    
    input_path = Path(input_dir) if input_dir else config.pdf_dir
    output_path = Path(output_dir) if output_dir else config.txt_dir
    
    stats = extract_text_from_pdfs(input_path, output_path, force=force)
    
    click.echo(f"PDF files found: {stats['found']}")
    click.echo(f"✓ Extracted: {stats['extracted']}")
    click.echo(f"✓ Skipped: {stats['skipped']}")
    click.echo(f"✗ Failed: {stats['failed']}")


# =============================================================================
# Status and Queue Commands
# =============================================================================

@cli.command()
@click.option('--journal-id', '-j', help='Show status for specific journal')
@click.pass_context
def status(ctx, journal_id):
    """Show download status statistics."""
    db, config = get_db_and_config(ctx)
    
    if journal_id:
        journal = db.get_journal(journal_id)
        if journal:
            click.echo(f"Status for: {journal.title}\n")
    
    # Get stats
    paper_count = db.count_papers(journal_id=journal_id)
    download_stats = db.get_download_stats(journal_id=journal_id)
    
    click.echo(f"Total papers: {paper_count}")
    click.echo(f"Downloads recorded: {download_stats.get('total', 0)}")
    click.echo(f"  - Success: {download_stats.get('success', 0)}")
    click.echo(f"  - Failed: {download_stats.get('failed', 0)}")
    if download_stats.get('skipped', 0):
        click.echo(f"  - Skipped: {download_stats.get('skipped', 0)}")
    click.echo(f"  - Pending: {download_stats.get('pending', 0)}")
    
    # Calculate coverage
    if paper_count > 0 and download_stats.get('total', 0) > 0:
        coverage = download_stats.get('success', 0) / paper_count * 100
        click.echo(f"\nCoverage: {coverage:.1f}%")


@cli.command()
@click.option('--journal-id', '-j', help='Filter by journal')
@click.option('--status', 'filter_status', help='Filter by status (success/failed/pending)')
@click.option('--limit', '-l', type=int, default=50, help='Maximum results')
@click.pass_context
def queue(ctx, journal_id, filter_status, limit):
    """Show download queue and status."""
    db, config = get_db_and_config(ctx)
    
    downloads = db.list_downloads(
        status=filter_status,
        journal_id=journal_id,
        limit=limit,
    )
    
    if not downloads:
        click.echo("No downloads found.")
        return
    
    click.echo(f"{'DOI':<50} {'Status':<10} {'Mirror':<25} {'Date'}")
    click.echo("-" * 105)
    
    for download, paper in downloads:
        doi_short = download.doi[:47] + "..." if len(download.doi) > 50 else download.doi
        mirror_short = (download.mirror or 'N/A')[:22] + "..." if len(download.mirror or '') > 25 else (download.mirror or 'N/A')
        date = (download.completed_at or download.started_at or 'N/A')
        if isinstance(date, datetime):
            date = date.strftime('%Y-%m-%d')
        
        click.echo(f"{doi_short:<50} {download.status:<10} {mirror_short:<25} {date}")


@cli.command()
@click.argument('doi')
@click.pass_context
def show(ctx, doi):
    """Show details for a specific DOI."""
    db, config = get_db_and_config(ctx)
    
    paper = db.get_paper(doi)
    if not paper:
        click.echo(f"Paper not found: {doi}")
        return
    
    download = db.get_download_by_doi(doi)
    
    click.echo(f"DOI: {paper.doi}")
    click.echo(f"Title: {paper.title}")
    click.echo(f"Journal: {paper.journal_id}")
    click.echo(f"Year: {paper.published_year}")
    click.echo(f"\nAuthors:")
    for author in paper.authors[:5]:
        click.echo(f"  - {author.get('name', 'Unknown')}")
    if len(paper.authors) > 5:
        click.echo(f"  ... and {len(paper.authors) - 5} more")
    
    click.echo(f"\nDownload Status:")
    if download:
        click.echo(f"  Status: {download.status}")
        click.echo(f"  Mirror: {download.mirror or 'N/A'}")
        click.echo(f"  File: {download.file_path or 'N/A'}")
        click.echo(f"  Size: {download.file_size or 'N/A'}")
        click.echo(f"  SHA256: {download.sha256 or 'N/A'}")
        if download.error_message:
            click.echo(f"  Error: {download.error_message}")
    else:
        click.echo("  Not downloaded yet")


# =============================================================================
# Mirror Commands
# =============================================================================

@cli.command()
@click.pass_context
def check_mirrors(ctx):
    """Check Sci-Hub mirror availability."""
    db, config = get_db_and_config(ctx)
    client = SciHubClient(config, db)
    
    click.echo("Checking Sci-Hub mirrors...\n")
    
    results = client.check_all_mirrors()
    
    click.echo(f"{'Mirror':<35} {'Status':<10} {'Response Time':<15}")
    click.echo("-" * 60)
    
    for mirror_url, is_available, response_time, error in results:
        status = "✓ OK" if is_available else "✗ FAIL"
        time_str = f"{response_time}ms" if response_time else "N/A"
        click.echo(f"{mirror_url:<35} {status:<10} {time_str:<15}")
        if error:
            click.echo(f"  Error: {error}")


# =============================================================================
# Report Commands
# =============================================================================

@cli.command()
@click.option('--format', 'output_format', default='csv', type=click.Choice(['csv', 'markdown']),
              help='Output format')
@click.option('--output', '-o', 'output_path', required=True, help='Output file path')
@click.option('--by-platform', is_flag=True, help='Group by platform')
@click.option('--by-year', is_flag=True, help='Group by year')
@click.option('--by-journal', is_flag=True, help='Group by journal')
@click.option('--status', 'filter_status', help='Filter by status')
@click.pass_context
def report(ctx, output_format, output_path, by_platform, by_year, by_journal, filter_status):
    """Generate download report."""
    db, config = get_db_and_config(ctx)
    
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    stats = generate_report(
        db, output_format, output_file,
        by_platform=by_platform,
        by_year=by_year,
        by_journal=by_journal,
        status=filter_status,
    )
    
    click.echo(f"✓ Report saved to: {output_file}")
    click.echo(f"  Total papers: {stats['total']}")
    click.echo(f"  Successful: {stats['success']}")
    click.echo(f"  Failed: {stats['failed']}")


@cli.command()
@click.option('--output', '-o', required=True, help='Output JSON file path')
@click.pass_context
def export_map(ctx, output):
    """Export DOI to file mapping."""
    db, config = get_db_and_config(ctx)
    
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    count = export_doi_map(db, output_path)
    click.echo(f"✓ Exported {count} mappings to: {output}")


# =============================================================================
# Maintenance Commands
# =============================================================================

@cli.command()
@click.pass_context
def cleanup(ctx):
    """Clean up incomplete/corrupt downloads."""
    db, config = get_db_and_config(ctx)
    
    click.echo("Scanning for incomplete downloads...")
    
    # Find files without database entries or with failed status
    removed = 0
    for pdf_file in config.pdf_dir.rglob('*.pdf'):
        # Check if in database with success status
        # This is a simplified check
        if pdf_file.stat().st_size < 1024:
            click.echo(f"  Removing incomplete file: {pdf_file}")
            pdf_file.unlink()
            removed += 1
    
    click.echo(f"✓ Removed {removed} incomplete file(s)")


@cli.command('migrate-paths')
@click.option('--apply', 'apply_changes', is_flag=True, help='Apply file and database changes')
@click.option('--output', '-o', 'output_path', help='Output JSON manifest path')
@click.pass_context
def migrate_paths(ctx, apply_changes, output_path):
    """Migrate download records to the current collision-safe PDF path rule."""
    db, config = get_db_and_config(ctx)

    if output_path:
        manifest_path = Path(output_path)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest_path = config.manifests_dir / f"path_migration_{timestamp}.json"

    if apply_changes:
        click.echo("Applying PDF path migration...")
    else:
        click.echo("DRY RUN - no files or database rows will be changed")

    manifest = migrate_pdf_paths(
        db=db,
        data_dir=config.data_dir,
        pdf_dir=config.pdf_dir,
        manifest_path=manifest_path,
        apply=apply_changes,
    )

    summary = manifest["summary"]
    click.echo(f"✓ Manifest: {manifest_path}")
    click.echo(f"✓ Moved: {summary['moved']}")
    click.echo(f"✓ Unchanged: {summary['unchanged']}")
    click.echo(f"✓ Invalidated for redownload: {summary['invalidated']}")
    click.echo(f"⚠ Missing: {summary['missing']}")
    click.echo(f"⚠ Conflicts: {summary['conflict']}")
    click.echo(f"⚠ Unresolved: {summary['unresolved']}")


@cli.command('cleanup-non-papers')
@click.option('--apply', 'apply_changes', is_flag=True, help='Apply database changes and quarantine PDFs')
@click.option('--output', '-o', 'output_path', help='Output JSON manifest path')
@click.option(
    '--quarantine-dir',
    type=click.Path(file_okay=False),
    help='Directory for quarantined PDFs (defaults to DATA_DIR/fulltext/quarantine/non_papers)',
)
@click.pass_context
def cleanup_non_papers(ctx, apply_changes, output_path, quarantine_dir):
    """Mark current non-paper downloads as skipped and quarantine their PDFs."""
    db, config = get_db_and_config(ctx)

    if output_path:
        manifest_path = Path(output_path)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest_path = config.manifests_dir / f"non_paper_cleanup_{timestamp}.json"

    quarantine_path = (
        Path(quarantine_dir)
        if quarantine_dir
        else config.data_dir / "fulltext" / "quarantine" / "non_papers"
    )

    if apply_changes:
        click.echo("Applying non-paper cleanup...")
    else:
        click.echo("DRY RUN - no files or database rows will be changed")

    manifest = cleanup_non_paper_downloads(
        db=db,
        data_dir=config.data_dir,
        quarantine_dir=quarantine_path,
        manifest_path=manifest_path,
        apply=apply_changes,
    )

    summary = manifest["summary"]
    click.echo(f"✓ Manifest: {manifest_path}")
    click.echo(f"✓ Candidates: {summary['candidates']}")
    click.echo(f"✓ Marked skipped: {summary['marked_skipped']}")
    click.echo(f"✓ Quarantined: {summary['quarantined']}")
    click.echo(f"⚠ Missing/no path: {summary['missing']}")
    click.echo(f"⚠ Shared with downloadable: {summary['shared_with_downloadable']}")
    click.echo(f"⚠ Conflicts: {summary['conflict']}")
    click.echo(f"⚠ Unresolved: {summary['unresolved']}")


@cli.command()
@click.option('--limit', '-l', type=int, default=50, help='Maximum to retry')
@click.pass_context
def retry_failed(ctx, limit):
    """Retry failed downloads."""
    db, config = get_db_and_config(ctx)
    client = SciHubClient(config, db)
    
    click.echo(f"Retrying up to {limit} failed downloads...")
    
    stats = retry_failed_downloads(db, client, config.pdf_dir, limit=limit)
    
    click.echo(f"✓ Retried: {stats['retried']}")
    click.echo(f"✓ Now successful: {stats['now_success']}")
    if stats.get('skipped'):
        click.echo(f"✓ Skipped: {stats['skipped']}")
    click.echo(f"✗ Still failed: {stats['still_failed']}")


@cli.command()
@click.option('--apply', 'apply_changes', is_flag=True, help='Update stale database paths and compact current download state')
@click.option('--archive-dir', type=click.Path(exists=True, file_okay=False), help='Offline/archive PDF directory to reconcile moved files')
@click.pass_context
def verify(ctx, apply_changes, archive_dir):
    """Verify all downloaded files."""
    db, config = get_db_and_config(ctx)
    
    if apply_changes:
        click.echo("Verifying downloaded files and updating database state...")
    else:
        click.echo("Verifying downloaded files...")
    
    stats = verify_downloads(
        db,
        config.pdf_dir,
        apply=apply_changes,
        archive_dir=Path(archive_dir) if archive_dir else None,
    )
    
    click.echo(f"\n✓ Valid: {stats['valid']}")
    if stats.get('archive_found'):
        click.echo(f"✓ Found in archive: {stats['archive_found']}")
    if stats.get('paths_updated'):
        click.echo(f"✓ Paths updated: {stats['paths_updated']}")
    if stats.get('non_success_paths_cleared'):
        label = "cleared" if apply_changes else "to clear"
        click.echo(f"✓ Non-success file paths {label}: {stats['non_success_paths_cleared']}")
    if stats.get('historical_rows_removed'):
        label = "removed" if apply_changes else "to remove"
        click.echo(f"✓ Historical download rows {label}: {stats['historical_rows_removed']}")
    click.echo(f"✗ Corrupt: {stats['corrupt']}")
    click.echo(f"✗ Missing: {stats['missing']}")


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Entry point for CLI."""
    cli()


if __name__ == '__main__':
    main()
