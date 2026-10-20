# 用户网站配置与适配

网页展开“添加网站 / 适配规则 / 导入登录后的职位文档”，输入 URL → “匹配并复用模板” → 解析预览 → 保存挂载。也可以手工编辑配置。预览只读；挂载前校验每条职位的字段。保存后立即出现在数据源列表，公开 HTTP 来源后台抓取配置范围内的分页；文档来源立即填入本次文档中的职位。

配置保存至项目根目录 `sources.local.json`，不上传 Git，不与 `data/` 职位缓存一起清空。服务及新 CLI 进程启动时自动加载，无需改 Python 注册表。ID 必须为 `custom_` 开头，不能覆盖内置平台；同 ID 再次提交用于修改适配规则。配置错误不覆盖旧配置；样式/字段校验失败不写入旧职位缓存。配置版本与快照不同会在页面提醒刷新。

## 解析方式

- `auto`：优先识别 JSON-LD / JSON 中的 Schema.org `JobPosting`；普通 JSON 使用数组映射；HTML 配置了 `items` 时再尝试 CSS 解析。
- `schema`：读取页面 `application/ld+json`、`@graph` 或 JSON `JobPosting`，映射标题、稳定标识、链接、公司、地点、描述、要求、发布日期。
- `json`：`items` 为数组点分路径，根数组用空字符串；`fields` 为公共职位字段到原始 JSON 路径的映射。路径支持数字数组下标和 `*` 数组遍历，如 `secondaryLocations.*.location`，不执行表达式。映射值可以是路径列表，地点合并去重，文本以换行连接（Lever 的职责段落不会丢失）。`json_script` 为 HTML 中 script 的零起始序号，用于读取内嵌 JSON，不执行脚本。
- `dom`：`items` 为每条职位卡片的 CSS 选择器；字段规则为 CSS 字符串或 `{"selector":"a.title","attr":"href"}`。空 selector 表示卡片本身。地点会收集全部匹配项，其他字段取首项。字符串形式的 URL 字段默认读 `href`，其余取文本。

公共字段复用现有 `build_job`：`id/title/url/company/locations/requirements/description/published_at/updated_at/department/category/work_years`。默认必须匹配标题、URL；无 ID 时从规范化职位 URL 生成稳定 ID，不用标题或抓取时间生成。`required_fields` 可以增加必须存在的职责等字段；未知日期/年限不伪造。没有职责和要求的列表项标记 `details_pending=true`。

serve 条件默认用逗号、中文逗号、顿号或换行分隔；地点/关键词自身含逗号时加双引号，例如 `"Foster City, CA"`。引号内的双引号写成 `""`。已保存条件重载时会自动补回必要引号，避免英文地点被拆成两个地点；年限未知时需勾选“保留未明确年限的职位”或留空年限。

## DOM 适配示例

```json
{
  "id": "custom_company",
  "name": "某公司招聘",
  "url": "https://example.com/jobs",
  "mode": "http",
  "parser": "dom",
  "items": ".job-card",
  "fields": {
    "title": ".job-title",
    "url": {"selector": "a.job-title", "attr": "href"},
    "company": ".company",
    "locations": ".location",
    "work_years": ".experience",
    "description": ".description"
  },
  "required_fields": ["title", "url", "description"]
}
```

默认 `auto_adapt=true`：当前规则失败时，在同一份文档中尝试 JobPosting、常见字段别名的 JSON 职位数组、内嵌 JSON、语义化职位 DOM 卡片。候选必须通过原有 required_fields 与分页校验；多个不同职位集合会拒绝，不能自动把文章/菜单数组当职位。不会自动跟随职位链接或猜测详情接口。无法适配时修改 items/fields 再预览；`auto_adapt=false` 可要求严格使用手写规则。真正的空职位页面可用 `empty_selector` 明确指定；已配置路径的 JSON 显式空数组也允许表示零职位，未知空数组不会用于学习规则。

预览返回成功映射；挂载后保存到 `sources.local.json`。serve 后续刷新中发现样式变化时，也会在整轮抓取成功后保存学习规则；后续页失败不保存新规则、不覆盖旧职位。CLI 会执行自动探测，但目前不把刷新中学习到的规则写回配置文件；持久化请通过网页/API 挂载。快照记录 template_id、adapter_revision、adaptation 与 scope。登录/错误响应不能当职位入库，缺失职责/要求仍标记待补抓，不编造数据。

## 网站模板目录（2026-10-20 联网整理）

