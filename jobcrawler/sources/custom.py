"""Declarative, public-site adapters. Configuration is data, never executable code."""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from ..models import build_job
from .base import JobPage
from .common import iso_date, locations, plain_text

CONFIG_PATH = Path(__file__).resolve().parents[2] / "sources.local.json"
MAX_DOCUMENT = 2 * 1024 * 1024
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
FIELDS = {"id", "title", "url", "company", "locations", "requirements", "description",
          "published_at", "updated_at", "department", "category", "work_years", "recruitment_type"}


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("网站 URL 必须为不超过 4096 字符的字符串")
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.port not in (None, 80, 443) or any(c.isspace() for c in value)):
        raise ValueError("仅支持无凭据的公开 HTTP/HTTPS 网站及标准端口")
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("不能挂载本机或内网网站")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("不能挂载本机或内网地址")
    # A temporary challenge marker is not a stable source configuration.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in ("_security_check", "securityid", "verifyauthtoken", "anti_content")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), ""))


def _doh_addresses(host, timeout):
    """Bootstrap at a fixed public IP with normal TLS verification; no system DNS/proxy."""
    connection = http.client.HTTPSConnection("1.1.1.1", timeout=min(timeout, 10))
    try:
        connection.request("GET", "/dns-query?" + urlencode({"name": host, "type": "A"}),
                           headers={"Accept": "application/dns-json"})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("公开 DNS-over-HTTPS 响应异常，已阻止请求")
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get("Status")) is not int or data["Status"] != 0:
            raise ValueError("公开 DNS-over-HTTPS 未成功解析目标域名")
        answers = data.get("Answer", [])
        if not isinstance(answers, list) or any(not isinstance(row, dict) for row in answers):
            raise ValueError("公开 DNS-over-HTTPS 响应结构异常")
        addresses = [ipaddress.ip_address(row.get("data", "")) for row in answers if row.get("type") in (1, 28)]
        if not addresses or any(not ip.is_global for ip in addresses):
            raise ValueError("公开 DNS-over-HTTPS 返回内网或非公开地址，已阻止请求")
        return list(dict.fromkeys(addresses))
    except (http.client.HTTPException, UnicodeError, TypeError) as exc:
        raise ValueError("公开 DNS-over-HTTPS 返回无效响应") from exc
    finally:
        connection.close()


def _public_socket(address, timeout, source_address=None, audit=None):
    """Resolve once and connect to that numeric address, preventing DNS rebinding."""
    entries = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    mode = "system"
    if entries and all(ipaddress.ip_address(entry[4][0]) in FAKE_IP for entry in entries):
        addresses = _doh_addresses(address[0], timeout)
        entries = [(socket.AF_INET if ip.version == 4 else socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                    (str(ip), address[1]) if ip.version == 4 else (str(ip), address[1], 0, 0)) for ip in addresses]
        mode = "cloudflare_doh"
    if not entries or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in entries):
        raise ValueError("网站 DNS 指向内网或非公开地址，已阻止请求")
    if audit is not None:
        audit.update(dns_mode=mode, resolved_addresses=list(dict.fromkeys(entry[4][0] for entry in entries)))
    last = None
    for family, kind, protocol, _, endpoint in entries:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(endpoint)
            if audit is not None:
                audit["connected_ip"] = endpoint[0]
            return connection
        except OSError as exc:
            last = exc
            connection.close()
    raise OSError(f"无法连接公开网站: {last}")


