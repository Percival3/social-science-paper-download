"""
Database models and operations for Paper Harvester.
"""
import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple


# =============================================================================
# Schema Definition
# =============================================================================

SCHEMA_SQL = """
-- Journals table
CREATE TABLE IF NOT EXISTS journals (
    journal_id TEXT PRIMARY KEY,
    source_id INTEGER,
    title TEXT NOT NULL,
    platform TEXT,
    publisher TEXT,
    issn TEXT,
    eissn TEXT,
    discipline TEXT,
    source_file TEXT,
    raw_columns TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Papers table (metadata from Crossref)
CREATE TABLE IF NOT EXISTS papers (
    doi TEXT PRIMARY KEY,
    title TEXT,
    journal_id TEXT REFERENCES journals(journal_id),
    published_year INTEGER,
    published_date TEXT,
    authors TEXT,  -- JSON array
    volume TEXT,
    issue TEXT,
    pages TEXT,
    abstract TEXT,
    keywords TEXT,  -- JSON array
    crossref_raw TEXT,  -- Full JSON response
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Downloads table (tracks actual PDF downloads)
CREATE TABLE IF NOT EXISTS downloads (
    download_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT REFERENCES papers(doi),
    file_path TEXT,
    file_size INTEGER,
    sha256 TEXT,
    mirror TEXT,
    scihub_url TEXT,
    pdf_url TEXT,
    status TEXT NOT NULL,  -- success/failed/pending
    http_status INTEGER,
    error_message TEXT,
    attempts INTEGER DEFAULT 0,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    response_time_ms INTEGER
);

-- Mirrors table (tracks mirror health)
CREATE TABLE IF NOT EXISTS mirrors (
    mirror_url TEXT PRIMARY KEY,
    status TEXT DEFAULT 'active',  -- active/inactive/cooldown
    last_checked TIMESTAMP,
    response_time_ms INTEGER,
    fail_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    cooldown_until TIMESTAMP
);

-- Logs table (detailed operation logs)
CREATE TABLE IF NOT EXISTS logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    doi TEXT,
    mirror TEXT,
    action TEXT NOT NULL,  -- discover/download/check/etc
    status TEXT NOT NULL,  -- success/fail/retry
    message TEXT,
    http_status INTEGER,
    response_time_ms INTEGER
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_papers_journal_id ON papers(journal_id);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(published_year);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(doi);  -- Join with downloads
CREATE INDEX IF NOT EXISTS idx_downloads_doi ON downloads(doi);
CREATE INDEX IF NOT EXISTS idx_downloads_file_path ON downloads(file_path);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_logs_doi ON logs(doi);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
"""


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Journal:
    """Journal record."""
    journal_id: str
    title: str
    source_id: Optional[int] = None
    platform: Optional[str] = None
    publisher: Optional[str] = None
    issn: Optional[str] = None
    eissn: Optional[str] = None
    discipline: Optional[str] = None
    source_file: Optional[str] = None
    raw_columns: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class Paper:
    """Paper metadata record."""
    doi: str
    title: Optional[str] = None
    journal_id: Optional[str] = None
    published_year: Optional[int] = None
    published_date: Optional[str] = None
    authors: List[Dict] = field(default_factory=list)
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    abstract: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    crossref_raw: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Download:
    """Download record."""
    download_id: Optional[int] = None
    doi: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    sha256: Optional[str] = None
    mirror: Optional[str] = None
    scihub_url: Optional[str] = None
    pdf_url: Optional[str] = None
    status: str = "pending"
    http_status: Optional[int] = None
    error_message: Optional[str] = None
    attempts: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    response_time_ms: Optional[int] = None


@dataclass
class Mirror:
    """Mirror health record."""
    mirror_url: str
    status: str = "active"
    last_checked: Optional[datetime] = None
    response_time_ms: Optional[int] = None
    fail_count: int = 0
    success_count: int = 0
    cooldown_until: Optional[datetime] = None


# =============================================================================
# Database Manager
# =============================================================================

