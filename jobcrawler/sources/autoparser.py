"""Infer declarative rules from the supplied document, not from guessed job data."""
import json
import re

from .custom import path_value, schema_rows, soup

ALIASES = {
    "id": ("id", "jobid", "positionid", "postid", "requisitionid"),
    "title": ("title", "jobtitle", "jobname", "positionname", "positiontitle", "postname", "vacancytitle", "text"),
    "url": ("url", "joburl", "jobinfourlpc", "jobinfourl", "absoluteurl", "hostedurl", "detailurl", "joblink", "link"),
    "company": ("company", "companyname", "hiringorganizationname", "employername"),
    "locations": ("locations", "location", "city", "cityname", "jobareastring", "workplace", "jobcity", "locationname"),
    "description": ("description", "descriptionplain", "descriptionhtml", "jobdescription", "responsibilities", "content"),
    "requirements": ("requirements", "qualifications", "jobrequirements", "requirement"),
    "published_at": ("publishedat", "dateposted", "publishtime", "publishdate", "createdat"),
    "updated_at": ("updatedat", "updatetime"),
    "department": ("department", "departmentname"),
    "recruitment_type": ("recruitmenttype",),
    "work_years": ("workyears", "workyearstring", "experience", "experiencerequirement"),
}


def key_name(value):
    return re.sub(r"[_\W]", "", value).lower()


def leaves(value, prefix="", depth=0):
    if not isinstance(value, dict) or depth > 4:
        return
    for key, item in value.items():
        if not isinstance(key, str) or "." in key:
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            yield from leaves(item, path, depth + 1)
        else:
            yield path, item


def arrays(value, prefix="", depth=0):
    if depth > 8:
        return
    if isinstance(value, list):
        if value and all(isinstance(row, dict) for row in value):
            yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and "." not in key:
                yield from arrays(item, f"{prefix}.{key}" if prefix else key, depth + 1)


def json_rules(data):
    if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
        return
    for items, rows in arrays(data):
        fields = {}
        first = list(leaves(rows[0]))
        for field, aliases in ALIASES.items():
            for alias in aliases:
                matches = [p for p, v in first if key_name(p.split(".")[-1]) == alias
                           or key_name(p) == alias]
                matches.sort(key=lambda p: p.count("."))
                if not matches:
                    continue
                path = matches[0]
                values = [path_value(row, path) for row in rows]
                if all(v is None or isinstance(v, (str, int, float)) and not isinstance(v, bool)
                       or field == "locations" and isinstance(v, list) and all(isinstance(s, str) for s in v)
                       for v in values):
                    fields[field] = path
                    break
        if not {"title", "url"} <= set(fields):
            continue
        if not all(isinstance(path_value(row, fields[k]), str) and path_value(row, fields[k]).strip()
                   for row in rows for k in ("title", "url")):
            continue
        # An arbitrary article/menu/message array with title+URL is not a job list.
        evidence = (re.search(r"job|position|posting|vacanc", items, re.I)
                    or key_name(fields["title"].split(".")[-1]) not in ("title", "text")
                    or key_name(fields["url"].split(".")[-1]) == "hostedurl"
                    or {"company", "locations"} <= set(fields)
                    or all(re.search(r"/(?:jobs?|positions?|careers?|postings?|vacanc\w*)/", path_value(row, fields["url"]), re.I)
                           for row in rows))
        if evidence:
            yield {"parser": "json", "items": items, "fields": fields}


def candidate_rules(document, base=None):
    try:
        data = json.loads(document)
    except ValueError:
        data = None
    if data is not None:
        if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
            return
        if schema_rows(data):
            yield {"parser": "schema"}
        yield from json_rules(data)
        return
    html = soup(document)
    if base:
        from .templates import resolve_template
        matched = resolve_template(base)
        config = matched["config"]
        if matched["template"]["kind"] in ("public_dom", "browser_dom", "public_browser_dom") and config.get("items") and html.select(config["items"]):
            yield {k: config[k] for k in ("parser", "items", "fields")}
    if html.select('script[type="application/ld+json"]'):
        yield {"parser": "schema"}
    for number, node in enumerate(html.find_all('script')):
        if node.get("type") != "application/json" and node.get("id") != "__NEXT_DATA__":
            continue
        try:
            data = json.loads(node.get_text())
        except ValueError:
            continue
        for rule in json_rules(data):
            yield {**rule, "json_script": number}
    # Only explicit job-detail links with a separate title, never category links.
    selector = 'a[href*="/wn/jobs/"][title]'
    if html.select(selector):
        yield {"parser": "dom", "items": selector, "fields": {
            "title": {"selector": "", "attr": "title"},
            "url": {"selector": "", "attr": "href"}}}
    selector = 'a[href*="/job_detail/"]:has(.job-name)'
    if html.select(selector):
        yield {"parser": "dom", "items": selector, "fields": {
            "title": '.job-name', "url": {"selector": "", "attr": "href"},
            "locations": '.job-area', "work_years": '.tag-list li:first-child'}}
    # ponytail: infer semantic cards only; opaque/minified DOM needs explicit selectors.
    for selector in ('.job-card', '.job-item', '.job-list-item', '.position-card', '.position-item',
                     '.posting', '[data-job-id]', '[itemtype$="/JobPosting"]'):
        cards = html.select(selector)
        if not cards:
            continue
        rules = {"title": '[itemprop="title"], .job-title, .position-title, h2, h3',
                 "url": {"selector": 'a.job-title, a.position-title, a[href*="/job/"], a[href*="/jobs/"], a[href*="/position/"], a[itemprop="url"]', "attr": "href"},
                 "company": '.company, .company-name, [itemprop="hiringOrganization"]',
                 "locations": '.location, .job-location, [itemprop="jobLocation"]',
                 "description": '.description, .job-description, [itemprop="description"]',
                 "requirements": '.requirements, [itemprop="qualifications"]',
                 "work_years": '.experience, .work-years'}
        yield {"parser": "dom", "items": selector, "fields": rules}
