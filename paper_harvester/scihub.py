"""
Sci-Hub download functionality for Paper Harvester.
"""
import hashlib
import html
import re
import shutil
import subprocess
import time
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Set
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup

from .config import Config
from .database import Database, Mirror, Download, Paper
from .download_result import DownloadResult
from .official_sources import official_source_for_doi as _official_source_for_doi
from .official_sources import try_official_source
from .pdf_utils import calculate_sha256, inspect_local_pdf
from .paths import (
    build_pdf_path,
    is_non_downloadable_paper,
    path_identity_key,
    suffixed_pdf_path,
)


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

SSRN_DELIVERY_PATTERN = re.compile(
    r'["\']([^"\']*Delivery\.cfm/[^"\']*?\.pdf[^"\']*)["\']',
    re.IGNORECASE,
)
NON_PAPER_SKIP_ERROR = "Skipped non-paper title"
DUPLICATE_TARGET_SKIP_ERROR = "Skipped duplicate target path"


SciHubResult = DownloadResult


def _calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 for a local file."""
    return calculate_sha256(file_path)


def _inspect_local_pdf(
    file_path: Path,
    expected_size: Optional[int] = None,
    expected_sha256: Optional[str] = None,
) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
    """Check whether a local PDF exists and matches known metadata."""
    return inspect_local_pdf(file_path, expected_size, expected_sha256)


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
    return _official_source_for_doi(doi)


def _allows_numbered_collision_for_doi(doi: str) -> bool:
    """Return whether a DOI family should keep _02/_03 collision variants."""
    source = official_pdf_source_for_doi(doi) or {}
    return source.get("source") == "nber-official"


def _stored_path_candidates(data_dir: Path, file_path: Path) -> set[str]:
    """Return likely DB spellings for a PDF path."""
    candidates = {str(file_path)}
    try:
        candidates.add(str(file_path.resolve(strict=False)))
    except OSError:
        pass

    try:
        relative_path = file_path.relative_to(data_dir)
        candidates.add(str(relative_path))
        candidates.add(relative_path.as_posix())
    except ValueError:
        pass

    return candidates


def _success_path_owners_for_candidate(
    *,
    db: Database,
    data_dir: Path,
    file_path: Path,
    owner_cache: Dict[str, set[str]],
) -> set[str]:
    """Return current successful DOI owners for one candidate PDF path."""
    key = path_identity_key(data_dir, file_path)
    if key in owner_cache:
        return owner_cache[key]

    owners: set[str] = set()
    seen_download_ids: set[int] = set()
    for stored_path in _stored_path_candidates(data_dir, file_path):
        for download in db.list_downloads_by_file_path(stored_path, status="success"):
            if download.download_id in seen_download_ids:
                continue
            seen_download_ids.add(download.download_id)

            if not download.doi:
                continue

            latest = db.get_download_by_doi(download.doi)
            if (
                latest
                and latest.status == "success"
                and latest.file_path
                and path_identity_key(data_dir, latest.file_path) == key
            ):
                owners.add(download.doi.lower())

    owner_cache[key] = owners
    return owners


def _choose_collision_safe_output_path(
    *,
    db: Database,
    data_dir: Path,
    base_path: Path,
    doi: str,
    success_path_owners: Dict[str, set[str]],
    force: bool,
    allow_numbered_collision: bool = False,
    max_suffix: int = 999,
) -> tuple[Optional[Path], Optional[str]]:
    """Choose a PDF path without overwriting a different DOI's successful file."""
    doi_key = doi.lower()
    base_key = path_identity_key(data_dir, base_path)
    base_owners = _success_path_owners_for_candidate(
        db=db,
        data_dir=data_dir,
        file_path=base_path,
        owner_cache=success_path_owners,
    )

    other_base_owners = sorted(owner for owner in base_owners if owner != doi_key)
    if other_base_owners and not allow_numbered_collision:
        return (
            None,
            "Target path already belongs to DOI(s) "
            f"{', '.join(other_base_owners)}: {base_path.relative_to(data_dir)}",
        )

    if not base_owners and base_path.exists() and not force:
        return None, f"Untracked target path already exists: {base_path.relative_to(data_dir)}"

    for suffix in range(1, max_suffix + 1):
        candidate = suffixed_pdf_path(base_path, suffix)
        key = path_identity_key(data_dir, candidate)
        owners = _success_path_owners_for_candidate(
            db=db,
            data_dir=data_dir,
            file_path=candidate,
            owner_cache=success_path_owners,
        )
        if any(owner != doi_key for owner in owners):
            continue

        if candidate.exists() and not owners and not force:
            continue

        return candidate, None

    return None, f"No available numbered PDF path after _{max_suffix:02d}: {base_path.relative_to(data_dir)}"


