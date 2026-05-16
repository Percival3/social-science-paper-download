"""
Crossref API integration for DOI discovery.
"""
import json
import re
import time
from typing import List, Optional, Dict, Any, Iterator
import httpx

from .config import Config
from .database import Database, Paper


CROSSREF_API_BASE = "https://api.crossref.org"

CROSSREF_NON_JOURNAL_RULES = {
    # Crossref stores NBER working papers as report records under the NBER DOI
    # prefix, without ISSN or container-title metadata.
    "nber working paper": {"prefix": "10.3386", "type": "report"},
    "nber working papers": {"prefix": "10.3386", "type": "report"},
    "nber working paper series": {"prefix": "10.3386", "type": "report"},
}


def normalize_journal_title(title: Optional[str]) -> str:
    """Normalize journal titles for strict Crossref container-title matching."""
    if not title:
        return ""
    title = re.sub(r"\([^)]*\)", "", title)
    title = title.replace("&", "and")
    title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", title).strip()


def crossref_item_matches_journal(item: Dict[str, Any], journal_title: Optional[str]) -> bool:
    """Return True if a Crossref item belongs to the requested journal title."""
    expected = normalize_journal_title(journal_title)
    if not expected:
        return True

    container_titles = item.get("container-title") or []
    for container_title in container_titles:
        if normalize_journal_title(container_title) == expected:
            return True

    return False


def get_non_journal_rule(title: Optional[str]) -> Optional[Dict[str, str]]:
    """Return a Crossref rule for serial sources that are not journal records."""
    return CROSSREF_NON_JOURNAL_RULES.get(normalize_journal_title(title))


def build_crossref_query(
    issn: Optional[str] = None,
    from_year: Optional[int] = None,
    until_year: Optional[int] = None,
    journal_title: Optional[str] = None,
) -> str:
    """
    Build Crossref query string.
    
    Args:
        issn: Journal ISSN
        from_year: Start year
        until_year: End year
        journal_title: Journal title (fallback if no ISSN)
        
    Returns:
        Query string for Crossref API
    """
    if issn:
        return ""
    return journal_title or ""


def build_crossref_filters(
    issn: Optional[str] = None,
    from_year: Optional[int] = None,
    until_year: Optional[int] = None,
    doi_prefix: Optional[str] = None,
    work_type: Optional[str] = None,
) -> Dict[str, str]:
    """Build Crossref filter parameters."""
    filters: Dict[str, str] = {}

    if issn:
        filters["issn"] = issn
    if doi_prefix:
        filters["prefix"] = doi_prefix
    if work_type:
        filters["type"] = work_type
    if from_year:
        filters["from-pub-date"] = f"{from_year}-01-01"
    if until_year:
        filters["until-pub-date"] = f"{until_year}-12-31"

    return filters


def parse_crossref_item(item: Dict[str, Any]) -> Optional[Paper]:
    """
    Parse a Crossref API item into a Paper object.
    
    Args:
        item: Raw Crossref item dict
        
    Returns:
        Paper object or None if parsing fails
    """
    try:
        # Extract DOI (required)
        doi = item.get('DOI')
        if not doi:
            return None
        
        # Extract title
        titles = item.get('title', [])
        title = titles[0] if titles else None
        
        # Extract authors
        authors = []
        for author in item.get('author', []):
            author_info = {
                'given': author.get('given'),
                'family': author.get('family'),
                'name': f"{author.get('given', '')} {author.get('family', '')}".strip(),
            }
            if author.get('affiliation'):
                author_info['affiliation'] = author['affiliation']
            if author.get('ORCID'):
                author_info['orcid'] = author['ORCID']
            authors.append(author_info)
        
        # Extract publication date
        published = item.get('published-print', item.get('published-online', {}))
        date_parts = published.get('date-parts', [[]])[0] if published else []
        
        year = None
        published_date = None
        if date_parts:
            year = date_parts[0]
            if len(date_parts) >= 3:
                published_date = f"{date_parts[0]}-{date_parts[1]:02d}-{date_parts[2]:02d}"
            elif len(date_parts) >= 2:
                published_date = f"{date_parts[0]}-{date_parts[1]:02d}"
            else:
                published_date = str(date_parts[0])
        
        # Extract journal info
        journal_title = item.get('container-title', [None])[0]
        
        # Extract abstract (rarely available in Crossref)
        abstract = item.get('abstract')
        
        return Paper(
            doi=doi.lower(),
            title=title,
            journal_id=None,  # Will be set by caller
            published_year=year,
            published_date=published_date,
            authors=authors,
            volume=item.get('volume'),
            issue=item.get('issue'),
            pages=item.get('page'),
            abstract=abstract,
            keywords=[],  # Crossref doesn't typically provide keywords
            crossref_raw=json.dumps(item, ensure_ascii=False),
        )
    except Exception as e:
        return None


