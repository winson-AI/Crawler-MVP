from .baidu import BaiduSource
from .base import JobSource
from .alibaba import AlibabaSource
from .tencent import TencentSource
from .meituan import MeituanSource
from .bytedance import BytedanceSource
from .jd import JdSource
from .pinduoduo import PinduoduoSource
from .custom import CONFIG_PATH, CustomSource, read_configs, validate_config

# Add a source instance here to expose it in the CLI, API and UI.
SOURCES: dict[str, JobSource] = {source.id: source for source in (
    BaiduSource(), AlibabaSource(), TencentSource(), MeituanSource(), BytedanceSource(), JdSource(), PinduoduoSource(),
)}


def load_custom_sources(path=CONFIG_PATH):
    adapters = [CustomSource(config) for config in read_configs(path)]
    for key in [key for key, adapter in SOURCES.items() if isinstance(adapter, CustomSource)]:
        del SOURCES[key]
    SOURCES.update((adapter.id, adapter) for adapter in adapters)


def register_custom_source(config, path=CONFIG_PATH):
    from ..crawler import save_cache
    config = validate_config(config)
    configs = [c for c in read_configs(path) if c["id"] != config["id"]]
    if len(configs) >= 50:
        raise ValueError("最多挂载 50 个自定义网站")
    adapter = CustomSource(config)
    save_cache({"version": 1, "sources": [*configs, config]}, path)
    SOURCES[adapter.id] = adapter
    return adapter


def get_source(source_id="baidu"):
    try:
        return SOURCES[source_id]
    except (KeyError, TypeError):
        raise ValueError(f"未知数据源 {source_id!r}；可选：{', '.join(SOURCES)}") from None


def list_sources():
    return [{"id": source.id, "name": source.name, "url": source.url,
             **({"custom": True, "config": source.config} if isinstance(source, CustomSource) else {})}
            for source in tuple(SOURCES.values())]


load_custom_sources()
