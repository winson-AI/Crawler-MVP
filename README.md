# 职位雷达 / Job Crawler MVP

通过可扩展的数据源适配器抓取公开社招职位。在本地按工作年限、地点、技术栈及关键词组合筛选，再用 ranker 排序，最后导出 JSON/CSV。增量同步先比较列表，仅补抓新增或列表信息变化的详情；拼多多默认只刷新列表并复用详情缓存，缺失或变化详情标记待补抓。支持详情缓存、断点恢复及人工验证暂停；新增与更新分开导出，可交给本地调度器每三天运行。Python **3.9+**，当前支持 **macOS / Linux**。百度、阿里、腾讯、美团、京东使用标准库；字节、拼多多使用 Playwright，通过正常官网页面读取公开响应。

## 快速开始

```sh
git clone https://github.com/winson-AI/Crawler-MVP.git
cd Crawler-MVP
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m jobcrawler serve
```

打开 <http://127.0.0.1:8000>，选择平台并刷新职位，随后筛选、排序和导出。拼多多还需要已安装 Google Chrome 以及图形界面，遇到交互验证码时需在抓取程序打开的窗口手动完成。

仓库包含程序、测试、公开职位样本和条件配置；`data/`、`output/`、浏览器会话及本地定时任务均不会随代码上传。首次使用需要自行抓取数据。

## 真实数据验证

