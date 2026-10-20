"""Single-owner readers for anonymous public pages or opt-in user sessions.

Only recruitment pages are opened. Never export credentials or close the user's
browser/context. Login and CAPTCHA remain human checkpoints.
"""
import hashlib
import base64
import html
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

HOSTS = ('zhipin.com', 'bosszhipin.com', 'zhaopin.com', '51job.com', 'liepin.com', 'lagou.com')
PORT_FILE = Path.home() / 'Library/Application Support/Google/Chrome/DevToolsActivePort'
CONNECT_TIMEOUT_MS = 300000  # Human confirmation can outlive an agent turn.
READ_TIMEOUT_SECONDS = 420
JOB_LINKS = 'a[href*="/job_detail/"],a[href*="/jobdetail/"],a[href*="/job/"],a[href*="/wn/jobs/"],a[href*="jobs.51job.com/"],a[href*="jobs.51job.com.cn/"]'
_reader = None
_public_reader = None
_lock = threading.Lock()


def response_text(raw):
    """Decode JSON/official JSONP as data, never execute a callback body."""
    value = raw.decode('utf-8')
    match = re.fullmatch(r'\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\((.*)\)\s*;?\s*', value, re.S)
    try:
        return json.dumps(json.loads(match[1] if match else value), ensure_ascii=False)
    except ValueError:
        return html.unescape(value)


def recruitment_url(url):
    from .custom import public_url
    url = public_url(url)
    host = urlsplit(url).hostname.lower().rstrip('.')
    if not any(host == domain or host.endswith('.' + domain) for domain in HOSTS):
        raise ValueError('受控浏览器仅允许已授权的五个招聘平台，不访问其他网站')
    return url


def chrome_endpoint(path=PORT_FILE):
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        raise ValueError('请在原 Chrome 的 chrome://inspect/#remote-debugging 开启连接并手动确认') from exc
    if len(lines) != 2 or not lines[0].isdigit() or not 1 <= int(lines[0]) <= 65535 or not re.fullmatch(r'/devtools/browser/[\w-]+', lines[1]):
        raise ValueError('Chrome 受控连接元数据无效，未连接其他地址')
    return 'ws://127.0.0.1:' + lines[0] + lines[1]


