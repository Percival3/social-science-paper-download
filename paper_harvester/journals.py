"""
Journal import and management for Paper Harvester.
"""
import re
from pathlib import Path
from typing import List, Optional, Dict, Any
import pandas as pd

from .database import Database, Journal


# Column name mappings for common variations
COLUMN_MAPPINGS = {
    'source_id': ['id', 'ID', '编号', '序号', 'no', 'number'],
    'journal': ['journal', 'title', '期刊', '期刊名', '期刊名称', 'journal_title', 'journal name'],
    'platform': ['platform', '平台', 'database', 'db', 'source'],
    'publisher': ['publisher', '出版社', '出版商', '出版机构'],
    'issn': ['issn', 'ISSN', 'print_issn', 'pissn'],
    'eissn': ['eissn', 'eISSN', 'online_issn', 'electronic_issn'],
    'discipline': ['discipline', 'subject', 'field', 'category', '学科', '分类'],
}


def normalize_column_name(col: str) -> Optional[str]:
    """
    Normalize a column name to standard field name.
    
    Args:
        col: Original column name
        
    Returns:
        Standard field name or None if not recognized
    """
    col_lower = str(col).strip().lower().replace(' ', '_')
    
    for standard, variations in COLUMN_MAPPINGS.items():
        if col_lower in [v.lower().replace(' ', '_') for v in variations]:
            return standard
    
    return None


def generate_journal_id(title: str) -> str:
    """
    Generate a normalized journal_id from title.
    
    Args:
        title: Journal title
        
    Returns:
        Normalized ID (lowercase, spaces to underscores, special chars removed)
    """
    # Convert to lowercase
    journal_id = title.lower()
    # Replace spaces and hyphens with underscores
    journal_id = re.sub(r'[\s\-]+', '_', journal_id)
    # Remove special characters except underscores
    journal_id = re.sub(r'[^a-z0-9_]', '', journal_id)
    # Remove consecutive underscores
    journal_id = re.sub(r'_+', '_', journal_id)
    # Strip leading/trailing underscores
    journal_id = journal_id.strip('_')
    
    return journal_id


def infer_platform_from_filename(file_path: Path) -> Optional[str]:
    """Infer a platform label from files such as wiley_journals_50.xlsx."""
    stem = file_path.stem.lower()
    if stem.startswith("期刊列表"):
        return None
    stem = re.sub(r'_journals?(?:_\d+)?$', '', stem)
    stem = re.sub(r'_\d+$', '', stem)
    return stem or None


def clean_cell_value(value: Any) -> str:
    """Convert an Excel cell value to a trimmed string."""
    if pd.isna(value):
        return ''
    return str(value).strip()