| 数据源 ID | 官方入口 | 历史真实验证（2026-10-19，北京时间） |
| --- | --- | --- |
| `baidu` | [百度](https://talent.baidu.com/jobs/social-list) | 完成 86 页，按 ID 去重后 1714 条；首次发现 5 条，更新 0 条 |
| `alibaba` | [阿里集团](https://talent.alibaba.com/) | 完成 42 页，按 ID 去重后 4185 条；首次发现 71 条，更新 129 条 |
| `tencent` | [腾讯](https://careers.tencent.com/search.html) | 完成 22 页，按 ID 去重后 2156 条；首次发现 36 条，更新 56 条 |
| `meituan` | [美团](https://hr.meituan.com/web/social) | 完成 258 页，按 ID 去重后 2577 条；首次发现 36 条，更新 371 条 |
| `bytedance` | [字节](https://jobs.bytedance.com/experienced/position) | 完成 100 页，按 ID 去重后 10000 条；首次发现 75 条，更新 28 条 |
| `jd` | [京东](https://zhaopin.jd.com/web/job/job_info_list/3) | 完成 20 页，按 ID 去重后 1902 条；首次发现 48 条，更新 0 条 |
| `pinduoduo` | [拼多多集团](https://careers.pddglobalhr.com/jobs) | 列表 79 页：784 行、743 个唯一 ID；已保存 93 条可复用详情，剩余详情等待人工验证；正式缓存仍为 19 条部分样本 |

以上数字是当次真实快照。“完成分页”表示完成官方接口返回的范围，不能据此推断整个集团或公司的全部岗位数量，字节本次范围为官网报告的 10000 条。抓取期间官网内容会变化；快照分别记录原始行数、去重数及上游报告总量。首次发现按平台及职位 ID 相对于本地历史计算，不等同于当天新发布。拼多多已完成列表读取，但详情全量验证尚未完成。

本次运行 `output/incremental/20261019T080252Z-721fd42f/` 中，六个平台成功导出新增 **271 条**、更新 **584 条**。逐个平台以运行前历史和原始快照复核新增差集、内容更新、两批 ID 不重叠、历史推进以及 JSON/CSV 精确一致；真实快照还通过地点、年限、技术栈、关键词组合 selector 和四种 ranker 检查。证据为 `output/list-first-live-20261019T080245Z/validation.json`，缓存、断点和导出均为本地生成文件，不上传 GitHub。

腾讯首次比较 2156 条列表，只请求 92 条新增或变化的详情，其余 2064 条从旧快照建立缓存。在隔离状态下再次真实读取官网列表 2155 条，复用 2147 条详情，仅请求 8 条，导出新增 2 条、更新 6 条；列表变化不会被误报为零新增。拼多多中断后保留完整列表与逐条详情缓存；恢复的实际请求数和缓存命中数见报告，验证超时不覆盖其正式缓存或已见历史。弹窗状态已通过 Codex 查看真实截图复核为需要人工；不提供验证码解题或代填。

2026-10-20 以详情延后模式恢复 `output/incremental/20261019T102325Z-b64e3048/`：复用已完成的 79 页列表断点（784 行，738 个唯一 ID）和 731 条详情缓存，7 条详情明确待补抓；本次恢复没有访问官网列表或详情，也未启动浏览器。PDD 首次发现 723 条是相对本地历史的差集，不代表当天发布。正式快照记录 `details_complete=false`、`pending_detail_count=7`，保留原列表获取时间；该结果不等同于重新读取官网列表或完成所有详情。

2026-10-20 再次新建运行、实际读取官网列表：`output/incremental/20261020T021706Z-0c608032/` 完成 79 页，列表缓存复用 0 页，取得 741 个唯一职位，详情请求 0 次、复用 727 条、延后 14 条。耗时约 351 秒，出现 6 次持续至少 5 秒、触发人工接管的验证码提示，页码为 11、21、31、41、61、71；逐页时间及验证事件见 `output/pdd-list-live-zsz2iyp1/observation.json`。此结果证明列表优先仍可能频繁验证，不代表免验证。

测试命令 `python3 -m unittest discover -s tests -q` 最近通过 **146 项**，覆盖只取变化详情、缓存复用、分页过期重读、失败页重新请求、写入失败重试、历史已提交但报告写入失败后的导出恢复、旧断点防回退、详情延后与后续补齐、官网 50 条分页选择及切换未生效时安全失败、同线程跨轮会话复用、初始化请求分轮计数、连续/失败刷新冷却与冷却等待可中断，以及验证暂停/人工成功后继续、重复提示去重、脱敏事件历史、自定义 schema/JSON/DOM 解析与安全挂载、模板域名边界/租户绑定、自动适配与歧义拒绝、内嵌 JSON、多地点及段落保留、配置重载、后续页失败时规则与旧数据保留、Fake-IP 的安全 DoH 恢复/内网与混合答案拒绝/证书失败、逐轮 HTTP 证据、层级地址城市筛选和带逗号的页面条件恢复。历史上的六平台 71 条首次发现属于前一轮运行，不与本轮数字相加。

安装浏览器依赖（字节、拼多多需要）：

```sh
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

拼多多还需要本机安装 Google Chrome，并在有图形界面的环境中运行。它会打开独立 Chrome 窗口，不读取用户已有浏览器配置或登录资料。默认自动化浏览器上下文会返回 400023；适配器使用经真实请求验证的 Chrome 配置，复用列表和详情标签页以保留官网 `sessionStorage` 中的验证状态。在导航或翻页之前监听并缓冲同一职位/页码的响应，避免官网 SDK 已完成快速重试、程序却漏接成功响应。遇到 54001 时保持页面存活，等待官网 SDK 后续的最终成功响应；若官网要求交互验证码，由用户在该窗口手动完成，`crawl` 默认最多等待 180 秒，`sync` 默认 600 秒，程序不自动解答或绕过验证码。未完成验证、认证拒绝和接口错误均报错并保留旧缓存。缓存和导出仅包含规范化职位字段，不包含校验令牌。

## 启动页面

现已支持用户自主配置的网站：在页面展开“添加网站 / 适配规则 / 导入登录后的职位文档”，输入 URL 匹配模板，再预览并保存挂载。模板目录包含 24 项规则（7 个已有适配器、3 个公开 ATS 接口映射、4 个公开 DOM 模板、1 个匿名动态 DOM 模板、1 个已授权浏览器 DOM 映射、5 个站点识别入口、3 个通用探测器），不是所有网站均已验证可抓取。无模板或样式变化时自动探测 schema/JSON/内嵌 JSON/语义化 DOM，成功的字段规则持久保存、重启复用；失败保留旧快照。配置保存在独立的 `sources.local.json`；公开 GET、匿名浏览器、已授权 Chrome、文档导入明确区分，文档不会冒充联网刷新。[模板范围、API、自动适配与安全限制](docs/custom-sources.md)。HTML 解析依赖 `beautifulsoup4`，与浏览器依赖一起用 `python3 -m pip install -r requirements.txt` 安装。

Fake-IP DNS 现已支持安全恢复：仅当系统答案全部落入 `198.18.0.0/15` 时，使用固定公网 HTTPS DoH 解析；继续阻止内网/混合答案并校验目标 TLS，不修改系统 VPN/DNS。2026-10-20 已通过 serve 真实联网验收 Greenhouse 19 条、Lever 的 Zoox Software + Boston 列表 12 条、Ashby 63 条；均验证了网址匹配、失效路径自动恢复、规则保存、后台新请求入库、组合筛选、四种排序及 JSON/CSV 导出，未使用文档导入或本地列表缓存抓取。复现：服务启动后执行 `PYTHONPATH=. python3 scripts/verify_ats_live.py`；逐次 HTTP 时间/响应哈希/连接 IP 等证据见本机 `output/ats-live-20261020T035422Z/validation.json`。数量仅表示本次指定 URL 范围，不代表所有平台或公司的全站职位。

五平台新一轮实测：`output/portals-live-20261020T041605Z/validation.json` 中 BOSS 公开首页 81 条、智联公开首页 90 条、猎聘公开首页 53 条完成真实 HTTP → 失效选择器自动恢复 → 挂载 → 后台新请求入库 → serve 筛选/四种排序/JSON 与 CSV 导出。三者均为当前页精选范围、详情待补；智联首页无地点，90 条地点均保持未知，未假称地点筛选通过。前程无忧搜索页及拉勾首页尚无可靠职位结构，未入库，也未覆盖原数据。测试最近通过 151 项。

五平台默认无登录公开抓取：`PYTHONPATH=. python3 scripts/verify_portals_live.py`；静态页面走 HTTP，明确适配的动态公开页可用匿名浏览器，`--http-only` 则禁止浏览器。2026-10-20 14:30 起的一轮中，智联首页 90 条、猎聘首页 53 条通过；当时 BOSS、前程无忧、拉勾未通过。历史汇总见 `output/portals-live-20261020T063046Z/public-only-summary.json`，不与后续结果混算。

14:48、14:50 两轮已找到并实测新入口：拉勾 `https://campus.lagou.com/` 校招首页主职位区 9 条（HTTP）；前程无忧 `https://campus.51job.com/asiainfo/jobs.html` 亚信2027校招当前页 10 条（`mode=public_browser`）。均完成 URL 匹配、失效规则自动恢复、挂载、后台新请求入库、地点+关键词筛选、四种排序及 JSON/CSV 导出；未登录、未人工验证、未导入文档、未复用列表缓存。两轮职位 ID 一致；拉勾详情 URL 的 `show/source` 跟踪参数不再产生新 ID。两者均为限定当前页，详情待补，不代表全站或该企业全部职位。证据：`output/portals-live-20261020T065007Z/public-validation.json`。

BOSS 本轮首页及官网链接的无障碍公开入口均转向安全验证页，仍未跑通；保留旧职位但不计作新成功，不破解或规避验证。仅复跑新公开入口：`PYTHONPATH=. python3 scripts/verify_portals_live.py --platform 51job --platform lagou`。匿名浏览器用标准无头 Chrome 和全新隔离上下文执行正常官网脚本，不连接用户 Chrome、不读取已有 Cookie，不重写官方 SDK 的签名逻辑；遇到验证直接停止。快照 `acquisition=public_browser_network`，记录匿名/缓存标记及本轮响应哈希；SDK JSONP 只作为 JSON 数据解包，URL 查询参数不写入网络证据。

最近验证：`python3 -m unittest discover -s tests -q` 通过 162 项，另通过网页脚本语法检查和 `git diff --check`。本轮只复用既有 parser/serve 链路补充公开模板和匿名传输，没有新增依赖。

仅显式 `--browser-session` 或配置 `mode=browser` 才复用用户已授权 Chrome：须手动开启 remote debugging 并确认 Allow，服务使用单线程持有连接，不导出 Cookie、不关闭原浏览器。每轮禁用缓存/Service Worker 缓存，核对本轮响应；只读当前 URL，不猜接口或全站分页。动态 DOM 缺少链接时可解析本轮已观察到的职位 JSON；多个不同集合仍拒绝，不自行重放认证 API。登录/验证码不会自动处理。此前 `output/portals-live-20261020T062828Z/validation.json` 的拉勾 9 条是已授权浏览器实测，不是当前匿名抓取成功；其地点保持未知。匹配浏览器 DOM 模板本身不自动连接用户会话。

在项目根目录执行：

```sh
python3 -m jobcrawler serve
```

打开 <http://127.0.0.1:8000>，选择招聘数据源，点击“刷新全量职位”。抓取在后台执行，页面显示页码与进度；完成后自动应用筛选。百度首次全量抓取通常需要 1–2 分钟，后续筛选直接读取本地缓存。页面记住条件、数据源和排序方式，结果可展开查看原始任职要求与工作职责，并打开原始职位详情。JSON/CSV 导出使用页面当前的筛选和排序。

端口可通过 `--port 8001` 修改；默认仅监听本机回环地址。百度缓存位于 `data/jobs.json`，其他数据源位于 `data/jobs.<source>.json`。`--cache /path/to/jobs.json` 修改缓存基准路径，其他源仍按同样规则加后缀，避免互相覆盖。

## 命令行

```sh
# 抓取全部职位；串行分页，默认每页等待 0.4 秒
python3 -m jobcrawler sources
python3 -m jobcrawler crawl --source baidu
python3 -m jobcrawler crawl --source alibaba --max-pages 2
python3 -m jobcrawler crawl --source tencent --max-pages 2
python3 -m jobcrawler crawl --source meituan --max-pages 2
python3 -m jobcrawler crawl --source bytedance --max-pages 2
python3 -m jobcrawler crawl --source jd --max-pages 2
python3 -m jobcrawler crawl --source pinduoduo --max-pages 2

# 快速试抓两页（导出与页面会明确标示“部分数据”）
python3 -m jobcrawler crawl --max-pages 2

# 使用条件文件筛选缓存
python3 -m jobcrawler filter --selector selector.example.json --output output/matches.json
python3 -m jobcrawler filter --selector selector.example.json --format csv --output output/matches.csv

# 工作年限降序；同样支持 published_asc / published_desc / experience_asc
python3 -m jobcrawler filter --source baidu --selector selector.example.json --ranker experience_desc --output output/ranked.json
python3 -m jobcrawler filter --source alibaba --selector selector.example.json --ranker published_desc --format csv --output output/alibaba.csv

# 验证核心逻辑和本地 HTTP 服务
python3 -m unittest discover -s tests -v

# 重新验证六个新增源的真实抓取、组合 selector、四种 ranker 和 JSON/CSV
# 每个源默认两页；任一源失败仍继续检查其他源，最后返回非零退出码
python3 scripts/verify_sources.py
# 复用已抓到的真实快照；没有缓存的源仍会真实请求
python3 scripts/verify_sources.py --reuse-cache
# 仅验证指定来源
python3 scripts/verify_sources.py --sources alibaba tencent

# 五家全量抓取并验证；成功分页保存为本轮任务检查点
python3 scripts/verify_sources.py --sources alibaba tencent meituan bytedance jd --max-pages 0 --require-complete --checkpoint-dir output/run-pages --output output/full-run
# 验证已经完成的全量快照及导出
python3 scripts/verify_sources.py --sources alibaba tencent meituan bytedance jd --reuse-cache --require-complete --output output/full-run
```

抓取及筛选失败会返回非零退出码。JSON 导出保留 selector、ranker、数据源、缓存时间、完整性标记和逐条匹配依据。CSV 使用 UTF-8 BOM，可用 Excel 打开，包含数据源、公司、发布时间、职责和要求全文。

真实验证报告默认保存至 `output/verification/source-verification.json`，包含每个来源的抓取时间、数量、完整性、真实样例、组合筛选条件、四种排序检查和失败原因；旁边保存筛选后的 JSON/CSV，以及当前快照全部记录的 `*-all.json/csv`。`--reuse-cache` 会注明使用已有真实快照。上游缺少职责或任职要求时记录缺失数量，不伪造文本；检查失败时额外保存 `*-failed-snapshot.json` 供排查，不替换正式缓存。

`--checkpoint-dir` 保存本轮成功分页；网络异常后使用同一目录重试可复用 HTTP 来源的成功页。浏览器来源会重放导航以建立会话，检查点仅用于核对。新一轮抓取或更改分页配置时应使用新目录，避免混入旧分页。六家新增来源均有真实职位样本；另有专门的 HTML、验证码占位响应、超时和分页契约测试。

## Selector

页面“招聘类型”支持不限、校招、社招和未知，对应 `recruitment_type: all/campus/social/unknown`，默认不限（含未知）。可与地点、年限、技术及关键词组合，保存条件和 JSON/CSV 导出同步生效。优先使用明确的招聘类型，其次识别标题/类别中的校招或社招标记、已适配的校招入口、内置社招来源；不根据零年限、实习或职责中出现的“校园”猜测。旧快照读取时兼容补充该字段，不需要重新抓取；类型未知不会混入指定校招/社招结果。

示例见 `selector.example.json`：

```json
{
  "recruitment_type": "all",
  "locations": ["北京", "上海"],
  "years": 3,
  "include_unknown_experience": false,
  "technologies": ["Python", "Java"],
  "technology_mode": "any",
  "keywords": ["研发", "算法"],
  "keyword_mode": "any",
  "exclude_keywords": ["实习"]
}
```

| 字段 | 行为 |
| --- | --- |
| `locations` | 地点任选其一；精确匹配，支持北京/北京市，支持职位的多个地点 |
| `years` | 候选人的工作年限，例如 3；要求 3–5 年的职位可匹配 3、4、5；5 年以上可匹配 5 及以上。省略或 `null` 表示不限 |
| `include_unknown_experience` | 设置年限时是否保留未知年限的职位；默认 `false` |
| `technologies` | 标题、要求、职责中匹配技术词；大小写不敏感，英文使用词边界；支持 Go/Golang、K8s/Kubernetes 等别名及自定义技术词 |
| `technology_mode` | `all` 全部技术词命中（默认），或 `any` 任一命中 |
| `keywords` | 标题、要求、职责中匹配文字片段；大小写不敏感 |
| `keyword_mode` | `all` 全部关键词命中（默认），或 `any` 任一命中 |
| `exclude_keywords` | 任一片段命中即排除 |

不同字段之间为 AND。空数组不限制该字段。未知字段或无效数据类型会报错，防止条件拼写错误造成错误结果。`{}` 返回全部缓存职位。

年限首先读取站点 `workYears`，为空时从任职要求中提取数字/中文数字的范围、以上/以下和经验不限。原文中的优先条件不作为硬性年限要求；无法识别的文本标记为 `unknown`，不会自动当成零年。结果保存 `experience.min/max/evidence/source`，方便核对。自然语言中的多重条件、模糊描述仍需查看原文；技术栈命中表示文本中出现该技术，不代表所有列出的技术都为必备技能。

## 本地 API

| 请求 | 用途 |
| --- | --- |
| `GET /api/sources` | 注册的数据源列表，页面自动生成数据源选项 |
| `GET /api/status?source=baidu` | 当前抓取进度、错误、所选数据源缓存元信息 |
| `POST /api/crawl` | 请求体 `{"source":"baidu","max_pages":0}` 启动后台全量抓取；返回 202，已有抓取时返回 409 |
| `GET /api/jobs?source=baidu&selector=...&ranker=published_desc` | URL 编码的 JSON selector；筛选后排序，返回匹配数量和职位 |
| `GET /api/export?source=baidu&selector=...&ranker=experience_asc&format=json` | 导出筛选并排序的 JSON |
| `GET /api/export?source=baidu&selector=...&ranker=experience_asc&format=csv` | 导出相同顺序的 CSV |

省略 `source` 时默认百度，省略 `ranker` 时默认 `published_desc`。selector JSON 结构保持不变，排序与数据源通过独立参数传入。

## Ranker

| 名称 | 行为 |
| --- | --- |
| `published_desc` | 发布时间从新到旧，默认 |
| `published_asc` | 发布时间从旧到新 |
| `experience_asc` | 职位要求的最低工作年限从低到高 |
| `experience_desc` | 职位要求的最低工作年限从高到低 |

年限范围按下限排序，例如 3–5 年的排序值为 3；经验不限为 0。所有方向下，未知年限、缺失或无效发布时间均排在最后。同值保持输入顺序，排序不会修改缓存。发布时间使用 `published_at`，与 `updated_at` 分开；日期支持 ISO 8601 日期或带时区的时间，无时区值按 UTC 比较。旧缓存缺少发布时间时保留为空，刷新该数据源后补齐，避免把更新时间冒充发布时间。

## 添加招聘网站

所有数据源遵循 `jobcrawler/sources/base.py` 中的 `JobSource` 协议：提供 `id`、`name`、`url` 和 `fetch_page(page)`。需要另取详情的来源再提供 `fetch_list_page(page)` 和 `fetch_job_detail(job)`，供增量同步先比较列表；腾讯和拼多多已拆分，其他五家的列表响应已包含完整职位。`crawl` 和页面“刷新全量”仍使用完整 `fetch_page`，需要减少详情请求时使用 `sync`。页码从 1 开始，返回 `JobPage(page, total, jobs)`；`total` 为上游报告的数量。可选 `has_more` 表示明确的分页终点，避免上游重复行造成收集数量永远达不到总数。快照另记录 `scanned` 和 `duplicates`；京东以发布记录 `id` 为标识，`requirementId` 可能被多个发布记录复用。网络请求、分页、解析及字段映射均由适配器处理。

统一结构由 `jobcrawler/models.py` 的 `build_job()` 创建，复用技术栈提取和年限解析。下方是一个可运行的静态样例；接入真实网站时替换 `fetch_page` 内的数据获取和字段映射：

```python
# jobcrawler/sources/demo.py
from ..models import build_job
from .base import JobPage

class DemoSource:
    id = "demo"
    name = "示例招聘"
    url = "https://example.com/jobs"

    def fetch_page(self, page):
        jobs = [build_job(
            id="123", title="Python 研发工程师", source=self.id,
            company="示例公司", url="https://example.com/jobs/123",
            locations=["北京市"], published_at="2026-10-01",
            requirements="3-5年开发经验，熟悉Python", description="后端服务研发",
        )] if page == 1 else []
        return JobPage(page=page, total=1, jobs=jobs)
```

然后在 `jobcrawler/sources/__init__.py` 导入 `DemoSource`，向 `SOURCES` 增加 `"demo": DemoSource()`，重启服务即可。数据源 id 使用小写字母、数字或下划线，注册键应与实例 id 一致。CLI 可用 `--source demo`，页面会自动出现新选项；无需修改 selector、ranker 或导出逻辑。

需要浏览器的适配器可以提供 `session()` 上下文管理器，通用 crawler 会在整个抓取期间复用会话，并在成功或异常退出时关闭自己拥有的浏览器。本地网页服务的 PDD 专用线程拥有跨轮会话，内部抓取只借用，服务关闭时才释放。单独调用 `fetch_page(1)`、且没有外层会话时，也会自动建立和关闭会话；拼多多在同一轮中必须按页码顺序读取。

每个职位至少包含稳定 `id`、`title`、`source`、`company`、HTTP(S) 详情 `url` 和 `locations`；发布日期和更新日期使用 ISO 8601，未知值留空。如果网站提供结构化年限，可向 `build_job(experience=...)` 传入 `{"min":3,"max":5,"evidence":"3-5年","source":"structured"}`；否则提供 `work_years` 或 `requirements` 复用现有解析。职位 `source` 必须等于适配器 id，id 在该数据源内唯一。

抓取接口是页面公开使用的 `POST https://talent.baidu.com/httservice/getPostListNew`，请求格式为表单，`recruitType=SOCIAL`、`curPage`、`pageSize=20`。每条返回数据包含完整要求和职责，无需额外逐条请求详情。

新增站点请求仅存在于对应适配器：阿里从公开页面读取 CSRF 和社招 channel 后 POST `/position/search`；腾讯 GET `/tencentcareer/api/post/Query` 和 `/ByPostId`；美团 POST `/api/official/job/getJobList`；京东表单 POST `/web/job/job_count` 与 `/web/job/job_list`；字节正常页面发起 `/api/v1/search/job/posts`；拼多多正常页面发起 `/api/recruit/position/list` 与 `/detail`。所有源都输出 `build_job()` 的统一结构，并使用同一套缓存、selector、ranker 和导出。

腾讯只有更新时间，美团本次返回的 `firstPostTime` 为空，所以这些职位的 `published_at` 保留未知，发布时间排序时排到最后；阿里和字节的毫秒时间戳按 UTC 转为 ISO 8601，京东同样保留原始时间戳对应的真实时区时刻。阿里优先使用结构化 `experience.from/to`，其他来源复用工作年限字段或任职要求提取。

抓取会按适配器的职位 `id` 去重，验证页码，并检测空页或重复页造成的停滞；最多 1000 页。连接失败、429 和部分 5xx 最多尝试 3 次；百度 HTTP 超时 20 秒，其他 HTTP 来源 25 秒，浏览器导航 45 秒；认证拒绝及数据结构变化直接报错。成功后以临时文件原子替换缓存，抓取失败保留原有缓存。结果是一次分页抓取的快照，抓取期间源站职位变化可能导致重复或遗漏，可再次刷新。

## 增量同步、详情缓存与恢复

可在本地 Codex 定时任务或系统调度器中配置每隔三天执行 `python3 -m jobcrawler sync`，工作目录设为项目根目录，使用安装依赖的 Python 环境。当前开发工作区已配置每三天首尔时间 09:00（北京时间 08:00）运行；此配置不随 Git 克隆自动安装，其他使用者需要自行登记。电脑需要保持开机、联网并运行对应调度器。默认覆盖注册表中的所有平台，新接入的平台也会自动加入。

```bash
# 首次部署：仅将现有缓存登记为已见，不访问网站。
python3 -m jobcrawler sync --initialize

# 完整比较列表；PDD 默认延后详情，其他来源只补抓缺失或变化详情。
python3 -m jobcrawler sync

# 可选：沿用同一套 selector 和 ranker 处理新增导出。
python3 -m jobcrawler sync --sources baidu tencent --selector selector.example.json --ranker experience_asc

# 按 report.json 返回的运行目录恢复；成功来源会自动跳过。
python3 -m jobcrawler sync --resume output/incremental/实际运行目录

# 仅恢复拼多多，复用详情缓存，剩余详情延后。
python3 -m jobcrawler sync --resume output/incremental/实际运行目录 --sources pinduoduo --pdd-details defer

# 需要补齐详情时显式执行；复用同一份列表断点，仍可能需要人工验证。
python3 -m jobcrawler sync --resume output/incremental/实际运行目录 --sources pinduoduo --pdd-details full --verification-wait 600
```

历史文件为 `data/sync-state.json`，以平台及职位 ID 去重。已有缓存时先执行 `sync --initialize` 建立基线；全新安装没有缓存时，首次 `sync` 会将抓到的全部职位作为首次发现。开发工作区的首次验证基线为 22,518 条，后续手动增量运行中六个平台成功发现 71 条，拼多多等待验证的失败运行保留其旧缓存和历史。同一职位改名、更新、下架后重新出现均不会再次作为新增；不同平台的同名或同 ID 职位分别保留。“新增”指相对于本地历史首次发现，可能包含源站早已发布但此前尚未收集的职位，尤其是当前缓存只有部分样本的平台。

对腾讯、拼多多，列表按 ID、标题、地点、发布时间、更新时间、类别及部门生成比较信息。详情缓存存于 `data/details/<source>/`：列表信息相同就复用；腾讯对新增、列表变化或缓存缺失时补抓详情，拼多多新运行默认 `--pdd-details defer`，这些职位只更新列表字段，保留已有详情文本并标记 `details_pending=true`，不请求详情。需要补齐时用 `--pdd-details full`；恢复默认沿用原模式，旧版未记录模式的运行沿用 full，可显式切换。旧版完整缓存可作为首次详情缓存的基线，待补抓快照不会被误当成完整详情缓存。其他五家无需额外详情请求，直接比较列表已包含的完整字段。如果官网更新了详情却完全没有改变这些列表字段，当前方案无法保证发现该次修改；它也不是源站的事务快照，分页期间职位移动仍可能造成重复或遗漏。

`complete` 表示列表分页完整，`details_complete` 和 `pending_detail_count` 单独描述详情是否齐全；JSON 和 CSV 均保留逐职位 `details_pending` 标记。网页 PDD 默认按钮为“刷新列表（详情延后）”，待补抓职位显示提醒，年限、技术栈及全文筛选可能因缺少或过期详情而不完整。列表自身仍可能触发官网验证，详情延后不保证免验证。

每次运行在 `output/incremental/<UTC时间-运行标识>/` 保存 `report.json`、`*-new.json/csv` 和 `*-updated.json/csv`。更新导出只包含已见 ID 中内容发生变化的职位，不会重新计作新增。两类导出复用相同 selector、ranker 和 CSV 处理，默认按发布时间倒序，未知日期放在最后。筛选只影响导出，已见历史记录本次全部职位。`report.json` 的 `acquisition` 包含列表网络页数、列表缓存页数、详情请求数、详情缓存命中数及旧快照复用数；这些是应用层操作次数，HTTP 重试或官网 SDK 验证可能产生额外网络请求。网页继续读取各平台最新缓存。

分页、完整列表、原始职位快照及本轮开始时的去重历史保存在运行目录的 `checkpoints/<source>/`。每条成功详情立即写入独立缓存；后续失败不会丢失已完成详情。`--resume` 复用同一轮已完成列表和详情，恢复时沿用原 selector、ranker、缓存路径及抓取间隔；可显式传入 `--delay 3` 将后续请求间隔调整为 3 秒，实际值写回报告，下次恢复沿用。HTTP 来源可复用已完成分页；未完成且已超过 15 分钟的 HTTP 列表前缀会另存并重新读取，以降低新旧分页混合风险。拼多多必须重放已完成分页来重建正常浏览器会话；如果完整列表已经保存，就直接恢复详情。新一轮 `sync` 会重新读取列表，但仍保留详情缓存。旧版报告不支持新版 `--resume`，旧职位快照仍可作为缓存基线。

仅完成列表分页、且满足所选详情模式的来源才更新正式职位快照和已见历史；defer 可成功提交待补抓状态，full 要求全部详情成功。暂停或失败时只保存断点/详情缓存，其他来源继续执行。导出先落盘，再推进历史；写入失败可恢复，需结合报告状态判断文件是否属于已成功提交的批次。旧断点不能覆盖另一轮已完成的更新快照。运行锁防止并发同步；不要删除或重置 `data/sync-state.json`，否则旧职位会重新被视为新增。

## 验证暂停与 Codex 视觉复核

拼多多通过官网公开的页容量选择器切换到“50 条/页”，以减少全量分页次数。2026-10-20 的新一轮实测 `output/incremental/20261020T022533Z-44696838/` 已完整读取官网 16 页（首批 50 行、末批 34 行，合计 784 行，去重后 772 个职位），列表缓存复用 0 页；另有 1 次默认 10 条列表的初始化请求，由 `acquisition.listing_setup_requests` 单独记录，不与正式的 `listing_network_pages` 混算。出现 2 次人工验证提示，位于第 1、10 页，均通过后继续；整轮耗时约 272 秒，包含等待验证。详情请求 0 次、复用 734 条、38 条明确延后。逐页时间和验证事件见 `output/pdd-list-50-live-ecmeq6b5/observation.json`。与前一轮 10 条/页实测的 79 页、6 次提示相比，本轮分页和人工打断均减少，但不代表稳定免验证；官网内容和会话状态也可能变化。如果切换后首批数据不足所选页容量，程序报错保留旧缓存，不误报全量成功。

拼多多 `sync` 的列表翻页和完整详情补抓至少间隔 3 秒；`--delay` 设置更长时取更长值，其他来源不受该下限影响。报告 `acquisition.request_delay` 记录各来源的实际抓取间隔，`details_deferred` 记录延后的详情数量。断点恢复时复用已完成列表和详情，不为缓存命中或延后详情启动浏览器。降频旨在减少重复请求和人工打断，不保证源站免验证。

本地网页服务在同一后台线程保留 PDD 专用浏览器、上下文和列表标签页；再次点击“刷新列表（详情延后）”时，通过官网原生搜索按钮重新读取全部列表，保留当前会话及 50 条/页设置，而不是新建标签页或读取列表缓存。报告 `acquisition.listing_page_reused` 表示是否复用活跃列表页，`listing_setup_requests` 只统计本轮额外初始化请求；`verification_handoffs` 统计本轮需要人工接管的次数（同一次等待超时不重复计数），`verification_cleared` 记录收到官网成功响应的已解除提示次数。复用只在该网页服务进程及浏览器存活期间有效，不读取用户 Chrome 配置，也不导出或伪造验证令牌；服务重启、独立 CLI 运行、浏览器关闭或官网会话过期仍可能要求验证。

网页 PDD 全量列表刷新目前使用 6 秒页间隔；每轮任务结束（包括失败）后，下一轮在同一服务内至少冷却 120 秒。提前点击会排队等待，网页显示冷却提示，等待期间不读取源站列表，也不把旧列表当成新刷新；浏览器会话继续保留。关闭服务可以中断冷却。独立 CLI 不受这项跨轮冷却控制，需自行合理安排运行频率；可用 `sync --sources pinduoduo --delay 6 --pdd-details defer` 设置页间隔。6 秒和 120 秒是本地保守降频参数，不是官网公布的限额或免验证保证。

2026-10-20 通过本地服务连续实测两轮：新会话建立轮 `output/incremental/20261020T023903Z-f2315929/` 和随后同一窗口复用轮 `output/incremental/20261020T024000Z-8833ed67/` 均成功刷新 16 个真实列表页、772 个唯一职位，列表缓存/重放均为 0，人工接管均为 0 次；观察耗时分别约 61 秒和 56 秒。第二轮 `listing_page_reused=true`、`listing_setup_requests=0`，第一轮则为 false、1，确认没有关闭重建列表页。两轮详情请求均为 0，734 条详情复用、38 条继续延后；没有新增或内容更新。逐轮状态及报告路径保存在 `output/pdd-session-live-gkk8mqqz/observation.json`。相比之前的 2 次提示，本次样本无需人工输入，但新会话建立轮本身也是 0 次，不能把下降全部归因于复用，更不能推断官网永久免验证。

随后同会话确认轮 `output/incremental/20261020T024153Z-e8c4ba2f/` 仍在第 9 页出现 1 次人工接管提示，收到成功响应后完成 16 页；`listing_page_reused=true`、初始化请求 0，排除了该轮冷启动，但不能排除官网重新评估会话。此前两轮间隔约 0.85 秒，该确认轮前约 57 秒，页间隔为 3 秒；累积请求频率是降频试验的依据，不是已证实的触发规则。提示约在第 30 秒出现，第 34 秒已回到正常分页；现有观测无法确认中间是否有人实际输入，不能把提示次数当成实际手动输入次数。

调整为网页页间 6 秒、跨轮冷却 120 秒后，实测新会话建立轮 `output/incremental/20261020T024917Z-acdc0252/` 初始化时有 1 次提示并解除，完整读取 16 页；随后复用轮 `output/incremental/20261020T025309Z-200a190a/` 等待冷却后，完整读取 16 页、772 个职位，第 9 页正常通过，人工接管提示 0 次。复用轮 `listing_page_reused=true`、初始化请求/列表缓存/列表重放均为 0，详情请求 0、详情命中 734、延后 38，无新增或更新。复用轮实际抓取约 93 秒，连同冷却等待约 212 秒；冷启动轮约 113 秒，不能与复用轮的 0 次提示混算。逐轮时间和状态见 `output/pdd-paced-live-t87d0599/observation.json`。该样本支持当前节奏可正常刷新，不证明永久免验证，也不能用这次同时改变两个参数的运行单独识别触发机制。

拼多多的正常官网 SDK 校验由页面自行完成。出现交互验证码时，程序保持窗口并等待人工处理；收到匹配职位/页码的真实成功响应后继续。同步任务超过 `--verification-wait`（默认 600 秒，允许 0–1800 秒）则将该来源标为 `paused`，保存 `verification.json`、`verification.png` 和恢复目录，并继续处理其他来源。进程结束后窗口会关闭；之后执行 `--resume` 会建立新的正常会话，官网可能再次要求人工验证。部分新详情不会提前记入已见历史。

`verification.json` 和 `verification.png` 仅表示最新状态；`verification-events.jsonl` 追加保存每次提示、超时通知或解除事件的时间、公开页码/职位 ID，以及当时的会话复用、页间隔、列表页数、详情请求数和提示/解除计数。历史事件不引用会被覆盖的截图，也不保存验证令牌、原始请求体或答案。重复超时通知会保留为事件，但不会增加同一轮的人工接管次数。日志描述的是提示与官网响应，不能据此确认人实际输入过几次，也不能反推出官网未公开的风控规则；旧运行不会补造历史事件。

清空数据后的实测 `output/fresh-all-20261020T025729Z/` 中，PDD 使用全新浏览器，16 页、6 秒页间隔、详情请求 0 次，仍出现 2 次提示，最后一次在第 10 页并解除。批处理结束后该浏览器已关闭，随后启动的服务不能继承其活跃标签页。后续重复刷新优先使用持续运行的网页服务，避免反复执行独立 CLI 后退出，或为分析日志而重开浏览器；不要为了追求“免验证”连续启动全量试抓。第 10 页是在同一轮会话内再次校验，不能将所有提示都归因于冷启动，也不能保证延长等待就会免验证。

Browser Agent 先等待最多 5 秒，给短暂的官网 SDK 校验留出完成时间；之后检测到可见交互验证输入框时才将对应窗口置前、记录经过清理的职位页面链接及待处理状态，并在终端给出证据路径。总验证等待上限仍由 `--verification-wait` 控制，设置短于 5 秒时不会额外延长。短暂校验不再提示人工接管；验证通过后的提示也不再要求输入。它只负责检测、截图、人工接管及观察官网是否成功，不点击或填写验证码。可以让 Codex 查看该截图，仅分类为 `ready`、`needs_human` 或 `unknown`，然后记录复核结果：

```sh
python3 -m jobcrawler verification-review --evidence output/incremental/实际运行目录/checkpoints/pinduoduo/verification.json --state needs_human
```

这会生成带截图校验值的 `visual-review.json`。当前采用 Codex 查看本地截图的方式，未接入外部 VLM 服务；视觉结果只用于判断是否需要人工及通知，不会识别或转录验证码题目、计算答案、代填或提交。即使视觉结果是 `ready`，也不能代替官网成功响应，更不能解除校验。截图、会话凭据和验证令牌不会上传 GitHub；缓存及导出只保存规范化职位字段。

也可以使用视觉模型做同样的只读分类（需要设置 `OPENAI_API_KEY`）：

```bash
python3 -m jobcrawler verification-ai-review --evidence output/.../verification.json
```

模型仅返回 `ready`、`needs_human` 或 `unknown`；请求不包含职位接口令牌，结果仍不会解除服务端验证，也不会解题、填充或提交验证码。可用 `OPENAI_VISION_MODEL` 覆盖默认模型，或用 `--model` 指定单次模型。

CLI 退出码按整个运行报告计算，包括恢复时未选中的来源：`0` 全部完成；`1` 存在普通错误；`2` 存在验证暂停且没有普通错误。`report.json` 分别记录 `failed_sources` 与 `paused_sources`。定时任务应报告等待人工验证并提供截图/恢复路径，不能循环尝试解题。定时任务不会自动修改代码或安装依赖。

## 代码结构

- `jobcrawler/sources/`：数据源协议、注册表、七家适配器、公共 HTTP/浏览器读取工具。
- `jobcrawler/models.py`：各数据源共享的职位结构。
- `jobcrawler/crawler.py`：通用分页抓取、独立缓存、CSV 导出。
- `jobcrawler/sync.py`：跨次去重历史、运行锁、增量导出及单平台失败隔离。
- `jobcrawler/incremental.py`：先比较列表、详情缓存与分页/详情断点恢复。
- `jobcrawler/verification.py`：人工验证证据及 Codex 页面状态复核记录。
- `jobcrawler/selectors.py`：年限提取、组合筛选。
- `jobcrawler/rankers.py`：发布时间和工作年限排序。
- `jobcrawler/server.py`、`jobcrawler/web/index.html`：本地 API 和筛选页面。
- `jobcrawler/__main__.py`：CLI。
- `tests/`：分页、去重、年限、技术词边界、组合筛选、四种排序、独立缓存、多源适配与 API/导出验证；官方真实样本位于 `fixtures/`。
- `scripts/verify_sources.py`：可重复运行的真实抓取与共享处理流程验证。

站点接口变化时仅更新对应适配器。拼多多会等待官网验证流程结束再读取真实职位，验证中间态或失败结果不会作为空职位写入缓存。
