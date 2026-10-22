import re
import unicodedata
from urllib.parse import urlsplit


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


def validate_selector(value):
    if not isinstance(value, dict):
        raise ValueError("selector 必须是 JSON 对象")
    allowed = {"locations", "technologies", "technology_mode", "keywords", "keyword_mode",
               "exclude_keywords", "years", "include_unknown_experience", "recruitment_type"}
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
    return result


def select_jobs(jobs, selector):
    s = validate_selector(selector)
    selected = []
    for job in jobs:
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
        selected.append({**job, "recruitment_type": recruitment_type,
                         "matches": {"technologies": matched_tech, "keywords": matched_words}})
    return selected
