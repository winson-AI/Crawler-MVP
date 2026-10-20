"""Live portal acceptance through serve; never imports documents or replays lists."""
import argparse
import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from jobcrawler.crawler import save_cache
from jobcrawler.rankers import RANKERS, rank_jobs
from jobcrawler.selectors import select_jobs
from jobcrawler.sources.custom import fetch_document, soup

TARGETS = [
    ('boss', 'BOSS直聘', 'https://www.zhipin.com/web/geek/jobs', 'https://www.zhipin.com/'),
    ('zhaopin', '智联招聘', 'https://www.zhaopin.com/', 'https://www.zhaopin.com/'),
    ('51job', '前程无忧', 'https://we.51job.com/pc/search', 'https://campus.51job.com/asiainfo/jobs.html'),
    ('liepin', '猎聘', 'https://www.liepin.com/', 'https://www.liepin.com/'),
    ('lagou', '拉勾', 'https://www.lagou.com/', 'https://campus.lagou.com/'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8000')
    parser.add_argument('--http-only', action='store_true', help='只验证公开 HTTP，明确不声称复用登录会话')
    parser.add_argument('--browser-session', action='store_true', help='显式选择已授权原 Chrome；可能需人工确认连接')
    parser.add_argument('--platform', choices=[t[0] for t in TARGETS], action='append', help='只跑指定平台，可重复')
    parser.add_argument('--output', type=Path, default=Path('output') / ('portals-live-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
    args = parser.parse_args()
    if args.http_only and args.browser_session:
        parser.error('--http-only 与 --browser-session 不能同时使用')
    if urlsplit(args.server).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('只连接本机 serve')
    args.output.mkdir(parents=True, exist_ok=True)

    def request(route, body=None, text=False):
        req = Request(args.server.rstrip('/') + route,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={} if body is None else {'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=435) as response:
                content = response.read().decode()
        except HTTPError as exc:
            raise ValueError(json.loads(exc.read().decode()).get('error', 'HTTP ' + str(exc.code))) from None
        return content if text else json.loads(content)

    if request('/api/status')['running']:
        raise RuntimeError('已有抓取任务，未并发启动')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'uses_document_import': False,
              'uses_listing_cache_for_crawl': False, 'http_only': args.http_only,
              'uses_user_chrome_session': args.browser_session, 'sources': []}
    save_cache(request('/api/sources'), args.output / 'before.json')
    for provider, name, browser_url, public_url in TARGETS:
        if args.platform and provider not in args.platform:
            continue
        entry = {'provider': provider, 'attempts': [], 'status': 'blocked'}
        report['sources'].append(entry)
        attempts = ([('browser', browser_url)] if args.browser_session else []) + [('public', public_url)]
        for mode, url in attempts:
            attempt = {'mode': mode, 'url': url, 'started_at': datetime.now(timezone.utc).isoformat()}
            entry['attempts'].append(attempt)
            print(name, mode, 'starting', flush=True)
            try:
                matched = request('/api/templates/match', {'url': url})
                config = matched['config']
                if mode == 'public':
                    mode = 'public_browser' if config['mode'] == 'public_browser' and not args.http_only else 'http'
                    attempt['mode'] = mode
                label = ('·亚信2027校招当前页（匿名实时）' if provider == '51job' else
                         '·校招首页职位（实时）' if provider == 'lagou' else '·公开首页精选（实时）')
                config.update(id='custom_live_' + provider + '_' + mode, mode=mode,
                              name=name + ('·当前页面（会话实时）' if mode == 'browser' else label),
                              parser='dom', items='.obsolete-job-layout', fields={'title': '.obsolete-title', 'url': 'a'},
                              auto_adapt=True)
                attempt.update(source_id=config['id'], matched_kind=matched['template']['kind'],
                               fault_injected_items=config['items'])
                preview = request('/api/sources/preview', {'config': config})
                acquisition = {'browser':'browser_network', 'public_browser':'public_browser_network', 'http':'network'}[mode]
                def evidence(value):
                    events = value.get('network_requests', [])
                    if value.get('acquisition') != acquisition or not events:
                        raise RuntimeError('缺少本轮联网证据')
                    for event in events:
                        if event.get('requested_at', '') < attempt['started_at']:
                            raise RuntimeError('网络记录不属于本轮')
                        if mode in ('browser', 'public_browser'):
                            if not event.get('cache_disabled') or not event.get('jobs_correlated_with_network'):
                                raise RuntimeError('浏览器列表未与新响应核对')
                            if mode == 'public_browser' and (not event.get('anonymous') or event.get('session_reused')):
                                raise RuntimeError('匿名抓取不得使用用户登录会话')
                        elif event.get('http_status') != 200 or not event.get('response_sha256'):
                            raise RuntimeError('缺少新 HTTP 全文响应')
                evidence(preview)
                if not preview.get('adaptation'):
                    raise RuntimeError('未通过失效规则自动适配')
                attempt['preview'] = {k: preview[k] for k in ('collected', 'adaptation', 'network_requests', 'acquisition')}
                mounted = request('/api/sources/custom', {'config': config})
                evidence(mounted)
                if not mounted.get('mounted') or not mounted.get('refreshing'):
                    raise RuntimeError('未完成挂载和后台刷新')
                deadline = time.monotonic() + 435
                while True:
                    state = request('/api/status?' + urlencode({'source': config['id']}))
                    if not state['running']:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('后台刷新超时')
                    time.sleep(.5)
                if state['error'] or state['configuration_changed']:
                    raise RuntimeError(state['error'] or '配置与快照不一致')
                snapshot = state['snapshot']
                evidence(snapshot)
                exported = request('/api/export?' + urlencode({'source': config['id']}))
                jobs = exported['jobs']
                if not jobs or len(jobs) != snapshot['collected'] or not snapshot['complete']:
                    raise RuntimeError('入库数量/范围不一致')
                sample = jobs[0]
                selector = {'keywords': [sample['title']], 'include_unknown_experience': True}
                if sample['locations']:
                    selector['locations'] = [sample['locations'][0]]
                for ranker in RANKERS:
                    query = urlencode({'source': config['id'], 'selector': json.dumps(selector), 'ranker': ranker})
                    actual = request('/api/jobs?' + query)
                    expected = rank_jobs(select_jobs(jobs, selector), ranker)
                    if actual['jobs'] != expected or not expected:
                        raise RuntimeError('serve 筛选/排序不一致或为空')
                    if request('/api/export?' + query)['jobs'] != actual['jobs']:
                        raise RuntimeError('JSON 导出不一致')
                    rows = list(csv.DictReader(io.StringIO(request('/api/export?' + query + '&format=csv', text=True))))
                    if len(rows) != len(expected):
                        raise RuntimeError('CSV 导出数量不一致')
                learned = request('/api/templates/match', {'url': url})
                if learned['template']['kind'] != 'learned' or learned['config']['items'] == '.obsolete-job-layout':
                    raise RuntimeError('适配规则未持久化复用')
                save_cache(exported, args.output / (config['id'] + '.json'))
                attempt.update(status='ok', collected=len(jobs), selector=selector,
                               snapshot=snapshot, rankers=list(RANKERS),
                               jobs_with_location=sum(bool(j['locations']) for j in jobs),
                               details_pending=sum(bool(j.get('details_pending')) for j in jobs))
                entry['status'] = 'ok'
                print(name, mode, 'ok', len(jobs), flush=True)
                break
            except Exception as exc:
                attempt.update(status='blocked', error=str(exc))
                if mode == 'http':
                    # A fresh diagnostic response, not a cached substitute for parsing.
                    events = []
                    try:
                        document, final_url = fetch_document(url, audit=events.append)
                        html = soup(document)
                        for node in html.select('script,style'):
                            node.decompose()
                        visible = html.get_text(' ', strip=True)
                        attempt['diagnostic'] = {'url': final_url, 'network_requests': events,
                            'title': html.title.get_text(strip=True) if html.title else '',
                            'visible_characters': len(visible),
                            'javascript_required': any(marker in visible.lower() for marker in ('javascript', '启用js')),
                            'job_link_count': len(html.select('a[href*="/job_detail/"],a[href*="/jobdetail/"],a[href*="/job/"],a[href*="/wn/jobs/"]'))}
                    except Exception as diagnostic_error:
                        attempt['diagnostic'] = {'error': str(diagnostic_error), 'network_requests': events}
                print(name, mode, 'blocked:', str(exc), flush=True)
            finally:
                save_cache(report, args.output / 'validation.json')
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save_cache(report, args.output / 'validation.json')
    print(str(args.output.resolve() / 'validation.json'), flush=True)


if __name__ == '__main__':
    main()
