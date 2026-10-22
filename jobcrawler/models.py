from .selectors import TECH_ALIASES, job_recruitment_type, parse_experience, tech_matches


def build_job(*, id, title, source, company, url, locations, requirements="", description="",
              published_at="", updated_at="", category="", department="", work_years="",
              experience=None, recruitment_type="", source_url=""):
    """Common job schema for every source; dates use ISO 8601, unknown dates stay empty."""
    if not id or not title:
        raise ValueError("职位缺少 id 或 title；数据结构可能已变化")
    text = "\n".join([title, requirements, description])
    return {
        "id": str(id), "title": title, "source": source, "company": company, "url": url,
        "locations": list(locations), "requirements": requirements, "description": description,
        "published_at": published_at, "updated_at": updated_at, "category": category,
        "department": department,
        "recruitment_type": job_recruitment_type({"recruitment_type": recruitment_type,
            "title": title, "category": category, "url": url, "source": source}, source_url),
        "experience": experience if experience is not None else parse_experience(work_years, requirements),
        "technologies": [name for name in TECH_ALIASES if tech_matches(name, text)],
    }
