"""Unified cached search; selectors, ranking and exports keep one shared path."""
from datetime import datetime, timezone

from .crawler import cache_path, load_cache
from .rankers import rank_jobs, publication_time
from .selectors import select_jobs
from .fields import normalize_fields


def enrich_job(job, snapshot, favorite=None, archived=False):
    observed = job.get("last_seen_at") or snapshot.get("listing_fetched_at") or snapshot.get("fetched_at")
    timestamp = publication_time(observed)
    age = datetime.now(timezone.utc).timestamp() - timestamp if timestamp is not None else None
    freshness = "unknown" if age is None or age < 0 else "stale" if age > 72 * 3600 else "recent"
    return {**job, **normalize_fields(job), "first_seen_at": job.get("first_seen_at"), "last_seen_at": observed,
            "details_fetched_at": job.get("details_fetched_at"), "freshness_status": freshness,
            "observation_basis": job.get("observation_basis", "job" if job.get("last_seen_at") else "snapshot"),
            "source_name": snapshot.get("source_name", job.get("source_name", job["source"])),
            "source_scope": snapshot.get("scope", job.get("source_scope", "listing")),
            "favorite": favorite is not None, "saved_at": favorite.get("saved_at") if favorite else None,
            "archived": archived}


def search_jobs(sources, selector, ranker, cache, favorites=(), only_favorites=False, include_uncertain=False):
    saved = {(row["source"], row["id"]): row for row in favorites}
    found, snapshots, errors = {}, [], []
    for source in sources:
        try:
            snapshot = load_cache(cache_path(source, cache), source)
            snapshots.append({k: v for k, v in snapshot.items() if k != "jobs"})
            for job in snapshot["jobs"]:
                key = (source, job["id"])
                if only_favorites and key not in saved:
                    continue
                found[key] = enrich_job(job, snapshot, saved.get(key))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({"source": source, "error": str(exc)})
    # Saved snapshots remain readable when a listing disappears or its source fails.
    if only_favorites:
        for key, row in saved.items():
            if key not in found and (key[0] in sources or not sources):
                found[key] = enrich_job(row["job"], {}, row, archived=True)
    jobs = select_jobs(list(found.values()), selector, include_uncertain=include_uncertain)
    if not include_uncertain:
        jobs = [{**j, "match_state": "matched", "uncertainty_reasons": []} for j in jobs]
    jobs = rank_jobs(jobs, ranker)
    return {"total": len(jobs), "matched_count": sum(j["match_state"] == "matched" for j in jobs),
            "uncertain_count": sum(j["match_state"] == "uncertain" for j in jobs),
            "sources": sources, "ranker": ranker, "selector": selector,
            "snapshots": snapshots, "source_errors": errors, "jobs": jobs}