class ChromeReader:
    def __init__(self, anonymous=False):
        self.anonymous = anonymous
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='approved-chrome')
        self.playwright = self.browser = None
        self.pages = {}
        self.pending = None
        self.pending_lock = threading.Lock()

    def read(self, url):
        url = recruitment_url(url)
        with self.pending_lock:
            if self.pending is not None and not self.pending.done():
                if self.anonymous:
                    raise ValueError('匿名公开抓取正在进行，请稍后重试；不会请求登录或人工验证')
                raise ValueError('Chrome 已有连接/页面读取正在等待，请先完成原窗口操作；未重复发起授权')
            pending = self.pending = self.executor.submit(self._read, url)
        try:
            return pending.result(timeout=READ_TIMEOUT_SECONDS)
        except FutureTimeout as exc:
            if self.anonymous:
                raise ValueError('匿名抓取超时，旧职位未覆盖；不会请求登录或人工验证') from exc
            raise ValueError('Chrome 连接/页面等待超时，请检查原窗口；旧职位未覆盖') from exc
        except ValueError:
            raise
        except Exception as exc:
            code = re.search(r'net::[A-Z_]+', str(exc))
            raise ValueError('Chrome 页面读取失败（' + (code[0] if code else type(exc).__name__) + '）；请检查保留的页面') from exc

    def connect(self):
        if self.browser is not None and self.browser.is_connected():
            return
        if self.playwright:
            self.playwright.stop()
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            if self.anonymous:
                self.browser = self.playwright.chromium.launch(channel='chrome', headless=True)
                self.browser.new_context(service_workers='block')
            else:
                self.browser = self.playwright.chromium.connect_over_cdp(chrome_endpoint(), timeout=CONNECT_TIMEOUT_MS)
            if len(self.browser.contexts) != 1:
                raise ValueError('Chrome 存在多个上下文，未擅自选择用户会话')
        except Exception as exc:
            self.playwright.stop()
            self.playwright = self.browser = None
            # Do not leak the capability-bearing websocket path in an error/log.
            reason = '等待人工确认已超时' if type(exc).__name__ == 'TimeoutError' else '连接失败（' + type(exc).__name__ + '）'
            if self.anonymous:
                raise ValueError('独立匿名 Chrome ' + reason + '；未使用用户登录资料') from exc
            raise ValueError('原 Chrome ' + reason + '，请检查原窗口；未自动重复发起授权') from exc

    def _read(self, url):
        from .custom import MAX_DOCUMENT
        self.connect()
        context = self.browser.contexts[0]
        page = self.pages.get(url)
        if page is None or page.is_closed():
            page = context.new_page()
            self.pages[url] = page
        cdp = context.new_cdp_session(page)
        transport = 'anonymous_chrome' if self.anonymous else 'approved_chrome_cdp'
        events = []
        def observed(event):
            value = event.get('response', {})
            parts = urlsplit(value.get('url', ''))
            public_jsonp = (event.get('type') == 'Script' and parts.hostname == 'coapi.51job.com'
                            and parts.path == '/job_list.php')
            if event.get('type') not in ('Document', 'XHR', 'Fetch') and not public_jsonp:
                return
            if not parts.hostname or not any(parts.hostname == h or parts.hostname.endswith('.' + h) for h in HOSTS):
                return
            events.append({'url': urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')),
                'http_status': value.get('status'), 'from_disk_cache': bool(value.get('fromDiskCache')),
                'from_service_worker': bool(value.get('fromServiceWorker')),
                'from_prefetch_cache': bool(value.get('fromPrefetchCache')),
                'resource_type': event.get('type'), 'transport': transport,
                'request_id': event.get('requestId'),
                'requested_at': started, 'response_date': next((v for k,v in value.get('headers', {}).items() if k.lower() == 'date'), None)})
        started = datetime.now(timezone.utc).isoformat()
        cdp.on('Network.responseReceived', observed)
        cdp.send('Network.enable')
        cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})
        cdp.send('Network.setBypassServiceWorker', {'bypass': True})
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            recruitment_url(page.url)
            parts = urlsplit(page.url)
            ready = '#jobs .jobname' if parts.hostname == 'campus.51job.com' and parts.path == '/asiainfo/jobs.html' else JOB_LINKS
            deadline = monotonic() + 25
            while True:
                visible = page.locator('body').inner_text(timeout=5000)
                if re.search(r'安全验证|请完成验证|访问异常|滑动验证|请拖动滑块|拖动.*拼图|访问过于频繁', visible):
                    if self.anonymous:
                        raise ValueError('匿名公开入口要求验证，已停止；不会登录、自动解答或请求人工验证')
                    raise ValueError('官网要求人工验证，已保留原会话页面，请手动完成后重试；未写入职位')
                if page.locator(ready).evaluate_all('(nodes) => nodes.some(n => n.getClientRects().length > 0 && n.textContent.trim())'):
                    break
                if monotonic() >= deadline:
                    if self.anonymous:
                        raise ValueError('匿名公开职位列表未就绪，已停止；未把白屏或导航当职位')
                    raise ValueError('登录/动态列表尚未就绪，页面已保留供人工检查；未将白屏或首页导航当职位')
                page.wait_for_timeout(500)
            document = page.content()
            if len(document.encode()) > MAX_DOCUMENT:
                raise ValueError('浏览器职位 DOM 超过 2 MiB，未写入职位')
            fresh = [e for e in events if e['http_status'] == 200 and not any(e[k] for k in ('from_disk_cache','from_service_worker','from_prefetch_cache'))]
            if not fresh:
                raise ValueError('没有观察到本轮未缓存的 HTTP 200 响应，未将旧页面当作实时数据')
            texts, correlated = [], []
            # Keep the document even when later analytics responses arrive.
            main = urlsplit(page.url)
            main_url = urlunsplit((main.scheme, main.netloc, main.path, '', ''))
            candidates = [e for e in fresh if e['resource_type'] == 'Document' and e['url'] == main_url][-1:] + [
                e for e in fresh if e['resource_type'] != 'Document'
                and re.search(r'job|search|position|recommend|list', urlsplit(e['url']).path, re.I)][-12:]
            for event in candidates:
                try:
                    payload = cdp.send('Network.getResponseBody', {'requestId': event['request_id']})
                    raw = base64.b64decode(payload['body']) if payload.get('base64Encoded') else payload['body'].encode()
                    if len(raw) > MAX_DOCUMENT:
                        continue
                    text = response_text(raw)
                    texts.append(text)
                    correlated.append({**{k:v for k,v in event.items() if k != 'request_id'},
                        'response_bytes':len(raw), 'response_sha256':hashlib.sha256(raw).hexdigest()})
                except Exception:
                    continue
            if not texts:
                raise ValueError('未取得可核对职位内容的新网络响应，未使用旧 DOM 数据')
            evidence = {'url': recruitment_url(page.url), 'requested_at': started,
                'transport': transport, 'session_reused': not self.anonymous,
                'anonymous': self.anonymous, 'cache_disabled': True,
                'service_worker_bypassed': True, 'dom_bytes': len(document.encode()),
                'dom_sha256': hashlib.sha256(document.encode()).hexdigest(), 'responses': correlated,
                '_network_texts':texts}
            return document, page.url, evidence
        finally:
            cdp.detach()

    def disconnect(self):
        # Stopping only the client transport preserves Chrome and all user tabs.
        try:
            if self.anonymous and self.browser and self.browser.is_connected():
                try:
                    self.browser.close()
                except Exception:
                    # A terminal interrupt may already have stopped the driver.
                    if self.browser.is_connected():
                        raise
        finally:
            if self.playwright:
                self.playwright.stop()
            self.playwright = self.browser = None


def fetch_browser_document(url, anonymous=False):
    global _reader, _public_reader
    with _lock:
        if anonymous:
            if _public_reader is None:
                _public_reader = ChromeReader(anonymous=True)
            reader = _public_reader
        else:
            if _reader is None:
                _reader = ChromeReader()
            reader = _reader
    return reader.read(url)


def close_browser_reader():
    global _reader, _public_reader
    with _lock:
        readers = [_reader, _public_reader]
        _reader = _public_reader = None
    for reader in readers:
        if reader is None:
            continue
        try:
            reader.executor.submit(reader.disconnect).result(timeout=3)
        except FutureTimeout:
            pass
        finally:
            reader.executor.shutdown(wait=False)