class CrossrefClient:
    """Client for Crossref API."""
    
    def __init__(self, config: Config):
        """
        Initialize Crossref client.
        
        Args:
            config: Application configuration
        """
        self.config = config
        self.headers = {
            'User-Agent': config.user_agent,
            'mailto': config.crossref_mailto,
        }
        
        # Rate limiting: sleep between requests
        self.min_request_interval = 60.0 / config.requests_per_minute
        self._last_request_time = 0.0
    
    def _rate_limit(self) -> None:
        """Apply rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self._last_request_time = time.time()
    
    def search_works(
        self,
        query: str = "",
        filters: Optional[Dict[str, str]] = None,
        rows: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Search for works in Crossref.
        
        Args:
            query: Search query string
            rows: Number of results per page
            offset: Result offset for pagination
            
        Returns:
            API response dict
        """
        self._rate_limit()
        
        url = f"{CROSSREF_API_BASE}/works"
        params = {
            'rows': rows,
            'offset': offset,
            'sort': 'published-print',
            'order': 'desc',
            'select': 'DOI,title,author,container-title,published-print,published-online,volume,issue,page,abstract,type,ISSN',
        }
        if query:
            params['query.container-title'] = query
        if filters:
            params['filter'] = ','.join(f'{key}:{value}' for key, value in filters.items())
        
        with httpx.Client(headers=self.headers, timeout=30) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    def search_journals(
        self,
        query: str,
        rows: int = 10,
    ) -> Dict[str, Any]:
        """Search Crossref journal records by title."""
        self._rate_limit()

        url = f"{CROSSREF_API_BASE}/journals"
        params = {
            'query': query,
            'rows': rows,
        }

        with httpx.Client(headers=self.headers, timeout=30) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    def resolve_journal_issns(self, journal_title: str) -> List[str]:
        """Resolve ISSNs from a journal title using Crossref's journal index."""
        response = self.search_journals(journal_title, rows=10)
        expected = normalize_journal_title(journal_title)
        issns: List[str] = []

        for item in response.get('message', {}).get('items', []):
            if normalize_journal_title(item.get('title')) != expected:
                continue

            for issn in item.get('ISSN', []):
                if issn and issn not in issns:
                    issns.append(issn)

        return issns
    
    def get_all_works(
        self,
        query: str,
        filters: Optional[Dict[str, str]] = None,
        max_results: Optional[int] = None,
        expected_journal_title: Optional[str] = None,
    ) -> Iterator[Paper]:
        """
        Get all works matching query, with pagination.
        
        Args:
            query: Search query string
            max_results: Maximum number of results (None for all)
            
        Yields:
            Paper objects
        """
        offset = 0
        rows = 100
        total_yielded = 0
        
        while True:
            last_error: Optional[Exception] = None
            for attempt in range(3):
                try:
                    response = self.search_works(query, filters=filters, rows=rows, offset=offset)
                    break
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            else:
                raise RuntimeError(
                    f"Crossref request failed at offset {offset}; discovery stopped before all pages were fetched"
                ) from last_error

            items = response.get('message', {}).get('items', [])
            
            if not items:
                break
            
            for item in items:
                if not crossref_item_matches_journal(item, expected_journal_title):
                    continue

                paper = parse_crossref_item(item)
                if paper:
                    yield paper
                    total_yielded += 1
                    
                    if max_results and total_yielded >= max_results:
                        return
            
            offset += rows
            
            # Check if we've reached the end
            total_results = response.get('message', {}).get('total-results', 0)
            if offset >= total_results:
                break


