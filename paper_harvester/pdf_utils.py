"""Shared PDF file validation and hashing helpers."""
import hashlib
from pathlib import Path
from typing import Optional, Tuple


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 for a local file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def inspect_local_pdf(
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

    sha256 = calculate_sha256(file_path)
    if expected_sha256 and sha256.lower() != expected_sha256.lower():
        return False, file_size, sha256, "SHA256 does not match database record"

    return True, file_size, sha256, None
