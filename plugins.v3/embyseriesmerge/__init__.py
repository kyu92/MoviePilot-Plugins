"""Emby 同名剧集跨库合并补丁插件。"""

from typing import Any, Dict, List, Optional, Tuple

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerIdentityHelper
from app.modules.emby.emby import Emby
from app.plugins import _PluginBase
from app.runtime.log import logger

# 补丁状态与原始方法在类上的保存键
_PATCH_FLAG = "_emby_series_merge_patched"
_ORIGINAL_TV_EPISODES = "_emby_series_merge_original_tv_episodes"
_ORIGINAL_SEASON_IDS = "_emby_series_merge_original_season_ids"


def _search_series_candidates(
    server: Emby,
    title: str,
    year: Optional[str],
) -> List[Dict[str, Any]]:
    """按标题和年份在 Emby 中搜索全部同名剧集条目。"""
    host = getattr(server, "_host", None)
    apikey = getattr(server, "_apikey", None)
    if not host or not apikey or not title:
        return []
    url = f"{host}emby/Items"
    params = {
        "IncludeItemTypes": "Series",
        "Fields": "ProductionYear,ProviderIds",
        "StartIndex": 0,
        "Recursive": "true",
        "SearchTerm": title,
        "Limit": 30,
        "IncludeSearchTypes": "false",
        "api_key": apikey,
    }
    try:
        response = RequestUtils().get_res(url, params)
        if not response:
            return []
        items = response.json().get("Items") or []
    except Exception as error:  # noqa: BLE001 - 补丁失败时由调用方回退原实现
        logger.error(f"Emby 同名剧集搜索失败：{error}")
        return []
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        series_id = item.get("Id")
        if not series_id or str(series_id) in seen:
            continue
        if item.get("Name") != title:
            continue
        if year and str(item.get("ProductionYear")) != str(year):
            continue
        seen.add(str(series_id))
        candidates.append(item)
    return candidates


def _fetch_series_episodes(
    server: Emby,
    series_id: str,
    season: Optional[int] = None,
) -> Optional[Dict[int, List[int]]]:
    """获取单个剧集条目的季集映射，请求失败时返回 None。"""
    host = getattr(server, "_host", None)
    apikey = getattr(server, "_apikey", None)
    if not host or not apikey or not series_id:
        return None
    url = f"{host}emby/Shows/{series_id}/Episodes"
    params = {
        "Season": season,
        "IsMissing": "false",
        "api_key": apikey,
    }
    try:
        response = RequestUtils().get_res(url, params)
        if not response:
            return None
        payload = response.json()
    except Exception as error:  # noqa: BLE001 - 补丁失败时由调用方回退原实现
        logger.error(f"Emby 剧集集数查询失败：{error}")
        return None
    episodes: Dict[int, List[int]] = {}
    for item in payload.get("Items") or []:
        season_index = item.get("ParentIndexNumber")
        episode_index = item.get("IndexNumber")
        if season_index is None or episode_index is None:
            continue
        if season is not None and int(season_index) != int(season):
            continue
        episodes.setdefault(int(season_index), []).append(int(episode_index))
    for season_index, episode_list in episodes.items():
        episodes[season_index] = sorted(set(episode_list))
    return episodes


def _merged_get_tv_episodes(
    self,
    item_id: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[str] = None,
    media_source: Optional[Any] = None,
    media_id: Optional[str] = None,
    season: Optional[int] = None,
) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
    """合并同名同年同身份的多个剧集条目的季集数据，失败时回退原实现。"""
    original = getattr(Emby, _ORIGINAL_TV_EPISODES)

    def fallback():
        """调用未被补丁覆盖的原始查询。"""
        return original(
            self,
            item_id=item_id,
            title=title,
            year=year,
            media_source=media_source,
            media_id=media_id,
            season=season,
        )

    if not title:
        return fallback()
    try:
        candidate_ids: List[str] = []
        for candidate in _search_series_candidates(self, title, year):
            candidate_source, candidate_id = MediaServerIdentityHelper.from_provider_ids(
                candidate.get("ProviderIds")
            )
            if not MediaServerIdentityHelper.are_compatible(
                candidate_source, candidate_id, media_source, media_id
            ):
                continue
            series_id = str(candidate.get("Id"))
            if series_id not in candidate_ids:
                candidate_ids.append(series_id)
        if item_id and str(item_id) not in candidate_ids:
            candidate_ids.append(str(item_id))
        if not candidate_ids:
            return fallback()
        merged: Dict[int, List[int]] = {}
        season_coverage: Dict[str, int] = {}
        succeeded = False
        for series_id in candidate_ids:
            episodes = _fetch_series_episodes(self, series_id, season)
            if episodes is None:
                continue
            succeeded = True
            for season_index, episode_list in episodes.items():
                merged[season_index] = sorted(
                    set(merged.get(season_index) or []) | set(episode_list)
                )
            season_coverage[series_id] = max(episodes) if episodes else -1
        if not succeeded:
            return fallback()
        primary_id = str(item_id) if item_id and str(item_id) in candidate_ids else None
        if not primary_id:
            primary_id = max(
                candidate_ids,
                key=lambda candidate: season_coverage.get(candidate, -1),
            )
        logger.debug(
            f"Emby 同名剧集合并：{title} ({year}) 合并 {len(candidate_ids)} 个条目，"
            f"季数 {sorted(merged)}"
        )
        return primary_id, merged
    except Exception as error:  # noqa: BLE001 - 补丁失败时由调用方回退原实现
        logger.error(f"Emby 同名剧集季集合并失败，回退原始查询：{error}")
        return fallback()


