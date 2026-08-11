"""
字幕命名补全插件

在文件整理重命名前，为字幕文件补全与同目录视频一致的
色深（videoBit）、分辨率（videoFormat）、视频编码（videoCodec）、
音轨（audioCodec）等命名字段，使字幕文件名与视频文件名主干一致，
解决外挂字幕无法被播放器自动匹配的问题。
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import ChainEventType

try:
    from app.schemas import TransferRenameBuildEventData
except ImportError:
    TransferRenameBuildEventData = None

# 字幕文件语言/用途尾缀集合（剥离后用于匹配同名主视频）
SUBTITLE_STEM_TAGS = {
    "cc", "chi", "chs", "cht", "cn", "default", "en", "eng", "english",
    "forced", "gb", "gb2312", "hk", "ja", "jap", "japanese", "jp", "jpn",
    "sc", "sdh", "tc", "zh", "zh-cn", "zh-hans", "zh-hant", "zh-tw",
    "zh_cn", "zh_hans", "zh_hant", "zh_tw", "zho",
    "中英", "中字", "双语", "简中", "简体", "繁中", "繁体",
    # 常见未内置的日系字幕尾缀与弹幕/特效字幕标记
    "jpsc", "jptc", "jps", "jpt", "jpch", "japsc", "japtc",
    "danmu", "danmaku", "弹幕", "scx", "tcx",
}

# 目标字段：模板中与视频技术参数相关的命名变量
TARGET_FIELDS = ("videoBit", "videoFormat", "videoCodec", "audioCodec", "releaseGroup")

# 视频编码映射
_VIDEO_CODEC_MAP = {
    "h264": "H264", "avc": "H264", "hevc": "H265", "h265": "H265",
    "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "mpeg2video": "MPEG2",
    "vc1": "VC1", "mpeg4": "MPEG4",
}

# 音频编码映射
_AUDIO_CODEC_MAP = {
    "aac": "AAC", "eac3": "EAC3", "ac3": "AC3", "dts": "DTS",
    "truehd": "Dolby TrueHD", "flac": "FLAC", "opus": "OPUS",
    "mp3": "MP3", "vorbis": "Vorbis", "pcm_s16le": "PCM", "pcm_s24le": "PCM",
}

# 分辨率分档（按宽度优先，兼容 21:9 宽屏片源）
_WIDTH_FORMAT_BUCKETS = (
    (7680, "4320p"), (3840, "2160p"), (2560, "1440p"),
    (1920, "1080p"), (1280, "720p"), (854, "480p"), (640, "360p"),
)

# 声道数映射
_CHANNEL_MAP = {
    1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1",
    7: "6.1", 8: "7.1", 10: "7.1.2", 12: "7.1.4",
}

# 集数标记正则（S01E05 / E05 / 第 5 集 / 第5話 / [08]）
_EPISODE_RE = re.compile(
    r"(?:[Ss](\d{1,2}))?(?:[Ee](\d{1,3})|第\s*(\d{1,3})\s*[集話话]"
    r"|[\[(（_](\d{1,3})(?=[\]）)]|$))",
)


class SubtitleMetaPatch(_PluginBase):
    """字幕命名补全插件。"""

    plugin_name = "字幕命名补全"
    plugin_desc = "整理时让字幕文件名带上与视频一致的色深等编码信息，解决外挂字幕无法自动匹配的问题。"
    plugin_version = "1.0.0"
    plugin_author = "kyu92"
    plugin_label = "整理命名"
    plugin_config_prefix = "subtitlemetapatch_"
    plugin_order = 99
    auth_level = 1

    _enabled = False
    _overwrite_mode = "fill_missing"
    _extra_tags: List[str] = []
    _probe_timeout = 15

    # 探测结果缓存：relation_key -> 命名字段字典
    _probe_cache: Dict[str, Dict[str, str]] = {}

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._overwrite_mode = "fill_missing"
        self._extra_tags = []
        self._probe_timeout = 15
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._overwrite_mode = str(
            config.get("overwrite_mode") or "fill_missing"
        ).strip() or "fill_missing"
        if self._overwrite_mode not in ("fill_missing", "always"):
            self._overwrite_mode = "fill_missing"
        extra = str(config.get("extra_subtitle_tags") or "").strip()
        if extra:
            self._extra_tags = [
                tag.strip().lower().lstrip(".")
                for tag in extra.split(",")
                if tag.strip()
            ]
        try:
            self._probe_timeout = max(
                5, min(int(config.get("probe_timeout") or 15), 60)
            )
        except (TypeError, ValueError):
            self._probe_timeout = 15

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "overwrite_mode",
                                            "label": "补全模式",
                                            "items": [
                                                {
                                                    "title": "仅补缺失字段",
                                                    "value": "fill_missing",
                                                },
                                                {
                                                    "title": "总是覆盖",
                                                    "value": "always",
                                                },
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "extra_subtitle_tags",
                            "label": "额外字幕尾缀（逗号分隔）",
                            "placeholder": "如 jpsc,jptc,danmu",
                            "hint": "字幕文件名中用于匹配同名视频的语言/用途标记，默认已内置常见中英日字幕尾缀",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "probe_timeout",
                            "label": "媒体探测超时（秒）",
                            "placeholder": "15",
                            "type": "number",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "插件在整理重命名时探测字幕同目录的主视频文件，将色深、分辨率、视频/音频编码补入字幕命名，使字幕与视频同名（含 .10bit 等标记）。仅影响之后整理的文件。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "overwrite_mode": "fill_missing",
            "extra_subtitle_tags": "",
            "probe_timeout": 15,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "已启用。整理文件时字幕将自动继承同目录视频的技术参数（色深/分辨率/编码/音轨），保持字幕与视频文件名主干一致。",
                },
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._probe_cache.clear()

    @eventmanager.register(ChainEventType.TransferRenameBuild)
    def on_transfer_rename_build(self, event: Event) -> None:
        """响应整理重命名构建事件，为字幕文件补全视频技术参数。"""
        if not self._enabled:
            return
        if TransferRenameBuildEventData is None:
            return
        data = event.event_data
        if not isinstance(data, TransferRenameBuildEventData):
            return
        source_path = str(data.source_path or "").strip()
        source_item = data.source_item
        if not source_path or not source_item:
            return
        if getattr(source_item, "type", None) != "file":
            return

        extension = Path(source_path).suffix.lower()
        if extension not in settings.RMT_SUBEXT:
            # 仅处理字幕文件，视频/音轨等交由其他机制处理
            return

        rename_dict = data.rename_dict or {}
        if self._overwrite_mode == "fill_missing":
            missing = [
                key for key in TARGET_FIELDS
                if not str(rename_dict.get(key) or "").strip()
            ]
            if not missing:
                return
        else:
            missing = list(TARGET_FIELDS)

        try:
            fields = self._resolve_media_fields(source_path, source_item)
        except Exception as e:
            logger.warning(f"【字幕命名补全】获取媒体信息失败: {e}")
            return
        if not fields:
            logger.debug(f"【字幕命名补全】未获取到媒体信息: {source_path}")
            return

        for key in TARGET_FIELDS:
            value = fields.get(key)
            if not value:
                continue
            if self._overwrite_mode == "fill_missing" and key not in missing:
                continue
            rename_dict[key] = value
        logger.info(
            f"【字幕命名补全】已为字幕补全字段: {source_path} -> "
            f"{ {k: rename_dict.get(k) for k in TARGET_FIELDS if rename_dict.get(k)} }"
        )

    def _resolve_media_fields(
        self, source_path: str, source_item: FileItem
    ) -> Dict[str, str]:
        """
        解析字幕同目录主视频的媒体命名信息

        :param source_path: 字幕文件路径
        :param source_item: 字幕文件项
        :return: 命名信息字典，探测失败或未匹配返回空字典
        """
        video_item = self._find_related_video(source_path, source_item)
        if not video_item:
            return {}
        video_path = str(getattr(video_item, "path", "") or "")
        if not video_path:
            return {}
        cache_key = f"{getattr(source_item, 'storage', '')}\0{video_path}"
        cached = self._probe_cache.get(cache_key)
        if isinstance(cached, dict):
            return cached

        # 先从视频文件名解析技术字段（可拿到制作组等 ffprobe 无法探测的信息）
        fields: Dict[str, str] = {}
        video_name = str(getattr(video_item, "name", "") or "")
        if video_name:
            file_meta = MetaInfo(title=video_name)
            self._merge_meta_fields(fields, file_meta)
        # 再以 ffprobe 探测结果覆盖（更准确）
        probe_fields = self._probe_video_fields(video_path)
        if probe_fields:
            fields.update(probe_fields)
        if fields:
            self._probe_cache[cache_key] = fields
        return fields

    @staticmethod
    def _merge_meta_fields(fields: Dict[str, str], meta: MetaBase) -> None:
        """
        将文件名解析得到的媒体技术字段合并到字段字典

        :param fields: 字段字典
        :param meta: 文件名解析结果
        """
        mapping = {
            "videoBit": "video_bit",
            "videoFormat": "resource_pix",
            "videoCodec": "video_encode",
            "audioCodec": "audio_encode",
            "releaseGroup": "resource_team",
        }
        for field, attr in mapping.items():
            value = getattr(meta, attr, None)
            if value:
                fields[field] = str(value)

    def _find_related_video(
        self, source_path: str, source_item: FileItem
    ) -> Optional[FileItem]:
        """
        在字幕同目录查找对应的主视频文件

        优先按剥离字幕尾缀后的主干精确匹配；失败时按季/集标记匹配。

        :param source_path: 字幕文件路径
        :param source_item: 字幕文件项
        :return: 唯一匹配的视频文件项，未匹配或存在歧义时返回 None
        """
        try:
            storagechain = StorageChain()
            parent_item = storagechain.get_parent_item(source_item)
            sibling_items = (
                storagechain.list_files(parent_item, recursion=False)
                if parent_item
                else []
            )
        except Exception as e:
            logger.warning(f"【字幕命名补全】读取同目录文件列表失败: {e}")
            sibling_items = []

        media_exts = set(settings.RMT_MEDIAEXT)
        video_items = [
            item for item in sibling_items or []
            if item
            and getattr(item, "type", None) == "file"
            and self._item_extension(item) in media_exts
        ]
        if not video_items:
            return None

        stem = self._get_subtitle_stem(source_path)
        if stem:
            candidates = [
                item for item in video_items
                if Path(str(getattr(item, "name", "") or "")).stem.casefold() == stem
            ]
            if len(candidates) == 1:
                return candidates[0]

        # 季集匹配兜底（季标记缺失时视为任意季）
        sub_ep = self._extract_episode(source_path)
        if sub_ep:
            matched = []
            for item in video_items:
                item_ep = self._extract_episode(
                    str(getattr(item, "name", "") or "")
                )
                if not item_ep or item_ep[1] != sub_ep[1]:
                    continue
                if sub_ep[0] is not None and item_ep[0] is not None \
                        and sub_ep[0] != item_ep[0]:
                    continue
                matched.append(item)
            if len(matched) == 1:
                return matched[0]
        return None

    @staticmethod
    def _item_extension(item: FileItem) -> str:
        """获取文件项扩展名（小写）。"""
        ext = getattr(item, "extension", None)
        if ext:
            return f".{str(ext).lower().lstrip('.')}"
        return Path(str(getattr(item, "path", "") or "")).suffix.lower()

    def _get_subtitle_stem(self, source_path: str) -> str:
        """
        剥离字幕语言/用途尾缀，返回用于匹配主视频的主干名（小写）

        :param source_path: 字幕文件路径
        :return: 主干名
        """
        stem = Path(source_path).stem.casefold()
        tags = SUBTITLE_STEM_TAGS | set(self._extra_tags)
        while stem:
            media_stem, separator, suffix = stem.rpartition(".")
            if not separator or suffix not in tags:
                break
            stem = media_stem
        return stem

    @staticmethod
    def _extract_episode(path: str) -> Optional[Tuple[Optional[int], Optional[int]]]:
        """
        从文件名提取季/集标记

        :param path: 文件路径或文件名
        :return: (季, 集) 元组；集数缺失时返回 None
        """
        match = _EPISODE_RE.search(path or "")
        if not match:
            return None
        season = int(match.group(1)) if match.group(1) else None
        episode = (
            int(match.group(2) or match.group(3) or match.group(4) or 0) or None
        )
        if episode is None:
            return None
        return season, episode

    def _probe_video_fields(self, video_path: str) -> Dict[str, str]:
        """
        通过 ffprobe 探测视频文件的媒体命名信息

        支持 .strm 文件（读取首行内容作为探测目标）。

        :param video_path: 视频文件路径
        :return: 命名信息字典
        """
        probe_target = self._resolve_probe_target(video_path)
        if not probe_target:
            return {}
        probe_json = self._run_ffprobe(probe_target)
        if not probe_json:
            return {}
        streams = probe_json.get("streams")
        if not isinstance(streams, list):
            return {}
        video_s, audio_s = self._pick_streams(streams)
        fields: Dict[str, str] = {}
        if video_s:
            height = self._to_int(video_s.get("height"))
            width = self._to_int(video_s.get("width"))
            video_format = self._height_to_video_format(height, width)
            if video_format:
                fields["videoFormat"] = video_format
            video_codec = self._map_video_codec(video_s.get("codec_name"))
            if video_codec:
                fields["videoCodec"] = video_codec
            video_bit = self._extract_video_bit(video_s)
            if video_bit:
                fields["videoBit"] = video_bit
        if audio_s:
            audio_codec = self._format_audio_codec(audio_s)
            if audio_codec:
                fields["audioCodec"] = audio_codec
        return fields

    @staticmethod
    def _resolve_probe_target(video_path: str) -> Optional[str]:
        """
        解析探测目标：普通文件直接返回路径；STRM 读取首条有效行

        :param video_path: 视频文件路径
        :return: 探测目标，无效时返回 None
        """
        path = Path(video_path)
        if path.suffix.lower() != ".strm":
            return video_path
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.lstrip().startswith("#"):
                continue
            if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
                line = line[1:-1].strip()
            if line:
                return line
        return None

    def _run_ffprobe(self, probe_target: str) -> Optional[Dict[str, Any]]:
        """
        执行 ffprobe 获取媒体流信息

        :param probe_target: 探测目标（文件路径或 URL）
        :return: ffprobe JSON 字典，失败返回 None
        """
        cmd = [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", "-i", probe_target,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._probe_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"【字幕命名补全】ffprobe 超时: {probe_target}")
            return None
        except OSError as e:
            logger.warning(f"【字幕命名补全】无法执行 ffprobe: {e}")
            return None
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return None

    @staticmethod
    def _pick_streams(
        streams: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        选取首路视频流与默认音轨

        :param streams: ffprobe 流列表
        :return: (视频流, 音频流)
        """
        video_s: Optional[Dict[str, Any]] = None
        audio_s: Optional[Dict[str, Any]] = None
        audios: List[Dict[str, Any]] = []
        for s in streams:
            if not isinstance(s, dict):
                continue
            codec_type = s.get("codec_type")
            if codec_type == "video" and video_s is None:
                video_s = s
            elif codec_type == "audio":
                audios.append(s)
        for s in audios:
            disposition = s.get("disposition")
            if isinstance(disposition, dict) and disposition.get("default") == 1:
                audio_s = s
                break
        if audio_s is None and audios:
            audio_s = audios[0]
        return video_s, audio_s

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        """安全转换为整数。"""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _height_to_video_format(
        height: Optional[int], width: Optional[int] = None
    ) -> Optional[str]:
        """
        根据视频宽/高生成分辨率标签

        :param height: 视频高度
        :param width: 视频宽度
        :return: 分辨率标签，如 1080p
        """
        if width is not None and width > 0:
            for min_w, label in _WIDTH_FORMAT_BUCKETS:
                if width >= min_w:
                    return label
        if not height or height <= 0:
            return None
        return f"{height}p"

    @staticmethod
    def _map_video_codec(codec_name: Optional[str]) -> Optional[str]:
        """映射视频编码名为命名标签。"""
        if not codec_name:
            return None
        key = codec_name.lower().strip()
        return _VIDEO_CODEC_MAP.get(key, codec_name.upper())

    @staticmethod
    def _extract_video_bit(video_s: Dict[str, Any]) -> Optional[str]:
        """
        从 ffprobe 视频流提取位深

        :param video_s: 视频流字典
        :return: 位深标签，如 10bit
        """
        bps = str(video_s.get("bits_per_raw_sample") or "").strip()
        if bps.isdigit() and int(bps) > 0:
            return f"{bps}bit"
        pix = str(video_s.get("pix_fmt") or "").strip().lower()
        if pix:
            match = re.search(r"(\d+)(?:le|be)", pix)
            if match and int(match.group(1)) > 0:
                return f"{match.group(1)}bit"
        profile = str(video_s.get("profile") or "").strip()
        if profile:
            match = re.search(r"(?:main|high)\s*(\d+)", profile, re.IGNORECASE)
            if match:
                return f"{match.group(1)}bit"
        if pix:
            return "8bit"
        return None

    @staticmethod
    def _format_audio_codec(audio_s: Dict[str, Any]) -> Optional[str]:
        """
        生成音轨命名标签（编码 + 声道）

        :param audio_s: 音频流字典
        :return: 音轨标签，如 FLAC 2.0
        """
        codec_name = audio_s.get("codec_name")
        if not codec_name:
            return None
        key = codec_name.lower().strip()
        label = _AUDIO_CODEC_MAP.get(key, codec_name.upper())
        channels = SubtitleMetaPatch._to_int(audio_s.get("channels"))
        tag = None
        if channels:
            tag = _CHANNEL_MAP.get(channels)
        if tag:
            return f"{label} {tag}"
        return label