class Database:
    """SQLite database manager for Paper Harvester."""
    
    def __init__(self, db_path: Path):
        """
        Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self) -> None:
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.executescript(SCHEMA_SQL)
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply lightweight schema migrations for existing local databases."""
        journal_columns = conn.execute("PRAGMA table_info(journals)").fetchall()
        if not any(col[1] == "source_id" for col in journal_columns):
            conn.execute("ALTER TABLE journals ADD COLUMN source_id INTEGER")

        columns = conn.execute("PRAGMA table_info(downloads)").fetchall()
        file_path_col = next((col for col in columns if col[1] == "file_path"), None)

        if file_path_col and file_path_col[3] == 1:
            conn.execute("ALTER TABLE downloads RENAME TO downloads_old")
            conn.execute("""
                CREATE TABLE downloads (
                    download_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi TEXT REFERENCES papers(doi),
                    file_path TEXT,
                    file_size INTEGER,
                    sha256 TEXT,
                    mirror TEXT,
                    scihub_url TEXT,
                    pdf_url TEXT,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    error_message TEXT,
                    attempts INTEGER DEFAULT 0,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    response_time_ms INTEGER
                )
            """)
            conn.execute("""
                INSERT INTO downloads
                (download_id, doi, file_path, file_size, sha256, mirror, scihub_url,
                 pdf_url, status, http_status, error_message, attempts, started_at,
                 completed_at, response_time_ms)
                SELECT download_id, doi, file_path, file_size, sha256, mirror,
                       scihub_url, pdf_url, status, http_status, error_message,
                       attempts, started_at, completed_at, response_time_ms
                FROM downloads_old
            """)
            conn.execute("DROP TABLE downloads_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_doi ON downloads(doi)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_file_path ON downloads(file_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)")
    
    @contextmanager
    def _get_connection(self):
        """Get database connection context manager."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _download_from_row(row: sqlite3.Row) -> Download:
        """Build a Download dataclass from a SQLite row."""
        data = dict(row)
        data.pop("row_number", None)
        return Download(**data)
    
    # =========================================================================
    # Journal Operations
    # =========================================================================
    
    def insert_journal(self, journal: Journal) -> None:
        """Insert or replace a journal record."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO journals
                (journal_id, source_id, title, platform, publisher, issn, eissn, discipline, source_file, raw_columns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                journal.journal_id,
                journal.source_id,
                journal.title,
                journal.platform,
                journal.publisher,
                journal.issn,
                journal.eissn,
                journal.discipline,
                journal.source_file,
                journal.raw_columns,
            ))
    
    def get_journal(self, journal_id: str) -> Optional[Journal]:
        """Get a journal by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM journals WHERE journal_id = ?",
                (journal_id,)
            ).fetchone()
            if row:
                return Journal(**dict(row))
            return None
    
    def list_journals(
        self,
        platform: Optional[str] = None,
        discipline: Optional[str] = None,
    ) -> List[Journal]:
        """List all journals, optionally filtered by platform or discipline."""
        query = "SELECT * FROM journals WHERE 1=1"
        params = []
        
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        
        if discipline:
            query += " AND discipline = ?"
            params.append(discipline)
        
        query += " ORDER BY title"
        
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [Journal(**dict(row)) for row in rows]
    
    def count_journals(self) -> int:
        """Count total journals."""
        with self._get_connection() as conn:
            result = conn.execute("SELECT COUNT(*) FROM journals").fetchone()
            return result[0] if result else 0
    
    # =========================================================================
    # Paper Operations
    # =========================================================================
    
    def insert_paper(self, paper: Paper) -> None:
        """Insert or replace a paper record."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO papers
                (doi, title, journal_id, published_year, published_date, authors,
                 volume, issue, pages, abstract, keywords, crossref_raw, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                paper.doi,
                paper.title,
                paper.journal_id,
                paper.published_year,
                paper.published_date,
                json.dumps(paper.authors, ensure_ascii=False) if paper.authors else None,
                paper.volume,
                paper.issue,
                paper.pages,
                paper.abstract,
                json.dumps(paper.keywords, ensure_ascii=False) if paper.keywords else None,
                paper.crossref_raw,
            ))
    
    def get_paper(self, doi: str) -> Optional[Paper]:
        """Get a paper by DOI."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM papers WHERE doi = ?",
                (doi,)
            ).fetchone()
            if row:
                data = dict(row)
                if data.get('authors'):
                    data['authors'] = json.loads(data['authors'])
                if data.get('keywords'):
                    data['keywords'] = json.loads(data['keywords'])
                return Paper(**data)
            return None
    
    def list_papers(
        self,
        journal_id: Optional[str] = None,
        year: Optional[int] = None,
        from_year: Optional[int] = None,
        until_year: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Paper]:
        """List papers with optional filters."""
        query = "SELECT * FROM papers WHERE 1=1"
        params = []
        
        if journal_id:
            query += " AND journal_id = ?"
            params.append(journal_id)
        
        if year:
            query += " AND published_year = ?"
            params.append(year)
        elif from_year and until_year:
            query += " AND published_year BETWEEN ? AND ?"
            params.extend([from_year, until_year])
        elif from_year:
            query += " AND published_year >= ?"
            params.append(from_year)
        elif until_year:
            query += " AND published_year <= ?"
            params.append(until_year)
        
        query += " ORDER BY published_year DESC, doi"
        
        if limit:
            query += f" LIMIT {limit}"
        
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            papers = []
            for row in rows:
                data = dict(row)
                if data.get('authors'):
                    data['authors'] = json.loads(data['authors'])
                if data.get('keywords'):
                    data['keywords'] = json.loads(data['keywords'])
                papers.append(Paper(**data))
            return papers
    
    def count_papers(self, journal_id: Optional[str] = None) -> int:
        """Count papers, optionally filtered by journal."""
        with self._get_connection() as conn:
            if journal_id:
                result = conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE journal_id = ?",
                    (journal_id,)
                ).fetchone()
            else:
                result = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
            return result[0] if result else 0
    
    # =========================================================================
    # Download Operations
    # =========================================================================
    
    def insert_download(self, download: Download) -> int:
        """Insert a download record and return its ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO downloads
                (doi, file_path, file_size, sha256, mirror, scihub_url, pdf_url,
                 status, http_status, error_message, attempts, started_at, completed_at, response_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                download.doi,
                download.file_path,
                download.file_size,
                download.sha256,
                download.mirror,
                download.scihub_url,
                download.pdf_url,
                download.status,
                download.http_status,
                download.error_message,
                download.attempts,
                download.started_at,
                download.completed_at,
                download.response_time_ms,
            ))
            return cursor.lastrowid
    
    def update_download(self, download: Download) -> None:
        """Update a download record."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE downloads SET
                    doi = ?,
                    file_path = ?,
                    file_size = ?,
                    sha256 = ?,
                    mirror = ?,
                    scihub_url = ?,
                    pdf_url = ?,
                    status = ?,
                    http_status = ?,
                    error_message = ?,
                    attempts = ?,
                    started_at = ?,
                    completed_at = ?,
                    response_time_ms = ?
                WHERE download_id = ?
            """, (
                download.doi,
                download.file_path,
                download.file_size,
                download.sha256,
                download.mirror,
                download.scihub_url,
                download.pdf_url,
                download.status,
                download.http_status,
                download.error_message,
                download.attempts,
                download.started_at,
                download.completed_at,
                download.response_time_ms,
                download.download_id,
            ))
    
    def get_download(self, download_id: int) -> Optional[Download]:
        """Get a download by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM downloads WHERE download_id = ?",
                (download_id,)
            ).fetchone()
            if row:
                return Download(**dict(row))
            return None
    
    def get_download_by_doi(self, doi: str) -> Optional[Download]:
        """Get the most recent download record for a DOI."""
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM downloads
                WHERE doi = ?
                ORDER BY COALESCE(completed_at, started_at) DESC, download_id DESC
                LIMIT 1
                """,
                (doi,)
            ).fetchone()
            if row:
                return self._download_from_row(row)
            return None

    def list_downloads_for_doi(
        self,
        doi: str,
        status: Optional[str] = None,
    ) -> List[Download]:
        """List all download records for one DOI, newest first."""
        query = "SELECT * FROM downloads WHERE doi = ?"
        params = [doi]

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY COALESCE(completed_at, started_at) DESC, download_id DESC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._download_from_row(row) for row in rows]

    def list_downloads_by_file_path(
        self,
        file_path: str,
        status: Optional[str] = None,
    ) -> List[Download]:
        """List download records that point at one stored file path."""
        query = "SELECT * FROM downloads WHERE file_path = ?"
        params = [file_path]

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY COALESCE(completed_at, started_at) DESC, download_id DESC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._download_from_row(row) for row in rows]

    def list_latest_downloads(self, status: Optional[str] = None) -> List[Download]:
        """List the newest download record for each DOI."""
        query = """
            SELECT * FROM (
                SELECT
                    d.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.doi
                        ORDER BY COALESCE(d.completed_at, d.started_at) DESC, d.download_id DESC
                    ) AS row_number
                FROM downloads d
                WHERE d.doi IS NOT NULL
            )
            WHERE row_number = 1
        """
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY COALESCE(completed_at, started_at) DESC, download_id DESC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._download_from_row(row) for row in rows]

    def list_latest_success_downloads_by_file_path(self) -> Dict[str, List[Download]]:
        """Group latest successful DOI records by stored file path."""
        grouped: Dict[str, List[Download]] = defaultdict(list)
        for download in self.list_latest_downloads(status="success"):
            if download.file_path:
                grouped[download.file_path].append(download)
        return dict(grouped)
    
    def list_downloads(
        self,
        status: Optional[str] = None,
        journal_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Tuple[Download, Paper]]:
        """List downloads with optional filters, joined with papers."""
        query = """
            SELECT d.*, p.* FROM downloads d
            LEFT JOIN papers p ON d.doi = p.doi
            WHERE 1=1
        """
        params = []
        
        if status:
            query += " AND d.status = ?"
            params.append(status)
        
        if journal_id:
            query += " AND p.journal_id = ?"
            params.append(journal_id)
        
        query += " ORDER BY COALESCE(d.completed_at, d.started_at) DESC, d.download_id DESC"
        
        if limit:
            query += f" LIMIT {limit}"
        
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            results = []
            for row in rows:
                data = dict(row)
                download_data = {k: v for k, v in data.items() if k.startswith('download_') or k in ['doi', 'file_path', 'file_size', 'sha256', 'mirror', 'scihub_url', 'pdf_url', 'status', 'http_status', 'error_message', 'attempts', 'started_at', 'completed_at', 'response_time_ms']}
                paper_data = {k.replace('p.', ''): v for k, v in data.items() if k in ['doi', 'title', 'journal_id', 'published_year', 'published_date', 'authors', 'volume', 'issue', 'pages', 'abstract', 'keywords', 'crossref_raw']}
                
                if paper_data.get('authors'):
                    paper_data['authors'] = json.loads(paper_data['authors'])
                if paper_data.get('keywords'):
                    paper_data['keywords'] = json.loads(paper_data['keywords'])
                
                results.append((Download(**download_data), Paper(**paper_data)))
            return results
    
    def get_download_stats(self, journal_id: Optional[str] = None) -> Dict[str, int]:
        """Get download statistics."""
        with self._get_connection() as conn:
            if journal_id:
                rows = conn.execute("""
                    SELECT d.status, COUNT(*) as count
                    FROM downloads d
                    JOIN papers p ON d.doi = p.doi
                    WHERE p.journal_id = ?
                    GROUP BY d.status
                """, (journal_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT status, COUNT(*) as count
                    FROM downloads
                    GROUP BY status
                """).fetchall()
            
            stats = {"total": 0, "success": 0, "failed": 0, "pending": 0}
            for row in rows:
                stats[row[0]] = row[1]
                stats["total"] += row[1]
            return stats
    
    # =========================================================================
    # Mirror Operations
    # =========================================================================
    
    def upsert_mirror(self, mirror: Mirror) -> None:
        """Insert or update a mirror record."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO mirrors
                (mirror_url, status, last_checked, response_time_ms, fail_count, success_count, cooldown_until)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mirror_url) DO UPDATE SET
                    status = excluded.status,
                    last_checked = excluded.last_checked,
                    response_time_ms = excluded.response_time_ms,
                    fail_count = excluded.fail_count,
                    success_count = excluded.success_count,
                    cooldown_until = excluded.cooldown_until
            """, (
                mirror.mirror_url,
                mirror.status,
                mirror.last_checked,
                mirror.response_time_ms,
                mirror.fail_count,
                mirror.success_count,
                mirror.cooldown_until,
            ))
    
    def get_mirror(self, mirror_url: str) -> Optional[Mirror]:
        """Get a mirror by URL."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM mirrors WHERE mirror_url = ?",
                (mirror_url,)
            ).fetchone()
            if row:
                return Mirror(**dict(row))
            return None
    
    def list_mirrors(self, active_only: bool = False) -> List[Mirror]:
        """List all mirrors, optionally only active ones."""
        with self._get_connection() as conn:
            if active_only:
                now = datetime.now()
                rows = conn.execute("""
                    SELECT * FROM mirrors
                    WHERE status = 'active'
                    AND (cooldown_until IS NULL OR cooldown_until < ?)
                    ORDER BY success_count DESC, fail_count ASC
                """, (now,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM mirrors ORDER BY mirror_url").fetchall()
            return [Mirror(**dict(row)) for row in rows]
    
    # =========================================================================
    # Log Operations
    # =========================================================================
    
    def insert_log(
        self,
        action: str,
        status: str,
        doi: Optional[str] = None,
        mirror: Optional[str] = None,
        message: Optional[str] = None,
        http_status: Optional[int] = None,
        response_time_ms: Optional[int] = None,
    ) -> None:
        """Insert a log entry."""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO logs
                (doi, mirror, action, status, message, http_status, response_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                doi,
                mirror,
                action,
                status,
                message,
                http_status,
                response_time_ms,
            ))
    
    def get_recent_logs(self, limit: int = 100, doi: Optional[str] = None) -> List[Dict]:
        """Get recent log entries."""
        with self._get_connection() as conn:
            if doi:
                rows = conn.execute(
                    "SELECT * FROM logs WHERE doi = ? ORDER BY timestamp DESC LIMIT ?",
                    (doi, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(row) for row in rows]
