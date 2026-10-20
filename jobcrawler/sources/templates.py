"""Researched templates and conservative URL binding; never execute downloaded code."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from .custom import public_url, validate_config


def template_catalog():
    return json.loads(Path(__file__).with_name("templates.json").read_text(encoding="utf-8"))


def resolve_template(url, configs=()):
    url = public_url(url)
    parts = urlsplit(url)
    host = parts.hostname.lower().rstrip(".")
    config = {"id": "custom_" + hashlib.sha256(url.encode()).hexdigest()[:16],
              "name": host, "url": url, "mode": "http", "parser": "auto", "auto_adapt": True}
    for saved in configs:
        saved = validate_config(saved)
        old = urlsplit(saved["url"])
        if saved["url"] == url or (old.netloc == parts.netloc and old.path == parts.path
                and old.scheme == parts.scheme and not saved.get("fetch_url") and not saved.get("pagination")):
            reused = dict(saved)
            reused.update(url=url, id=saved["id"] if saved["url"] == url else config["id"])
            return {"template": {"id": "learned", "name": saved["name"], "kind": "learned"},
                    "config": reused, "warnings": ["复用本机已校验映射；仍需对新文档做字段校验"]}
    template = next((t for t in template_catalog()["templates"] if any(
        host == h or (t.get("subdomains") and host.endswith("." + h)) for h in t["hosts"])), None)
    warnings = []
    if template:
        config.update(name=template["name"], template_id=template["id"])
        if template["kind"] == "builtin":
            warnings.append("已存在专用适配器；切换会读取该平台全量库，不继承输入网址的筛选参数。自定义 URL 也可单独探测。")
        elif template["kind"] == "document_probe":
            config["mode"] = "document"
            warnings.append("仅站点识别，非已验证的专用解析器；请提供有权使用的职位 HTML/JSON，可能需要人工登录或验证。")
        elif template["kind"] in ("public_dom", "browser_dom", "public_browser_dom"):
            if template["kind"] == "browser_dom":
                warnings.append("此映射适用于动态 DOM；默认仍为公开 HTTP，不会自行连接登录会话。仅显式选择 browser 并授权后才复用 Chrome。")
            if parts.path in template.get("paths", ["/"]) and not parts.query:
                config.update({k: template[k] for k in ("parser", "items", "fields")})
                if template["kind"] == 'public_browser_dom':
                    config['mode'] = 'public_browser'
                    warnings.append('使用全新匿名浏览器执行官网公开脚本，不读取已有登录资料；验证/登录阻断时停止。')
                if template.get('company'):
                    config['company'] = template['company']
                warnings.append("仅已适配 URL 的当前页，不代表登录搜索或全站；页面未提供的字段保持未知。")
            else:
                warnings.append("该 URL 不是已适配的公开首页，将使用通用解析；登录/动态页面需用户授权的 browser 模式。")
        elif template["kind"] == "public_api":
            endpoint = _public_endpoint(template["id"], host, parts.path, parts.query)
            if endpoint:
                config.update({k: template[k] for k in ("parser", "items", "fields")})
                config["fetch_url"] = endpoint
            else:
                warnings.append("此详情路径/筛选参数不适用整板 API 模板；保留原 URL，由通用 parser 探测，避免扩大范围。")
    else:
        template = {"id": "auto", "name": "通用自动适配", "kind": "generic"}
        warnings.append("没有站点专用模板，将探测 JobPosting、JSON 数组、内嵌 JSON 和语义化 DOM。")
    return {"template": template, "config": validate_config(config), "warnings": warnings}


def _public_endpoint(provider, host, path, query):
    """Only board/list routes; never turn a detail URL into a whole-board crawl."""
    pairs = parse_qsl(query, keep_blank_values=True)
    if provider == "greenhouse":
        pattern = r"/v1/boards/([\w-]+)/jobs/?" if host == "boards-api.greenhouse.io" else r"/([\w-]+)/?"
        match = re.fullmatch(pattern, path)
        if match and all(k == "content" for k, _ in pairs):
            return f"https://boards-api.greenhouse.io/v1/boards/{match[1]}/jobs?content=true"
    elif provider == "ashby":
        pattern = r"/posting-api/job-board/([\w-]+)/?" if host == "api.ashbyhq.com" else r"/([\w-]+)/?"
        match = re.fullmatch(pattern, path)
        if match and all(k == "includeCompensation" for k, _ in pairs):
            return f"https://api.ashbyhq.com/posting-api/job-board/{match[1]}"
    elif provider == "lever":
        match = re.fullmatch(r"/v0/postings/([\w-]+)/?" if host.startswith("api.") else r"/([\w-]+)/?", path)
        allowed = {"mode", "location", "commitment", "team", "department", "level"}
        if match and all(k in allowed for k, _ in pairs):
            domain = "api.eu.lever.co" if ".eu." in host else "api.lever.co"
            return f"https://{domain}/v0/postings/{match[1]}?" + urlencode(
                [(k, v) for k, v in pairs if k != "mode"] + [("mode", "json")])
    return None
