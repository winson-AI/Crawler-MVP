from .baidu import BaiduSource
from .base import JobSource
from .alibaba import AlibabaSource
from .tencent import TencentSource
from .meituan import MeituanSource
from .bytedance import BytedanceSource
from .jd import JdSource
from .pinduoduo import PinduoduoSource

# Add a source instance here to expose it in the CLI, API and UI.
SOURCES: dict[str, JobSource] = {source.id: source for source in (
    BaiduSource(), AlibabaSource(), TencentSource(), MeituanSource(), BytedanceSource(), JdSource(), PinduoduoSource(),
)}


def get_source(source_id="baidu"):
    try:
        return SOURCES[source_id]
    except (KeyError, TypeError):
        raise ValueError(f"未知数据源 {source_id!r}；可选：{', '.join(SOURCES)}") from None


def list_sources():
    return [{"id": source.id, "name": source.name, "url": source.url} for source in SOURCES.values()]