class SSRNBrowserSession:
    """Reusable Playwright browser session for batch SSRN downloads."""

    def __init__(
        self,
        *,
        download_dir: Path,
        user_data_dir: Path,
        timeout_seconds: int = 180,
        browser_channel: str = "chrome",
    ):
        self.download_dir = download_dir.expanduser()
        self.user_data_dir = user_data_dir.expanduser()
        self.timeout_seconds = max(timeout_seconds, 1)
        self.timeout_ms = self.timeout_seconds * 1000
        self.navigation_timeout_ms = min(self.timeout_ms, 30_000)
        self.verification_timeout_seconds = max(self.timeout_seconds, 600)
        self.browser_channel = browser_channel
        self._playwright = None
        self._context = None

    def start(self) -> "SSRNBrowserSession":
        """Start a persistent visible browser context."""
        from playwright.sync_api import sync_playwright

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        channels = [self.browser_channel]
        if self.browser_channel == "auto":
            channels = ["chrome", "msedge", "chromium"]

        errors = []
        for channel in channels:
            try:
                kwargs = {
                    "user_data_dir": str(self.user_data_dir),
                    "headless": False,
                    "accept_downloads": True,
                    "downloads_path": str(self.download_dir),
                    "chromium_sandbox": True,
                }
                if channel != "chromium":
                    kwargs["channel"] = channel
                self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
                self._context.set_default_timeout(self.timeout_ms)
                return self
            except Exception as e:
                errors.append(f"{channel}: {e}")

        self.close()
        raise RuntimeError("Could not start browser session: " + "; ".join(errors))

    def close(self) -> None:
        """Close browser resources."""
        if self._context:
            self._context.close()
            self._context = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> "SSRNBrowserSession":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _extract_ssrn_pdf_url(self, html_text: str, page_url: str, abstract_id: str) -> Optional[str]:
        """Extract the dynamic SSRN Delivery.cfm PDF URL from browser HTML."""
        for match in SSRN_DELIVERY_PATTERN.finditer(html_text):
            pdf_url = html.unescape(match.group(1))
            if abstract_id in pdf_url or f"abstractid={abstract_id}" in pdf_url.lower():
                return urljoin(page_url, pdf_url)

        soup = BeautifulSoup(html_text, 'html.parser')
        candidate_urls = []
        for link in soup.find_all('a', href=True):
            href = html.unescape(link['href'])
            if 'delivery.cfm' in href.lower() and '.pdf' in href.lower():
                candidate_urls.append(urljoin(page_url, href))

        for pdf_url in candidate_urls:
            if abstract_id in pdf_url or f"abstractid={abstract_id}" in pdf_url.lower():
                return pdf_url

        return candidate_urls[0] if candidate_urls else None

    def _save_pdf_bytes(self, body: bytes, output_path: Path) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
        """Write bytes to output_path and validate that they are a PDF."""
        temp_path = output_path.with_suffix(f"{output_path.suffix}.browser")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.unlink(missing_ok=True)
        temp_path.write_bytes(body)

        is_valid, file_size, sha256, error = _inspect_local_pdf(temp_path)
        if not is_valid:
            temp_path.unlink(missing_ok=True)
            return False, file_size, sha256, error

        temp_path.replace(output_path)
        return True, file_size, sha256, None

    def _fetch_pdf_with_browser_context(
        self,
        *,
        pdf_url: str,
        page_url: str,
        output_path: Path,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
        """Fetch a PDF URL with browser cookies/session state."""
        response = self._context.request.get(
            pdf_url,
            headers={
                "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
                "Referer": page_url,
            },
            timeout=self.timeout_ms,
        )
        if not response.ok:
            return False, None, None, f"Browser request returned HTTP {response.status}"

        return self._save_pdf_bytes(response.body(), output_path)

    def _save_playwright_download(
        self,
        *,
        download,
        output_path: Path,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
        """Save a Playwright download object to output_path and validate it."""
        temp_path = output_path.with_suffix(f"{output_path.suffix}.browser")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.unlink(missing_ok=True)
        download.save_as(str(temp_path))

        is_valid, file_size, sha256, error = _inspect_local_pdf(temp_path)
        if not is_valid:
            temp_path.unlink(missing_ok=True)
            return False, file_size, sha256, error

        temp_path.replace(output_path)
        return True, file_size, sha256, None

    def _find_pdf_url_in_page(self, page, official_source: Dict[str, str]) -> Optional[str]:
        """Find a Delivery.cfm PDF URL in the current browser page."""
        try:
            page_url = page.url
            if "delivery.cfm" in page_url.lower() and ".pdf" in page_url.lower():
                return page_url
        except Exception:
            pass

        try:
            pdf_url = self._extract_ssrn_pdf_url(
                page.content(),
                official_source["page_url"],
                official_source["abstract_id"],
            )
            if pdf_url:
                return pdf_url
        except Exception:
            pass

        try:
            return page.evaluate(
                """
                (abstractId) => {
                    const links = Array.from(document.querySelectorAll('a[href]'))
                        .map((link) => link.href)
                        .filter((href) => /Delivery\\.cfm/i.test(href) && /\\.pdf/i.test(href));
                    return links.find((href) => href.includes(abstractId) || href.toLowerCase().includes(`abstractid=${abstractId}`))
                        || links[0]
                        || null;
                }
                """,
                official_source["abstract_id"],
            )
        except Exception:
            return None

    def _find_pdf_url_in_browser_pages(self, official_source: Dict[str, str]) -> Optional[str]:
        """Find a PDF URL across all open browser pages in this context."""
        for page in list(self._context.pages):
            pdf_url = self._find_pdf_url_in_page(page, official_source)
            if pdf_url:
                return pdf_url
        return None

    def _attach_download_collector(self, page, downloads: list) -> None:
        """Collect downloads from any page involved in the SSRN flow."""
        try:
            page.on("download", lambda download: downloads.append(download))
        except Exception:
            pass

    def _click_download_control(self, page, clicked: set[str]) -> Optional[str]:
        """Click a visible SSRN download/open-PDF control once."""
        patterns = [
            ("download-this-paper", re.compile(r"download\s+this\s+paper", re.IGNORECASE)),
            ("download", re.compile(r"download", re.IGNORECASE)),
            ("open-pdf", re.compile(r"open\s+pdf", re.IGNORECASE)),
            ("view-pdf", re.compile(r"view\s+pdf", re.IGNORECASE)),
        ]

        for key, pattern in patterns:
            if key in clicked:
                continue
            locators = [
                page.get_by_role("link", name=pattern),
                page.get_by_role("button", name=pattern),
            ]
            for locator in locators:
                try:
                    if locator.count() < 1:
                        continue
                    first = locator.first
                    if not first.is_visible(timeout=1000):
                        continue
                    first.click(timeout=5000)
                    clicked.add(key)
                    return key
                except Exception:
                    continue

        return None

    def _visible_page_text(self, page) -> str:
        """Return user-visible page text for gate detection."""
        parts = []
        try:
            parts.append(page.title())
        except Exception:
            pass
        try:
            parts.append(page.locator("body").inner_text(timeout=1000))
        except Exception:
            pass
        return "\n".join(part for part in parts if part).casefold()

    def _browser_challenge_active(self, page) -> bool:
        """Return whether the page looks like an active browser verification challenge."""
        text = self._visible_page_text(page)
        return (
            "verify you are human" in text
            or "checking if the site connection is secure" in text
            or "just a moment" in text
            or "security of your connection" in text
        )

    def _manual_gate_hint_needed(self, page) -> bool:
        """Return whether the page likely needs user action before the PDF is exposed."""
        text = self._visible_page_text(page)
        return (
            self._browser_challenge_active(page)
            or "sign in to download" in text
            or "login to download" in text
            or "log in to download" in text
        )

    def download_ssrn_pdf(
        self,
        *,
        output_path: Path,
        official_source: Dict[str, str],
    ) -> SciHubResult:
        """Use a persistent browser session to download one SSRN PDF."""
        if not self._context:
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message="Browser session is not started",
            )

        start_time = time.time()
        page = self._context.new_page()
        downloads = []
        self._attach_download_collector(page, downloads)
        page_handler = lambda new_page: self._attach_download_collector(new_page, downloads)
        self._context.on("page", page_handler)
        clicked: set[str] = set()
        hinted = False

        try:
            print(f"\nBrowser session: opening {official_source['page_url']}", flush=True)
            try:
                page.goto(
                    official_source["page_url"],
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
            except Exception as e:
                print(
                    f"Browser session: page load still pending or blocked ({e}); "
                    "waiting for user/session state.",
                    flush=True,
                )

            verification_deadline = time.time() + self.verification_timeout_seconds
            download_deadline: Optional[float] = None
            last_error = "Browser session timed out before a PDF was available"
            while True:
                now = time.time()
                challenge_active = self._browser_challenge_active(page)
                if challenge_active:
                    if not hinted:
                        print(
                            "Browser session: SSRN is showing a verification page. "
                            "Complete every prompt in this browser; the same session will be reused.",
                            flush=True,
                        )
                        hinted = True
                    if now >= verification_deadline:
                        last_error = "Browser verification was not completed before timeout"
                        break
                    time.sleep(2)
                    continue

                if download_deadline is None:
                    download_deadline = now + self.timeout_seconds
                elif now >= download_deadline:
                    break

                if downloads:
                    download = downloads.pop(0)
                    success, file_size, sha256, error = self._save_playwright_download(
                        download=download,
                        output_path=output_path,
                    )
                    if success:
                        return SciHubResult(
                            success=True,
                            file_path=output_path,
                            file_size=file_size,
                            sha256=sha256,
                            mirror="ssrn-browser",
                            scihub_url=official_source["page_url"],
                            pdf_url=getattr(download, "url", None),
                            response_time_ms=int((time.time() - start_time) * 1000),
                    )
                    last_error = error or "Browser download was not a valid PDF"

                pdf_url = self._find_pdf_url_in_browser_pages(official_source)
                if pdf_url:
                    success, file_size, sha256, error = self._fetch_pdf_with_browser_context(
                        pdf_url=pdf_url,
                        page_url=official_source["page_url"],
                        output_path=output_path,
                    )
                    if success:
                        return SciHubResult(
                            success=True,
                            file_path=output_path,
                            file_size=file_size,
                            sha256=sha256,
                            mirror="ssrn-browser",
                            scihub_url=official_source["page_url"],
                            pdf_url=pdf_url,
                            response_time_ms=int((time.time() - start_time) * 1000),
                        )
                    last_error = error or "Browser request did not return a valid PDF"

                clicked_key = self._click_download_control(page, clicked)
                if clicked_key:
                    last_error = f"Clicked {clicked_key}, waiting for PDF"

                if not hinted and self._manual_gate_hint_needed(page):
                    print(
                        "Browser session: if SSRN shows a verification or sign-in page, "
                        "complete it once in this browser. The same session will be reused.",
                        flush=True,
                    )
                    hinted = True

                time.sleep(2)

            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message=last_error,
                response_time_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message=f"Browser session failed: {str(e)}",
                response_time_ms=int((time.time() - start_time) * 1000),
            )
        finally:
            try:
                self._context.remove_listener("page", page_handler)
            except Exception:
                pass
            try:
                page.close()
            except Exception:
                pass


class SSRNExternalBrowserBatch:
    """Batch SSRN collector that uses the user's normal browser.

    This mode does not automate the page. It opens SSRN tabs in the user's
    default browser and imports newly downloaded PDFs from the browser download
    directory.
    """

    def __init__(
        self,
        *,
        download_dir: Path,
        official_sources: List[Dict[str, str]],
        timeout_seconds: int = 180,
    ):
        self.download_dir = download_dir.expanduser()
        self.official_sources = official_sources
        self.timeout_seconds = max(timeout_seconds, 1)
        self.snapshot: Dict[Path, tuple[int, int]] = {}
        self.started_at: float = 0.0
        self.assigned_paths: Set[Path] = set()
        self.candidate_text_cache: Dict[Path, str] = {}
        self.source_by_abstract_id = {
            source.get("abstract_id"): source
            for source in official_sources
            if source.get("abstract_id")
        }
        self.opened = False

    def start(self) -> "SSRNExternalBrowserBatch":
        """Open all SSRN abstract pages in the user's normal browser."""
        if not self.download_dir.exists():
            raise RuntimeError(f"Browser download directory does not exist: {self.download_dir}")

        self.snapshot = self._pdf_snapshot(self.download_dir)
        self.started_at = time.time()

        print(
            "\nBrowser assist: opening SSRN pages in your normal browser, not Playwright.",
            flush=True,
        )
        print(
            f"Browser assist: opened {len(self.official_sources)} tab(s). "
            "Complete any SSRN verification in the browser, then download the PDFs. "
            "If PDF filenames do not contain the SSRN abstract id, download tabs in order.",
            flush=True,
        )
        print(
            f"Browser assist: watching {self.download_dir} for new PDFs.",
            flush=True,
        )

        for source in self.official_sources:
            try:
                webbrowser.open_new_tab(source["page_url"])
            except Exception:
                webbrowser.open(source["page_url"])
            time.sleep(0.3)

        self.opened = True
        return self

    def close(self) -> None:
        """No browser resources are owned by this process."""
        return None

    @staticmethod
    def _pdf_snapshot(download_dir: Path) -> Dict[Path, tuple[int, int]]:
        """Snapshot current PDF files by path, mtime_ns, and size."""
        snapshot: Dict[Path, tuple[int, int]] = {}
        if not download_dir.exists():
            return snapshot

        for pdf_path in download_dir.glob("*.pdf"):
            try:
                stat = pdf_path.stat()
            except OSError:
                continue
            snapshot[pdf_path.resolve(strict=False)] = (stat.st_mtime_ns, stat.st_size)

        return snapshot

    @staticmethod
    def _has_active_browser_temp_file(pdf_path: Path) -> bool:
        """Return whether a browser temp file suggests the PDF is still downloading."""
        temp_suffixes = (".crdownload", ".part", ".tmp")
        return any(pdf_path.with_name(f"{pdf_path.name}{suffix}").exists() for suffix in temp_suffixes)

    def _stable_new_pdf_candidates(self) -> List[Path]:
        """Return stable new or changed PDFs not yet assigned to a DOI."""
        candidates = []
        for pdf_path in self.download_dir.glob("*.pdf"):
            try:
                resolved = pdf_path.resolve(strict=False)
                stat = pdf_path.stat()
            except OSError:
                continue

            if resolved in self.assigned_paths:
                continue

            previous = self.snapshot.get(resolved)
            changed = previous is None or previous != (stat.st_mtime_ns, stat.st_size)
            if not changed or stat.st_mtime < self.started_at - 2:
                continue

            if self._has_active_browser_temp_file(pdf_path):
                continue

            try:
                first_size = pdf_path.stat().st_size
                time.sleep(0.2)
                second_size = pdf_path.stat().st_size
            except OSError:
                continue

            if first_size != second_size:
                continue

            is_valid, _, _, _ = _inspect_local_pdf(pdf_path)
            if is_valid:
                candidates.append(pdf_path)

        return sorted(candidates, key=lambda path: path.stat().st_mtime)

    @staticmethod
    def _normalize_match_text(value: Optional[str]) -> str:
        """Normalize title and PDF text for conservative matching."""
        if not value:
            return ""
        text = re.sub(r"[^a-z0-9]+", " ", value.casefold())
        return re.sub(r"\s+", " ", text).strip()

    def _candidate_text(self, candidate: Path) -> str:
        """Extract cached first-pages text from a candidate PDF."""
        key = candidate.resolve(strict=False)
        if key in self.candidate_text_cache:
            return self.candidate_text_cache[key]

        text = ""
        try:
            import fitz

            with fitz.open(candidate) as doc:
                pages = min(len(doc), 2)
                text = "\n".join(doc[index].get_text("text") for index in range(pages))
        except Exception:
            text = ""

        normalized = self._normalize_match_text(text[:12000])
        self.candidate_text_cache[key] = normalized
        return normalized

    def _enriched_source(self, official_source: Dict[str, str]) -> Dict[str, str]:
        """Return the batch source record with title metadata when available."""
        abstract_id = official_source.get("abstract_id")
        if abstract_id and abstract_id in self.source_by_abstract_id:
            merged = dict(official_source)
            merged.update(self.source_by_abstract_id[abstract_id])
            return merged
        return official_source

    def _candidate_matches_source(self, candidate: Path, source: Dict[str, str]) -> bool:
        """Return whether a PDF candidate appears to belong to a source."""
        abstract_id = source.get("abstract_id") or ""
        normalized_filename = self._normalize_match_text(candidate.name)
        normalized_text = self._candidate_text(candidate)

        if abstract_id and (
            re.search(rf"(?<!\d){re.escape(abstract_id)}(?!\d)", candidate.name)
            or abstract_id in normalized_text
            or abstract_id in normalized_filename
        ):
            return True

        title = self._normalize_match_text(source.get("title"))
        if title and title in normalized_text:
            return True

        main_title = title.split(" ", 12)
        if len(main_title) > 5:
            title_prefix = " ".join(main_title[:8])
            if title_prefix and title_prefix in normalized_text:
                return True

        return False

    def _choose_candidate(
        self,
        candidates: List[Path],
        official_source: Dict[str, str],
    ) -> Optional[Path]:
        """Choose the best candidate for the current DOI."""
        source = self._enriched_source(official_source)
        for candidate in candidates:
            if self._candidate_matches_source(candidate, source):
                return candidate

        if len(self.official_sources) == 1 and candidates:
            return candidates[0]

        return None

    def _match_other_source(
        self,
        candidates: List[Path],
        official_source: Dict[str, str],
    ) -> Tuple[Optional[Path], Optional[Dict[str, str]]]:
        """Return a candidate that clearly belongs to another batch DOI."""
        current_id = official_source.get("abstract_id")
        for candidate in candidates:
            for source in self.official_sources:
                if source.get("abstract_id") == current_id:
                    continue
                if self._candidate_matches_source(candidate, source):
                    return candidate, source
        return None, None

    def _import_candidate(
        self,
        *,
        candidate: Path,
        output_path: Path,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
        """Move a browser-downloaded PDF into the project PDF path."""
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_candidate = candidate.resolve(strict=False)
            if resolved_candidate != output_path.resolve(strict=False):
                if output_path.exists():
                    output_path.unlink()
                shutil.move(str(candidate), str(output_path))
                self.assigned_paths.add(resolved_candidate)
            else:
                self.assigned_paths.add(resolved_candidate)
        except Exception as e:
            return False, None, None, f"Could not import browser PDF: {str(e)}"

        return _inspect_local_pdf(output_path)

    def download_ssrn_pdf(
        self,
        *,
        output_path: Path,
        official_source: Dict[str, str],
    ) -> SciHubResult:
        """Wait for the next matching browser-downloaded SSRN PDF."""
        if not self.opened:
            self.start()

        start_time = time.time()
        deadline = start_time + self.timeout_seconds
        abstract_id = official_source.get("abstract_id") or "unknown"
        last_error = "No browser-downloaded PDF was available before timeout"

        print(
            f"\nBrowser assist: waiting for SSRN {abstract_id}; "
            f"target {output_path.name}",
            flush=True,
        )

        while time.time() < deadline:
            candidates = self._stable_new_pdf_candidates()
            candidate = self._choose_candidate(candidates, official_source)
            if candidate:
                success, file_size, sha256, error = self._import_candidate(
                    candidate=candidate,
                    output_path=output_path,
                )
                if success:
                    return SciHubResult(
                        success=True,
                        file_path=output_path,
                        file_size=file_size,
                        sha256=sha256,
                        mirror="ssrn-official",
                        scihub_url=official_source["page_url"],
                        pdf_url=None,
                        response_time_ms=int((time.time() - start_time) * 1000),
                    )
                last_error = error or f"{candidate.name} was not a valid PDF"
            elif candidates:
                other_candidate, other_source = self._match_other_source(candidates, official_source)
                if other_candidate and other_source:
                    other_id = other_source.get("abstract_id") or "unknown"
                    return SciHubResult(
                        success=False,
                        mirror="ssrn-browser",
                        scihub_url=official_source["page_url"],
                        error_message=(
                            "No matching PDF for this SSRN page; detected a downloaded "
                            f"PDF for SSRN {other_id}. Marking current DOI as unavailable "
                            "or skipped so the matching PDF can be imported for its DOI."
                        ),
                        response_time_ms=int((time.time() - start_time) * 1000),
                    )
                last_error = "New browser-downloaded PDF did not match the current DOI or title"

            time.sleep(1)

        return SciHubResult(
            success=False,
            mirror="ssrn-browser",
            scihub_url=official_source["page_url"],
            error_message=last_error,
            response_time_ms=int((time.time() - start_time) * 1000),
        )


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

    def _make_request_with_curl(
        self,
        url: str,
        method: str,
        follow_redirects: bool,
    ) -> Tuple[Optional[httpx.Response], Optional[str]]:
        """Fetch an HTML page with system curl when Python networking is blocked."""
        if method != 'GET':
            return None, "curl fallback only supports GET requests"

        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return None, "curl executable not found"

        status_marker = "__PAPER_HARVESTER_CURL_STATUS__:"
        url_marker = "__PAPER_HARVESTER_CURL_URL__:"
        args = [
            curl_path,
            "--silent",
            "--show-error",
            "--max-time",
            str(self.config.scihub_timeout),
            "-A",
            self.headers.get("User-Agent", self.config.user_agent),
            "-H",
            f"Accept: {self.headers.get('Accept', 'text/html,application/pdf,*/*;q=0.8')}",
            "-H",
            f"Accept-Language: {self.headers.get('Accept-Language', 'en-US,en;q=0.5')}",
            "-w",
            f"\n{status_marker}%{{http_code}}\n{url_marker}%{{url_effective}}\n",
        ]
        if follow_redirects:
            args.append("-L")

        if self.proxy:
            args.extend(["--proxy", self.proxy])

        args.append(url)

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.scihub_timeout + 10,
                check=False,
            )
        except Exception as e:
            return None, f"curl fallback failed: {str(e)}"

        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            return None, f"curl fallback failed ({completed.returncode}): {stderr}"

        output = completed.stdout or ""
        status_index = output.rfind(status_marker)
        if status_index < 0:
            return None, "curl fallback did not report HTTP status"

        body = output[:status_index].rstrip("\r\n")
        metadata = output[status_index:].splitlines()
        status_code = 0
        effective_url = url
        for line in metadata:
            if line.startswith(status_marker):
                try:
                    status_code = int(line.removeprefix(status_marker).strip())
                except ValueError:
                    status_code = 0
            elif line.startswith(url_marker):
                effective_url = line.removeprefix(url_marker).strip() or url

        if status_code <= 0:
            return None, "curl fallback returned no HTTP status"

        request = httpx.Request(method, effective_url)
        return httpx.Response(
            status_code=status_code,
            content=body.encode("utf-8", errors="replace"),
            request=request,
        ), None
    
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
            response, curl_error = self._make_request_with_curl(url, method, follow_redirects)
            if response is not None:
                return response, None
            return None, f"Request timeout; {curl_error}"
        except httpx.NetworkError as e:
            response, curl_error = self._make_request_with_curl(url, method, follow_redirects)
            if response is not None:
                return response, None
            return None, f"Network error: {str(e)}; {curl_error}"
        except httpx.HTTPStatusError as e:
            return None, f"HTTP error: {e.response.status_code}"
        except Exception as e:
            response, curl_error = self._make_request_with_curl(url, method, follow_redirects)
            if response is not None:
                return response, None
            return None, f"Request failed: {str(e)}; {curl_error}"
    
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
            curl_success, file_size, sha256, response_time, curl_error = self._download_pdf_with_curl_headers(
                pdf_url,
                output_path,
                headers=headers or self.headers,
            )
            if curl_success:
                return True, file_size, sha256, response_time, None
            return False, file_size, sha256, response_time, f"{str(e)}; curl fallback failed: {curl_error}"

    def _download_pdf_with_curl_headers(
        self,
        pdf_url: str,
        output_path: Path,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str]]:
        """Download a PDF with system curl using caller-supplied headers."""
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return False, None, None, None, "curl executable not found"

        self._rate_limit()
        start_time = time.time()
        temp_path = output_path.with_suffix(f"{output_path.suffix}.part")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.unlink(missing_ok=True)

        request_headers = headers or self.headers
        user_agent = request_headers.get("User-Agent", self.config.user_agent)
        args = [
            curl_path,
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(self.config.scihub_timeout),
            "-A",
            user_agent,
            "-o",
            str(temp_path),
        ]

        for key, value in request_headers.items():
            if key.lower() == "user-agent":
                continue
            args.extend(["-H", f"{key}: {value}"])

        if self.proxy:
            args.extend(["--proxy", self.proxy])

        args.append(pdf_url)

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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

    def _download_pdf_with_curl(
        self,
        pdf_url: str,
        output_path: Path,
        official_source: Dict[str, str],
        use_cookie: bool = True,
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

        cookie = self._official_cookie(official_source) if use_cookie else None
        if cookie:
            args.extend(["-H", f"Cookie: {cookie}"])

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

    def _official_cookie(self, official_source: Dict[str, str]) -> Optional[str]:
        """Return the configured cookie for a supported official source."""
        if official_source["source"] == "ssrn-official":
            return self.config.ssrn_cookie
        return None

    def _official_headers(
        self,
        official_source: Dict[str, str],
        use_cookie: bool = True,
    ) -> Dict[str, str]:
        """Build browser-like headers for official source downloads."""
        headers = {
            "User-Agent": self.config.official_user_agent,
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": official_source["page_url"],
            "Connection": "keep-alive",
        }
        cookie = self._official_cookie(official_source) if use_cookie else None
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _official_page_headers(
        self,
        official_source: Dict[str, str],
        use_cookie: bool = False,
    ) -> Dict[str, str]:
        """Build browser-like headers for official landing pages."""
        headers = self._official_headers(official_source, use_cookie=use_cookie)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        return headers

    def _fetch_official_page(
        self,
        official_source: Dict[str, str],
        use_cookie: bool = False,
    ) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """Fetch an official landing page for sources whose PDF URL is dynamic."""
        html_text, response_time, error = self._fetch_official_page_with_curl(
            official_source,
            use_cookie=use_cookie,
        )
        if html_text or not error:
            return html_text, response_time, error

        self._rate_limit()
        start_time = time.time()

        try:
            with httpx.Client(
                timeout=self.timeout,
                proxy=self.proxy,
                follow_redirects=True,
            ) as client:
                response = client.get(
                    official_source["page_url"],
                    headers=self._official_page_headers(official_source, use_cookie=use_cookie),
                )

            response_time = int((time.time() - start_time) * 1000)
            if response.status_code >= 400:
                return None, response_time, f"Official page returned HTTP {response.status_code}"

            html_text = response.text
            if self._check_captcha(html_text):
                return None, response_time, "Captcha detected on official page"

            return html_text, response_time, None
        except Exception as e:
            return None, None, f"Official page request failed: {str(e)}"

    def _fetch_official_page_with_curl(
        self,
        official_source: Dict[str, str],
        use_cookie: bool = False,
    ) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """Fetch an official landing page with system curl when Python HTTP is blocked."""
        curl_path = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_path:
            return None, None, "curl executable not found"

        self._rate_limit()
        start_time = time.time()
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
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]

        cookie = self._official_cookie(official_source) if use_cookie else None
        if cookie:
            args.extend(["-H", f"Cookie: {cookie}"])

        proxy = self.config.https_proxy or self.config.http_proxy
        if proxy:
            args.extend(["--proxy", proxy])

        args.append(official_source["page_url"])

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.scihub_timeout + 10,
                check=False,
            )
            response_time = int((time.time() - start_time) * 1000)
            if completed.returncode != 0:
                stderr = (completed.stderr or completed.stdout or "").strip()
                return None, response_time, f"curl page failed ({completed.returncode}): {stderr}"

            html_text = completed.stdout
            if self._check_captcha(html_text):
                return None, response_time, "Captcha detected on official page"
            if not html_text.strip():
                return None, response_time, "Official page was empty"

            return html_text, response_time, None
        except Exception as e:
            return None, None, f"curl page request failed: {str(e)}"

    def _extract_ssrn_pdf_url(self, html_text: str, page_url: str, abstract_id: str) -> Optional[str]:
        """Extract the dynamic SSRN Delivery.cfm PDF URL from an abstract page."""
        for match in SSRN_DELIVERY_PATTERN.finditer(html_text):
            pdf_url = html.unescape(match.group(1))
            if abstract_id in pdf_url or f"abstractid={abstract_id}" in pdf_url.lower():
                return urljoin(page_url, pdf_url)

        soup = BeautifulSoup(html_text, 'html.parser')
        candidate_urls = []
        for link in soup.find_all('a', href=True):
            href = html.unescape(link['href'])
            if 'delivery.cfm' in href.lower() and '.pdf' in href.lower():
                candidate_urls.append(urljoin(page_url, href))

        for pdf_url in candidate_urls:
            if abstract_id in pdf_url or f"abstractid={abstract_id}" in pdf_url.lower():
                return pdf_url

        return candidate_urls[0] if candidate_urls else None

    def _resolve_official_pdf_url(
        self,
        official_source: Dict[str, str],
        use_cookie: bool = False,
    ) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """Resolve an official source to a concrete PDF URL."""
        pdf_url = official_source.get("pdf_url")
        if pdf_url:
            return pdf_url, 0, None

        if official_source["source"] != "ssrn-official":
            return None, 0, "No official PDF URL is configured"

        html_text, response_time, error = self._fetch_official_page(
            official_source,
            use_cookie=use_cookie,
        )
        if error or not html_text:
            return None, response_time, error or "Official page was empty"

        resolved_pdf_url = self._extract_ssrn_pdf_url(
            html_text,
            official_source["page_url"],
            official_source["abstract_id"],
        )
        if not resolved_pdf_url:
            return None, response_time, "Could not find SSRN PDF delivery URL on abstract page"

        return resolved_pdf_url, response_time, None

    def _download_official_pdf_url(
        self,
        pdf_url: str,
        output_path: Path,
        official_source: Dict[str, str],
        use_cookie: bool = True,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[int], Optional[str]]:
        """Download one resolved official PDF URL with curl first, then httpx."""
        success, file_size, sha256, response_time, error = self._download_pdf_with_curl(
            pdf_url,
            output_path,
            official_source,
            use_cookie=use_cookie,
        )
        if success:
            return success, file_size, sha256, response_time, error

        return self._download_pdf(
            pdf_url,
            output_path,
            headers=self._official_headers(official_source, use_cookie=use_cookie),
        )

    @staticmethod
    def _default_browser_download_dir() -> Path:
        """Return the usual browser download directory for the current user."""
        downloads_dir = Path.home() / "Downloads"
        return downloads_dir if downloads_dir.exists() else Path.home()

    @staticmethod
    def _pdf_snapshot(download_dir: Path) -> Dict[Path, tuple[int, int]]:
        """Snapshot current PDF files by path, mtime_ns, and size."""
        snapshot: Dict[Path, tuple[int, int]] = {}
        if not download_dir.exists():
            return snapshot

        for pdf_path in download_dir.glob("*.pdf"):
            try:
                stat = pdf_path.stat()
            except OSError:
                continue
            snapshot[pdf_path.resolve(strict=False)] = (stat.st_mtime_ns, stat.st_size)

        return snapshot

    def _new_browser_pdf_candidates(
        self,
        download_dir: Path,
        snapshot: Dict[Path, tuple[int, int]],
        started_at: float,
    ) -> List[Path]:
        """Return PDFs created or changed after browser assist started."""
        candidates = []
        if not download_dir.exists():
            return candidates

        for pdf_path in download_dir.glob("*.pdf"):
            try:
                resolved = pdf_path.resolve(strict=False)
                stat = pdf_path.stat()
            except OSError:
                continue

            previous = snapshot.get(resolved)
            changed = previous is None or previous != (stat.st_mtime_ns, stat.st_size)
            if changed and stat.st_mtime >= started_at - 2:
                candidates.append(pdf_path)

        return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)

    @staticmethod
    def _has_active_browser_temp_file(pdf_path: Path) -> bool:
        """Return whether a browser temp file suggests the PDF is still downloading."""
        temp_suffixes = (".crdownload", ".part", ".tmp")
        return any(pdf_path.with_name(f"{pdf_path.name}{suffix}").exists() for suffix in temp_suffixes)

    def _wait_for_browser_pdf(
        self,
        *,
        download_dir: Path,
        snapshot: Dict[Path, tuple[int, int]],
        started_at: float,
        timeout_seconds: int,
    ) -> Tuple[Optional[Path], Optional[str]]:
        """Wait for a new browser-downloaded PDF and return its path."""
        deadline = time.time() + max(timeout_seconds, 1)
        last_error = "No PDF was downloaded before timeout"

        while time.time() < deadline:
            candidates = self._new_browser_pdf_candidates(download_dir, snapshot, started_at)
            for candidate in candidates:
                if self._has_active_browser_temp_file(candidate):
                    last_error = f"Browser is still writing {candidate.name}"
                    continue

                try:
                    first_size = candidate.stat().st_size
                    time.sleep(0.5)
                    second_size = candidate.stat().st_size
                except OSError:
                    continue

                if first_size != second_size:
                    last_error = f"Browser is still writing {candidate.name}"
                    continue

                is_valid, _, _, error = _inspect_local_pdf(candidate)
                if is_valid:
                    return candidate, None
                last_error = f"{candidate.name}: {error}"

            time.sleep(1)

        return None, last_error

    def _download_ssrn_with_browser_assist(
        self,
        *,
        output_path: Path,
        official_source: Dict[str, str],
        download_dir: Optional[Path],
        timeout_seconds: int,
    ) -> SciHubResult:
        """Open SSRN in a browser and import the PDF the user downloads."""
        browser_download_dir = download_dir or self._default_browser_download_dir()
        browser_download_dir = browser_download_dir.expanduser()
        if not browser_download_dir.exists():
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message=f"Browser download directory does not exist: {browser_download_dir}",
            )

        snapshot = self._pdf_snapshot(browser_download_dir)
        started_at = time.time()
        print(
            "\nBrowser assist: opening SSRN. Click the SSRN download button in the browser; "
            f"waiting up to {timeout_seconds}s for a new PDF in {browser_download_dir}."
        )

        try:
            opened = webbrowser.open(official_source["page_url"])
        except Exception as e:
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message=f"Could not open browser: {str(e)}",
            )

        if not opened:
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message="Could not open browser",
            )

        downloaded_pdf, wait_error = self._wait_for_browser_pdf(
            download_dir=browser_download_dir,
            snapshot=snapshot,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
        )
        if not downloaded_pdf:
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message=wait_error or "Browser-assisted download timed out",
                response_time_ms=int((time.time() - started_at) * 1000),
            )

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if downloaded_pdf.resolve(strict=False) != output_path.resolve(strict=False):
                if output_path.exists():
                    output_path.unlink()
                shutil.move(str(downloaded_pdf), str(output_path))

            is_valid, file_size, sha256, error = _inspect_local_pdf(output_path)
            if not is_valid:
                return SciHubResult(
                    success=False,
                    mirror="ssrn-browser",
                    scihub_url=official_source["page_url"],
                    error_message=error or "Browser-downloaded file is not a valid PDF",
                    response_time_ms=int((time.time() - started_at) * 1000),
                )

            return SciHubResult(
                success=True,
                file_path=output_path,
                file_size=file_size,
                sha256=sha256,
                mirror="ssrn-official",
                scihub_url=official_source["page_url"],
                pdf_url=None,
                response_time_ms=int((time.time() - started_at) * 1000),
            )
        except Exception as e:
            return SciHubResult(
                success=False,
                mirror="ssrn-browser",
                scihub_url=official_source["page_url"],
                error_message=f"Could not import browser-downloaded PDF: {str(e)}",
                response_time_ms=int((time.time() - started_at) * 1000),
            )

    def _download_official_pdf(
        self,
        doi: str,
        output_path: Path,
        browser_assist: bool = False,
        browser_download_dir: Optional[Path] = None,
        browser_timeout: int = 180,
        browser_session: Optional[Any] = None,
    ) -> Optional[SciHubResult]:
        """Try a supported official PDF source before falling back to Sci-Hub."""
        split_result = try_official_source(
            config=self.config,
            db=self.db,
            doi=doi,
            output_path=output_path,
        )
        if split_result is not None:
            return split_result

        official_source = official_pdf_source_for_doi(doi)
        if not official_source:
            return None

        if browser_assist and official_source["source"] == "ssrn-official":
            if browser_session:
                browser_result = browser_session.download_ssrn_pdf(
                    output_path=output_path,
                    official_source=official_source,
                )
            else:
                browser_result = self._download_ssrn_with_browser_assist(
                    output_path=output_path,
                    official_source=official_source,
                    download_dir=browser_download_dir,
                    timeout_seconds=browser_timeout,
                )

            if not browser_result.success:
                self.db.insert_log(
                    action='official_download',
                    status='retry',
                    doi=doi,
                    mirror="ssrn-browser",
                    message=(
                        "SSRN browser-assisted download failed: "
                        f"{browser_result.error_message or 'unknown error'}"
                    ),
                    response_time_ms=browser_result.response_time_ms,
                )
            return browser_result

        attempt_errors: List[str] = []
        attempts = [True]
        if official_source["source"] == "ssrn-official":
            attempts = [False]
            if self.config.ssrn_cookie:
                attempts.append(True)

        last_pdf_url = official_source.get("pdf_url")
        last_response_time: Optional[int] = None

        for use_cookie in attempts:
            label = (
                "cookie"
                if official_source["source"] == "ssrn-official" and use_cookie
                else "public"
                if official_source["source"] == "ssrn-official"
                else "official"
            )
            pdf_url, page_response_time, resolve_error = self._resolve_official_pdf_url(
                official_source,
                use_cookie=use_cookie,
            )
            if page_response_time:
                last_response_time = (last_response_time or 0) + page_response_time
            if resolve_error or not pdf_url:
                attempt_errors.append(f"{label}: {resolve_error or 'Could not resolve official PDF URL'}")
                continue

            last_pdf_url = pdf_url
            success, file_size, sha256, response_time, error = self._download_official_pdf_url(
                pdf_url,
                output_path,
                official_source,
                use_cookie=use_cookie,
            )
            if response_time is not None:
                last_response_time = (last_response_time or 0) + response_time
            if success:
                return SciHubResult(
                    success=True,
                    file_path=output_path,
                    file_size=file_size,
                    sha256=sha256,
                    mirror=official_source["source"],
                    scihub_url=official_source["page_url"],
                    pdf_url=pdf_url,
                    response_time_ms=last_response_time,
                )

            attempt_errors.append(f"{label}: {error or 'Official PDF download failed'}")

        error = "; ".join(attempt_errors) if attempt_errors else "Official PDF download failed"

        self.db.insert_log(
            action='official_download',
            status='retry',
            doi=doi,
            mirror=official_source["source"],
            message=f"Official PDF download failed: {error}",
            response_time_ms=last_response_time,
        )
        return SciHubResult(
            success=False,
            mirror=official_source["source"],
            scihub_url=official_source["page_url"],
            pdf_url=last_pdf_url,
            error_message=error,
            response_time_ms=last_response_time,
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
        browser_assist: bool = False,
        browser_download_dir: Optional[Path] = None,
        browser_timeout: int = 180,
        browser_session: Optional[Any] = None,
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
            browser_assist: If True, use the user's normal browser for SSRN
                downloads before falling back to Sci-Hub.
            browser_download_dir: Directory where the browser saves PDFs.
            browser_timeout: Seconds to wait for a browser-downloaded PDF.
            
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

        official_result = self._download_official_pdf(
            doi,
            output_path,
            browser_assist=browser_assist,
            browser_download_dir=browser_download_dir,
            browser_timeout=browser_timeout,
            browser_session=browser_session,
        )
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
        if not mirrors:
            return SciHubResult(
                success=False,
                error_message="No Sci-Hub mirrors configured",
            )

        # Try every configured mirror at least once. The old mirror-major loop
        # could spend the whole DOI attempt budget on the first two mirrors.
        effective_max_attempts = max(self.config.max_retries_per_doi, len(mirrors))
        total_attempts = 0
        exhausted_mirrors: set[str] = set()
        mirror_errors: Dict[str, str] = {}

        for attempt_round in range(self.config.scihub_retry):
            made_attempt = False

            for mirror_url in mirrors:
                if total_attempts >= effective_max_attempts:
                    break
                if mirror_url in exhausted_mirrors:
                    continue

                made_attempt = True
                total_attempts += 1
                scihub_url = f"{mirror_url}/{doi}"

                response, error = self._make_request(scihub_url)

                if error:
                    message = f"Page request failed: {error}"
                    mirror_errors[mirror_url] = message
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                    )
                    continue

                if response.status_code == 404:
                    message = "DOI not found (404)"
                    mirror_errors[mirror_url] = message
                    exhausted_mirrors.add(mirror_url)
                    self.db.insert_log(
                        action='scihub_download',
                        status='fail',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                        http_status=404,
                    )
                    continue

                if response.status_code == 403:
                    message = "Access forbidden (403)"
                    mirror_errors[mirror_url] = message
                    exhausted_mirrors.add(mirror_url)
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                        http_status=403,
                    )
                    continue

                if response.status_code == 429:
                    message = "Rate limited (429)"
                    mirror_errors[mirror_url] = message
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                        http_status=429,
                    )
                    time.sleep(5)
                    continue

                if response.status_code >= 500:
                    message = f"Server error: {response.status_code}"
                    mirror_errors[mirror_url] = message
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                        http_status=response.status_code,
                    )
                    time.sleep(2 ** attempt_round)
                    continue

                html = response.text

                if self._check_captcha(html):
                    message = "Captcha detected"
                    mirror_errors[mirror_url] = message
                    exhausted_mirrors.add(mirror_url)
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                    )
                    continue

                pdf_url = self._extract_pdf_url(html, scihub_url)

                if not pdf_url:
                    message = "Could not extract PDF URL from page"
                    mirror_errors[mirror_url] = message
                    self.db.insert_log(
                        action='scihub_download',
                        status='retry',
                        doi=doi,
                        mirror=mirror_url,
                        message=message,
                    )
                    continue

                success, file_size, sha256, response_time, download_error = self._download_pdf(
                    pdf_url,
                    output_path,
                    headers={**self.headers, "Referer": scihub_url},
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

                message = f"PDF download failed: {download_error or 'unknown error'}"
                mirror_errors[mirror_url] = message
                self.db.insert_log(
                    action='scihub_download',
                    status='retry',
                    doi=doi,
                    mirror=mirror_url,
                    message=message,
                )

            if not made_attempt:
                break

        details = "; ".join(
            f"{mirror}: {mirror_errors.get(mirror, 'not attempted')}"
            for mirror in mirrors
            if mirror in mirror_errors
        )
        error_message = (
            f"All mirrors failed or DOI not found after {total_attempts} attempt(s)"
            + (f": {details}" if details else "")
        )
        return SciHubResult(
            success=False,
            error_message=error_message,
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
    browser_assist: bool = False,
    browser_download_dir: Optional[Path] = None,
    browser_timeout: int = 180,
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
        browser_assist: If True, use the user's normal browser for SSRN
            downloads before Sci-Hub fallback.
        browser_download_dir: Directory where the browser saves PDFs.
        browser_timeout: Seconds to wait for a browser-downloaded PDF.
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
    success_path_owners: Dict[str, set[str]] = {}
    browser_session: Optional[Any] = None

    try:
        if browser_assist and any(
            (official_pdf_source_for_doi(doi) or {}).get("source") == "ssrn-official"
            for doi, _ in entries
        ):
            session_download_dir = browser_download_dir or SciHubClient._default_browser_download_dir()
            ssrn_sources = [
                {
                    **source,
                    "doi": doi,
                    "title": paper.title or "",
                }
                for doi, _ in entries
                for paper in [db.get_paper(doi) or Paper(doi=doi)]
                for source in [official_pdf_source_for_doi(doi)]
                if source and source.get("source") == "ssrn-official"
            ]
            browser_session = SSRNExternalBrowserBatch(
                download_dir=session_download_dir,
                official_sources=ssrn_sources,
                timeout_seconds=browser_timeout,
            )
            browser_session.start()

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

            if is_non_downloadable_paper(paper):
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

            # Current-state success means the DOI has already been acquired. File
            # presence is verified separately by the verify command so offline
            # archives/removable drives do not trigger accidental re-downloads.
            if not force:
                existing = db.get_download_by_doi(doi)
                if existing and existing.status == 'success' and (existing.sha256 or existing.file_path):
                    stats['skipped'] += 1
                    volume_attempts[current_volume_key] += 1
                    volume_successes[current_volume_key] += 1
                    if progress_callback:
                        progress_callback(i + 1, len(entries), doi, True, "skipped existing success")
                    continue

            base_output_path = build_pdf_path(db, output_dir, paper)
            output_path, path_error = _choose_collision_safe_output_path(
                db=db,
                data_dir=client.config.data_dir,
                base_path=base_output_path,
                doi=doi,
                success_path_owners=success_path_owners,
                force=force,
                allow_numbered_collision=_allows_numbered_collision_for_doi(doi),
            )
            if output_path is None:
                now = datetime.now()
                stats['skipped'] += 1
                db.insert_download(
                    Download(
                        doi=doi,
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
                    message=f"{DUPLICATE_TARGET_SKIP_ERROR}: {path_error}",
                )
                if progress_callback:
                    progress_callback(i + 1, len(entries), doi, True, "skipped duplicate path")
                continue

            # Download
            started_at = datetime.now()
            result = client.download(
                doi,
                output_path,
                force=force,
                preferred_mirror=preferred_mirror,
                official_only=official_only,
                skip_existing_file=False,
                browser_assist=browser_assist,
                browser_download_dir=browser_download_dir,
                browser_timeout=browser_timeout,
                browser_session=browser_session,
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

            db.insert_download(download)
            if result.success and output_path.exists():
                success_path_owners[path_identity_key(client.config.data_dir, output_path)].add(doi.lower())

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
    finally:
        if browser_session:
            browser_session.close()

    return stats