def clean_source_id(value: Any) -> Optional[int]:
    """Convert an Excel journal ID to an integer when present."""
    if pd.isna(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return int(float(text))
    except ValueError:
        match = re.search(r"\d+", text)
        return int(match.group(0)) if match else None


def read_excel_journals(file_path: Path) -> List[Dict[str, Any]]:
    """
    Read journals from Excel file with flexible column name handling.
    
    Args:
        file_path: Path to Excel file
        
    Returns:
        List of journal dictionaries
    """
    # Read all sheets
    xl = pd.ExcelFile(file_path)
    inferred_platform = infer_platform_from_filename(file_path)
    
    all_journals = []
    
    for sheet_name in xl.sheet_names:
        df = pd.read_excel(file_path, sheet_name=sheet_name)
        
        # Normalize column names
        column_map = {}
        raw_columns = list(df.columns)
        
        for col in df.columns:
            normalized = normalize_column_name(col)
            if normalized:
                column_map[col] = normalized
        
        # Rename columns
        df = df.rename(columns=column_map)
        
        # Convert to list of dicts
        for _, row in df.iterrows():
            journal_data = {
                'raw_columns': str(raw_columns),
                'source_file': file_path.name,
            }
            
            # Extract known fields
            if 'source_id' in df.columns and pd.notna(row.get('source_id')):
                journal_data['source_id'] = clean_source_id(row['source_id'])

            if 'journal' in df.columns:
                journal_data['title'] = clean_cell_value(row['journal'])
            elif 'title' in df.columns:
                journal_data['title'] = clean_cell_value(row['title'])
            
            if 'platform' in df.columns and pd.notna(row.get('platform')):
                journal_data['platform'] = clean_cell_value(row['platform'])
            elif inferred_platform:
                journal_data['platform'] = inferred_platform
            
            if 'publisher' in df.columns and pd.notna(row.get('publisher')):
                journal_data['publisher'] = clean_cell_value(row['publisher'])
            
            if 'issn' in df.columns and pd.notna(row.get('issn')):
                journal_data['issn'] = clean_cell_value(row['issn'])
            
            if 'eissn' in df.columns and pd.notna(row.get('eissn')):
                journal_data['eissn'] = clean_cell_value(row['eissn'])
            
            if 'discipline' in df.columns and pd.notna(row.get('discipline')):
                journal_data['discipline'] = clean_cell_value(row['discipline'])
            
            # Only add if we have at least a title
            if journal_data.get('title'):
                journal_data['journal_id'] = generate_journal_id(journal_data['title'])
                all_journals.append(journal_data)
    
    return all_journals


def import_journals_from_file(
    db: Database,
    file_path: Path,
    verbose: bool = False,
) -> Dict[str, int]:
    """
    Import journals from a single Excel file (.xlsx or .xls).

    Same row semantics as import_journals_from_directory; returns the same stats keys.
    """
    stats = {
        'files_processed': 0,
        'files_skipped': 0,
        'journals_imported': 0,
        'journals_updated': 0,
    }
    suffix = file_path.suffix.lower()
    if suffix not in ('.xlsx', '.xls'):
        raise ValueError(f"Unsupported file type (expected .xlsx or .xls): {file_path}")

    if verbose:
        print(f"Processing: {file_path.name}")

    try:
        journals_data = read_excel_journals(file_path)

        if verbose:
            print(f"  Found {len(journals_data)} journal(s)")

        imported_count = 0
        for journal_data in journals_data:
            existing = db.get_journal(journal_data['journal_id'])

            journal = Journal(
                journal_id=journal_data['journal_id'],
                title=journal_data['title'],
                source_id=journal_data.get('source_id') or (existing.source_id if existing else None),
                platform=journal_data.get('platform') or (existing.platform if existing else None),
                publisher=journal_data.get('publisher') or (existing.publisher if existing else None),
                issn=journal_data.get('issn') or (existing.issn if existing else None),
                eissn=journal_data.get('eissn') or (existing.eissn if existing else None),
                discipline=journal_data.get('discipline') or (existing.discipline if existing else None),
                source_file=journal_data.get('source_file'),
                raw_columns=journal_data.get('raw_columns'),
            )

            db.insert_journal(journal)

            if existing:
                stats['journals_updated'] += 1
            else:
                stats['journals_imported'] += 1
                imported_count += 1

        if verbose:
            print(f"  Imported: {imported_count}")

        stats['files_processed'] = 1

    except Exception as e:
        if verbose:
            print(f"  Error: {e}")
        stats['files_skipped'] = 1
        db.insert_log(
            action='import_journals',
            status='fail',
            message=f"Failed to process {file_path.name}: {str(e)}",
        )

    return stats


def import_journals_from_directory(
    db: Database,
    input_dir: Path,
    verbose: bool = False,
) -> Dict[str, int]:
    """
    Import all journal Excel files from a directory.
    
    Args:
        db: Database instance
        input_dir: Directory containing Excel files
        verbose: Whether to print progress
        
    Returns:
        Statistics dict with counts
    """
    stats = {
        'files_processed': 0,
        'files_skipped': 0,
        'journals_imported': 0,
        'journals_updated': 0,
    }
    
    # Find all Excel files
    excel_files = list(input_dir.glob('*.xlsx')) + list(input_dir.glob('*.xls'))
    
    if verbose:
        print(f"Found {len(excel_files)} Excel file(s) in {input_dir}")
    
    for file_path in excel_files:
        if verbose:
            print(f"\nProcessing: {file_path.name}")
        
        try:
            journals_data = read_excel_journals(file_path)
            
            if verbose:
                print(f"  Found {len(journals_data)} journal(s)")
            
            imported_count = 0
            for journal_data in journals_data:
                # Check if journal already exists
                existing = db.get_journal(journal_data['journal_id'])
                
                journal = Journal(
                    journal_id=journal_data['journal_id'],
                    title=journal_data['title'],
                    source_id=journal_data.get('source_id') or (existing.source_id if existing else None),
                    platform=journal_data.get('platform') or (existing.platform if existing else None),
                    publisher=journal_data.get('publisher') or (existing.publisher if existing else None),
                    issn=journal_data.get('issn') or (existing.issn if existing else None),
                    eissn=journal_data.get('eissn') or (existing.eissn if existing else None),
                    discipline=journal_data.get('discipline') or (existing.discipline if existing else None),
                    source_file=journal_data.get('source_file'),
                    raw_columns=journal_data.get('raw_columns'),
                )
                
                db.insert_journal(journal)
                
                if existing:
                    stats['journals_updated'] += 1
                else:
                    stats['journals_imported'] += 1
                    imported_count += 1
            
            if verbose:
                print(f"  Imported: {imported_count}")
            
            stats['files_processed'] += 1
            
        except Exception as e:
            if verbose:
                print(f"  Error: {e}")
            stats['files_skipped'] += 1
            db.insert_log(
                action='import_journals',
                status='fail',
                message=f"Failed to process {file_path.name}: {str(e)}"
            )
    
    return stats


def list_journals(
    db: Database,
    platform: Optional[str] = None,
    discipline: Optional[str] = None,
) -> List[Journal]:
    """
    List all journals from database.
    
    Args:
        db: Database instance
        platform: Optional platform filter
        discipline: Optional discipline filter
        
    Returns:
        List of Journal objects
    """
    return db.list_journals(platform=platform, discipline=discipline)


def get_journal_stats(db: Database) -> Dict[str, Any]:
    """
    Get statistics about journals in database.
    
    Args:
        db: Database instance
        
    Returns:
        Statistics dictionary
    """
    total = db.count_journals()
    
    with db._get_connection() as conn:
        # Platform distribution
        platform_rows = conn.execute(
            "SELECT platform, COUNT(*) as count FROM journals GROUP BY platform ORDER BY count DESC"
        ).fetchall()
        platforms = {row[0] or 'Unknown': row[1] for row in platform_rows}
        
        # Publisher distribution
        publisher_rows = conn.execute(
            "SELECT publisher, COUNT(*) as count FROM journals GROUP BY publisher ORDER BY count DESC"
        ).fetchall()
        publishers = {row[0] or 'Unknown': row[1] for row in publisher_rows}
    
    return {
        'total_journals': total,
        'platforms': platforms,
        'publishers': publishers,
    }
