import math
import re
import unicodedata
from urllib.parse import urlsplit

from .fields import normalize_fields, EDUCATION_LEVELS, OFFICE_MODES, EMPLOYMENT_TYPES


TECH_ALIASES = {
    "Python": ["python"], "Java": ["java"], "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript", "ts"], "Go": ["go", "golang"], "C++": ["c++", "cpp"],
    "C#": ["c#", "csharp"], "Kotlin": ["kotlin"], "Swift": ["swift"],
    "React": ["react", "reactjs"], "Vue": ["vue", "vuejs"], "Node.js": ["node.js", "nodejs"],
    "Spring": ["spring", "springboot", "spring boot"], "SQL": ["sql"],
    "MySQL": ["mysql"], "PostgreSQL": ["postgresql", "postgres"], "Redis": ["redis"],
    "Linux": ["linux"], "Docker": ["docker"], "Kubernetes": ["kubernetes", "k8s"],
    "Spark": ["spark"], "Hive": ["hive"], "Flink": ["flink"], "Kafka": ["kafka"],
    "PyTorch": ["pytorch"], "TensorFlow": ["tensorflow"], "Android": ["android"],
    "iOS": ["ios"], "大模型": ["大模型", "llm"], "RAG": ["rag"],
}


def fold(value):
    return unicodedata.normalize("NFKC", value).casefold()


def term_matches(term, text):
    term, text = fold(term), fold(text)
    # ASCII token boundaries prevent Java matching JavaScript and Go matching Google.
    left = r"(?<![a-z0-9_])" if term[0].isascii() else ""
    right = r"(?![a-z0-9_])" if term[-1].isascii() else ""
    return bool(re.search(left + re.escape(term) + right, text))


def tech_matches(term, text):
    aliases = next((values for name, values in TECH_ALIASES.items()
                    if fold(term) in [fold(name), *values]), [term])
    return any(term_matches(alias, text) for alias in aliases)


def parse_experience(raw, requirements):
    """Conservative extraction: use workYears first, then mandatory experience clauses."""
    num = r"(?:\d+(?:\.\d+)?|[零一二三四五六七八九十两]+)"
    number_words = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

    def number(text):
        if text in number_words:
            return number_words[text]
        if "十" in text:
            a, b = text.split("十", 1)
            return number_words.get(a, 1) * 10 + number_words.get(b, 0)
        return float(text)

    for source, text in [("workYears", raw), ("requirements", requirements)]:
        for clause in re.split(r"[\n；;。，,]", fold(text or "")):
            if source == "requirements" and ("优先" in clause or "经验" not in clause):
                continue
            if re.search(r"经验不限|不限经验|无.{0,3}经验要求|无需.{0,3}经验|应届|无经验", clause) or (
                    source == "workYears" and "不限" in clause):
                return {"min": 0, "max": None, "evidence": clause.strip(), "source": source}
            match = re.search(rf"({num})\s*(?:年\s*)?[-~～—–‐‑至到]\s*({num})\s*年", clause)
            if match:
                low, high = map(number, match.groups())
                if low <= high:
                    return {"min": low, "max": high, "evidence": match[0], "source": source}
            match = re.search(rf"({num})\s*年\s*(以上|及以上|起|以下|以内|及以下)?", clause)
            if match:
                # A graduation date or a project duration is not a work experience requirement.
                tail = clause[match.end():]
                prefix = clause[max(0, match.start() - 12):match.start()]
                if source == "requirements" and ("经验" not in tail + prefix or "项目周期" in prefix):
                    continue
                years, suffix = number(match[1]), match[2]
                if suffix in ("以下", "以内", "及以下"):
                    low, high = 0, years
                else:
                    low, high = years, None
                return {"min": low, "max": high, "evidence": match[0], "source": source}
    return {"min": None, "max": None, "evidence": "", "source": "unknown"}


