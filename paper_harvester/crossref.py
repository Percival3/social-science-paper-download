"""
Crossref API integration for DOI discovery.
"""
import json
import re
import shutil
import subprocess
import time
from typing import List, Optional, Dict, Any, Iterator
from urllib.parse import quote
import httpx

from .config import Config
from .database import Database, Paper


CROSSREF_API_BASE = "https://api.crossref.org"

CROSSREF_SOURCE_RULES: Dict[str, Dict[str, Any]] = {
    # Crossref stores NBER working papers as report records under the NBER DOI
    # prefix, without ISSN or container-title metadata.
    "nber working paper": {
        "journal_id": "nber_working_paper",
        "title": "NBER Working Paper",
        "source_id": 226,
        "publisher": "工作论文",
        "prefix": "10.3386",
        "type": "report",
    },
    "nber working papers": {
        "journal_id": "nber_working_paper",
        "title": "NBER Working Paper",
        "source_id": 226,
        "publisher": "工作论文",
        "prefix": "10.3386",
        "type": "report",
    },
    "nber working paper series": {
        "journal_id": "nber_working_paper",
        "title": "NBER Working Paper",
        "source_id": 226,
        "publisher": "工作论文",
        "prefix": "10.3386",
        "type": "report",
    },
    # SSRN research papers are registered as SSRN Electronic Journal items.
    "ssrn": {
        "journal_id": "ssrn",
        "title": "SSRN",
        "source_id": 227,
        "publisher": "工作论文",
        "issn": "1556-5068",
        "prefix": "10.2139",
        "type": "journal-article",
    },
}

NBER_SOURCE_DOI = re.compile(r"^10\.3386/[wth]\d+$", re.IGNORECASE)
SSRN_SOURCE_DOI = re.compile(r"^10\.2139/ssrn\.\d+$", re.IGNORECASE)


def normalize_journal_title(title: Optional[str]) -> str:
    """Normalize journal titles for strict Crossref container-title matching."""
    if not title:
        return ""
    title = re.sub(r"\([^)]*\)", "", title)
    title = title.replace("&", "and")
    title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", title).strip()


def normalize_journal_title_for_issn_resolution(title: Optional[str]) -> str:
    """Normalize title variants that Crossref journal search commonly returns."""
    normalized = normalize_journal_title(title)
    return re.sub(r"^(the|a|an)\s+", "", normalized)