def _merged_get_season_episode_ids(
    self,
    item_id: str,
    season: int,
) -> Dict[int, str]:
    """合并同名剧集条目的集号到条目 ID 映射，便于订阅详情定位文件。"""
    original = getattr(Emby, _ORIGINAL_SEASON_IDS)
    result = original(self, item_id, season)
    try:
        item_info = self.get_iteminfo(item_id)
        if not item_info or not item_info.title:
            return result
        candidates = _search_series_candidates(self, item_info.title, item_info.year)
        if len(candidates) <= 1:
            return result
        merged: Dict[int, str] = dict(result or {})
        for candidate in candidates:
            series_id = str(candidate.get("Id"))
            extra = original(self, series_id, season)
            for episode_index, episode_item_id in (extra or {}).items():
                merged.setdefault(episode_index, episode_item_id)
        return merged
    except Exception as error:  # noqa: BLE001 - 补丁失败时保留原始结果
        logger.error(f"Emby 同名剧集条目 ID 合并失败：{error}")
        return result


def _apply_patch() -> None:
    """为 Emby 客户端类安装跨库同名剧集合并补丁。"""
    if getattr(Emby, _PATCH_FLAG, False):
        return
    setattr(Emby, _ORIGINAL_TV_EPISODES, Emby.get_tv_episodes)
    setattr(Emby, _ORIGINAL_SEASON_IDS, Emby.get_season_episode_ids)
    Emby.get_tv_episodes = _merged_get_tv_episodes
    Emby.get_season_episode_ids = _merged_get_season_episode_ids
    setattr(Emby, _PATCH_FLAG, True)
    logger.info("Emby 同名剧集跨库合并补丁已启用")


def _remove_patch() -> None:
    """移除 Emby 客户端类上的合并补丁并恢复原始实现。"""
    if not getattr(Emby, _PATCH_FLAG, False):
        return
    original_tv_episodes = getattr(Emby, _ORIGINAL_TV_EPISODES, None)
    original_season_ids = getattr(Emby, _ORIGINAL_SEASON_IDS, None)
    if original_tv_episodes:
        Emby.get_tv_episodes = original_tv_episodes
    if original_season_ids:
        Emby.get_season_episode_ids = original_season_ids
    setattr(Emby, _PATCH_FLAG, False)
    logger.info("Emby 同名剧集跨库合并补丁已停用")


class EmbySeriesMerge(_PluginBase):
    """合并 Emby 同名同身份剧集条目，修正跨媒体库连载剧的缺集判定。"""

    plugin_name = "Emby 同名剧集合并"
    plugin_desc = (
        "当同一部剧在 Emby 的多个媒体库中都存在条目时，合并同名同年、同一 TMDB 身份的"
        "季集数据，避免订阅因只读取单个条目而反复判定缺集。"
    )
    plugin_icon = "emby.png"
    plugin_version = "1.0.0"
    plugin_label = "媒体库"
    plugin_author = "local"
    plugin_config_prefix = "emby_series_merge_"
    plugin_order = 60
    auth_level = 1

    _enabled: bool = True

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """根据插件配置启用或移除 Emby 剧集合并补丁。"""
        if config is None or "enabled" not in config:
            self._enabled = True
        else:
            self._enabled = bool(config.get("enabled"))
        if self._enabled:
            _apply_patch()
        else:
            _remove_patch()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> Optional[List[Dict[str, Any]]]:
        """本插件不注册远程命令。"""
        return None

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件不注册 API。"""
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "cols": 12,
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用跨库同名剧集合并",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ], {"enabled": True}

    def get_page(self) -> Optional[List[dict]]:
        """本插件没有详情页面。"""
        return None

    def stop_service(self) -> None:
        """插件停止时保留补丁状态，仅记录日志。"""
        logger.debug("Emby 同名剧集合并插件已停止")