def job_recruitment_type(job, source_url=""):
    """Use explicit labels or known source scope, never experience/requirements."""
    aliases = {"campus": "campus", "校招": "campus", "校园招聘": "campus",
               "social": "social", "社招": "social", "社会招聘": "social"}
    explicit = fold(job.get("recruitment_type") or "").strip()
    if explicit and explicit != "unknown":
        return aliases.get(explicit, "unknown")
    labels = " ".join([job.get("title") or "", job.get("category") or ""])
    campus, social = bool(re.search(r"校招|校园招聘", labels)), bool(re.search(r"社招|社会招聘", labels))
    if campus or social:
        return "unknown" if campus and social else "campus" if campus else "social"
    for url in (source_url, job.get("url") or ""):
        if urlsplit(url).hostname in ("campus.51job.com", "campus.lagou.com"):
            return "campus"
    if job.get("source") in ("baidu", "alibaba", "tencent", "meituan", "bytedance", "jd", "pinduoduo"):
        return "social"
    return "unknown"


def validate_selector(value, _preferences=False):
    if not isinstance(value, dict):
        raise ValueError("selector 必须是 JSON 对象")
    allowed = {"locations", "technologies", "technology_mode", "keywords", "keyword_mode",
               "exclude_keywords", "years", "include_unknown_experience", "recruitment_type",
               "salary_min", "salary_currency", "salary_period", "education", "office_modes", "employment_types"}
    if not _preferences:
        allowed.add("preferences")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"未知 selector 字段: {', '.join(sorted(unknown))}")
    result = dict(value)
    result["recruitment_type"] = value.get("recruitment_type", "all")
    if result["recruitment_type"] not in ("all", "campus", "social", "unknown"):
        raise ValueError("recruitment_type 必须为 all/campus/social/unknown（不限/校招/社招/未知）")
    for key in ("locations", "technologies", "keywords", "exclude_keywords"):
        terms = value.get(key, [])
        if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
            raise ValueError(f"{key} 必须是非空字符串数组")
        result[key] = [t.strip() for t in terms]
    for key in ("technology_mode", "keyword_mode"):
        result[key] = value.get(key, "all")
        if result[key] not in ("all", "any"):
            raise ValueError(f"{key} 必须为 all 或 any")
    years = value.get("years")
    if years is not None and (isinstance(years, bool) or not isinstance(years, (int, float))
                              or not 0 <= years <= 80):
        raise ValueError("years 必须为 0–80 之间的工作年限")
    result["years"] = years
    result["include_unknown_experience"] = value.get("include_unknown_experience", False)
    if not isinstance(result["include_unknown_experience"], bool):
        raise ValueError("include_unknown_experience 必须为布尔值")
    salary = value.get("salary_min")
    if salary is not None and (type(salary) not in (int, float) or not math.isfinite(salary) or not 0 < salary <= 10**9):
        raise ValueError("salary_min 必须为大于 0 的有限薪资数值")
    result.update(salary_min=salary, salary_currency=value.get("salary_currency", "CNY"), salary_period=value.get("salary_period", "month"), education=value.get("education"))
    if result["salary_currency"] not in ("CNY", "USD", "EUR", "GBP", "JPY", "HKD") or result["salary_period"] not in ("month", "year"):
        raise ValueError("薪资币种或周期无效")
    if result["education"] is not None and result["education"] not in EDUCATION_LEVELS:
        raise ValueError("学历值无效")
    for key, values in (("office_modes", OFFICE_MODES), ("employment_types", EMPLOYMENT_TYPES)):
        terms = value.get(key, [])
        if not isinstance(terms, list) or any(t not in values for t in terms):
            raise ValueError(key + " 值无效")
        result[key] = list(dict.fromkeys(terms))
    if not _preferences:
        preferences = value.get("preferences", {})
        if not isinstance(preferences, dict) or preferences.get("exclude_keywords"):
            raise ValueError("偏好必须是条件对象；排除关键词始终属于硬条件")
        result["preferences"] = validate_selector(preferences, _preferences=True) if preferences else {}
    if _preferences:
        active = {}
        for key, dependencies in (("recruitment_type", ()), ("locations", ()), ("years", ("include_unknown_experience",)),
                ("technologies", ("technology_mode",)), ("keywords", ("keyword_mode",)),
                ("salary_min", ("salary_currency", "salary_period")), ("education", ()), ("office_modes", ()), ("employment_types", ())):
            if result[key] not in (None, [], "all"):
                active.update({k: result[k] for k in (key, *dependencies)})
        return active
    return result