def fetch_document(url, audit=None):
    for _ in range(5):
        url = public_url(url)
        parts = urlsplit(url)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = cls(parts.hostname, parts.port, timeout=20)
        record = {"url": url, "requested_at": datetime.now(timezone.utc).isoformat(),
                  "tls_verified": False}
        def connect(address, timeout, source_address=None):
            return _public_socket(address, timeout, source_address, audit=record)
        connection._create_connection = connect
        try:
            target = quote(urlunsplit(("", "", parts.path, parts.query, "")), safe="/%?=&:+,;@!$'()*[]~-._")
            connection.request("GET", target, headers={
                "User-Agent": "JobCrawler/0.3 (public recruitment data)", "Accept-Encoding": "identity",
                "Cache-Control": "no-cache"})
            response = connection.getresponse()
            record["tls_verified"] = parts.scheme == "https"
            record["http_status"] = response.status
            record["response_date"] = response.getheader("Date")
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("网站重定向缺少目标地址")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"目标网站 HTTP {response.status}；可能需要登录、验证或限制访问，未写入职位")
            raw = response.read(MAX_DOCUMENT + 1)
            if len(raw) > MAX_DOCUMENT:
                raise ValueError("页面超过 2 MiB，停止解析")
            record.update(response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
            encoding = response.headers.get_content_charset() or "utf-8"
            return raw.decode(encoding), url
        except (http.client.HTTPException, UnicodeError, LookupError) as exc:
            raise ValueError("目标网站返回了无法解析的 HTTP 或文本响应") from exc
        finally:
            connection.close()
            if audit is not None:
                audit(record)
    raise ValueError("网站重定向次数过多")


def soup(document):
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ValueError("自定义 HTML 解析需要安装 requirements.txt 中的 beautifulsoup4") from exc
    return BeautifulSoup(document, "html.parser")


def path_value(value, path):
    if not isinstance(path, str):
        raise ValueError("JSON 字段映射必须是点分路径字符串")
    keys = path.removeprefix("$.").split(".") if path not in ("", "$") else []
    for index, key in enumerate(keys):
        if key == "*" and isinstance(value, list):
            return [path_value(item, ".".join(keys[index + 1:])) for item in value]
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def mapped_value(row, rule, field):
    if isinstance(rule, str):
        return path_value(row, rule)
    values = []
    for path in rule:
        value = path_value(row, path)
        for item in value if isinstance(value, list) else [value]:
            if item is not None:
                if not isinstance(item, str):
                    raise ValueError("多路径字段映射只接受文本，请选择正确的子字段")
                if item.strip() and item not in values:
                    values.append(item)
    return values if field == "locations" else "\n".join(values)


def validate_config(config):
    allowed = {"id", "name", "url", "company", "mode", "parser", "items", "fields",
               "required_fields", "pagination", "empty_selector", "fetch_url", "template_id",
               "auto_adapt", "json_script"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("网站配置必须为 JSON 对象，且不能包含未知配置或可执行脚本")
    config = json.loads(json.dumps(config, ensure_ascii=False))
    if not isinstance(config.get("id"), str) or not re.fullmatch(r"custom_[a-z0-9_]{1,48}", config["id"]):
        raise ValueError("自定义网站 ID 必须为 custom_ 开头的小写字母、数字或下划线")
    if not isinstance(config.get("name"), str) or not 1 <= len(config["name"].strip()) <= 100:
        raise ValueError("请填写 1–100 字符的网站名称")
    config["url"] = public_url(config.get("url"))
    if "fetch_url" in config:
        config["fetch_url"] = public_url(config["fetch_url"])
    if "auto_adapt" in config and not isinstance(config["auto_adapt"], bool):
        raise ValueError("auto_adapt 必须为布尔值")
    if "template_id" in config and (not isinstance(config["template_id"], str)
            or not re.fullmatch(r"[a-z0-9_]{1,50}", config["template_id"])):
        raise ValueError("template_id 必须为模板标识")
    if "json_script" in config and (isinstance(config["json_script"], bool)
            or not isinstance(config["json_script"], int) or not 0 <= config["json_script"] <= 10000):
        raise ValueError("json_script 必须为 script 节点的非负序号")
    config.setdefault("parser", "auto")
    config.setdefault("mode", "http")
    if config["parser"] not in ("auto", "schema", "json", "dom") or config["mode"] not in ("http", "document", "browser", "public_browser"):
        raise ValueError("parser 仅支持 auto/schema/json/dom；mode 仅支持 http/document/browser/public_browser")
    if config["mode"] in ("browser", "public_browser"):
        from .live_browser import recruitment_url
        recruitment_url(config["url"])
        if config.get("fetch_url") or config.get("pagination"):
            raise ValueError('受控浏览器只读取配置 URL 当前页，不使用隐藏接口或推测分页')
    if not isinstance(config.get("company", ""), str):
        raise ValueError("company 必须为字符串")
    fields = config.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - FIELDS:
        raise ValueError("字段映射包含未知职位字段")
    for rule in fields.values():
        if isinstance(rule, list) and config["parser"] == "json" and rule and all(isinstance(p, str) for p in rule):
            continue
        if not isinstance(rule, (str, dict)) or (isinstance(rule, dict) and (
                set(rule) - {"selector", "attr"} or not isinstance(rule.get("selector", ""), str)
                or not isinstance(rule.get("attr", ""), str))):
            raise ValueError("字段映射使用 JSON 点分路径、CSS 字符串或 {selector, attr}")
        if config["parser"] == "json" and not isinstance(rule, str):
            raise ValueError("JSON 解析仅接受点分路径映射")
    required = config.setdefault("required_fields", ["title", "url"])
    if not isinstance(required, list) or not all(isinstance(k, str) and k in FIELDS for k in required):
        raise ValueError("required_fields 必须为已知职位字段列表")
    pagination = config.get("pagination", {})
    if not isinstance(pagination, dict) or set(pagination) - {"url", "page_size", "total_path", "next_selector"}:
        raise ValueError("分页配置仅支持 url/page_size/total_path/next_selector")
    if pagination:
        template = pagination.get("url", "")
        if not isinstance(template, str) or template.count("{page}") != 1:
            raise ValueError("分页 URL 必须包含一个 {page} 占位符")
        public_url(template.replace("{page}", "1"))
        if not pagination.get("total_path") and not pagination.get("next_selector"):
            raise ValueError("分页必须提供 total_path 或 next_selector，以验证结束边界")
        if any(not isinstance(pagination[k], str) for k in ("total_path", "next_selector") if k in pagination):
            raise ValueError("total_path/next_selector 必须为字符串")
        size = pagination.get("page_size", 20)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 200:
            raise ValueError("page_size 必须为 1–200 的整数")
        if config["mode"] == "document":
            raise ValueError("文档导入只表示本次提交的文档范围，不配置联网分页")
    for key in ("items", "empty_selector"):
        if key in config and not isinstance(config[key], str):
            raise ValueError(f"{key} 必须为字符串")
    if config["parser"] in ("dom", "auto") and config.get("items"):
        try:
            import soupsieve
            for selector in [config["items"], config.get("empty_selector"), pagination.get("next_selector"),
                             *[r if isinstance(r, str) else r.get("selector") for r in fields.values()]]:
                if selector:
                    soupsieve.compile(selector)
        except Exception as exc:
            raise ValueError(f"CSS 选择器配置无效: {exc}") from exc
    return config


def schema_rows(value):
    if isinstance(value, list):
        return [row for item in value for row in schema_rows(item)]
    if not isinstance(value, dict):
        return []
    types = value.get("@type") or []
    types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
    if any(str(t).rsplit("/", 1)[-1] == "JobPosting" for t in types):
        return [value]
    return [row for item in value.values() if isinstance(item, (list, dict)) for row in schema_rows(item)]


class CustomSource:
    minimum_delay = 1
    requires_listing_replay = True

    def __init__(self, config):
        self.config = validate_config(config)
        self.id, self.name, self.url = (self.config[key] for key in ("id", "name", "url"))
        self.revision = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()[:16]
        self._observed = self._page = 0
        self.parser_used = None
        self.adaptation = None
        self.network_requests = []

    @property
    def snapshot_metadata(self):
        return {"adapter_revision": self.revision, "parser_used": self.parser_used,
                "template_id": self.config.get("template_id"), "adaptation": self.adaptation,
                "network_requests": self.network_requests,
                "acquisition": {"browser": "browser_network", "public_browser": "public_browser_network"}.get(self.config["mode"], "network") if self.network_requests else
                               "provided_document" if self.config["mode"] == "document" else "unverified",
                "scope": "paginated" if self.config.get("pagination") else
                         "document" if self.config["mode"] == "document" else "single_page"}

    def normalize(self, raw, base):
        values = {k: raw.get(k) for k in FIELDS}
        for key in FIELDS - {"locations"}:
            value = values[key]
            numeric = key in ("id", "published_at", "updated_at") and isinstance(value, (int, float)) and not isinstance(value, bool)
            if value is not None and not isinstance(value, str) and not numeric:
                raise ValueError(f"职位字段 {key} 类型已变化，请适配到文本或正确的子字段")
            values[key] = plain_text(values[key] or "")
        if values["url"]:
            values["url"] = public_url(urljoin(base, values["url"]))
            parts = urlsplit(values["url"])
            # Campus show/source tags change each response, not the job identity.
            if (parts.hostname == 'lagou.com' or parts.hostname.endswith('.lagou.com')) and re.fullmatch(r'/wn/jobs/\d+\.html', parts.path):
                query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ('show', 'source')]
                values['url'] = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
        for key in set(self.config["required_fields"]) | {"title", "url"}:
            if not values[key]:
                raise ValueError(f"职位缺少 {key}；页面样式或 schema 已变化，请调整字段映射")
        values["id"] = values["id"] or hashlib.sha256(values["url"].encode()).hexdigest()[:24]
        place = values["locations"] or []
        values["locations"] = [str(v).strip() for v in place if str(v).strip()] if isinstance(place, list) else locations(str(place))
        values["company"] = values["company"] or self.config.get("company", "")
        for key in ("published_at", "updated_at"):
            values[key] = iso_date(raw.get(key) if isinstance(raw.get(key), (int, float)) else values[key])
        job = build_job(source=self.id, source_url=self.url, **values)
        job["details_pending"] = not bool(job["requirements"] or job["description"])
        return job

    def parse_document(self, document, base=None, page=1):
        if not isinstance(document, str) or len(document.encode("utf-8")) > MAX_DOCUMENT:
            raise ValueError("页面文档必须为不超过 2 MiB 的 HTML/JSON 字符串")
        base = public_url(base or self.url)
        try:
            envelope = json.loads(document)
        except ValueError:
            envelope = None
        if isinstance(envelope, dict) and (envelope.get("success") is False or envelope.get("error")):
            raise ValueError("目标返回错误/认证响应，未适配或写入职位")
        try:
            return self._parse_document(document, base, page)
        except ValueError as original:
            if not self.config.get("auto_adapt", True):
                raise
            from .autoparser import candidate_rules
            matches = []
            for rule in candidate_rules(document, base):
                # Preserve pagination and required fields: adaptation cannot weaken acceptance.
                candidate = {k: v for k, v in self.config.items()
                             if k not in ("items", "fields", "empty_selector", "json_script")}
                candidate.update(rule, auto_adapt=False)
                # A moved array should not discard working multi-location/description rules.
                mappings = [candidate.get("fields", {})]
                if rule["parser"] == "json" and self.config["parser"] == "json" and self.config.get("fields"):
                    mappings.insert(0, self.config["fields"])
                for mapping in mappings:
                    adapter = CustomSource({**candidate, "fields": mapping})
                    adapter._observed, adapter._page = self._observed, self._page
                    try:
                        result = adapter._parse_document(document, base, page)
                    except ValueError:
                        continue
                    if result.jobs:
                        matches.append((adapter, result))
                        break
            identities = {tuple(sorted((j["id"], j["url"], j["title"]) for j in result.jobs))
                          for _, result in matches}
            if len(identities) > 1:
                raise ValueError("自动适配发现多个不同职位集合，需明确 items / fields，旧数据已保留") from original
            if not matches:
                raise ValueError(f"{original}；自动适配未找到满足必要字段的可靠职位结构") from original
            adapter, result = matches[0]
            adapter.config["auto_adapt"] = True
            self.config = adapter.config
            self.revision = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()[:16]
            self.parser_used = adapter.parser_used
            self._observed, self._page = adapter._observed, adapter._page
            self.adaptation = {"status": "adapted", "parser": self.parser_used,
                               "reason": str(original), "fields": sorted(self.config.get("fields", {}))}
            return result

    def _parse_document(self, document, base, page):
        parser = self.config["parser"]
        data = None
        try:
            data = json.loads(document)
        except ValueError:
            pass
        html = None if data is not None else soup(document)
        if "json_script" in self.config:
            scripts = html.find_all("script") if html is not None else []
            index = self.config["json_script"]
            if index >= len(scripts):
                raise ValueError("内嵌 JSON 节点已变化")
            try:
                data = json.loads(scripts[index].get_text())
            except ValueError as exc:
                raise ValueError("内嵌 JSON 文档无效") from exc
        if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
            raise ValueError("目标返回错误/认证响应，未写入职位")
        schema = schema_rows(data) if data is not None else []
        if html is not None and parser in ("auto", "schema"):
            for script in html.find_all("script", type="application/ld+json"):
                try:
                    schema.extend(schema_rows(json.loads(script.string or script.get_text())))
                except ValueError:
                    continue
        raw_jobs = []
        if schema and parser in ("auto", "schema"):
            self.parser_used = "schema"
            for row in schema:
                identifier = row.get("identifier") or ""
                if isinstance(identifier, dict):
                    identifier = identifier.get("value") or identifier.get("name") or ""
                company = row.get("hiringOrganization") or {}
                company = company.get("name", "") if isinstance(company, dict) else company
                places = row.get("jobLocation") or []
                places = places if isinstance(places, list) else [places]
                names = []
                for place in places:
                    address = place.get("address", {}) if isinstance(place, dict) else place
                    names.append(" ".join(str(address.get(k) or "") for k in ("addressLocality", "addressRegion", "addressCountry")).strip()
                                 if isinstance(address, dict) else str(address))
                raw_jobs.append({"id": identifier, "title": row.get("title"), "url": row.get("url") or (base if len(schema) == 1 else ""),
                    "company": company, "locations": names, "description": row.get("description") or row.get("responsibilities"),
                    "requirements": "\n".join(v for k in ("qualifications", "experienceRequirements", "skills")
                                                if isinstance((v := row.get(k)), str)), "published_at": row.get("datePosted")})
        elif data is not None and parser in ("auto", "json"):
            self.parser_used = "json"
            rows = path_value(data, self.config.get("items", ""))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("JSON 职位数组未匹配；请调整 items 路径，不能把错误响应当作空列表")
            # Respect public-board visibility (notably Ashby's isListed contract).
            rows = [row for row in rows if row.get("isListed") is not False]
            mapping = self.config.get("fields") or {k: k for k in FIELDS}
            raw_jobs = [{k: mapped_value(row, rule, k) for k, rule in mapping.items()} for row in rows]
        elif html is not None and parser in ("auto", "dom") and self.config.get("items"):
            self.parser_used = "dom"
            try:
                rows = html.select(self.config["items"])
                for row in rows:
                    values = {}
                    for key, rule in self.config.get("fields", {}).items():
                        rule = {"selector": rule} if isinstance(rule, str) else rule
                        nodes = row.select(rule["selector"]) if rule.get("selector") else [row]
                        attr = rule.get("attr") or ("href" if key == "url" else None)
                        found = [node.get(attr, "") if attr else node.get_text(" ", strip=True) for node in nodes]
                        values[key] = found if key == "locations" else next((value for value in found if value), "")
                    raw_jobs.append(values)
                if not rows and not (self.config.get("empty_selector") and html.select(self.config["empty_selector"])):
                    raise ValueError("DOM 职位卡片未匹配；可能需要登录/验证或页面样式已变化，请重新适配")
            except Exception as exc:
                raise ValueError(f"DOM 适配失败: {exc}") from exc
        else:
            raise ValueError("未发现 JobPosting 或配置的职位结构；可能需要登录/验证、动态渲染或重新适配")
        jobs = [self.normalize(raw, base) for raw in raw_jobs]
        pagination = self.config.get("pagination") or {}
        if not pagination:
            return JobPage(page, len(jobs), jobs, has_more=False)
        if pagination.get("total_path"):
            total = path_value(data, pagination["total_path"])
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("分页总数未匹配，请适配 total_path")
            size = pagination.get("page_size", 20)
            if len(jobs) != max(0, min(size, total - (page - 1) * size)):
                raise ValueError("实际页容量与配置不一致，停止以免误报列表完整")
            return JobPage(page, total, jobs, has_more=page * size < total)
        if html is None:
            raise ValueError("next_selector 仅适用于 HTML 文档")
        links = html.select(pagination["next_selector"])
        more = any(not node.has_attr("disabled") and node.get("aria-disabled") != "true"
                   and "disabled" not in node.get("class", []) for node in links)
        if page == 1:
            self._observed = 0
        elif page != self._page + 1:
            raise ValueError("HTML 分页必须依序读取")
        self._page, self._observed = page, self._observed + len(jobs)
        return JobPage(page, self._observed + int(more), jobs, has_more=more)

    def fetch_page(self, page):
        if self.config["mode"] == "document":
            raise ValueError("该网站需要登录后页面文档；请重新导入当前浏览器的职位文档，不会用旧文档假装联网刷新")
        if page == 1:
            self.network_requests = []
            self.adaptation = None
        template = (self.config.get("pagination") or {}).get("url", self.config.get("fetch_url", self.url))
        if self.config["mode"] in ("browser", "public_browser"):
            from .live_browser import fetch_browser_document
            document, final_url, evidence = (fetch_browser_document(self.url, anonymous=True)
                if self.config["mode"] == 'public_browser' else fetch_browser_document(self.url))
            self.network_requests.append(evidence)
        else:
            document, final_url = fetch_document(template.replace("{page}", str(page)), audit=self.network_requests.append)
        if self.config["mode"] in ("browser", "public_browser"):
            texts = evidence.pop('_network_texts', [])
            try:
                result = self.parse_document(document, final_url, page)
                evidence['parser_input'] = 'live_dom'
            except ValueError as original:
                if not self.config.get('auto_adapt', True):
                    raise
                matches = []
                for index, text in enumerate(texts):
                    try:
                        data = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(data, (dict, list)):
                        continue
                    adapter = CustomSource(self.config)
                    try:
                        parsed = adapter.parse_document(text, final_url, page)
                    except ValueError:
                        continue
                    if parsed.jobs:
                        matches.append((adapter, parsed, index))
                identities = {tuple(sorted((j['id'], j['url'], j['title']) for j in parsed.jobs))
                              for _, parsed, _ in matches}
                if len(identities) > 1:
                    raise ValueError('本轮浏览器响应含多个不同职位集合，请明确映射；旧数据已保留') from original
                if not matches:
                    raise original
                adapter, result, index = matches[0]
                self.config, self.revision = adapter.config, adapter.revision
                self.parser_used, self.adaptation = adapter.parser_used, adapter.adaptation
                self._observed, self._page = adapter._observed, adapter._page
                evidence['parser_input'] = 'observed_network_json'
                responses = evidence.get('responses', [])
                if index < len(responses):
                    evidence['parsed_response_url'] = responses[index]['url']
            found = sum(any(job['title'] in text for text in texts) for job in result.jobs)
            if not result.jobs or found != len(result.jobs):
                raise ValueError('浏览器职位未全部匹配到本轮新网络响应，未将旧页面/本地列表当作实时数据')
            evidence['jobs_correlated_with_network'] = found
            return result
        return self.parse_document(document, final_url, page)


def read_configs(path=CONFIG_PATH):
    path = Path(path)
    if not path.exists():
        return []
    if path.stat().st_size > 256 * 1024:
        raise ValueError("网站配置文件过大")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("sources"), list):
        raise ValueError("网站配置文件需要 version=1 和 sources 数组")
    configs = [validate_config(c) for c in data["sources"]]
    if len(configs) > 50 or len({c["id"] for c in configs}) != len(configs):
        raise ValueError("最多挂载 50 个网站，且 ID 不能重复")
    return configs
