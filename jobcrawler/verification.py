"""Capture verification state for visual review; never decode or answer a CAPTCHA."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import base64
import os
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode


class VerificationRequired(RuntimeError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def record_verification(page, directory, source, endpoint, operation, status, acquisition=None):
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
    if acquisition is not None:
        record["acquisition"] = {key: acquisition[key] for key in (
            "request_delay", "listing_network_pages", "listing_setup_requests", "listing_page_reused",
            "detail_requests", "verification_handoffs", "verification_cleared") if key in acquisition}
    try:
        page.screenshot(path=record["screenshot"])
    except Exception:
        record["screenshot"] = None
    save_cache(record, path)
    # Keep every handoff/clear event; verification.json and its screenshot are latest-only.
    event = {key: record[key] for key in ("source", "endpoint", "operation", "status",
        "observed_at", "browser_agent", "acquisition") if key in record}
    with (directory / "verification-events.jsonl").open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(event, ensure_ascii=False) + "\n")
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


def ai_visual_review(evidence, api_key=None, model=None, endpoint=None):
    """Ask a vision model to classify a verification popup, never to solve it.

    The model receives only the screenshot and a constrained classification prompt.
    Its result is advisory: it cannot resume a crawl or replace a successful
    website response.
    """
    from .crawler import save_cache
    evidence = Path(evidence)
    record = json.loads(evidence.read_text(encoding="utf-8"))
    screenshot = Path(record["screenshot"]) if record.get("screenshot") else None
    if not screenshot or not screenshot.is_file():
        raise ValueError("没有可复核的验证截图")
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("AI 复核需要 OPENAI_API_KEY")
    model = model or os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini")
    endpoint = endpoint or os.environ.get("OPENAI_API_ENDPOINT", "https://api.openai.com/v1/responses")
    image = base64.b64encode(screenshot.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": (
                "Classify this browser verification screenshot only. Return JSON "
                "with exactly one key state whose value is ready, needs_human, or unknown. "
                "Do not read, transcribe, solve, calculate, click, fill, or submit any CAPTCHA. "
                "Use needs_human when an interactive verification or CAPTCHA is visible; "
                "ready only when no user action appears required; otherwise unknown.")},
            {"type": "input_image", "image_url": "data:image/png;base64," + image},
        ]}],
        "text": {"format": {"type": "json_object"}},
    }
    request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    text = result.get("output_text", "")
    if not text:
        for item in result.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text += content.get("text", "")
    try:
        state = json.loads(text)["state"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("AI 复核返回了无效分类") from exc
    if state not in ("ready", "needs_human", "unknown"):
        raise RuntimeError("AI 复核返回了不支持的状态")
    review = {"source": record["source"], "state": state, "reviewer": "ai",
              "model": model, "screenshot_sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
              "reviewed_at": datetime.now(timezone.utc).isoformat(),
              "allows_resume_without_server_success": False}
    save_cache(review, evidence.with_name("visual-review.json"))
    return review