`jobcrawler/sources/templates.json` 包含 24 项目录规则，不是全球招聘网站全集，也不表示 24 家都已完成联网抓取：

- 7 个已有专用适配器：百度、阿里、腾讯、美团、字节、京东、拼多多。匹配时可选择切换已有平台库；不会把用户输入的详情页/筛选参数默默替换成全平台范围。
- 3 个公开 ATS 字段模板：Greenhouse、Lever（全球/EU）、Ashby。按官方接口文档映射；仅适用于明确的企业职位板/列表路径。保留 Lever 支持的地点/团队等过滤参数；不支持的过滤或详情 URL 原样交给通用 parser，不扩大为全板。
- 4 个公开 DOM 模板：BOSS、智联、猎聘首页和 `campus.lagou.com` 校招首页主职位区。仅适用于对应无查询参数路径，不能替代登录后的搜索列表；智联首页未提供的地点/年限保持未知。精确校招主机优先于父站模板。
- 1 个匿名动态 DOM 模板：`campus.51job.com/asiainfo/jobs.html` 亚信校招当前页。只绑定该租户路径，不把任意校园页替换成亚信；由匿名浏览器正常加载公开脚本，无需登录/Chrome 授权。
- 1 个已授权浏览器 DOM 模板：拉勾首页 `/` 或 `/wn/` 的热门职位卡片；保留真实标题、链接、公司、年限，不能从公司简介推断职位城市。
- 5 个站点识别入口：前程无忧、LinkedIn、Indeed、Glassdoor、Dice。仅识别不代表可抓取；前程无忧可选下述已授权浏览器模式，其余可提供有权使用的职位文档。站点公开可见不等于允许转载。
- 3 个通用探测器：JobPosting、JSON/内嵌 JSON、DOM。

匹配优先级：本机已校验配置 → 已知站点模板 → 通用 parser。本机复用限定同协议/域名/路径，不能跨租户借用 API 或分页参数；新查询仍需重新校验。域名使用完整主机名或显式子域边界，`zhipin.com.evil.com` 不会匹配 BOSS。

`template-examples.json` 保存最小真实字段投影及来源，用于回归；Ashby 为公开职位样本，Lever 明确标记为官方演示数据，不自动写入正式职位库。接口模板不是职位缓存，新的 HTTP 刷新仍实际请求目标网站。`fetch_url` 区分用户职位板 URL 与官方公开 API URL，两者均经过公开 URL/DNS 安全检查；Ashby `isListed=false` 不进入可筛选库。

## 分页边界与范围

不配置 `pagination` 只处理当前 URL 或提交的文档，不代表全站完整；快照记录 `scope=single_page/document`，页面明确显示范围。

JSON 分页示例：`"pagination":{"url":"https://example.com/api/jobs?page={page}","page_size":20,"total_path":"data.total"}`。配置 URL 必须包含一个 `{page}`，items 可设为 `data.jobs`。实际页容量与总数必须一致，否则安全失败。HTML 分页使用相同 URL 模板及 `next_selector` 指定下一页控件；disabled/aria-disabled 控件视为结束。没有分页结束依据不允许声称全量成功。

## 登录网站与页面文档

`mode=http` 只读取公开 GET HTML/JSON，不执行 JavaScript，不复制 Chrome 登录资料，不使用登录后接口凭据，也不解答或绕过验证码。

`mode=document` 接收已登录页面的职位文档，按同样 schema 校验、规范化后入库；不保存原文，不把旧文档回放伪装成联网刷新。可提交 HTML 职位片段（配合 schema/dom）或只含公开职位的 JSON（配合 json）。请勿提交完整账户页面、Cookie、令牌或个人资料。更新时须重新提交页面文档。

BOSS 历史 `custom_boss_shanghai` 的 15 条仍是文档导入，不能计作新联网验收；保留原数据及来源标记。

### 无登录动态公开页面

`mode=public_browser` 使用标准无头 Chrome、新建隔离上下文，不连接原 Chrome，不读取用户资料/Cookie，也不导出凭据。服务内可复用它自己的匿名上下文；退出只关闭该程序拥有的匿名浏览器。没有伪装 UA、代理轮换、验证码处理或自造 SDK 签名；仅让正常公开网页执行官网脚本。遇到验证或未就绪直接失败，不发人工接管请求。

