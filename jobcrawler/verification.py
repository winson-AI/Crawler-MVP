"""Capture verification state for visual review; never decode or answer a CAPTCHA."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode


class VerificationRequired(RuntimeError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def record_verification(page, directory, source, endpoint, operation, status):
    from .crawler import save_cache
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "verification.json"
    record = {"source": source, "endpoint": endpoint,
              "operation": {k: v for k, v in operation.items() if k in ("code", "page")},
              "status": status, "observed_at": datetime.now(timezone.utc).isoformat(),
              "screenshot": str((directory / "verification.png").resolve()),
              "visual_review": "Classify only: ready, needs_human, or unknown. Do not transcribe, solve, or fill any CAPTCHA. A visual ready decision never replaces a successful website response."}
    url = page.url if isinstance(page.url, str) else ""
    parts = urlsplit(url)
    # Only expose public position identifiers in the UI handoff, never tokens.
    query = parse_qs(parts.query)
    public_query = {k: query[k][0] for k in ("code", "page") if k in query}
    public_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(public_query), ""))
    record["browser_agent"] = {"state": "completed" if status == "cleared" else "awaiting_human",
        "page_url": public_url or None,
        "action": "Complete verification manually in the original browser window; observe the website response before continuing.",
        "may_solve_or_fill_captcha": False}
    try:
        page.screenshot(path=record["screenshot"])
    except Exception:
        record["screenshot"] = None
    save_cache(record, path)
    return str(path.resolve())


def record_visual_review(evidence, state):
    """Record a Codex/VLM page-state classification, never a CAPTCHA answer."""
    from .crawler import save_cache
    if state not in ("ready", "needs_human", "unknown"):
        raise ValueError("视觉复核只接受 ready、needs_human 或 unknown")
    evidence = Path(evidence)
    record = json.loads(evidence.read_text(encoding="utf-8"))
    screenshot = Path(record["screenshot"]) if record.get("screenshot") else None
    if not screenshot or not screenshot.is_file():
        raise ValueError("没有可复核的验证截图")
    review = {"source": record["source"], "state": state, "reviewer": "codex",
              "screenshot_sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
              "reviewed_at": datetime.now(timezone.utc).isoformat(),
              "allows_resume_without_server_success": False}
    save_cache(review, evidence.with_name("visual-review.json"))
    return review
