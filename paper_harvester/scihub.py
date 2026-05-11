"""
Sci-Hub download functionality for Paper Harvester.
"""
import hashlib
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup

from .config import Config
from .database import Database, Mirror, Download, Paper
from .paths import build_pdf_path, is_non_downloadable_title


# Sci-Hub PDF extraction patterns
PDF_PATTERNS = [
    # iframe src
    re.compile(r'<iframe[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE),
    # embed src
    re.compile(r'<embed[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE),
    # location.href JS redirect
    re.compile(r'location\.href\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE),
    # window.location
    re.compile(r'window\.location\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE),
    # href link
    re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE),
]

NBER_WORKING_PAPER_DOI = re.compile(r"^10\.3386/(w\d+)$", re.IGNORECASE)
NON_PAPER_SKIP_ERROR = "Skipped non-paper title"
DUPLICATE_TARGET_SKIP_ERROR = "Skipped duplicate target path"


@dataclass
class SciHubResult:
    """Result of a Sci-Hub download attempt."""
    success: bool
    file_path: Optional[Path] = None
    file_size: Optional[int] = None
    sha256: Optional[str] = None
    mirror: Optional[str] = None
    scihub_url: Optional[str] = None
    pdf_url: Optional[str] = None
    http_status: Optional[int] = None
    error_message: Optional[str] = None
    response_time_ms: Optional[int] = None


def _calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 for a local file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _inspect_local_pdf(
    file_path: Path,
    expected_size: Optional[int] = None,
    expected_sha256: Optional[str] = None,
) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
    """Check whether a local PDF exists and matches known metadata."""
    if not file_path.exists():
        return False, None, None, "File does not exist"

    if not file_path.is_file():
        return False, None, None, "Path is not a file"

    file_size = file_path.stat().st_size
    if expected_size is not None and file_size != expected_size:
        return False, file_size, None, "File size does not match database record"

    if file_size < 1024:
        return False, file_size, None, "File is smaller than 1KB"

    with open(file_path, 'rb') as f:
        if f.read(4) != b'%PDF':
            return False, file_size, None, "File does not have a PDF header"

    sha256 = _calculate_sha256(file_path)
    if expected_sha256 and sha256.lower() != expected_sha256.lower():
        return False, file_size, sha256, "SHA256 does not match database record"

    return True, file_size, sha256, None


def _resolve_download_path(data_dir: Path, file_path: Optional[str]) -> Optional[Path]:
    """Resolve downloads.file_path against DATA_DIR when it is relative."""
    if not file_path:
        return None

    path = Path(file_path)
    return path if path.is_absolute() else data_dir / path


def _download_record_matches_local_file(download: Download, data_dir: Path) -> Tuple[bool, str]:
    """Return whether a successful download record still matches local disk."""
    file_path = _resolve_download_path(data_dir, download.file_path)
    if not file_path:
        return False, "Download record has no file path"

    is_valid, _, _, error = _inspect_local_pdf(
        file_path,
        expected_size=download.file_size,
        expected_sha256=download.sha256,
    )
    return is_valid, error or "OK"


def official_pdf_source_for_doi(doi: str) -> Optional[Dict[str, str]]:
    """Return official PDF source metadata for supported working-paper DOIs."""
    normalized_doi = doi.strip().lower()
    nber_match = NBER_WORKING_PAPER_DOI.match(normalized_doi)
    if nber_match:
        paper_number = nber_match.group(1)
        page_url = f"https://www.nber.org/papers/{paper_number}"
        return {
            "source": "nber-official",
            "page_url": page_url,
            "pdf_url": f"{page_url}.pdf",
        }

    return None


class SciHubClient:
    """Client for Sci-Hub downloads."""
    
    def __init__(self, config: Config, db: Database):
        """
        Initialize Sci-Hub client.
        
        Args:
            config: Application configuration
            db: Database instance
        """
        self.config = config
        self.db = db
        
        # HTTP client configuration
        self.timeout = httpx.Timeout(config.scihub_timeout)
        self.headers = {
            'User-Agent': config.user_agent,
            'Accept': 'text/html,application/pdf,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        # Proxy configuration
        self.proxy = config.https_proxy or config.http_proxy
        
        # Rate limiting
        self.min_request_interval = 60.0 / config.requests_per_minute
        self._last_request_time = 0.0
        
        # Mirror management
        self.mirrors = self._load_mirrors()
    
    def _rate_limit(self) -> None:
        """Apply rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self._last_request_time = time.time()
    
    def _load_mirrors(self) -> List[str]:
        """Load mirrors in config order while respecting database cooldowns."""
        active_mirrors = {mirror.mirror_url for mirror in self.db.list_mirrors(active_only=True)}
        known_mirrors = {mirror.mirror_url for mirror in self.db.list_mirrors(active_only=False)}
        ordered_mirrors = []

        for mirror_url in self.config.scihub_mirrors:
            if mirror_url not in known_mirrors or mirror_url in active_mirrors:
                ordered_mirrors.append(mirror_url)

        for mirror_url in active_mirrors:
            if mirror_url not in ordered_mirrors:
                ordered_mirrors.append(mirror_url)

        return ordered_mirrors or self.config.scihub_mirrors.copy()
    
    def _make_request(
        self,
        url: str,
        method: str = 'GET',
        follow_redirects: bool = True,
    ) -> Tuple[Optional[httpx.Response], Optional[str]]:
        """
        Make an HTTP request with error handling.
        
        Args:
            url: URL to request
            method: HTTP method
            follow_redirects: Whether to follow redirects
            
        Returns:
            Tuple of (response, error_message)
        """
        self._rate_limit()
        
        try:
            with httpx.Client(
                timeout=self.timeout,
                proxy=self.proxy,
                follow_redirects=follow_redirects,
            ) as client:
                if method == 'GET':
                    response = client.get(url, headers=self.headers)
                else:
                    response = client.request(method, url, headers=self.headers)
                
                return response, None
                
        except httpx.TimeoutException:
            return None, "Request timeout"
        except httpx.NetworkError as e:
            return None, f"Network error: {str(e)}"
        except httpx.HTTPStatusError as e:
            return None, f"HTTP error: {e.response.status_code}"
        except Exception as e:
            return None, f"Request failed: {str(e)}"
    
    def _extract_pdf_url(self, html: str, base_url: str) -> Optional[str]:
        """
        Extract PDF URL from Sci-Hub HTML page.
        
        Args:
            html: HTML content
            base_url: Base URL for resolving relative URLs
            
        Returns:
            PDF URL or None if not found
        """
        # Try regex patterns first (fastest)
        for pattern in PDF_PATTERNS:
            match = pattern.search(html)
            if match:
                pdf_url = match.group(1)
                # Resolve relative URLs
                if pdf_url.startswith('//'):
                    parsed = urlparse(base_url)
                    pdf_url = f"{parsed.scheme}:{pdf_url}"
                elif pdf_url.startswith('/'):
                    pdf_url = urljoin(base_url, pdf_url)
                elif not pdf_url.startswith('http'):
                    pdf_url = urljoin(base_url, pdf_url)
                return pdf_url
        
        # Try BeautifulSoup parsing (more robust)
        soup = BeautifulSoup(html, 'html.parser')
        
        # Look for iframe
        iframe = soup.find('iframe', id='pdf')
        if iframe and iframe.get('src'):
            return urljoin(base_url, iframe['src'])
        
        # Look for embed
        embed = soup.find('embed', {'type': 'application/pdf'})
        if embed and embed.get('src'):
            return urljoin(base_url, embed['src'])
        
        # Look for any link to PDF
        for link in soup.find_all('a', href=re.compile(r'\.pdf', re.IGNORECASE)):
            return urljoin(base_url, link['href'])
        
        # Look for button onclick
        button = soup.find('button', onclick=re.compile(r'location\.href'))
        if button:
            match = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', button.get('onclick', ''))
            if match:
                return urljoin(base_url, match.group(1))
        
        return None
    
    def _check_captcha(self, html: str) -> bool:
        """
        Check if response contains captcha challenge.
        
        Args:
            html: HTML content
            
        Returns:
            True if captcha detected
        """
        captcha_indicators = [
            'captcha',
            'CAPTCHA',
            'recaptcha',
            'g-recaptcha',
            'verify you are human',
            'are you a robot',
        ]
        html_lower = html.lower()
        return any(indicator in html_lower for indicator in captcha_indicators)
    
    def _download_pdf(
        self,
        pdf_url: str,
        output_path: Path,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str]]:
        """
        Download PDF from URL to file.
        
        Args:
            pdf_url: PDF URL
            output_path: Output file path
            
        Returns:
            Tuple of (success, file_size, sha256_hash, response_time_ms, error_message)
        """
        self._rate_limit()
        start_time = time.time()
        temp_path = output_path.with_suffix(f"{output_path.suffix}.part")
        
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.unlink(missing_ok=True)
            
            with httpx.Client(
                timeout=self.timeout,
                proxy=self.proxy,
                follow_redirects=True,
            ) as client:
                with client.stream('GET', pdf_url, headers=headers or self.headers) as response:
                    response.raise_for_status()
                    
                    # Check content type
                    content_type = response.headers.get('content-type', '')
                    if 'pdf' not in content_type.lower() and 'octet-stream' not in content_type.lower():
                        # Might still be valid, continue anyway
                        pass
                    
                    # Download to file
                    sha256_hash = hashlib.sha256()
                    file_size = 0
                    
                    with open(temp_path, 'wb') as f:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            f.write(chunk)
                            sha256_hash.update(chunk)
                            file_size += len(chunk)
                    
                    response_time = int((time.time() - start_time) * 1000)
                    
                    # Verify file is valid PDF
                    if file_size < 1024:  # Less than 1KB is suspicious
                        temp_path.unlink(missing_ok=True)
                        return False, None, None, response_time, "Downloaded file is smaller than 1KB"
                    
                    # Check PDF header
                    with open(temp_path, 'rb') as f:
                        header = f.read(4)
                        if header != b'%PDF':
                            temp_path.unlink(missing_ok=True)
                            return False, None, None, response_time, "Downloaded file does not have a PDF header"
                    
                    temp_path.replace(output_path)
                    return True, file_size, sha256_hash.hexdigest(), response_time, None
                    
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            return False, None, None, None, str(e)

    def _download_pdf_with_curl(
        self,
        pdf_url: str,
        output_path: Path,
        official_source: Dict[str, str],
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str]]:
        """Download an official PDF with system curl when Python HTTP is blocked."""
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return False, None, None, None, "curl executable not found"

        self._rate_limit()
        start_time = time.time()
        temp_path = output_path.with_suffix(f"{output_path.suffix}.part")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.unlink(missing_ok=True)

        args = [
            curl_path,
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(self.config.scihub_timeout),
            "-A",
            self.config.official_user_agent,
            "-e",
            official_source["page_url"],
            "-H",
            "Accept: application/pdf,application/octet-stream,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-o",
            str(temp_path),
        ]

        proxy = self.config.https_proxy or self.config.http_proxy
        if proxy:
            args.extend(["--proxy", proxy])

        if official_source["source"] == "nber-official" and self.config.nber_cookie:
            args.extend(["-H", f"Cookie: {self.config.nber_cookie}"])

        args.append(pdf_url)

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.config.scihub_timeout + 10,
                check=False,
            )
            response_time = int((time.time() - start_time) * 1000)
            if completed.returncode != 0:
                temp_path.unlink(missing_ok=True)
                stderr = (completed.stderr or completed.stdout or "").strip()
                return False, None, None, response_time, f"curl failed ({completed.returncode}): {stderr}"

            is_valid, file_size, sha256, error = _inspect_local_pdf(temp_path)
            if not is_valid:
                temp_path.unlink(missing_ok=True)
                return False, file_size, sha256, response_time, error

            temp_path.replace(output_path)
            return True, file_size, sha256, response_time, None
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            return False, None, None, None, str(e)

    def _official_headers(self, official_source: Dict[str, str]) -> Dict[str, str]:
        """Build browser-like headers for official source downloads."""
        headers = {
            "User-Agent": self.config.official_user_agent,
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": official_source["page_url"],
            "Connection": "keep-alive",
        }
        if official_source["source"] == "nber-official" and self.config.nber_cookie:
            headers["Cookie"] = self.config.nber_cookie
        return headers

    def _download_official_pdf(
        self,
        doi: str,
        output_path: Path,
    ) -> Optional[SciHubResult]:
        """Try a supported official PDF source before falling back to Sci-Hub."""
        official_source = official_pdf_source_for_doi(doi)
        if not official_source:
            return None

        success, file_size, sha256, response_time, error = self._download_pdf_with_curl(
            official_source["pdf_url"],
            output_path,
            official_source,
        )
        if not success:
            success, file_size, sha256, response_time, error = self._download_pdf(
                official_source["pdf_url"],
                output_path,
                headers=self._official_headers(official_source),
            )
        if success:
            return SciHubResult(
                success=True,
                file_path=output_path,
                file_size=file_size,
                sha256=sha256,
                mirror=official_source["source"],
                scihub_url=official_source["page_url"],
                pdf_url=official_source["pdf_url"],
                response_time_ms=response_time,
            )

        self.db.insert_log(
            action='official_download',
            status='retry',
            doi=doi,
            mirror=official_source["source"],
            message=f"Official PDF download failed: {error or 'unknown error'}",
            response_time_ms=response_time,
        )
        return SciHubResult(
            success=False,
            mirror=official_source["source"],
            scihub_url=official_source["page_url"],
            pdf_url=official_source["pdf_url"],
            error_message=error or "Official PDF download failed",
            response_time_ms=response_time,
        )
    
    def check_mirror(self, mirror_url: str) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Check if a mirror is available.
        
        Args:
            mirror_url: Mirror URL to check
            
        Returns:
            Tuple of (is_available, response_time_ms, error_message)
        """
        # Use a well-known DOI for testing
        test_url = f"{mirror_url}/10.1038/nature12373"
        
        start_time = time.time()
        response, error = self._make_request(test_url)
        response_time = int((time.time() - start_time) * 1000)
        
        if error:
            return False, response_time, error
        
        if response.status_code == 404:
            # 404 is expected (test DOI might not exist), but server responded
            return True, response_time, None
        
        if response.status_code >= 500:
            return False, response_time, f"Server error: {response.status_code}"
        
        if response.status_code == 403:
            return False, response_time, "Access forbidden"
        
        return True, response_time, None
    
    def check_all_mirrors(self) -> List[Tuple[str, bool, Optional[int], Optional[str]]]:
        """
        Check all configured mirrors and update database.
        
        Returns:
            List of (mirror_url, is_available, response_time_ms, error_message)
        """
        results = []
        
        for mirror_url in self.config.scihub_mirrors:
            is_available, response_time, error = self.check_mirror(mirror_url)
            
            # Update database
            existing = self.db.get_mirror(mirror_url)
            if existing:
                mirror = existing
            else:
                mirror = Mirror(mirror_url=mirror_url)
            
            mirror.last_checked = datetime.now()
            mirror.response_time_ms = response_time
            
            if is_available:
                mirror.status = 'active'
                mirror.fail_count = 0
                mirror.success_count += 1
                mirror.cooldown_until = None
            else:
                mirror.fail_count += 1
                if mirror.fail_count >= self.config.scihub_retry:
                    mirror.status = 'cooldown'
                    mirror.cooldown_until = datetime.now()
            
            self.db.upsert_mirror(mirror)
            
            results.append((mirror_url, is_available, response_time, error))
        
        # Reload mirrors list
        self.mirrors = self._load_mirrors()
        
        return results
    
    def download(
        self,
        doi: str,
        output_path: Path,
        force: bool = False,
        preferred_mirror: Optional[str] = None,
        official_only: bool = False,
        skip_existing_file: bool = True,
    ) -> SciHubResult:
        """
        Download a paper from Sci-Hub.
        
        Args:
            doi: DOI of paper to download
            output_path: Output file path
            force: If True, re-download even if file exists
            preferred_mirror: Specific mirror to use (optional)
            official_only: If True, do not fall back to Sci-Hub when an
                official source is unavailable or fails.
            skip_existing_file: If True, trust any valid existing output_path
                as already downloaded. Normal batch downloads pass False and
                only skip after a DOI-specific database match.
            
        Returns:
            SciHubResult object
        """
        # Batch callers should skip only after verifying the current DOI's
        # database record. A shared target path alone is not proof of ownership.
        if skip_existing_file and not force and output_path.exists():
            is_valid, file_size, sha256, error = _inspect_local_pdf(output_path)
            if is_valid:
                return SciHubResult(
                    success=True,
                    file_path=output_path,
                    file_size=file_size,
                    sha256=sha256,
                    error_message="File already exists (skipped)",
                )

            self.db.insert_log(
                action='scihub_download',
                status='retry',
                doi=doi,
                message=f"Existing local PDF is invalid; redownloading: {error}",
            )

        official_result = self._download_official_pdf(doi, output_path)
        if official_result and official_result.success:
            return official_result
        if official_result and official_only:
            return official_result
        if official_only:
            return SciHubResult(
                success=False,
                error_message="No official source rule is configured for this DOI",
            )
        
        # Prepare mirrors list
        mirrors = [preferred_mirror] if preferred_mirror else self.mirrors.copy()
        
        # Track attempts
        total_attempts = 0
        
        for mirror_url in mirrors:
            if total_attempts >= self.config.max_retries_per_doi:
                break
            
            scihub_url = f"{mirror_url}/{doi}"
            
            for attempt in range(self.config.scihub_retry):
                total_attempts += 1
                
                # Step 1: Get Sci-Hub page
                response, error = self._make_request(scihub_url)
                
                if error:
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=f"Page request failed: {error}",
                    )
                    continue
                
                if response.status_code == 404:
                    # DOI not found on this mirror, try next mirror
                    self.db.insert_log(
                        action='scihub_download',
                        status='fail',
                        doi=doi,
                        mirror=mirror_url,
                        message="DOI not found (404)",
                        http_status=404,
                    )
                    break
                
                if response.status_code == 403:
                    # Blocked, mark mirror for cooldown
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message="Access forbidden (403)",
                        http_status=403,
                    )
                    break
                
                if response.status_code == 429:
                    # Rate limited, slow down
                    time.sleep(5)
                    continue
                
                if response.status_code >= 500:
                    # Server error, retry
                    time.sleep(2 ** attempt)
                    continue
                
                # Step 2: Parse HTML for PDF URL
                html = response.text
                
                if self._check_captcha(html):
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message="Captcha detected",
                    )
                    break
                
                pdf_url = self._extract_pdf_url(html, scihub_url)
                
                if not pdf_url:
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message="Could not extract PDF URL from page",
                    )
                    continue
                
                # Step 3: Download PDF
                success, file_size, sha256, response_time, download_error = self._download_pdf(
                    pdf_url, output_path
                )
                
                if success:
                    return SciHubResult(
                        success=True,
                        file_path=output_path,
                        file_size=file_size,
                        sha256=sha256,
                        mirror=mirror_url,
                        scihub_url=scihub_url,
                        pdf_url=pdf_url,
                        response_time_ms=response_time,
                    )
                else:
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=f"PDF download failed: {download_error or 'unknown error'}",
                    )
        
        # All mirrors exhausted
        return SciHubResult(
            success=False,
            error_message="All mirrors failed or DOI not found",
        )


