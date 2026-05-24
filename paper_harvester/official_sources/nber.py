"""NBER official PDF downloader and metadata helpers."""
import json
import re
import shutil
import subprocess
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx

from ..config import Config
from ..database import Database, Paper
from ..download_result import DownloadResult
from ..pdf_utils import inspect_local_pdf


NBER_PAPER_DOI = re.compile(r"^10\.3386/([wth]\d+)$", re.IGNORECASE)


def source_for_doi(doi: str) -> Optional[Dict[str, str]]:
    """Return NBER official-source metadata for supported DOIs."""
    match = NBER_PAPER_DOI.match(doi.strip().lower())
    if not match:
        return None

    paper_number = match.group(1)
    page_url = f"https://www.nber.org/papers/{paper_number}"
    return {
        "source": "nber-official",
        "page_url": page_url,
        "pdf_url": f"{page_url}.pdf",
    }


class _NBERMetaParser(HTMLParser):
    """Extract citation metadata from an NBER paper page."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: Dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "meta":
            return

        values = {key.lower(): value for key, value in attrs if key and value is not None}
        name = (values.get("name") or values.get("property") or "").lower()
        content = values.get("content")
        if name and content:
            self.meta.setdefault(name, []).append(unescape(content).strip())


def _parse_publication_date(value: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    if not value:
        return None, None

    cleaned = value.strip().replace("/", "-")
    match = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", cleaned)
    if not match:
        return None, None

    year = int(match.group(1))
    month = match.group(2)
    day = match.group(3)
    if month and day:
        return year, f"{year:04d}-{int(month):02d}-{int(day):02d}"
    if month:
        return year, f"{year:04d}-{int(month):02d}"
    return year, f"{year:04d}"


def _fetch_page_html(config: Config, page_url: str) -> Optional[str]:
    headers = {
        "User-Agent": config.official_user_agent or config.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if config.nber_cookie:
        headers["Cookie"] = config.nber_cookie

    try:
        with httpx.Client(
            headers=headers,
            timeout=config.scihub_timeout,
            proxy=config.https_proxy or config.http_proxy,
            follow_redirects=True,
        ) as client:
            response = client.get(page_url)
            response.raise_for_status()
            return response.text
    except Exception:
        return _fetch_page_html_with_curl(config, page_url, headers)


def _fetch_page_html_with_curl(
    config: Config,
    page_url: str,
    headers: Dict[str, str],
) -> Optional[str]:
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
        str(max(int(config.scihub_timeout), 1)),
    ]
    for name, value in headers.items():
        args.extend(["-H", f"{name}: {value}"])

    proxy = config.https_proxy or config.http_proxy
    if proxy:
        args.extend(["--proxy", proxy])
    args.append(page_url)

    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(int(config.scihub_timeout), 1) + 10,
        check=False,
    )
    if completed.returncode != 0:
        return None

    return completed.stdout


def get_paper_metadata_by_doi(config: Config, doi: str) -> Optional[Paper]:
    """Fetch paper metadata from the NBER official page for direct DOI paths."""
    source = source_for_doi(doi)
    if not source:
        return None

    html = _fetch_page_html(config, source["page_url"])
    if not html:
        return None

    parser = _NBERMetaParser()
    parser.feed(html)
    metadata = parser.meta

    title = next(iter(metadata.get("citation_title", [])), None)
    date_value = next(iter(metadata.get("citation_publication_date", [])), None)
    year, published_date = _parse_publication_date(date_value)
    authors = [
        {"name": author}
        for author in metadata.get("citation_author", [])
        if author
    ]

    if not title and not year:
        return None

    return Paper(
        doi=doi.strip().lower(),
        title=title,
        journal_id="nber_working_paper",
        published_year=year,
        published_date=published_date,
        authors=authors,
        crossref_raw=json.dumps(
            {
                "source": "nber-official",
                "page_url": source["page_url"],
                "citation_metadata": metadata,
            },
            ensure_ascii=False,
        ),
    )


class NBEROfficialDownloader:
    """Download NBER working papers from nber.org before Sci-Hub fallback."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def download(self, doi: str, output_path: Path) -> Optional[DownloadResult]:
        """Download a supported NBER DOI, or return None if unsupported."""
        source = source_for_doi(doi)
        if not source:
            return None

        pdf_url = source["pdf_url"]
        success, file_size, sha256, response_time, error = self._download_pdf_with_curl(
            pdf_url,
            output_path,
            source,
        )
        if not success:
            success, file_size, sha256, response_time, error = self._download_pdf_httpx(
                pdf_url,
                output_path,
                source,
            )

        if success:
            return DownloadResult(
                success=True,
                file_path=output_path,
                file_size=file_size,
                sha256=sha256,
                mirror=source["source"],
                scihub_url=source["page_url"],
                pdf_url=pdf_url,
                response_time_ms=response_time,
            )

        self.db.insert_log(
            action='official_download',
            status='retry',
            doi=doi,
            mirror=source["source"],
            message=f"Official PDF download failed: {error or 'unknown error'}",
            response_time_ms=response_time,
        )
        return DownloadResult(
            success=False,
            mirror=source["source"],
            scihub_url=source["page_url"],
            pdf_url=pdf_url,
            error_message=error or "Official PDF download failed",
            response_time_ms=response_time,
        )

    def _headers(self, source: Dict[str, str]) -> Dict[str, str]:
        """Build official-source HTTP headers."""
        headers = {
            "User-Agent": self.config.official_user_agent or self.config.user_agent,
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": source["page_url"],
            "Connection": "keep-alive",
        }
        if self.config.nber_cookie:
            headers["Cookie"] = self.config.nber_cookie
        return headers

    def _download_pdf_httpx(
        self,
        pdf_url: str,
        output_path: Path,
        source: Dict[str, str],
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str]]:
        """Download official PDF with httpx."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(f"{output_path.suffix}.part")
        temp_path.unlink(missing_ok=True)

        start_time = time.time()
        try:
            with httpx.Client(
                headers=self._headers(source),
                timeout=self.config.scihub_timeout,
                proxy=self.config.https_proxy or self.config.http_proxy,
                follow_redirects=True,
            ) as client:
                with client.stream("GET", pdf_url) as response:
                    response_time = int((time.time() - start_time) * 1000)
                    if response.status_code != 200:
                        return False, None, None, response_time, f"Official PDF returned HTTP {response.status_code}"

                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" in content_type:
                        return False, None, None, response_time, "Official PDF URL returned HTML"

                    with open(temp_path, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

            is_valid, file_size, sha256, error = inspect_local_pdf(temp_path)
            if not is_valid:
                temp_path.unlink(missing_ok=True)
                return False, file_size, sha256, response_time, error

            temp_path.replace(output_path)
            return True, file_size, sha256, response_time, None
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            return False, None, None, int((time.time() - start_time) * 1000), str(e)

    def _download_pdf_with_curl(
        self,
        pdf_url: str,
        output_path: Path,
        source: Dict[str, str],
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str]]:
        """Download official PDF with system curl when Python networking is blocked."""
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return False, None, None, None, "curl is not available"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(f"{output_path.suffix}.part")
        temp_path.unlink(missing_ok=True)

        args = [
            curl_path,
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(int(self.config.scihub_timeout), 1)),
            "-A",
            self.config.official_user_agent or self.config.user_agent,
            "-e",
            source["page_url"],
            "-H",
            "Accept: application/pdf,application/octet-stream,*/*;q=0.8",
            "-H",
            f"Referer: {source['page_url']}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        proxy = self.config.https_proxy or self.config.http_proxy
        if proxy:
            args.extend(["--proxy", proxy])
        if self.config.nber_cookie:
            args.extend(["-H", f"Cookie: {self.config.nber_cookie}"])
        args.extend(["-o", str(temp_path), pdf_url])

        start_time = time.time()
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=max(int(self.config.scihub_timeout), 1) + 10,
                check=False,
            )
            response_time = int((time.time() - start_time) * 1000)
            if completed.returncode != 0:
                temp_path.unlink(missing_ok=True)
                stderr = (completed.stderr or completed.stdout or "").strip()
                return False, None, None, response_time, f"curl failed ({completed.returncode}): {stderr}"

            is_valid, file_size, sha256, error = inspect_local_pdf(temp_path)
            if not is_valid:
                temp_path.unlink(missing_ok=True)
                return False, file_size, sha256, response_time, error

            temp_path.replace(output_path)
            return True, file_size, sha256, response_time, None
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            return False, None, None, int((time.time() - start_time) * 1000), str(e)