def select_jobs(jobs, selector, include_uncertain=False):
    if include_uncertain:
        s = validate_selector(selector)
        return [explain_match(job, s) for job in _select_with_uncertainty(jobs, s)]
    s = validate_selector(selector)
    selected = []
    for job in jobs:
        job = {**job, **normalize_fields(job)}
        recruitment_type = job_recruitment_type(job)
        if s["recruitment_type"] != "all" and recruitment_type != s["recruitment_type"]:
            continue
        if s["locations"] and not any(fold(city.removesuffix("市")) in (
                                      fold(place.removesuffix("市")), fold(place.split("·", 1)[0].removesuffix("市")))
                                      for city in s["locations"] for place in job["locations"]):
            continue
        exp, years = job["experience"], s["years"]
        if years is not None:
            if exp["min"] is None:
                if not s["include_unknown_experience"]:
                    continue
            elif years < exp["min"] or (exp["max"] is not None and years > exp["max"]):
                continue
        text = "\n".join([job["title"], job["requirements"], job["description"]])
        matched_tech = [term for term in s["technologies"] if tech_matches(term, text)]
        matched_words = [term for term in s["keywords"] if fold(term) in fold(text)]
        if any(fold(term) in fold(text) for term in s["exclude_keywords"]):
            continue
        if s["technologies"] and (len(matched_tech) != len(s["technologies"]) if s["technology_mode"] == "all" else not matched_tech):
            continue
        if s["keywords"] and (len(matched_words) != len(s["keywords"]) if s["keyword_mode"] == "all" else not matched_words):
            continue
        if any(check["status"] != "matched" for check in fact_checks(job, s)):
            continue
        selected.append({**job, "recruitment_type": recruitment_type,
                         "matches": {"technologies": matched_tech, "keywords": matched_words}})
    return [explain_match(job, s) for job in selected]


def _select_with_uncertainty(jobs, selector):
    """Reuse strict selection after relaxing only conditions without evidence."""
    selected = []
    for job in jobs:
        job = {**job, **normalize_fields(job)}
        relaxed, reasons = dict(selector), []
        pending = bool(job.get("details_pending"))
        if selector["locations"] and not job["locations"]:
            relaxed["locations"] = []
            reasons.append("工作地点未知")
        if selector["recruitment_type"] not in ("all", "unknown") and job_recruitment_type(job) == "unknown":
            relaxed["recruitment_type"] = "all"
            reasons.append("招聘类型未知")
        if selector["years"] is not None and (pending or job["experience"]["min"] is None):
            relaxed["years"] = None
            reasons.append("工作年限待确认")
        for key, mode, match in (("technologies", "technology_mode", tech_matches),
                                  ("keywords", "keyword_mode", lambda term, text: fold(term) in fold(text))):
            terms = selector[key]
            title_hits = [term for term in terms if match(term, job["title"])]
            title_sufficient = bool(title_hits) if selector[mode] == "any" else len(title_hits) == len(terms)
            if terms and not title_sufficient and (pending or not (job["requirements"] or job["description"])):
                relaxed[key] = []
                reasons.append("技术栈待确认" if key == "technologies" else "关键词待确认")
        if selector["exclude_keywords"] and (pending or not (job["requirements"] or job["description"])):
            # A match in the current title is definite; an old/missing body is not.
            if not any(fold(term) in fold(job["title"]) for term in selector["exclude_keywords"]):
                relaxed["exclude_keywords"] = []
                reasons.append("排除关键词待确认")
        for check in fact_checks(job, selector):
            if check["status"] == "unknown":
                key = check["condition"]
                relaxed[key] = None if key in ("salary_min", "education") else []
                reasons.append(check["label"] + "待确认")
        result = select_jobs([job], relaxed)
        if result:
            item = result[0]
            text = job["title"] if pending else "\n".join((job["title"], job["requirements"], job["description"]))
            item["matches"] = {"technologies": [t for t in selector["technologies"] if tech_matches(t, text)],
                               "keywords": [t for t in selector["keywords"] if fold(t) in fold(text)]}
            item.update(match_state="uncertain" if reasons else "matched", uncertainty_reasons=reasons)
            selected.append(item)
    return selected


