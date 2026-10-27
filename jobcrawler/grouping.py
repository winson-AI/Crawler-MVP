"""Presentation-only suspected duplicates; original identities remain intact."""
import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import urldefrag

from .fields import normalize_fields


def normalized(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def group_key(job):
    return (normalized(job.get("company")), normalized(job.get("title")),
            tuple(sorted(normalized(p.removesuffix("市")) for p in job.get("locations", []))),
            normalized(job.get("department")), job.get("recruitment_type", "unknown"))


def duplicate_basis(a, b):
    for key in ("office_mode", "employment_type"):
        if a[key] != "unknown" and b[key] != "unknown" and a[key] != b[key]:
            return None
    if a["education"]["level"] and b["education"]["level"] and a["education"]["level"] != b["education"]["level"]:
        return None
    if a["salary"]["raw"] and b["salary"]["raw"] and a["salary"] != b["salary"]:
        return None
    if a.get("experience", {}).get("min") is not None and b.get("experience", {}).get("min") is not None and a["experience"] != b["experience"]:
        return None
    first = normalized(a.get("requirements")) + "\n" + normalized(a.get("description"))
    second = normalized(b.get("requirements")) + "\n" + normalized(b.get("description"))
    if len(first.strip()) < 80 or len(second.strip()) < 80 or a.get("details_pending") or b.get("details_pending"):
        return None
    same_url = bool(a.get("url")) and urldefrag(a["url"])[0] == urldefrag(b.get("url", ""))[0]
    if first == second:
        return "公司、职位、地点、部门一致，职责与要求一致；需人工确认"
    if same_url and SequenceMatcher(None, first, second, autojunk=False).ratio() >= .96:
        return "职位链接相同，职责与要求高度相近；需人工确认"
    return None


def collapse_jobs(jobs):
    buckets, groups = {}, []
    for original in jobs:
        job = {**original, **normalize_fields(original)}
        key = group_key(job)
        candidates = buckets.setdefault(key, []) if key[0] and key[1] and key[2] else []
        for group in candidates:
            # Every member must agree, preventing transitive conflict merges.
            basis = all(duplicate_basis(member, job) for member in group)
            if basis:
                group.append(job)
                break
        else:
            group = [job]
            candidates.append(group)
            groups.append(group)
    output = []
    for group in groups:
        if len(group) == 1:
            output.append(group[0])
            continue
        ident = hashlib.sha256(repr(sorted((j["source"], j["id"]) for j in group)).encode()).hexdigest()[:16]
        output.append({**group[0], "duplicate_group": ident, "duplicate_count": len(group),
                       "duplicate_basis": duplicate_basis(group[0], group[1]), "duplicate_jobs": group})
    return output


def collapse_result(result, enabled):
    result["raw_total"] = len(result["jobs"])
    result["collapse_duplicates"] = enabled
    if enabled:
        result["jobs"] = collapse_jobs(result["jobs"])
    result["total"] = len(result["jobs"])
    return result