def discover_papers_for_journal(
    db: Database,
    client: CrossrefClient,
    journal_id: str,
    from_year: int,
    until_year: int,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Discover papers for a specific journal.
    
    Args:
        db: Database instance
        client: Crossref client
        journal_id: Journal ID to search for
        from_year: Start year
        until_year: End year
        dry_run: If True, only count without saving
        
    Returns:
        Statistics dict
    """
    stats = {
        'found': 0,
        'imported': 0,
        'updated': 0,
        'failed': 0,
    }
    
    # Get journal info
    journal = db.get_journal(journal_id)
    if not journal:
        raise ValueError(f"Journal not found: {journal_id}")
    
    # Build query. If the source spreadsheet has no ISSN, resolve it from
    # Crossref by exact journal title before falling back to title search.
    # Some serial sources in the spreadsheet are reports rather than journals;
    # those need source-specific Crossref filters instead of container titles.
    issn = journal.issn or journal.eissn
    non_journal_rule = get_non_journal_rule(journal.title)
    if not issn and not non_journal_rule:
        resolved_issns = client.resolve_journal_issns(journal.title)
        if resolved_issns:
            issn = resolved_issns[0]

    query = build_crossref_query(
        issn=issn,
        from_year=from_year,
        until_year=until_year,
        journal_title=journal.title if not issn and not non_journal_rule else None,
    )
    # Crossref's type filter keeps regular journal discovery away from
    # non-journal works, but publisher book reviews may still be registered as
    # journal-article and must be handled by the download classification layer.
    work_type = non_journal_rule.get("type") if non_journal_rule else "journal-article"
    filters = build_crossref_filters(
        issn=issn,
        from_year=from_year,
        until_year=until_year,
        doi_prefix=non_journal_rule.get("prefix") if non_journal_rule else None,
        work_type=work_type,
    )
    expected_journal_title = None if issn or non_journal_rule else journal.title
    
    if dry_run:
        try:
            stats['found'] = sum(
                1 for _ in client.get_all_works(
                    query,
                    filters=filters,
                    expected_journal_title=expected_journal_title,
                )
            )
        except Exception as e:
            stats['failed'] = 1
        return stats
    
    # Fetch and save papers
    for paper in client.get_all_works(
        query,
        filters=filters,
        expected_journal_title=expected_journal_title,
    ):
        paper.journal_id = journal_id
        
        # Check if exists
        existing = db.get_paper(paper.doi)
        
        try:
            db.insert_paper(paper)
            stats['found'] += 1
            if existing:
                stats['updated'] += 1
            else:
                stats['imported'] += 1
        except Exception as e:
            stats['failed'] += 1
    
    return stats


def discover_papers_for_platform(
    db: Database,
    client: CrossrefClient,
    platform: str,
    from_year: int,
    until_year: int,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Discover papers for all journals on a platform.
    
    Args:
        db: Database instance
        client: Crossref client
        platform: Platform name
        from_year: Start year
        until_year: End year
        dry_run: If True, only count without saving
        
    Returns:
        Statistics dict with per-journal breakdown
    """
    journals = db.list_journals(platform=platform)
    
    results = {
        'platform': platform,
        'journals_processed': 0,
        'journals_skipped': 0,
        'total_found': 0,
        'total_imported': 0,
        'details': [],
    }
    
    for journal in journals:
        try:
            stats = discover_papers_for_journal(
                db=db,
                client=client,
                journal_id=journal.journal_id,
                from_year=from_year,
                until_year=until_year,
                dry_run=dry_run,
            )
            
            results['journals_processed'] += 1
            results['total_found'] += stats['found']
            results['total_imported'] += stats['imported']
            results['details'].append({
                'journal_id': journal.journal_id,
                'title': journal.title,
                **stats,
            })
            
        except Exception as e:
            results['journals_skipped'] += 1
            results['details'].append({
                'journal_id': journal.journal_id,
                'title': journal.title,
                'error': str(e),
            })
    
    return results
