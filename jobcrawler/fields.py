"""Conservative job facts: explicit source values or quoted requirement evidence."""
import json
import math
import re
from functools import lru_cache

EDUCATION_LEVELS = ("secondary", "associate", "bachelor", "master", "doctorate")
OFFICE_MODES = ("onsite", "hybrid", "remote")
EMPLOYMENT_TYPES = ("full_time", "part_time", "intern", "contract")


def text_value(value):
    if isinstance(value, dict):
        return str(value.get("i18n_name") or value.get("name") or "")
    return value.strip() if isinstance(value, str) else ""


def source_fields(raw):
    """Only named semantic fields; numeric education/recruitment codes stay unknown."""
    aliases = {"salary": ("salary", "salaryDesc", "salaryRange", "baseSalary"),
               "education": ("educationName", "RequireEducationName", "degreeName", "required_degree", "degree", "education"),
               "office_mode": ("office_mode", "officeMode", "workMode", "jobLocationType"),
               "employment_type": ("employment_type", "employmentType", "employmentTypeName")}
    values = {}
    for key, names in aliases.items():
        for name in names:
            value = raw.get(name)
            if key == "salary" and isinstance(value, dict):
                values[key] = value
                break
            text = text_value(value)
            if text:
                values[key] = text
                break
    return values


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def parse_salary(raw):
    result = {"raw": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True),
              "min": None, "max": None, "currency": None, "period": None, "pay_months": None}
    if isinstance(raw, dict):
        value = raw.get("value", raw)
        if isinstance(value, dict):
            unit = str(value.get("unitText", raw.get("period", ""))).lower()
            result.update(min=number(value.get("minValue", value.get("min", value.get("value")))),
                          max=number(value.get("maxValue", value.get("max", value.get("value")))),
                          currency=raw.get("currency") if raw.get("currency") in ("CNY", "USD", "EUR", "GBP", "JPY", "HKD") else None,
                          period={"month": "month", "monthly": "month", "year": "year", "yearly": "year", "annual": "year"}.get(unit))
    else:
        text = str(raw or "").replace(",", "").strip()
        currency = re.search(r"人民币|RMB|CNY|元|USD|美元|EUR|欧元|GBP|英镑|JPY|日元|HKD|港币", text, re.I)
        if currency:
            token = currency[0].upper()
            result["currency"] = next((c for c, words in (("USD", ("USD", "美元")), ("EUR", ("EUR", "欧元")), ("GBP", ("GBP", "英镑")), ("JPY", ("JPY", "日元")), ("HKD", ("HKD", "港币"))) if token in words), "CNY")
        result["period"] = "month" if re.search(r"月薪|/月|每月|/month|per month|monthly", text, re.I) else "year" if re.search(r"年薪|/年|每年|/year|per year|annual|yearly", text, re.I) else None
        months = re.search(r"(?:[·*×x]\s*)?(\d{2})\s*薪", text)
        if months and 12 <= int(months[1]) <= 24:
            result["pay_months"] = int(months[1])
        text = re.sub(r"\d{2}\s*薪", "", text)
        match = re.search(r"(\d+(?:\.\d+)?)\s*([kKwW万千]?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*([kKwW万千]?)", text)
        units = {"k": 1000, "千": 1000, "w": 10000, "万": 10000, "": 1}
        if match and (re.match(r"\s*(?:年(?:经验|工作)|years? (?:of )?experience)", text[match.end():], re.I) or not (match[2] or match[4] or (result["currency"] and result["period"]))):
            match = None
        if match:
            result["min"] = float(match[1]) * units[(match[2] or match[4]).lower()]
            result["max"] = float(match[3]) * units[(match[4] or match[2]).lower()]
        elif result["currency"] and result["period"] and not re.search(r"年经验|years? (?:of )?experience", text, re.I):
            match = re.search(r"(\d+(?:\.\d+)?)\s*([kKwW万千]?)", text)
            if match:
                result["min"] = float(match[1]) * units[match[2].lower()]
                result["max"] = None if re.search(r"以上|起|\+", text) else result["min"]
    if (result["min"] is not None and result["max"] is not None and result["min"] > result["max"]) or result["min"] == 0 or result["max"] == 0:
        result.update(min=None, max=None)
    return result


def education(raw, structured=False):
    text = str(raw or "")
    aliases = {"secondary": r"高中|中专|secondary|high school", "associate": r"大专|专科|associate",
               "bachelor": r"本科|学士|bachelor", "master": r"硕士|master", "doctorate": r"博士|doctorate|ph\.?d"}
    required, preferred, evidence = [], [], ""
    for level, pattern in aliases.items():
        for match in re.finditer(pattern, text, re.I):
            after, before = text[match.end():match.end()+20], text[max(0, match.start()-15):match.start()]
            if re.search(r"不限|不要求|无需|不限制", before + after[:4]):
                continue
            snippet = text[max(0, match.start()-10):match.end()+30].split("\n")[0]
            if re.match(r"(?:及以上|以上|或以上)?(?:学历|学位|毕业生)?\s*(?:者)?\s*(?:优先|preferred)|['’]s\s+degree\s+preferred", after, re.I):
                preferred.append(level)
            elif structured or re.match(r"(?:及以上|或以上|以上|学历|学位|毕业|或同等|['’]s\s+degree|\s+degree)", after) or re.search(r"学历[：:要求\s]*$|统招$|全日制$", before):
                required.append(level)
            evidence = evidence or snippet
    level = min(required, key=EDUCATION_LEVELS.index) if required else None
    unrestricted = bool(re.search(r"学历不限|不限学历|不要求学历|no degree required", text, re.I))
    return {"raw": text if structured else evidence or (re.search(r"学历不限|不限学历|不要求学历|no degree required", text, re.I)[0] if unrestricted else ""), "level": level, "unrestricted": unrestricted,
            "preferred": min(preferred, key=EDUCATION_LEVELS.index) if preferred else None}


