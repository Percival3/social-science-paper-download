"""
Estimate journal download coverage windows with one sampled download per journal.

For each imported journal, the script:
1. Uses Crossref facet counts to find yearly metadata coverage.
2. Samples one DOI from the earliest or latest covered year.
3. Attempts one real download through the existing SciHubClient.
4. Writes CSV and Markdown summaries for planning.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from paper_harvester.config import load_config
from paper_harvester.crossref import (
    get_non_journal_rule,
    normalize_journal_title,
    parse_crossref_item,
)
from paper_harvester.database import Database, Download, Journal, Paper
from paper_harvester.paths import build_pdf_path
from paper_harvester.scihub import SciHubClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate earliest/latest covered years and sample download time by journal."
    )
    parser.add_argument("--from-year", type=int, default=2000)
    parser.add_argument("--until-year", type=int, default=2025)
    parser.add_argument("--output-dir", type=Path, default=Path("data/manifests"))
    parser.add_argument("--output-prefix", default="journal_download_window_estimate")
    parser.add_argument("--max-journals", type=int, default=None)
    parser.add_argument(
        "--sample-year",
        choices=["earliest", "latest"],
        default="earliest",
        help="Which boundary year to sample for the one real download per journal.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Only collect Crossref yearly counts; do not attempt sample downloads.",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Do not redownload if the sampled file already exists.",
    )
    parser.add_argument("--mirror", default=None, help="Optional preferred Sci-Hub mirror URL.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip journals already present in the output CSV.",
    )
    return parser.parse_args()


class CrossrefFacetClient:
    def __init__(self, mailto: str, user_agent: str, requests_per_minute: int) -> None:
        self.headers = {
            "User-Agent": user_agent,
            "mailto": mailto,
        }
        self.min_interval = 60.0 / max(requests_per_minute, 1)
        self.last_request = 0.0
        self.client = httpx.Client(headers=self.headers, timeout=30, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        elapsed = time.time() - self.last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request = time.time()

        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self.client.get(f"https://api.crossref.org{path}", params=params)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - keep retry simple for a CLI utility
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)

        raise RuntimeError(f"Crossref request failed: {last_error}") from last_error

    def resolve_issns(self, title: str) -> List[str]:
        response = self.get("/journals", {"query": title, "rows": 10})
        expected = normalize_journal_title(title)
        issns: List[str] = []

        for item in response.get("message", {}).get("items", []):
            if normalize_journal_title(item.get("title")) != expected:
                continue
            for issn in item.get("ISSN", []):
                if issn and issn not in issns:
                    issns.append(issn)

        return issns

    def works(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.get("/works", params)


def filter_string(parts: Iterable[Tuple[str, Optional[str]]]) -> str:
    return ",".join(f"{key}:{value}" for key, value in parts if value)


def source_params(
    journal: Journal,
    client: CrossrefFacetClient,
) -> Tuple[Dict[str, Any], str, Optional[str], Optional[str]]:
    title = journal.title.strip()
    non_journal_rule = get_non_journal_rule(title)

    if non_journal_rule:
        return (
            {},
            "non_journal_rule",
            non_journal_rule.get("prefix"),
            non_journal_rule.get("type"),
        )

    issn = (journal.issn or journal.eissn or "").strip()
    if issn:
        return ({}, "spreadsheet_issn", issn, None)

    resolved = client.resolve_issns(title)
    if resolved:
        return ({}, "resolved_issn", resolved[0], None)

    return ({"query.container-title": title}, "title_query_approx", None, None)


def yearly_counts(
    journal: Journal,
    client: CrossrefFacetClient,
    from_year: int,
    until_year: int,
) -> Tuple[Dict[int, int], str, Dict[str, Any], Optional[str]]:
    query_params, source_kind, source_value, work_type = source_params(journal, client)
    filters = filter_string(
        [
            ("issn", source_value if source_kind in {"spreadsheet_issn", "resolved_issn"} else None),
            ("prefix", source_value if source_kind == "non_journal_rule" else None),
            ("type", work_type),
            ("from-pub-date", f"{from_year}-01-01"),
            ("until-pub-date", f"{until_year}-12-31"),
        ]
    )
    params: Dict[str, Any] = {
        "rows": 1,
        "facet": "published:*",
        "select": "DOI,title,author,container-title,published-print,published-online,volume,issue,page,abstract,type,ISSN",
        **query_params,
    }
    if filters:
        params["filter"] = filters

    response = client.works(params)
    values = response.get("message", {}).get("facets", {}).get("published", {}).get("values", {})
    counts = {
        int(year): int(count)
        for year, count in values.items()
        if str(year).isdigit() and from_year <= int(year) <= until_year
    }
    return counts, source_kind, query_params, source_value


def first_matching_paper(
    journal: Journal,
    client: CrossrefFacetClient,
    year: int,
    source_kind: str,
    source_value: Optional[str],
    query_params: Dict[str, Any],
) -> Optional[Paper]:
    title = journal.title.strip()
    non_journal_rule = get_non_journal_rule(title)
    filters = filter_string(
        [
            ("issn", source_value if source_kind in {"spreadsheet_issn", "resolved_issn"} else None),
            ("prefix", source_value if source_kind == "non_journal_rule" else None),
            ("type", non_journal_rule.get("type") if non_journal_rule else None),
            ("from-pub-date", f"{year}-01-01"),
            ("until-pub-date", f"{year}-12-31"),
        ]
    )
    params: Dict[str, Any] = {
        "rows": 20,
        "sort": "published",
        "order": "asc",
        "select": "DOI,title,author,container-title,published-print,published-online,volume,issue,page,abstract,type,ISSN",
        **query_params,
    }
    if filters:
        params["filter"] = filters

    response = client.works(params)
    for item in response.get("message", {}).get("items", []):
        if source_kind == "title_query_approx":
            container_titles = item.get("container-title") or []
            if not any(normalize_journal_title(value) == normalize_journal_title(title) for value in container_titles):
                continue
        paper = parse_crossref_item(item)
        if paper:
            paper.journal_id = journal.journal_id
            return paper

    return None


def insert_download_record(
    db: Database,
    config_data_dir: Path,
    doi: str,
    output_path: Path,
    result: Any,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    file_path = None
    if output_path.exists():
        file_path = str(output_path.relative_to(config_data_dir))

    db.insert_download(
        Download(
            doi=doi,
            file_path=file_path,
            file_size=result.file_size,
            sha256=result.sha256,
            mirror=result.mirror,
            scihub_url=result.scihub_url,
            pdf_url=result.pdf_url,
            status="success" if result.success else "failed",
            http_status=result.http_status,
            error_message=result.error_message,
            attempts=1,
            started_at=started_at,
            completed_at=completed_at,
            response_time_ms=result.response_time_ms,
        )
    )


def sample_download(
    db: Database,
    scihub: SciHubClient,
    paper: Paper,
    force: bool,
    preferred_mirror: Optional[str],
) -> Dict[str, Any]:
    db.insert_paper(paper)
    output_path = build_pdf_path(db, scihub.config.pdf_dir, paper)

    started_at = datetime.now()
    start = time.time()
    result = scihub.download(
        paper.doi,
        output_path,
        force=force,
        preferred_mirror=preferred_mirror,
    )
    seconds = time.time() - start
    completed_at = datetime.now()
    insert_download_record(db, scihub.config.data_dir, paper.doi, output_path, result, started_at, completed_at)

    return {
        "sample_doi": paper.doi,
        "sample_title": paper.title or "",
        "sample_success": result.success,
        "sample_seconds": round(seconds, 3),
        "sample_response_time_ms": result.response_time_ms or "",
        "sample_file_size": result.file_size or "",
        "sample_mirror": result.mirror or "",
        "sample_file_path": str(output_path) if output_path.exists() else "",
        "sample_error": result.error_message or "",
    }


def read_completed_journals(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return {row["journal_id"] for row in csv.DictReader(f) if row.get("journal_id")}


def write_csv(csv_path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(md_path: Path, rows: List[Dict[str, Any]], from_year: int, until_year: int) -> None:
    success_seconds = [
        float(row["sample_seconds"])
        for row in rows
        if str(row.get("sample_success")) == "True" and row.get("sample_seconds") not in {"", None}
    ]
    total_papers = sum(int(row.get("total_papers_in_window") or 0) for row in rows)
    journals_with_counts = sum(1 for row in rows if row.get("earliest_crossref_year"))
    sample_successes = sum(1 for row in rows if str(row.get("sample_success")) == "True")

    median_seconds = statistics.median(success_seconds) if success_seconds else 0
    mean_seconds = statistics.mean(success_seconds) if success_seconds else 0
    p90_seconds = statistics.quantiles(success_seconds, n=10)[8] if len(success_seconds) >= 10 else 0

    def hours(seconds_per_paper: float) -> float:
        return total_papers * seconds_per_paper / 3600 if seconds_per_paper else 0

    lines = [
        "# Journal Download Window Estimate",
        "",
        f"- Year range: {from_year}-{until_year}",
        f"- Journals evaluated: {len(rows)}",
        f"- Journals with Crossref records in range: {journals_with_counts}",
        f"- Sample download successes: {sample_successes}",
        f"- Total Crossref papers in covered years: {total_papers:,}",
        f"- Median successful sample seconds: {median_seconds:.2f}",
        f"- Mean successful sample seconds: {mean_seconds:.2f}",
        f"- P90 successful sample seconds: {p90_seconds:.2f}" if p90_seconds else "- P90 successful sample seconds: N/A",
        f"- Estimated sequential time by median: {hours(median_seconds):.1f} hours",
        f"- Estimated sequential time by mean: {hours(mean_seconds):.1f} hours",
        "",
        "The earliest/latest columns below are Crossref metadata coverage years. "
        "The sample columns record one real download attempt per journal.",
        "",
        "| journal_id | title | earliest | latest | papers | sample_year | sample_success | seconds | error |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    for row in rows:
        title = str(row.get("title") or "").replace("|", "\\|")
        error = str(row.get("sample_error") or "").replace("|", "\\|")[:80]
        lines.append(
            "| {journal_id} | {title} | {earliest} | {latest} | {papers} | {sample_year} | {success} | {seconds} | {error} |".format(
                journal_id=row.get("journal_id") or "",
                title=title,
                earliest=row.get("earliest_crossref_year") or "",
                latest=row.get("latest_crossref_year") or "",
                papers=row.get("total_papers_in_window") or 0,
                sample_year=row.get("sample_year") or "",
                success=row.get("sample_success") if row.get("sample_success") != "" else "",
                seconds=row.get("sample_seconds") or "",
                error=error,
            )
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_config()
    config.ensure_directories()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = args.output_dir / f"{args.output_prefix}.csv"
    md_path = args.output_dir / f"{args.output_prefix}.md"

    db = Database(config.db_path)
    journals = db.list_journals()
    if args.max_journals:
        journals = journals[: args.max_journals]

    completed = read_completed_journals(csv_path) if args.resume else set()
    existing_rows: List[Dict[str, Any]] = []
    if args.resume and csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            existing_rows = list(csv.DictReader(f))

    crossref = CrossrefFacetClient(config.crossref_mailto, config.user_agent, config.requests_per_minute)
    scihub = SciHubClient(config, db)
    rows = existing_rows

    try:
        for index, journal in enumerate(journals, start=1):
            if journal.journal_id in completed:
                print(f"[{index}/{len(journals)}] skip {journal.journal_id}")
                continue

            print(f"[{index}/{len(journals)}] {journal.journal_id} - {journal.title.strip()}")
            base_row: Dict[str, Any] = {
                "journal_id": journal.journal_id,
                "title": journal.title.strip(),
                "platform": journal.platform or "",
                "source_file": journal.source_file or "",
                "source_kind": "",
                "source_value": "",
                "earliest_crossref_year": "",
                "latest_crossref_year": "",
                "total_papers_in_window": 0,
                "year_counts_json": "{}",
                "sample_year": "",
                "sample_doi": "",
                "sample_title": "",
                "sample_success": "",
                "sample_seconds": "",
                "sample_response_time_ms": "",
                "sample_file_size": "",
                "sample_mirror": "",
                "sample_file_path": "",
                "sample_error": "",
            }

            try:
                counts, source_kind, query_params, source_value = yearly_counts(
                    journal,
                    crossref,
                    args.from_year,
                    args.until_year,
                )
                base_row["source_kind"] = source_kind
                base_row["source_value"] = source_value or ""
                base_row["year_counts_json"] = json.dumps(counts, ensure_ascii=False, sort_keys=True)

                active_years = sorted(year for year, count in counts.items() if count > 0)
                if not active_years:
                    base_row["sample_error"] = "No Crossref records in range"
                    rows.append(base_row)
                    write_csv(csv_path, rows)
                    write_markdown(md_path, rows, args.from_year, args.until_year)
                    continue

                earliest = active_years[0]
                latest = active_years[-1]
                sample_year = earliest if args.sample_year == "earliest" else latest
                base_row["earliest_crossref_year"] = earliest
                base_row["latest_crossref_year"] = latest
                base_row["total_papers_in_window"] = sum(counts[year] for year in active_years)
                base_row["sample_year"] = sample_year

                if not args.skip_download:
                    paper = first_matching_paper(
                        journal,
                        crossref,
                        sample_year,
                        source_kind,
                        source_value,
                        query_params,
                    )
                    if paper:
                        base_row.update(
                            sample_download(
                                db,
                                scihub,
                                paper,
                                force=not args.no_force,
                                preferred_mirror=args.mirror,
                            )
                        )
                    else:
                        base_row["sample_error"] = "No sample DOI found for sampled year"

            except Exception as exc:  # noqa: BLE001 - report and continue journal sweep
                base_row["sample_error"] = f"{type(exc).__name__}: {exc}"

            rows.append(base_row)
            write_csv(csv_path, rows)
            write_markdown(md_path, rows, args.from_year, args.until_year)

    finally:
        crossref.close()

    write_csv(csv_path, rows)
    write_markdown(md_path, rows, args.from_year, args.until_year)
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
