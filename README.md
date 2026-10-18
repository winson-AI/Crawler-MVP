# 职位雷达 / Job Crawler MVP

通过可扩展的数据源适配器抓取公开社招职位。在本地按工作年限、地点、技术栈及关键词组合筛选，再用 ranker 排序，最后导出 JSON/CSV。支持跨次运行去重、只导出首次发现的职位，可交给本地调度器每三天运行。Python **3.9+**，当前支持 **macOS / Linux**。百度、阿里、腾讯、美团、京东使用标准库；字节、拼多多使用 Playwright，通过正常官网页面读取公开响应。

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

| 数据源 ID | 官方入口 | 最近真实验证（2026-10-18 / 19，首尔时间） |
| --- | --- | --- |
| `baidu` | [百度](https://talent.baidu.com/jobs/social-list) | 全量 1711 条；相对于本地历史首次发现 58 条 |
| `alibaba` | [阿里集团](https://talent.alibaba.com/) | 全量 4180 条，42 页；首次发现 1 条 |
| `tencent` | [腾讯](https://careers.tencent.com/search.html) | 社招列表并补抓任职要求；全量 2136 条，22 页；首次发现 0 条 |
| `meituan` | [美团](https://hr.meituan.com/web/social) | 社招类型 `jobType=3`；全量 2575 条，258 页；首次发现 0 条 |
| `bytedance` | [字节](https://jobs.bytedance.com/experienced/position) | 完整读取本次公开搜索接口报告的 10000 条，100 页；首次发现 11 条 |
| `jd` | [京东](https://zhaopin.jd.com/web/job/job_info_list/3) | 按发布记录 ID 去重后 1874 条；首次发现 1 条 |
| `pinduoduo` | [拼多多集团](https://careers.pddglobalhr.com/jobs) | 修复默认浏览器的 400023 及验证中间态误判；列表报告 782 条；连续两页去重后 19 条；另验证用户提供的真实算法岗位，缓存为部分样本 |

以上数字是当次真实快照。“全量”表示完成官方接口返回的分页范围，不能据此推断整个集团或公司的全部岗位数量，字节本次范围为官网报告的 10000 条。阿里覆盖集团公开搜索接口的返回范围；京东原始链接打开官方列表，详情在列表内展开。拼多多已经验证部分真实样本，仍需完成全量验证。首次发现按平台及职位 ID 相对于本地历史计算，不等同于今天新发布。

本次报告和导出位于 `output/full-20261018/`。最近分页没有技术栈样本时，检查器如实记录 `technology_sample_available=false`；拼多多另用用户提供的 `T021220` 真实图像算法岗位验证 PyTorch/TensorFlow、地点、关键词和未知年限处理，证据位于 `pdd-known-sample/`。

最近一次手动增量运行中，六个平台成功导出共 71 条首次发现的职位：百度 58、阿里 1、腾讯 0、美团 0、字节 11、京东 1。已逐个平台验证新增 ID 等于当前快照与运行前历史的差集，JSON/CSV 内容一致；使用这批真实快照在隔离状态下重放，新增为 0。首轮拼多多因验证等待超时失败，旧缓存和历史均保持原样，未将其当作空列表成功。修复快速验证响应漏接的时序问题后，真实抓取仍可能要求交互验证码，全量验证尚未通过；程序不提供验证码自动识别、计算或代填。

测试命令 `python3 -m unittest discover -s tests -q` 最近通过 **49 项**，包括验证响应早于导航完成、不同职位响应隔离、验证失败保留数据及增量重复运行。真实运行报告和导出为本地生成文件，不包含在 Git 仓库中。

安装浏览器依赖（字节、拼多多需要）：

```sh
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

拼多多还需要本机安装 Google Chrome，并在有图形界面的环境中运行。它会打开独立 Chrome 窗口，不读取用户已有浏览器配置或登录资料。默认自动化浏览器上下文会返回 400023；适配器使用经真实请求验证的 Chrome 配置，复用列表和详情标签页以保留官网 `sessionStorage` 中的验证状态。在导航或翻页之前监听并缓冲同一职位/页码的响应，避免官网 SDK 已完成快速重试、程序却漏接成功响应。遇到 54001 时保持页面存活，等待官网 SDK 后续的最终成功响应；若官网要求交互验证码，由用户在该窗口手动完成，默认最多等待 180 秒，程序不自动解答或绕过验证码。未完成验证、认证拒绝和接口错误均报错并保留旧缓存。缓存和导出仅包含规范化职位字段，不包含校验令牌。

## 启动页面

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

示例见 `selector.example.json`：

```json
{
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

所有数据源遵循 `jobcrawler/sources/base.py` 中的 `JobSource` 协议：提供 `id`、`name`、`url` 和 `fetch_page(page)`。页码从 1 开始，返回 `JobPage(page, total, jobs)`；`total` 为上游报告的数量。可选 `has_more` 表示明确的分页终点，避免上游重复行造成收集数量永远达不到总数。快照另记录 `scanned` 和 `duplicates`；京东以发布记录 `id` 为标识，`requirementId` 可能被多个发布记录复用。网络请求、分页、解析及字段映射均由适配器处理。

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

需要浏览器的适配器可以提供 `session()` 上下文管理器，通用 crawler 会在整个抓取期间复用会话，并在成功或异常退出时关闭浏览器。单独调用 `fetch_page(1)` 也会自动建立和关闭会话；拼多多在同一会话内必须按页码顺序读取。

每个职位至少包含稳定 `id`、`title`、`source`、`company`、HTTP(S) 详情 `url` 和 `locations`；发布日期和更新日期使用 ISO 8601，未知值留空。如果网站提供结构化年限，可向 `build_job(experience=...)` 传入 `{"min":3,"max":5,"evidence":"3-5年","source":"structured"}`；否则提供 `work_years` 或 `requirements` 复用现有解析。职位 `source` 必须等于适配器 id，id 在该数据源内唯一。

抓取接口是页面公开使用的 `POST https://talent.baidu.com/httservice/getPostListNew`，请求格式为表单，`recruitType=SOCIAL`、`curPage`、`pageSize=20`。每条返回数据包含完整要求和职责，无需额外逐条请求详情。

新增站点请求仅存在于对应适配器：阿里从公开页面读取 CSRF 和社招 channel 后 POST `/position/search`；腾讯 GET `/tencentcareer/api/post/Query` 和 `/ByPostId`；美团 POST `/api/official/job/getJobList`；京东表单 POST `/web/job/job_count` 与 `/web/job/job_list`；字节正常页面发起 `/api/v1/search/job/posts`；拼多多正常页面发起 `/api/recruit/position/list` 与 `/detail`。所有源都输出 `build_job()` 的统一结构，并使用同一套缓存、selector、ranker 和导出。

腾讯只有更新时间，美团本次返回的 `firstPostTime` 为空，所以这些职位的 `published_at` 保留未知，发布时间排序时排到最后；阿里和字节的毫秒时间戳按 UTC 转为 ISO 8601，京东同样保留原始时间戳对应的真实时区时刻。阿里优先使用结构化 `experience.from/to`，其他来源复用工作年限字段或任职要求提取。

抓取会按适配器的职位 `id` 去重，验证页码，并检测空页或重复页造成的停滞；最多 1000 页。连接失败、429 和部分 5xx 最多尝试 3 次；百度 HTTP 超时 20 秒，其他 HTTP 来源 25 秒，浏览器导航 45 秒；认证拒绝及数据结构变化直接报错。成功后以临时文件原子替换缓存，抓取失败保留原有缓存。结果是一次分页抓取的快照，抓取期间源站职位变化可能导致重复或遗漏，可再次刷新。

## 每三天同步新增职位

可在本地 Codex 定时任务或系统调度器中配置每隔三天执行 `python3 -m jobcrawler sync`，工作目录设为项目根目录，使用安装依赖的 Python 环境。当前开发工作区已在 Codex 配置首尔时间上午 09:00 每隔三天运行；此配置不随 Git 克隆自动安装，其他使用者需要自行登记。电脑需要保持开机、联网并运行对应调度器。默认覆盖注册表中的所有平台，新接入的平台也会自动加入。拼多多可能打开独立 Chrome 窗口；需要人工验证码且等待超时时，该平台记录失败，其他平台继续执行。

```bash
# 首次部署：仅将现有缓存登记为已见，不访问网站。
python3 -m jobcrawler sync --initialize

# 执行一次全量刷新，只导出此前未发现的职位。
python3 -m jobcrawler sync

# 可选：沿用同一套 selector 和 ranker 处理新增导出。
python3 -m jobcrawler sync --sources baidu tencent --selector selector.example.json --ranker experience_asc
```

历史文件为 `data/sync-state.json`，以平台及职位 ID 去重。已有缓存时先执行 `sync --initialize` 建立基线；全新安装没有缓存时，首次 `sync` 会将抓到的全部职位作为首次发现。开发工作区的首次验证基线为 22,518 条，后续手动增量运行中六个平台成功发现 71 条，拼多多等待验证的失败运行保留其旧缓存和历史。同一职位改名、更新、下架后重新出现均不会再次作为新增；不同平台的同名或同 ID 职位分别保留。“新增”指相对于本地历史首次发现，可能包含源站早已发布但此前尚未收集的职位，尤其是当前缓存只有部分样本的平台。

每次运行在 `output/incremental/<UTC时间-运行标识>/` 保存 `report.json`、各平台的 `*-new.json` 和 `*-new.csv`，默认按发布时间倒序，未知日期放在最后。筛选只影响导出，已见历史会记录本次全部职位。网页继续读取各平台最新缓存；新增职位清单见上述单独导出目录。

仅完整成功的抓取才刷新该平台缓存及已见历史。新增文件先写入，再推进历史；写入中断时可重试，未完成提交的导出目录应结合报告判断，避免将失败运行当作另一批新增。一个平台失败不影响其余平台；运行锁防止两个增量同步同时执行。不要删除或重置历史文件，否则旧职位会重新被视为新增。定时任务不会自动修改代码或安装依赖。

## 代码结构

- `jobcrawler/sources/`：数据源协议、注册表、七家适配器、公共 HTTP/浏览器读取工具。
- `jobcrawler/models.py`：各数据源共享的职位结构。
- `jobcrawler/crawler.py`：通用分页抓取、独立缓存、CSV 导出。
- `jobcrawler/sync.py`：跨次去重历史、运行锁、增量导出及单平台失败隔离。
- `jobcrawler/selectors.py`：年限提取、组合筛选。
- `jobcrawler/rankers.py`：发布时间和工作年限排序。
- `jobcrawler/server.py`、`jobcrawler/web/index.html`：本地 API 和筛选页面。
- `jobcrawler/__main__.py`：CLI。
- `tests/`：分页、去重、年限、技术词边界、组合筛选、四种排序、独立缓存、多源适配与 API/导出验证；官方真实样本位于 `fixtures/`。
- `scripts/verify_sources.py`：可重复运行的真实抓取与共享处理流程验证。

站点接口变化时仅更新对应适配器。拼多多会等待官网验证流程结束再读取真实职位，验证中间态或失败结果不会作为空职位写入缓存。