def download_papers(
    db: Database,
    client: SciHubClient,
    dois: List[str],
    output_dir: Path,
    force: bool = False,
    preferred_mirror: Optional[str] = None,
    skip_year_after_failed_volume: bool = False,
    official_only: bool = False,
    progress_callback=None,
) -> Dict[str, Any]:
    """
    Download multiple papers.
    
    Args:
        db: Database instance
        client: SciHub client
        dois: List of DOIs to download
        output_dir: Base output directory
        force: If True, re-download existing files
        preferred_mirror: Specific mirror URL to use for Sci-Hub fallback.
        skip_year_after_failed_volume: If True, skip remaining papers in a
            journal-year once one full volume in that year has no successful
            downloads.
        official_only: If True, do not fall back to Sci-Hub.
        progress_callback: Optional callback function(current, total, doi, success)
        
    Returns:
        Statistics dict
    """
    stats = {
        'total': len(dois),
        'success': 0,
        'failed': 0,
        'skipped': 0,
        'skipped_unavailable_year': 0,
        'years_skipped_after_failed_volume': 0,
    }

    entries = []
    for doi in dois:
        paper = db.get_paper(doi)
        if not paper:
            paper = Paper(doi=doi)
        entries.append((doi, paper))

    if skip_year_after_failed_volume:
        entries.sort(
            key=lambda item: (
                item[1].journal_id or "",
                -(item[1].published_year or 0),
                str(item[1].volume or ""),
                str(item[1].issue or ""),
                item[0],
            )
        )

    def year_key(paper: Paper) -> Tuple[Optional[str], Optional[int]]:
        return (paper.journal_id, paper.published_year)

    def volume_key(paper: Paper) -> Tuple[Optional[str], Optional[int], str]:
        return (paper.journal_id, paper.published_year, str(paper.volume or ""))

    volume_totals = Counter(volume_key(paper) for _, paper in entries)
    volume_attempts: Counter = Counter()
    volume_successes: Counter = Counter()
    skipped_years = set()
    
    for i, (doi, paper) in enumerate(entries):
        current_year_key = year_key(paper)
        current_volume_key = volume_key(paper)

        if skip_year_after_failed_volume and current_year_key in skipped_years:
            stats['skipped'] += 1
            stats['skipped_unavailable_year'] += 1
            db.insert_log(
                action='download',
                status='fail',
                doi=doi,
                message=(
                    "Skipped because an earlier full volume in "
                    f"{paper.journal_id or 'unknown journal'} {paper.published_year or 'unknown year'} "
                    "had zero successful downloads"
                ),
            )
            if progress_callback:
                progress_callback(i + 1, len(entries), doi, False, "skipped unavailable year")
            continue

        if is_non_downloadable_title(paper.title):
            now = datetime.now()
            stats['skipped'] += 1
            db.insert_download(
                Download(
                    doi=doi,
                    status='skipped',
                    error_message=NON_PAPER_SKIP_ERROR,
                    started_at=now,
                    completed_at=now,
                )
            )
            db.insert_log(
                action='download',
                status='skip',
                doi=doi,
                message=f"{NON_PAPER_SKIP_ERROR}: {paper.title or ''}",
            )
            if progress_callback:
                progress_callback(i + 1, len(entries), doi, True, "skipped non-paper")
            continue
        
        output_path = build_pdf_path(db, output_dir, paper)
        force_existing_file = force
        
        # Check if already downloaded
        if not force:
            existing = db.get_download_by_doi(doi)
            if existing and existing.status == 'success' and existing.sha256:
                local_ok, local_message = _download_record_matches_local_file(
                    existing,
                    client.config.data_dir,
                )
                if local_ok:
                    stats['skipped'] += 1
                    volume_attempts[current_volume_key] += 1
                    volume_successes[current_volume_key] += 1
                    if progress_callback:
                        progress_callback(i + 1, len(entries), doi, True, "skipped")
                    continue

                force_existing_file = True
                db.insert_log(
                    action='download',
                    status='retry',
                    doi=doi,
                    message=f"Existing success record ignored: {local_message}",
                )

        relative_output_path = str(output_path.relative_to(client.config.data_dir))
        success_records_for_path = db.list_downloads_by_file_path(
            relative_output_path,
            status='success',
        )
        conflicting_records = [
            record for record in success_records_for_path
            if (record.doi or "").lower() != doi.lower()
        ]
        if conflicting_records or (output_path.exists() and not force and not success_records_for_path):
            now = datetime.now()
            stats['skipped'] += 1
            db.insert_download(
                Download(
                    doi=doi,
                    file_path=relative_output_path if output_path.exists() else None,
                    status='skipped',
                    error_message=DUPLICATE_TARGET_SKIP_ERROR,
                    started_at=now,
                    completed_at=now,
                )
            )
            db.insert_log(
                action='download',
                status='skip',
                doi=doi,
                message=f"{DUPLICATE_TARGET_SKIP_ERROR}: {relative_output_path}",
            )
            if progress_callback:
                progress_callback(i + 1, len(entries), doi, True, "skipped duplicate path")
            continue
        
        # Download
        started_at = datetime.now()
        result = client.download(
            doi,
            output_path,
            force=force_existing_file,
            preferred_mirror=preferred_mirror,
            official_only=official_only,
            skip_existing_file=False,
        )
        
        # Record in database
        download = Download(
            doi=doi,
            file_path=str(output_path.relative_to(client.config.data_dir)) if output_path.exists() else None,
            file_size=result.file_size,
            sha256=result.sha256,
            mirror=result.mirror,
            scihub_url=result.scihub_url,
            pdf_url=result.pdf_url,
            status='success' if result.success else 'failed',
            http_status=result.http_status,
            error_message=result.error_message,
            started_at=started_at,
            completed_at=datetime.now() if result.success else None,
            response_time_ms=result.response_time_ms,
        )
        
        download_id = db.insert_download(download)
        
        if result.success and result.error_message == "File already exists (skipped)":
            stats['skipped'] += 1
        elif result.success:
            stats['success'] += 1
        else:
            stats['failed'] += 1

        volume_attempts[current_volume_key] += 1
        if result.success:
            volume_successes[current_volume_key] += 1

        if (
            skip_year_after_failed_volume
            and current_year_key not in skipped_years
            and volume_attempts[current_volume_key] >= volume_totals[current_volume_key]
            and volume_successes[current_volume_key] == 0
        ):
            skipped_years.add(current_year_key)
            stats['years_skipped_after_failed_volume'] += 1
            db.insert_log(
                action='download',
                status='fail',
                doi=doi,
                message=(
                    "All attempted papers failed in "
                    f"{paper.journal_id or 'unknown journal'} {paper.published_year or 'unknown year'} "
                    f"volume {paper.volume or 'unknown'}; skipping remaining papers in this year"
                ),
            )
        
        if progress_callback:
            progress_callback(i + 1, len(entries), doi, result.success, result.error_message)
    
    return stats