此模式同样只允许五个招聘平台域名、只读配置当前页、禁止 `fetch_url/pagination`，禁用 HTTP/Service Worker 缓存，逐条核对职位标题与本轮新响应；快照标记 `acquisition=public_browser_network`、`anonymous=true`、`session_reused=false`。只收录主文档及职位相关响应；前程无忧公开 SDK 的 JSONP 响应虽属于 Script 类型，也作为 JSON 数据解包，不执行回调文本。查询参数与原文不落库，网络证据保留状态、时间、SHA-256 及缓存标记。匿名浏览器使用 Chrome 自身网络，不声称具备 HTTP 抓取器的数字地址固定/DoH 校验。

2026-10-20 连续两轮：亚信校招当前页 10 条，拉勾公开校招首页 9 条；均经过自动适配、后台重新请求、入库、地点+关键词筛选、四种排序和 JSON/CSV 导出。它们是不同于历史登录会话的公开范围，不代表全站；职责/要求暂缺。拉勾职位链接中的 `show/source` 展示跟踪参数仅在标准 `/wn/jobs/<数字>.html` 详情 URL 归一化时剔除，来源 URL 的筛选参数不变，两个新轮次的职位 ID 稳定。证据 `output/portals-live-20261020T065007Z/public-validation.json`；BOSS 当前首页与无障碍入口仍被验证阻断，不计作成功。

### 已授权 Chrome 会话（可选）

仅在用户明确授权后使用 `mode=browser`，当前仅允许 BOSS、智联、前程无忧、猎聘、拉勾域名。用户在原 Chrome 的 `chrome://inspect/#remote-debugging` 手动开启 remote debugging，并在连接时手动确认 Allow。程序只读 `DevToolsActivePort` 连接元数据，不读取配置数据库、不导出 Cookie/令牌；只新建或复用自己创建的招聘标签页。Chrome 的控制权限本身较广，请仅向可信本机程序授权。

同一 serve 进程使用单个工作线程持有连接，复用原有默认上下文的登录态；退出仅断开客户端，不关闭用户浏览器/上下文。重启、断连或用户关闭 Chrome 后可能需重新手动确认。登录和验证码仍由用户处理，遇到挑战保留页面并安全失败，不进行自动解答或绕过。

Chrome 连接授权最多等待 5 分钟（原 60 秒可能在用户回复前已失效）；页面读取总预算 420 秒，验收客户端为 435 秒，避免外层先超时。连接或页面读取未结束时，新的浏览器读取会明确报告“已有请求等待”，不排队重复弹授权。超时/取消只记录失败，不能算作会话复用成功；请在当前有效弹窗中确认，已结束请求的旧弹窗不证明新连接获准。

此模式只采配置 URL 当前页，不支持隐藏接口或猜测分页；不能同时配置 `fetch_url`/`pagination`，也不能传 document 冒充联网。每轮禁用 HTTP 缓存并绕过 Service Worker 缓存，要求观察到新 HTTP 200 响应，且所有职位标题能在新响应正文中核对。正文只在内存核对，快照仅保留脱敏 URL（去查询参数）、时间、状态、哈希、缓存标记与核对数量。网络使用原 Chrome 配置，不等同于 HTTP 模式的公网地址固定/DoH 校验。

动态页面的 DOM 缺少可读职位链接时，`auto_adapt=true` 还会解析本轮浏览器**实际已收到**的职位 JSON 响应，复用现有 JSON 字段推断和必要字段校验；不猜测接口、不自行重放带认证的 API。多个不同职位集合仍拒绝，严格模式不会转用其他数据，JSON 原文也不写库。`network_requests[].parser_input=live_dom/observed_network_json` 区分解析输入；后者另记录已脱敏的响应 URL。响应筛选先保留页面文档与职位响应，再限制数量，防止较晚的分析请求挤掉真实职位证据。

快照 `acquisition=browser_network`，与 `network`/`provided_document` 区分。白屏、未就绪、验证、DOM 结构不可靠或新响应不含职位均不写入旧库。模板/解析器并不保证每个网站始终可用。

真实五平台验收：先启动服务，再运行 `PYTHONPATH=. python3 scripts/verify_portals_live.py`，默认使用已适配的公开 HTTP / 匿名动态入口，不依赖登录/Chrome 授权。`--http-only` 禁止浏览器（亚信动态页预期会失败）；仅明确加 `--browser-session` 才优先使用原 Chrome。匹配旧的已授权 DOM 模板本身不会自行连接用户浏览器。失败保留阻断，不用文档或旧列表替代。成功项须经过失效选择器恢复、预览、挂载、后台新请求入库、实际可用字段筛选、四种排序、JSON/CSV 导出和规则复用检查；缺失地点不伪造为全字段通过。证据写入 `output/portals-live-<时间>/validation.json`。

