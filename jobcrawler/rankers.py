from datetime import datetime, timezone

RANKERS = ("published_desc", "published_asc", "experience_asc", "experience_desc", "relevance_desc")
DEFAULT_RANKER = "published_desc"


def publication_time(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def rank_jobs(jobs, ranker=DEFAULT_RANKER):
    """Stable ranking; relevance ties prefer evidence, then publication time."""
    if ranker not in RANKERS:
        raise ValueError(f"未知 ranker {ranker!r}；可选：{', '.join(RANKERS)}")
    if ranker == "relevance_desc":
        def publication_key(job):
            value = publication_time(job.get("published_at"))
            return -value if value is not None else float("inf")
        return sorted(jobs, key=lambda j: (-(j.get("match_score") if j.get("match_score") is not None else -1),
            sum(c["status"] == "unknown" for c in j.get("preference_explanation", [])),
            publication_key(j), j.get("source", ""), str(j.get("id", ""))))
    known, unknown = [], []
    for job in jobs:
        value = publication_time(job.get("published_at")) if ranker.startswith("published_") else job["experience"]["min"]
        if value is None:
            unknown.append(job)
        else:
            known.append((value, job))
    known.sort(key=lambda pair: pair[0], reverse=ranker.endswith("_desc"))
    return [job for _, job in known] + unknown
