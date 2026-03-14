"""Extract text from CV PDF and cache it."""

from pathlib import Path

import pdfplumber

from auto_apply.config import CV_CACHE_PATH


def extract_cv_text(cv_path: str | None = None, force: bool = False) -> str:
    """Extract text from CV PDF, using cache if available.

    Args:
        cv_path: Path to the PDF. If None, loads from config.json.
        force: If True, re-extract even if cache exists.

    Returns:
        Extracted text content.
    """
    if not force and CV_CACHE_PATH.exists():
        return CV_CACHE_PATH.read_text(encoding="utf-8")

    if cv_path is None:
        from auto_apply.config import load_config
        cv_path = load_config().get("cv_path", "")

    path = Path(cv_path)
    if not path.exists():
        raise FileNotFoundError(f"CV not found at {cv_path}")

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)

    full_text = "\n\n".join(pages)
    CV_CACHE_PATH.write_text(full_text, encoding="utf-8")
    return full_text