## API

- `POST /api/sources/preview`：`{"config":{...},"document":"可选 HTML/JSON"}`，返回规范化样本、数量、解析器、范围与采集方式，不持久化。
- `GET /api/templates`：返回研究日期、站点目录、官方参考链接与本机已保存配置。
- `POST /api/templates/match`：`{"url":"https://jobs.ashbyhq.com/公司"}`，只匹配配置，不联网、不写库。
- preview/custom 也接受 `{"url":"目标网址","document":"可选 HTML/JSON"}` 自动生成配置。URL + 文档自动使用 document 模式；显式传 config 时尊重其模式，网页可勾选“将本次提交文档作为文档导入源”。
- `POST /api/sources/custom`：相同请求体，校验后保存配置并自动挂载。HTTP 模式必须通过真实联网预览，不接受离线样本冒充联网验证；文档模式直接导入并返回 200，HTTP 模式启动后台全量分页返回 202。
- `GET /api/sources`：返回内置和自定义网站；自定义项包含配置，可继续编辑适配。
- 挂载后复用 `/api/jobs`、`/api/status`、`/api/export` 及 selector/ranker；文档来源不能直接调用 `/api/crawl` 假刷新。

管理写入要求 JSON、同源请求及本机/明确绑定的 Host。网站 URL 只允许无凭据的公开 HTTP(S) 与标准端口，拒绝 localhost、内网、链接本地地址。连接按同一次解析的公网数字地址建立，重定向逐跳验证，不读取代理环境配置；页面上限 2 MiB、最多 5 次跳转、不执行自定义脚本。

## Fake-IP 网络恢复与真实请求证据

系统解析全部落入 `198.18.0.0/15` 时，客户端会向固定公网地址 `https://1.1.1.1/dns-query` 发起正常验证证书的 DNS-over-HTTPS A 查询。它只发送目标域名，不发送职位页面、Cookie 或其他登录资料，也不修改系统 VPN、DNS、路由或代理配置。备用路径目前依赖 IPv4 DoH 服务可达；不可达或证书失败则停止。

DoH 返回的地址仍必须全部为公网地址，之后固定连接到已验证的数字 IP，目标 TLS 的 SNI/证书仍使用原目标域名。DoH 不跟随重定向，响应上限 64 KiB。普通公网系统解析继续原路径；内网、链路本地、Fake-IP 与公网/内网混合答案仍拒绝，不用备用解析掩盖异常。

每次 HTTP 抓取发送 `Cache-Control: no-cache`，不发送条件缓存头，不接受 304 或回放已有职位列表。快照记录 `acquisition=network` 与本轮 `network_requests`：请求时间、响应 Date/HTTP 状态、字节数、SHA-256、公网解析方式/地址、实际连接 IP 和 TLS 校验状态。每轮清空请求记录；文档导入不产生联网记录。serve 页面显示实际 HTTP 与 DoH 状态。源站/CDN 自身可能仍有服务端缓存，这不等同于使用本地职位缓存。

复现全链路验收（会实际联网，并刷新/挂载三个目标来源；原有非目标数据不变）：

```bash
python3 -m jobcrawler serve
# 在另一个终端执行
PYTHONPATH=. python3 scripts/verify_ats_live.py
```

脚本实际调用 serve 的网址匹配、联网预览与挂载，故意使用 `obsolete_layout.jobs` 失效路径来验证自动适配，再等待后台重新发起 HTTP 抓取，核对持久化规则、组合筛选、四种排序与 JSON/CSV 导出。不提交 document、不把旧列表输入爬虫。报告、原来源元数据和导出写入独立 `output/ats-live-<时间>/`。

默认范围：Greenhouse 自家公开职位板；Lever 的 Zoox Software + Boston 列表（保持两个筛选参数，不代表 Zoox 全站）；Ashby 自家公开职位板。2026-10-20 网络探测时 Lever 自家的 `lever` 租户返回 `[]`，因此用仍有公开职位的 Zoox 做非空全链路验收，不将演示职位当作真实招聘职位。

参考：[Schema.org JobPosting](https://schema.org/JobPosting)、[Greenhouse Job Board API](https://docs.greenhouse.io/job-board)、[Lever Postings API](https://github.com/lever/postings-api)、[Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api)、[Cloudflare DoH JSON API](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。各站点官方入口逐项保存在模板目录中；具体联网是否成功以每轮请求证据和验收报告为准，不能从模板匹配结果推断。