def journal_title_query_variants(title: str) -> List[str]:
    """Build conservative Crossref journal-search variants for local title quirks."""
    variants: List[str] = []
    bases: List[str] = []

    def add(values: List[str], value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip(" ,:;")
        if value and value not in values:
            values.append(value)

    parenthetical_stripped = re.sub(r"\([^)]*\)", "", title)
    add(bases, title)
    add(bases, parenthetical_stripped)
    add(bases, parenthetical_stripped.replace("(", "").replace(")", ""))

    for base in bases:
        candidates = [
            base,
            base.replace(" : ", ": "),
            base.replace("&", "and"),
            base.replace("&", "and").replace(" : ", ": "),
        ]
        if " and " in base:
            candidates.extend([
                base.replace(" and ", " & "),
                base.replace(" and ", " & ").replace(" : ", ": "),
            ])

        for candidate in candidates:
            add(variants, candidate)
            if not normalize_journal_title(candidate).startswith(("the ", "a ", "an ")):
                add(variants, f"The {candidate}")

        add(variants, re.sub(r"[^A-Za-z0-9&]+", " ", base).replace("&", "and"))

    return variants


def crossref_journal_title_candidates(title: Optional[str]) -> List[str]:
    """Return normalized exact-match candidates for Crossref journal titles."""
    if not title:
        return []

    candidates: List[str] = []
    values = [title]
    values.extend(re.split(r"/", re.sub(r"\([^)]*\)", "", title)))

    for value in values:
        normalized = normalize_journal_title_for_issn_resolution(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    return candidates


MANUAL_JOURNAL_ISSNS: Dict[str, List[str]] = {
    normalize_journal_title_for_issn_resolution("B E Journal of Economic Analysis & Policy"): [
        "2194-6108",
        "1935-1682",
        "1538-0637",
    ],
    normalize_journal_title_for_issn_resolution("Economic Modeling"): ["0264-9993"],
    normalize_journal_title_for_issn_resolution("Economic Modelling"): ["0264-9993"],
    normalize_journal_title_for_issn_resolution("Econonmics of Education Review"): ["0272-7757"],
    normalize_journal_title_for_issn_resolution("Economics of Education Review"): ["0272-7757"],
    normalize_journal_title_for_issn_resolution("Journal of Banking and Finance"): ["0378-4266"],
    normalize_journal_title_for_issn_resolution("Journal of Banking & Finance"): ["0378-4266"],
    normalize_journal_title_for_issn_resolution("Journal of Economic Behavior and Organization"): ["0167-2681"],
    normalize_journal_title_for_issn_resolution("Journal of Economic Behavior & Organization"): ["0167-2681"],
    normalize_journal_title_for_issn_resolution("Journal of Economics and Management Strategy"): [
        "1058-6407",
        "1530-9134",
    ],
    normalize_journal_title_for_issn_resolution("Journal of Economics & Management Strategy"): [
        "1058-6407",
        "1530-9134",
    ],
    normalize_journal_title_for_issn_resolution("Journal of Institutions and Theoretical Economics"): [
        "0932-4569",
    ],
    normalize_journal_title_for_issn_resolution("Journal of Institutional and Theoretical Economics JITE"): [
        "0932-4569",
    ],
    normalize_journal_title_for_issn_resolution("Journal of Public Budgeting, Accounting & Financial Management"): [
        "1096-3367",
        "1945-1814",
    ],
    normalize_journal_title_for_issn_resolution("Journal of Risk and Insurance"): [
        "0022-4367",
        "1539-6975",
    ],
    normalize_journal_title_for_issn_resolution("Journal of Risk & Insurance"): [
        "0022-4367",
        "1539-6975",
    ],
    normalize_journal_title_for_issn_resolution("Labor Economics"): ["0927-5371"],
    normalize_journal_title_for_issn_resolution("Labour Economics"): ["0927-5371"],
    normalize_journal_title_for_issn_resolution("Public Performance & Management Review"): [
        "1530-9576",
        "1557-9271",
    ],
    normalize_journal_title_for_issn_resolution("Review of Environment Economics and Policy"): [
        "1750-6816",
        "1750-6824",
    ],
    normalize_journal_title_for_issn_resolution("Review of Environmental Economics and Policy"): [
        "1750-6816",
        "1750-6824",
    ],
    normalize_journal_title_for_issn_resolution("Social Science & Medicine"): ["0277-9536"],
    normalize_journal_title_for_issn_resolution("Social Science & Medicine (SSM)"): ["0277-9536"],
    normalize_journal_title_for_issn_resolution("Social Science and Medicine"): ["0277-9536"],
}


def get_manual_journal_issns(journal_title: str) -> List[str]:
    """Return manually verified ISSNs for known local title variants."""
    return list(MANUAL_JOURNAL_ISSNS.get(normalize_journal_title_for_issn_resolution(journal_title), []))


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


def get_source_rule(title: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return a Crossref discovery rule for supported special sources."""
    return CROSSREF_SOURCE_RULES.get(normalize_journal_title(title))


def get_source_rule_for_doi(doi: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the special-source metadata rule for a supported DOI."""
    if not doi:
        return None

    normalized_doi = doi.strip().lower()
    if SSRN_SOURCE_DOI.match(normalized_doi):
        return CROSSREF_SOURCE_RULES["ssrn"]
    if NBER_SOURCE_DOI.match(normalized_doi):
        return CROSSREF_SOURCE_RULES["nber working paper"]

    return None


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
        
        # Extract publication date. SSRN and other non-standard records may
        # use issued/published rather than published-print/online.
        published = (
            item.get('published-print')
            or item.get('published-online')
            or item.get('published')
            or item.get('published-other')
            or item.get('issued')
            or {}
        )
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
            'select': 'DOI,title,author,container-title,published-print,published-online,issued,volume,issue,page,abstract,type,ISSN',
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
        manual_issns = get_manual_journal_issns(journal_title)
        if manual_issns:
            return manual_issns

        expected = normalize_journal_title_for_issn_resolution(journal_title)
        issns: List[str] = []
        last_error: Optional[Exception] = None
        had_response = False

        for query in journal_title_query_variants(journal_title):
            try:
                response = self.search_journals(query, rows=10)
                had_response = True
            except Exception as e:
                last_error = e
                continue

            for item in response.get('message', {}).get('items', []):
                if expected not in crossref_journal_title_candidates(item.get('title')):
                    continue

                for issn in item.get('ISSN', []):
                    if issn and issn not in issns:
                        issns.append(issn)

                if issns:
                    return issns

        if not had_response and last_error:
            raise last_error

        return issns

    def get_work_by_doi(self, doi: str) -> Optional[Paper]:
        """Fetch one Crossref work by DOI and parse it into a Paper."""
        self._rate_limit()

        url = f"{CROSSREF_API_BASE}/works/{quote(doi.strip(), safe='')}"
        last_error: Optional[Exception] = None
        try:
            with httpx.Client(headers=self.headers, timeout=30) as client:
                response = client.get(url)
                if response.status_code == 404:
                    return None
                response.raise_for_status()

            data = response.json()
        except Exception as e:
            last_error = e
            data = self._get_json_with_curl(url)
            if data is None:
                raise last_error

        return parse_crossref_item(data.get('message', {}))

    def _get_json_with_curl(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch JSON with system curl when Python TLS/networking is blocked."""
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return None

        args = [
            curl_path,
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "30",
            "-H",
            f"User-Agent: {self.config.user_agent}",
            "-H",
            f"mailto: {self.config.crossref_mailto}",
            url,
        ]
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
            check=False,
        )
        if completed.returncode != 0:
            return None

        return json.loads(completed.stdout)
    
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
    # Some sources in the spreadsheet need source-specific Crossref filters
    # instead of title search, such as NBER reports or SSRN research papers.
    source_rule = get_source_rule(journal.title)
    issn = journal.issn or journal.eissn or (source_rule.get("issn") if source_rule else None)
    if not issn and not source_rule:
        resolved_issns = client.resolve_journal_issns(journal.title)
        if resolved_issns:
            issn = resolved_issns[0]
            if not dry_run:
                journal.issn = journal.issn or resolved_issns[0]
                if len(resolved_issns) > 1:
                    journal.eissn = journal.eissn or resolved_issns[1]
                db.update_journal_issns(journal.journal_id, journal.issn, journal.eissn)

    query = build_crossref_query(
        issn=issn,
        from_year=from_year,
        until_year=until_year,
        journal_title=journal.title if not issn and not source_rule else None,
    )
    # Crossref's type filter keeps regular journal discovery away from
    # non-journal works, but publisher book reviews may still be registered as
    # journal-article and must be handled by the download classification layer.
    work_type = source_rule.get("type") if source_rule else "journal-article"
    filters = build_crossref_filters(
        issn=issn,
        from_year=from_year,
        until_year=until_year,
        doi_prefix=source_rule.get("prefix") if source_rule else None,
        work_type=work_type,
    )
    expected_journal_title = None if issn or source_rule else journal.title
    
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