@lru_cache(maxsize=32768)
def _facts(salary_raw, education_raw, office_raw, employment_raw, title, requirements, description):
    # Cached return values are copied by normalize_fields before use.
    salary_input = json.loads(salary_raw)
    salary_basis = "source" if salary_input else "unknown"
    if not salary_input:
        line = next((s.strip() for s in re.split(r"[\n；;]", requirements + "\n" + description) if re.search(r"薪资|月薪|年薪|salary|compensation", s, re.I)), "")
        salary_input = line
        salary_basis = "text" if line else "unknown"
    degree = education(education_raw or requirements, bool(education_raw))
    office_text = office_raw or requirements + "\n" + description
    office, office_evidence = "unknown", ""
    patterns = (("onsite", r"现场办公|到岗坐班|on[- ]site|onsite"),
                ("hybrid", r"(?:采用|支持|允许)混合办公|混合工作制|hybrid (?:work|role|working|position)"),
                ("remote", r"全远程|(?:支持|允许|采用)远程办公|居家办公|fully remote|work from home|remote position"))
    aliases = {"telecommute": "remote", "远程办公": "remote", "混合办公": "hybrid", "现场办公": "onsite"}
    explicit_office = aliases.get(office_raw.lower(), office_raw.lower())
    if explicit_office in OFFICE_MODES:
        office, office_evidence = explicit_office, office_raw
    else:
        for value, pattern in patterns:
            match = re.search(pattern, office_text, re.I)
            if match and not re.search(r"不(?:支持|接受|允许)?|不可|not (?:a )?|no ", office_text[max(0, match.start()-6):match.start()], re.I):
                office, office_evidence = value, match[0]
                break
    employment, employment_evidence = "unknown", ""
    if employment_raw.lower() in EMPLOYMENT_TYPES:
        employment, employment_evidence = employment_raw.lower(), employment_raw
    else:
        for value, pattern in (("full_time", r"全职|full[- ]time"), ("part_time", r"兼职|part[- ]time"), ("intern", r"实习生|\bintern(?:ship)?\b"), ("contract", r"合同工|contractor")):
            match = re.search(pattern, employment_raw or title, re.I)
            if match:
                employment, employment_evidence = value, match[0]
                break
    salary = parse_salary(salary_input or "")
    return {"salary": salary, "education": degree, "office_mode": office, "employment_type": employment,
            "field_evidence": {"salary": {"basis": salary_basis, "text": salary["raw"]},
                "education": {"basis": "source" if education_raw else "text" if degree["raw"] else "unknown", "text": degree["raw"]},
                "office_mode": {"basis": "source" if office_raw else "text" if office_evidence else "unknown", "text": office_evidence},
                "employment_type": {"basis": "source" if employment_raw else "text" if employment_evidence else "unknown", "text": employment_evidence}}}


def normalize_fields(job):
    values = []
    for key in ("salary", "education", "office_mode", "employment_type"):
        raw = job.get(key + "_raw")
        if raw is None:
            current = job.get(key)
            raw = current.get("raw", "") if isinstance(current, dict) else current or ""
            if job.get("field_evidence", {}).get(key, {}).get("basis") in ("text", "unknown") or raw == "unknown":
                raw = ""
        values.append(raw or "")
    facts = _facts(json.dumps(values[0], ensure_ascii=False, sort_keys=True), *(str(v) for v in values[1:]),
                   str(job.get("title") or ""), str(job.get("requirements") or ""), str(job.get("description") or ""))
    return {**facts, "salary": dict(facts["salary"]), "education": dict(facts["education"]),
            "field_evidence": {k: dict(v) for k, v in facts["field_evidence"].items()}}


CONTENT_FIELDS = ("id", "source", "title", "company", "url", "locations", "requirements", "description",
                  "published_at", "category", "department", "recruitment_type", "experience", "technologies", "details_pending",
                  "salary", "education", "office_mode", "employment_type")
FIELD_LABELS = {"title": "职位名称", "company": "公司", "url": "职位链接", "locations": "地点", "requirements": "任职要求",
                "description": "工作职责", "published_at": "发布时间", "category": "类别", "department": "部门",
                "recruitment_type": "招聘类型", "experience": "工作年限", "technologies": "技术栈", "details_pending": "详情完整性",
                "salary": "薪资", "education": "学历", "office_mode": "办公方式", "employment_type": "用工方式"}


def comparable_job(job):
    normalized = {**job, **normalize_fields(job), "details_pending": bool(job.get("details_pending"))}
    def clean(value):
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip()
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return sorted((clean(v) for v in value), key=lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True))
        return value
    return {k: clean(normalized.get(k)) for k in CONTENT_FIELDS}


def field_changes(before, after):
    from .selectors import job_recruitment_type
    old = comparable_job({**before, "recruitment_type": job_recruitment_type(before)})
    new = comparable_job({**after, "recruitment_type": job_recruitment_type(after)})
    return [{"field": key, "label": label, "before": old.get(key), "after": new.get(key)}
            for key, label in FIELD_LABELS.items() if old.get(key) != new.get(key)]