def fact_checks(job, selector):
    checks = []
    for key, label in (("salary_min", "薪资"), ("education", "学历"), ("office_modes", "办公方式"), ("employment_types", "用工类型")):
        wanted = selector.get(key)
        if wanted is None or wanted == []:
            continue
        status = None
        evidence_key = {"salary_min": "salary", "office_modes": "office_mode", "employment_types": "employment_type"}.get(key, key)
        evidence = job["field_evidence"][evidence_key]["text"]
        if key == "salary_min":
            salary = job["salary"]
            if salary["currency"] == selector["salary_currency"] and salary["period"] == selector["salary_period"]:
                if salary["max"] is not None:
                    status = salary["max"] >= wanted
                elif salary["min"] is not None and salary["min"] >= wanted:
                    status = True
        elif key == "education":
            required = job["education"]["level"]
            if job["education"].get("unrestricted"):
                status = True
            elif required is not None:
                status = EDUCATION_LEVELS.index(wanted) >= EDUCATION_LEVELS.index(required)
        else:
            value = job[evidence_key]
            if value != "unknown":
                status = value in wanted
        if job.get("details_pending") and job["field_evidence"][evidence_key]["basis"] == "text":
            status, evidence = None, "详情待补，旧正文不能确认"
        checks.append({"condition": key, "label": label, "requested": wanted, "status": "unknown" if status is None else "matched" if status else "unmet", "evidence": evidence})
    return checks


def condition_checks(job, selector):
    checks = fact_checks(job, selector)
    def add(key, label, status, evidence):
        checks.append({"condition": key, "label": label, "requested": selector.get(key), "status": "unknown" if status is None else "matched" if status else "unmet", "evidence": evidence})
    if selector.get("locations"):
        add("locations", "地点", None if not job["locations"] else any(fold(city.removesuffix("市")) in (fold(place.removesuffix("市")), fold(place.split("·", 1)[0].removesuffix("市"))) for city in selector["locations"] for place in job["locations"]), " / ".join(job["locations"]))
    if selector.get("years") is not None:
        e = job["experience"]
        add("years", "工作年限", None if e["min"] is None or job.get("details_pending") else e["min"] <= selector["years"] and (e["max"] is None or selector["years"] <= e["max"]), e.get("evidence", ""))
    if selector.get("recruitment_type", "all") != "all":
        kind = job_recruitment_type(job)
        add("recruitment_type", "招聘类型", None if kind == "unknown" else kind == selector["recruitment_type"], kind)
    for key, mode, label, match in (("technologies", "technology_mode", "技术栈", tech_matches), ("keywords", "keyword_mode", "关键词", lambda term, text: fold(term) in fold(text))):
        terms = selector.get(key, [])
        if not terms:
            continue
        text = job["title"] if job.get("details_pending") else "\n".join((job["title"], job["requirements"], job["description"]))
        hits = [t for t in terms if match(t, text)]
        matched = len(hits) == len(terms) if selector.get(mode, "all") == "all" else bool(hits)
        status = True if matched else None if job.get("details_pending") or not (job["requirements"] or job["description"]) else False
        excerpt = next((line.strip()[:180] for line in text.splitlines() if any(match(t, line) for t in hits)), "")
        add(key, label, status, ("命中：" + "、".join(hits) + "；" + excerpt) if hits else "没有已确认的命中证据")
    if selector.get("exclude_keywords"):
        text = job["title"] if job.get("details_pending") else "\n".join((job["title"], job["requirements"], job["description"]))
        hits = [t for t in selector["exclude_keywords"] if fold(t) in fold(text)]
        add("exclude_keywords", "排除关键词", False if hits else None if job.get("details_pending") else True,
            "命中：" + "、".join(hits) if hits else "未发现排除词" if not job.get("details_pending") else "详情待补，旧正文不能确认")
    return checks


def explain_match(job, selector):
    job = {**job, **normalize_fields(job)}
    hard = condition_checks(job, selector)
    preference = condition_checks(job, selector.get("preferences", {}))
    hits = sum(c["status"] == "matched" for c in preference)
    return {**job, "hard_explanation": hard, "preference_explanation": preference,
            "match_score": round(100 * hits / len(preference), 2) if preference else None,
            "score_basis": "偏好条件等权计分；未知与未满足均不加分，不代表录用概率"}
