#!/usr/bin/env python3
"""AstrBot 视频解析自动归档插件。

配合 ``astrbot_plugin_api_video_parser``（ParseVideo 视频图集解析）使用：
在解析插件调用 parse-video-py 成功、把视频/图集交给 QQ 机器人发送之前，
把同一份媒体文件另存到本机指定目录，并按可自定义的占位符模板命名。

设计要点：
* 不重复调用解析接口：通过挂钩解析插件的 ``parse_video_share_url`` 读取它
  已经拿到的解析结果（标题、作者、图集地址等），解析插件升级后方法名变化时
  自动退化为从消息文本里提取标题/作者。
* 在 ``on_decorating_result`` 阶段工作，也就是「结果已经产出、还没发给平台」，
  因此保存的是机器人真正要发出去的那份本地文件，不需要二次下载。
* 配置、历史记录都保存在 AstrBot 的 plugin_data 目录，插件升级不会丢。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import random
import re
import shutil
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import aiohttp

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star
from astrbot.api.web import json_response, request
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

PLUGIN_NAME = "astrbot_plugin_video_archiver"
PLUGIN_VERSION = "1.0.0"
PARSER_PLUGIN_NAME = "astrbot_plugin_api_video_parser"

DEFAULT_SAVE_DIR = "~/Videos/AstrBot"
DEFAULT_TEMPLATE = "[作者]/[标题]_[日期]"
MAX_TEMPLATE_LEN = 400
MAX_COMPONENT_BYTES = 240
PARSE_CACHE_TTL = 600.0
PARSE_CACHE_MAX = 64
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".flv", ".mkv", ".avi", ".ts", ".m3u8"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp", ".avif"}


# ---------------------------------------------------------------------------
# 占位符定义
# ---------------------------------------------------------------------------

PLACEHOLDERS: list[dict[str, str]] = [
    {"key": "作者", "desc": "作者 / UP 主昵称", "example": "小小鱼"},
    {"key": "作者ID", "desc": "作者 ID（解析结果提供时可用）", "example": "MS4wLjABAAAA"},
    {"key": "标题", "desc": "视频 / 图集标题", "example": "今天的晚霞真好看"},
    {"key": "日期", "desc": "保存日期，格式 2026-11-11", "example": "2026-11-11"},
    {"key": "时间", "desc": "保存时间，格式 22-21-33", "example": "22-21-33"},
    {"key": "年", "desc": "年份", "example": "2026"},
    {"key": "月", "desc": "月份（两位）", "example": "11"},
    {"key": "日", "desc": "日期（两位）", "example": "11"},
    {"key": "时", "desc": "小时（两位）", "example": "22"},
    {"key": "分", "desc": "分钟（两位）", "example": "21"},
    {"key": "秒", "desc": "秒（两位）", "example": "33"},
    {"key": "视频平台", "desc": "平台中文名，识别不出来时为空", "example": "抖音"},
    {"key": "平台代码", "desc": "平台英文代码", "example": "douyin"},
    {"key": "原视频URL", "desc": "原始分享链接（已清洗成适合做文件名的形式）", "example": "v.douyin.com_iRNBZ4k_ab12cd"},
    {"key": "视频ID", "desc": "从链接中提取的作品 ID", "example": "7300000000000000000"},
    {"key": "视频类型", "desc": "视频 / 图集", "example": "视频"},
    {"key": "序号", "desc": "图集序号（01、02……）", "example": "01"},
    {"key": "发送者", "desc": "发送者昵称", "example": "kakusei"},
    {"key": "发送者QQ", "desc": "发送者 QQ 号 / SID", "example": "123456789"},
    {"key": "群号", "desc": "群号，私聊时为空", "example": "987654321"},
    {"key": "随机", "desc": "6 位随机串，用于避免重名", "example": "a1b2c3"},
    {"key": "后缀", "desc": "文件后缀（不带点），写了它就不再自动追加", "example": "mp4"},
]

# 别名 -> 标准占位符名
PLACEHOLDER_ALIASES: dict[str, str] = {
    "作者": "作者",
    "作者昵称": "作者",
    "UP主": "作者",
    "up主": "作者",
    "主播": "作者",
    "标题": "标题",
    "作品标题": "标题",
    "日期": "日期",
    "时间": "时间",
    "年": "年",
    "月": "月",
    "日": "日",
    "时": "时",
    "分": "分",
    "秒": "秒",
    "视频平台": "视频平台",
    "平台": "视频平台",
    "平台代码": "平台代码",
    "原视频URL": "原视频URL",
    "原视频url": "原视频URL",
    "链接": "原视频URL",
    "URL": "原视频URL",
    "url": "原视频URL",
    "视频ID": "视频ID",
    "视频id": "视频ID",
    "作品ID": "视频ID",
    "视频类型": "视频类型",
    "类型": "视频类型",
    "序号": "序号",
    "索引": "序号",
    "发送者": "发送者",
    "发送者昵称": "发送者",
    "发送者QQ": "发送者QQ",
    "发送者ID": "发送者QQ",
    "QQ": "发送者QQ",
    "SID": "发送者QQ",
    "群号": "群号",
    "随机": "随机",
    "后缀": "后缀",
    "文件后缀": "后缀",
    "扩展名": "后缀",
}

TEMPLATE_PRESETS = [
    DEFAULT_TEMPLATE,
    "[作者]/[日期]_[标题]",
    "[视频平台]/[作者]/[标题]",
    "[日期]/[标题]_[原视频URL]",
    "[作者]/[标题]",
]

# 没有接入解析插件模块时的兜底平台识别表：(平台代码, 中文名, 域名列表)
FALLBACK_PLATFORMS: list[tuple[str, str, tuple[str, ...]]] = [
    ("douyin", "抖音", ("douyin.com", "iesdouyin.com")),
    ("tiktok", "TikTok", ("tiktok.com",)),
    ("kuaishou", "快手", ("kuaishou.com",)),
    ("bilibili", "B站", ("bilibili.com", "b23.tv")),
    ("xiaohongshu", "小红书", ("xiaohongshu.com", "xhslink.com", "xhslink.cn")),
    ("weibo", "微博", ("weibo.com", "weibo.cn")),
    ("xigua", "西瓜视频", ("ixigua.com",)),
    ("weishi", "微视", ("weishi.qq.com",)),
    ("pipixia", "皮皮虾", ("pipix.com",)),
    ("pipigx", "皮皮搞笑", ("pipigx.com", "xiaochuankeji.cn")),
    ("huoshan", "火山小视频", ("huoshan.com",)),
    ("pear", "梨视频", ("pearvideo.com",)),
    ("haokan", "好看视频", ("haokan.baidu.com",)),
    ("huya", "虎牙", ("huya.com",)),
    ("acfun", "AcFun", ("acfun.cn",)),
    ("meipai", "美拍", ("meipai.com",)),
    ("doupai", "逗拍", ("doupai.cc",)),
    ("quanminkge", "全民K歌", ("kg.qq.com",)),
    ("sixroom", "6间房", ("6.cn",)),
    ("xinpianchang", "新片场", ("xinpianchang.com",)),
    ("twitter", "Twitter/X", ("x.com", "twitter.com", "t.co")),
    ("zuiyou", "最右", ("izuiyou.com",)),
    ("cctv", "央视网", ("cctv.com",)),
    ("sohu", "搜狐视频", ("sohu.com",)),
    ("tencent_video", "腾讯视频", ("v.qq.com",)),
    ("lvzhou", "绿洲", ("lvzhou.com",)),
    ("duxiao", "度小视", ("quanmin.baidu.com",)),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "save_video": True,
    "save_album": True,
    "overwrite": False,
    "skip_duplicate": False,
    "whitelist": [],
    "save_dir": DEFAULT_SAVE_DIR,
    "template": DEFAULT_TEMPLATE,
    "max_filename_len": 100,
    "history_limit": 200,
}


# ---------------------------------------------------------------------------
# 文本 / 文件名工具
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\[([^\[\]\r\n]{1,32})\]")
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_SPACE_RE = re.compile(r"\s+")
_MULTI_UNDERSCORE_RE = re.compile(r"_{2,}")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_TITLE_RE = re.compile(r"(?:^|\n)\s*标题\s*[:：]\s*([^\n]+)")
_AUTHOR_RE = re.compile(r"(?:^|\n)\s*作者\s*[:：]\s*([^\n]+)")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_value(value: Any, limit: int = 120) -> str:
    """把任意值清洗成可以放进文件名的字符串。"""
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    text = _ILLEGAL_CHARS_RE.sub("_", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    text = text.strip(" .")
    if len(text) > limit:
        text = text[:limit]
    return text


def slugify_url(url: str) -> str:
    """把 URL 变成可读、稳定、带短哈希的文件名片段。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    text = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    text = text.split("#", 1)[0].split("?", 1)[0]
    text = urllib.parse.unquote(text).rstrip("/")
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", text)
    text = text.strip("._")
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:6]
    if len(text) > 48:
        text = text[:48].rstrip("._")
    return f"{text}_{digest}" if text else digest


def _clean_component(text: str, max_len: int) -> str:
    text = str(text or "").strip().strip(".")
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_UNDERSCORE_RE.sub("_", text)
    text = text.strip(" _-")
    if not text:
        return ""
    if text.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        text = f"_{text}"
    if len(text) > max_len:
        text = text[:max_len].rstrip(" _-")
    while len(text.encode("utf-8")) > MAX_COMPONENT_BYTES and len(text) > 1:
        text = text[:-1]
    return text


def render_template(
    template: str,
    values: dict[str, Any],
    max_component_len: int = 100,
) -> tuple[list[str], set[str]]:
    """渲染命名模板，返回 (相对路径分段, 未知占位符集合)。"""

    unknown: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        canonical = PLACEHOLDER_ALIASES.get(key)
        if canonical is None:
            unknown.add(key)
            return ""
        if canonical == "原视频URL":
            return slugify_url(values.get(canonical, ""))
        return sanitize_value(values.get(canonical, ""))

    rendered = _PLACEHOLDER_RE.sub(_replace, str(template or ""))
    parts: list[str] = []
    for chunk in rendered.replace("\\", "/").split("/"):
        component = _clean_component(chunk, max(20, int(max_component_len)))
        if component:
            parts.append(component)
    return parts, unknown


def validate_template(template: str) -> str:
    text = str(template or "").strip()
    if not text:
        raise ValueError("命名模板不能为空")
    if "\x00" in text:
        raise ValueError("命名模板包含非法字符")
    if len(text) > MAX_TEMPLATE_LEN:
        raise ValueError(f"命名模板过长（最多 {MAX_TEMPLATE_LEN} 个字符）")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("命名模板请使用相对路径（不要以 / 开头）")
    for part in normalized.split("/"):
        if part.strip() in ("..", "."):
            raise ValueError("命名模板不能包含 . 或 .. 路径片段")
    return normalized


def _has_known_ext(name: str) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in VIDEO_EXTS or suffix in IMAGE_EXTS


def _ext_from_name(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in VIDEO_EXTS or suffix in IMAGE_EXTS:
        return suffix
    return ""


def _video_ext(candidate: str) -> str:
    suffix = _ext_from_name(candidate)
    return suffix if suffix in VIDEO_EXTS else ""


def _image_ext(candidate: str) -> str:
    suffix = _ext_from_name(candidate)
    return suffix if suffix in IMAGE_EXTS else ""


def _image_ext_from_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) > 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
        if brand in (b"avif",):
            return ".avif"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


def normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text if "://" in text else f"https://{text}")
        return urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.query,
                "",
            )
        )
    except Exception:
        return text.lower()


def extract_video_id(url: str) -> str:
    text = str(url or "")
    patterns = (
        r"/video/(\d{6,})",
        r"/note/(\d{6,})",
        r"modal_id=(\d{6,})",
        r"/short-video/([0-9A-Za-z_-]{4,})",
        r"/photo/(\d{6,})",
        r"/explore/([0-9A-Za-z]{6,})",
        r"/status/(\d{6,})",
        r"/detail/([0-9A-Za-z_-]{6,})",
        r"(BV[0-9A-Za-z]{10})",
        r"[?&]v=([0-9A-Za-z_-]{6,})",
        r"/tv/(\d{6,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def human_size(size: int) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "-"


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _copy_atomic(src: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".part")
    try:
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dest)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _write_bytes_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".part")
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, dest)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def unique_path(path: Path, overwrite: bool) -> Path:
    if overwrite or not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{int(time.time())}{suffix}")


def _to_local_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("file://"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    elif text.startswith("file:"):
        text = text[5:]
    if os.path.isabs(text) and os.path.isfile(text):
        return text
    return None


def _decode_base64_text(text: str) -> bytes | None:
    payload = text.strip()
    if payload.startswith("base64://"):
        payload = payload[len("base64://") :]
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    payload = re.sub(r"\s+", "", payload)
    if not payload or len(payload) < 16 or not _BASE64_RE.match(payload):
        return None
    padding = (-len(payload)) % 4
    if padding:
        payload += "=" * padding
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def _looks_like_raw_base64(text: str) -> bool:
    payload = re.sub(r"\s+", "", text)
    return len(payload) >= 128 and len(payload) % 4 in (0, 2, 3) and bool(_BASE64_RE.match(payload))


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    return default


def expand_save_dir(value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("保存目录不能为空")
    if "\x00" in text:
        raise ValueError("保存目录包含非法字符")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("请填写绝对路径（可以以 ~ 开头，例如 ~/Videos/AstrBot）")
    return path


def normalize_whitelist(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(value, list):
        return result
    for item in value:
        if isinstance(item, dict):
            raw_id = str(item.get("id") or "").strip()
            note = str(item.get("note") or "").strip()[:60]
        else:
            raw_id = str(item or "").strip()
            note = ""
        if not raw_id or len(raw_id) > 128 or any(ch.isspace() for ch in raw_id):
            continue
        key = raw_id.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({"id": raw_id, "note": note})
    return result


def coerce_settings(raw: Any, *, strict: bool = False) -> dict[str, Any]:
    """把任意来源的设置整理成合法配置；strict 模式下非法值直接报错。"""
    data = raw if isinstance(raw, dict) else {}
    cfg: dict[str, Any] = {
        "enabled": _as_bool(data.get("enabled", DEFAULT_SETTINGS["enabled"]), True),
        "save_video": _as_bool(
            data.get("save_video", DEFAULT_SETTINGS["save_video"]), True
        ),
        "save_album": _as_bool(
            data.get("save_album", DEFAULT_SETTINGS["save_album"]), True
        ),
        "overwrite": _as_bool(
            data.get("overwrite", DEFAULT_SETTINGS["overwrite"]), False
        ),
        "skip_duplicate": _as_bool(
            data.get("skip_duplicate", DEFAULT_SETTINGS["skip_duplicate"]), False
        ),
        "whitelist": normalize_whitelist(
            data.get("whitelist", DEFAULT_SETTINGS["whitelist"])
        ),
        "save_dir": str(data.get("save_dir") or DEFAULT_SAVE_DIR).strip(),
        "template": str(data.get("template") or DEFAULT_TEMPLATE),
        "max_filename_len": 100,
        "history_limit": 200,
    }

    try:
        cfg["max_filename_len"] = int(data.get("max_filename_len", 100))
    except (TypeError, ValueError):
        if strict:
            raise ValueError("文件名长度上限必须是数字")
    cfg["max_filename_len"] = max(20, min(200, cfg["max_filename_len"]))

    try:
        cfg["history_limit"] = int(data.get("history_limit", 200))
    except (TypeError, ValueError):
        if strict:
            raise ValueError("历史记录条数必须是数字")
    cfg["history_limit"] = max(10, min(1000, cfg["history_limit"]))

    if strict:
        cfg["save_dir"] = str(expand_save_dir(cfg["save_dir"]))
        cfg["template"] = validate_template(cfg["template"])
    else:
        try:
            cfg["save_dir"] = str(expand_save_dir(cfg["save_dir"]))
        except ValueError:
            cfg["save_dir"] = DEFAULT_SAVE_DIR

    return cfg


# ---------------------------------------------------------------------------
# 采集到的消息媒体
# ---------------------------------------------------------------------------

@dataclass
class Collected:
    videos: list[Any] = field(default_factory=list)
    images: list[Any] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    node_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------

class VideoArchiverPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._data_dir = self._resolve_data_dir()
        self._settings_path = self._data_dir / "settings.json"
        self._history_path = self._data_dir / "history.json"

        raw_settings = _read_json(self._settings_path, None)
        self._cfg: dict[str, Any] = coerce_settings(raw_settings or {})
        if raw_settings is None:
            try:
                _write_json_atomic(self._settings_path, self._cfg)
            except OSError:
                self.logger.warning("无法创建默认配置文件: %s", self._settings_path)

        history = _read_json(self._history_path, {})
        if isinstance(history, list):
            history = {"items": history, "stats": {}}
        if not isinstance(history, dict):
            history = {}
        self._history: list[dict[str, Any]] = [
            item for item in (history.get("items") or []) if isinstance(item, dict)
        ][: self._cfg["history_limit"]]
        stats = history.get("stats") if isinstance(history.get("stats"), dict) else {}
        self._stats: dict[str, int] = {
            "saved": int(stats.get("saved") or 0),
            "failed": int(stats.get("failed") or 0),
            "skipped": int(stats.get("skipped") or 0),
            "bytes": int(stats.get("bytes") or 0),
        }

        self._http: aiohttp.ClientSession | None = None
        self._parse_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._io_lock = asyncio.Lock()
        self._hook_ok = False

        self._register_web_apis()
        self.logger.info(
            "video_archiver v%s 已加载：目录=%s，白名单=%d 人，模板=%s",
            PLUGIN_VERSION,
            self._cfg["save_dir"],
            len(self._cfg["whitelist"]),
            self._cfg["template"],
        )

    # -- 生命周期 ---------------------------------------------------------

    async def initialize(self) -> None:
        self._ensure_parser_hook()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self._ensure_parser_hook()

    async def terminate(self) -> None:
        self._restore_parser_hook()
        if self._http is not None and not self._http.closed:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None

    # -- 目录与配置 -------------------------------------------------------

    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            base = Path(get_astrbot_plugin_data_path())
            target = base / PLUGIN_NAME
            target.mkdir(parents=True, exist_ok=True)
            return target
        except Exception:
            fallback = Path(__file__).resolve().parent / "_data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _save_root(self) -> Path:
        try:
            return expand_save_dir(self._cfg.get("save_dir"))
        except ValueError:
            return Path(DEFAULT_SAVE_DIR).expanduser()

    async def _persist_settings(self) -> None:
        async with self._io_lock:
            await asyncio.to_thread(_write_json_atomic, self._settings_path, self._cfg)

    async def _persist_history(self) -> None:
        payload = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stats": self._stats,
            "items": self._history,
        }
        async with self._io_lock:
            await asyncio.to_thread(_write_json_atomic, self._history_path, payload)

    # -- 解析插件挂钩 -----------------------------------------------------

    def _parser_plugin(self):
        try:
            getter = getattr(self.context, "get_registered_star", None)
            if not callable(getter):
                return None
            return getter(PARSER_PLUGIN_NAME)
        except Exception:
            return None

    def _parser_instance(self):
        metadata = self._parser_plugin()
        return getattr(metadata, "star_cls", None) if metadata else None

    def _ensure_parser_hook(self) -> bool:
        """挂钩解析插件的 parse_video_share_url，复用其解析结果。"""
        instance = self._parser_instance()
        if instance is None:
            self._hook_ok = False
            return False
        if getattr(instance, "_va_hook_owner", None) is self:
            self._hook_ok = True
            return True

        original = getattr(instance, "_va_original_parse", None)
        if original is None:
            original = getattr(instance, "parse_video_share_url", None)
            if not callable(original):
                self._hook_ok = False
                return False
            try:
                setattr(instance, "_va_original_parse", original)
            except Exception:
                self._hook_ok = False
                return False

        async def _wrapped(url, *args, **kwargs):
            data = await original(url, *args, **kwargs)
            try:
                self._remember_parse(url, data)
            except Exception:
                self.logger.exception("记录解析结果失败 url=%s", url)
            return data

        try:
            setattr(instance, "parse_video_share_url", _wrapped)
            setattr(instance, "_va_hook_owner", self)
        except Exception:
            self._hook_ok = False
            return False
        self._hook_ok = True
        self.logger.info("已挂钩解析插件 parse_video_share_url，可复用其解析结果")
        return True

    def _restore_parser_hook(self) -> None:
        instance = self._parser_instance()
        if instance is None:
            return
        if getattr(instance, "_va_hook_owner", None) is not self:
            return
        try:
            original = getattr(instance, "_va_original_parse", None)
            if original is not None:
                setattr(instance, "parse_video_share_url", original)
            for attr in ("_va_original_parse", "_va_hook_owner"):
                if hasattr(instance, attr):
                    delattr(instance, attr)
            self.logger.info("已取消解析插件挂钩")
        except Exception:
            self.logger.exception("取消解析插件挂钩失败")

    def _remember_parse(self, url: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        task = asyncio.current_task()
        key = normalize_url(url) or f"raw:{url}"
        entry = {
            "url": str(url or ""),
            "data": data,
            "task": id(task) if task is not None else 0,
            "ts": time.time(),
        }
        self._parse_cache[key] = entry
        self._parse_cache.move_to_end(key)
        while len(self._parse_cache) > PARSE_CACHE_MAX:
            self._parse_cache.popitem(last=False)

    def _lookup_parse(self, url: str) -> tuple[dict[str, Any] | None, str]:
        now = time.time()
        entries = [e for e in self._parse_cache.values() if now - e["ts"] <= PARSE_CACHE_TTL]
        if url:
            entry = self._parse_cache.get(normalize_url(url))
            if entry is not None and now - entry["ts"] <= PARSE_CACHE_TTL:
                return entry["data"], entry["url"]
        task = asyncio.current_task()
        if task is not None:
            for entry in reversed(entries):
                if entry["task"] == id(task):
                    return entry["data"], entry["url"]
        return None, url

    # -- 平台与 URL -------------------------------------------------------

    def _parser_module(self):
        metadata = self._parser_plugin()
        return getattr(metadata, "module", None) if metadata else None

    def _detect_platform(self, url: str) -> tuple[str, str]:
        if not url:
            return "", ""
        module = self._parser_module()
        if module is not None:
            key_func = getattr(module, "get_platform_for_url", None)
            name_func = getattr(module, "get_platform_name", None)
            if callable(key_func):
                try:
                    code = key_func(url) or ""
                except Exception:
                    code = ""
                if code:
                    name = ""
                    if callable(name_func):
                        try:
                            name = str(name_func(code) or "")
                        except Exception:
                            name = ""
                    return str(code), name or str(code)

        try:
            host = urllib.parse.urlparse(
                url if "://" in url else f"https://{url}"
            ).netloc.lower()
        except Exception:
            return "", ""
        host = host.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
        for code, name, domains in FALLBACK_PLATFORMS:
            for domain in domains:
                if host == domain or host.endswith("." + domain):
                    return code, name
        return "", ""

    def _extract_url(self, event: AstrMessageEvent) -> str:
        module = self._parser_module()
        if module is not None:
            for name in ("extract_url_from_event_text", "extract_url_from_json_segments"):
                func = getattr(module, name, None)
                if not callable(func):
                    continue
                try:
                    value = func(event)
                except Exception:
                    value = None
                if value:
                    return str(value)
        text = ""
        try:
            text = event.get_message_str() or ""
        except Exception:
            text = ""
        url = self._url_from_text(text)
        if url:
            return url
        for attr in ("message_obj", "raw_message"):
            try:
                blob = getattr(event, attr, None)
            except Exception:
                blob = None
            if blob is None:
                continue
            try:
                text_blob = blob if isinstance(blob, str) else json.dumps(blob, default=str, ensure_ascii=False)
            except Exception:
                text_blob = str(blob)
            url = self._url_from_text(text_blob.replace("\\/", "/"))
            if url:
                return url
        return ""

    @staticmethod
    def _url_from_text(text: str) -> str:
        match = re.search(r"https?://[^\s<>\"'，。！？；：、）】》]+", text or "")
        return match.group(0).rstrip(").,，。;；]") if match else ""

    # -- 白名单 -----------------------------------------------------------

    def _is_whitelisted(self, sender_id: Any) -> bool:
        sid = str(sender_id or "").strip()
        if not sid:
            return False
        for item in self._cfg.get("whitelist") or []:
            candidate = str(item.get("id") or "").strip()
            if not candidate:
                continue
            if candidate == "*":
                return True
            if candidate == sid or candidate.lower() == sid.lower():
                return True
        return False

    # -- 消息处理 ---------------------------------------------------------

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        try:
            await self._handle_event(event)
        except Exception:
            self.logger.exception("video_archiver 发送前处理失败")

    async def _handle_event(self, event: AstrMessageEvent) -> None:
        if not self._cfg.get("enabled"):
            return
        # 运行期间才安装/重载解析插件时，也能在下一条消息前补上挂钩
        self._ensure_parser_hook()
        sender_id = str(event.get_sender_id() or "")
        if not self._is_whitelisted(sender_id):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return
        if event.get_extra("_va_archived"):
            return

        collected = Collected()
        self._collect_chain(list(chain), collected)

        if collected.texts:
            meta = self._meta_from_text("\n".join(collected.texts))
            if meta:
                stash = dict(event.get_extra("_va_text_meta") or {})
                stash.update(meta)
                event.set_extra("_va_text_meta", stash)

        if not collected.videos and not collected.images:
            return

        text_meta = dict(event.get_extra("_va_text_meta") or {})
        url = self._extract_url(event)
        data, resolved_url = self._lookup_parse(url)
        if resolved_url:
            url = resolved_url
        data = data or {}

        # 消息里没有链接时，只相信「明显来自解析插件」的媒体，避免把别的视频误存。
        if data and not url:
            video_paths = self._video_paths(collected.videos)
            if video_paths:
                if not any(self._is_parser_temp(path) for path in video_paths):
                    data = {}
            elif collected.images and not any(
                "图集" in name for name in collected.node_names
            ):
                data = {}

        is_album = bool(data.get("images")) or (
            not collected.videos and any("图集" in name for name in collected.node_names)
        )

        if not data:
            looks_parser = any("解析" in name for name in collected.node_names) or any(
                self._is_parser_temp(path) for path in self._video_paths(collected.videos)
            )
            if not looks_parser:
                return

        if is_album:
            if not self._cfg.get("save_album"):
                return
            await self._archive_album(event, data, url, collected, text_meta)
        else:
            if not self._cfg.get("save_video") or not collected.videos:
                return
            await self._archive_video(event, data, url, collected, text_meta)

    def _collect_chain(self, components: Iterable[Any], out: Collected) -> None:
        for component in components:
            if isinstance(component, Video):
                out.videos.append(component)
            elif isinstance(component, Nodes):
                for node in getattr(component, "nodes", []) or []:
                    name = str(getattr(node, "name", "") or "")
                    if name:
                        out.node_names.append(name)
                    self._collect_chain(getattr(node, "content", []) or [], out)
            elif isinstance(component, Node):
                self._collect_chain(getattr(component, "content", []) or [], out)
            elif isinstance(component, File):
                hint = f"{getattr(component, 'name', '') or ''} {getattr(component, 'file_', '') or ''}"
                if _video_ext(hint):
                    out.videos.append(component)
            elif isinstance(component, Image):
                out.images.append(component)
            elif isinstance(component, Plain):
                text = str(getattr(component, "text", "") or "")
                if text:
                    out.texts.append(text)

    @staticmethod
    def _meta_from_text(text: str) -> dict[str, str]:
        meta: dict[str, str] = {}
        title = _TITLE_RE.search(text or "")
        author = _AUTHOR_RE.search(text or "")
        if title:
            value = title.group(1).strip()
            if value and "未命名" not in value:
                meta["title"] = value
        if author:
            value = author.group(1).strip()
            if value and "未知作者" not in value:
                meta["author"] = value
        return meta

    def _video_paths(self, components: Iterable[Any]) -> list[str]:
        paths: list[str] = []
        for component in components:
            path = self._component_path(component)
            if path:
                paths.append(path)
        return paths

    @staticmethod
    def _component_path(component: Any) -> str | None:
        candidates: list[Any] = []
        if isinstance(component, Video):
            candidates = [
                getattr(component, "path", None),
                getattr(component, "file", None),
                getattr(component, "url", None),
            ]
        elif isinstance(component, File):
            candidates = [
                getattr(component, "file_", None),
                getattr(component, "url", None),
            ]
        for candidate in candidates:
            path = _to_local_path(candidate)
            if path:
                return path
        return None

    @staticmethod
    def _is_parser_temp(path: str) -> bool:
        if not path:
            return False
        try:
            base = Path(get_astrbot_temp_path()) / "video_parser"
            return base.resolve(strict=False) in Path(path).resolve(strict=False).parents
        except Exception:
            return False

    # -- 保存 -------------------------------------------------------------

    async def _archive_video(
        self,
        event: AstrMessageEvent,
        data: dict[str, Any],
        url: str,
        collected: Collected,
        text_meta: dict[str, str],
    ) -> None:
        event.set_extra("_va_archived", True)
        source = None
        for component in collected.videos:
            candidate = self._component_path(component)
            if candidate:
                source = candidate
                break
        if not source:
            self.logger.warning("没有找到可保存的本地视频文件（可能只有 URL）")
            await self._record(
                event,
                status="error",
                kind="视频",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[],
                size=0,
                message="未找到本地视频文件",
            )
            return

        if self._cfg.get("skip_duplicate") and self._seen_url(url):
            await self._record(
                event,
                status="skipped",
                kind="视频",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[],
                size=0,
                message="该链接已保存过，跳过",
            )
            return

        ext = _video_ext(source) or _video_ext(url) or ".mp4"
        values = self._build_values(event, url, data, text_meta, "视频", ext=ext)
        try:
            dest = self._build_dest(values, ext)
            if Path(source).resolve(strict=False) == dest.resolve(strict=False):
                self.logger.info("源文件与目标文件相同，跳过复制: %s", dest)
                return
            final = unique_path(dest, bool(self._cfg.get("overwrite")))
            await asyncio.to_thread(_copy_atomic, source, final)
            size = final.stat().st_size
            self.logger.info("视频已归档: %s (%s)", final, human_size(size))
            await self._record(
                event,
                status="ok",
                kind="视频",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[str(final)],
                size=size,
                message="已保存",
            )
        except Exception as exc:
            self.logger.error("保存视频失败: %s", exc, exc_info=True)
            await self._record(
                event,
                status="error",
                kind="视频",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[],
                size=0,
                message=f"{type(exc).__name__}: {exc}",
            )

    async def _archive_album(
        self,
        event: AstrMessageEvent,
        data: dict[str, Any],
        url: str,
        collected: Collected,
        text_meta: dict[str, str],
    ) -> None:
        event.set_extra("_va_archived", True)

        if self._cfg.get("skip_duplicate") and self._seen_url(url):
            await self._record(
                event,
                status="skipped",
                kind="图集",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[],
                size=0,
                message="该链接已保存过，跳过",
            )
            return

        items: list[dict[str, Any]] = []
        for component in collected.images[:100]:
            item = self._image_item(component)
            if item:
                items.append(item)
        for component in collected.videos[:20]:
            path = self._component_path(component)
            if path:
                items.append({"kind": "video", "path": path, "ext": _video_ext(path) or ".mp4"})

        if not items:
            for image in (data.get("images") or [])[:100]:
                image_url = str((image or {}).get("url") or "").strip()
                if image_url:
                    items.append({"kind": "image", "url": image_url})

        if not items:
            await self._record(
                event,
                status="error",
                kind="图集",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[],
                size=0,
                message="没有找到可保存的图片",
            )
            return

        multi = len(items) > 1
        saved_files: list[str] = []
        total_size = 0
        errors: list[str] = []

        for index, item in enumerate(items, start=1):
            try:
                payload: bytes | None = None
                ext = str(item.get("ext") or "")
                if item.get("data") is not None:
                    payload = item["data"]
                    ext = ext or _image_ext_from_bytes(payload)
                elif item.get("path"):
                    payload = await asyncio.to_thread(Path(item["path"]).read_bytes)
                    ext = ext or _ext_from_name(item["path"]) or _image_ext_from_bytes(payload)
                elif item.get("url"):
                    fetched = await self._fetch_bytes(item["url"])
                    if not fetched:
                        errors.append(f"第 {index} 个文件下载失败")
                        continue
                    payload = fetched
                    ext = ext or _image_ext(item["url"]) or _image_ext_from_bytes(payload)
                if not payload:
                    errors.append(f"第 {index} 个文件内容为空")
                    continue

                if not ext:
                    ext = _image_ext_from_bytes(payload)
                values = self._build_values(
                    event, url, data, text_meta, "图集", index=index, ext=ext
                )
                dest = self._build_dest(values, ext, index=index if multi else None)
                final = unique_path(dest, bool(self._cfg.get("overwrite")))
                await asyncio.to_thread(_write_bytes_atomic, final, payload)
                size = final.stat().st_size
                total_size += size
                saved_files.append(str(final))
            except Exception as exc:
                self.logger.error("保存图集第 %d 项失败: %s", index, exc, exc_info=True)
                errors.append(f"第 {index} 项: {type(exc).__name__}: {exc}")

        if saved_files:
            self.logger.info("图集已归档 %d 项 -> %s", len(saved_files), saved_files[0])
            message = f"已保存 {len(saved_files)}/{len(items)} 项"
            if errors:
                message += "；" + "；".join(errors[:3])
            await self._record(
                event,
                status="ok",
                kind="图集",
                url=url,
                data=data,
                text_meta=text_meta,
                files=saved_files,
                size=total_size,
                message=message,
            )
        else:
            await self._record(
                event,
                status="error",
                kind="图集",
                url=url,
                data=data,
                text_meta=text_meta,
                files=[],
                size=0,
                message="；".join(errors) or "保存失败",
            )

    def _image_item(self, component: Any) -> dict[str, Any] | None:
        candidate = (
            getattr(component, "file", None)
            or getattr(component, "url", None)
            or getattr(component, "path", None)
        )
        if not isinstance(candidate, str) or not candidate.strip():
            return None
        text = candidate.strip()
        if text.startswith(("http://", "https://")):
            return {"kind": "image", "url": text, "ext": _image_ext(text)}
        local = _to_local_path(text)
        if local:
            return {"kind": "image", "path": local, "ext": _image_ext(local)}
        payload = _decode_base64_text(text)
        if payload:
            return {"kind": "image", "data": payload, "ext": _image_ext_from_bytes(payload)}
        return None

    def _build_values(
        self,
        event: AstrMessageEvent,
        url: str,
        data: dict[str, Any],
        text_meta: dict[str, str],
        kind: str,
        index: int | None = None,
        ext: str = "",
    ) -> dict[str, str]:
        now = datetime.now()
        author_obj = data.get("author")
        if isinstance(author_obj, dict):
            author = str(author_obj.get("name") or "")
            author_id = str(author_obj.get("uid") or "")
        else:
            author = str(author_obj or "")
            author_id = ""
        title = str(data.get("title") or "")
        if not title:
            title = str(text_meta.get("title") or "")
        if not author:
            author = str(text_meta.get("author") or "")
        platform_code, platform_name = self._detect_platform(url)

        return {
            "作者": author or "未知作者",
            "作者ID": author_id,
            "标题": title or ("未命名图集" if kind == "图集" else "未命名视频"),
            "日期": now.strftime("%Y-%m-%d"),
            "时间": now.strftime("%H-%M-%S"),
            "年": now.strftime("%Y"),
            "月": now.strftime("%m"),
            "日": now.strftime("%d"),
            "时": now.strftime("%H"),
            "分": now.strftime("%M"),
            "秒": now.strftime("%S"),
            "视频平台": platform_name,
            "平台代码": platform_code,
            "原视频URL": url,
            "视频ID": extract_video_id(url),
            "视频类型": kind,
            "序号": f"{(index or 1):02d}",
            "发送者": str(event.get_sender_name() or ""),
            "发送者QQ": str(event.get_sender_id() or ""),
            "群号": str(event.get_group_id() or ""),
            "随机": f"{random.randint(0, 0xFFFFFF):06x}",
            "后缀": (ext or "").lstrip("."),
        }

    def _build_dest(
        self,
        values: dict[str, str],
        ext: str,
        index: int | None = None,
    ) -> Path:
        template = self._cfg.get("template") or DEFAULT_TEMPLATE
        max_len = int(self._cfg.get("max_filename_len") or 100)
        parts, _unknown = render_template(template, values, max_len)
        if not parts:
            parts = [values.get("标题") or "未命名"]

        name = parts[-1]
        template_lower = template
        has_index_tag = any(tag in template_lower for tag in ("[序号]", "[索引]"))
        has_suffix_tag = any(
            tag in template_lower for tag in ("[后缀]", "[文件后缀]", "[扩展名]")
        )
        if index is not None and not has_index_tag:
            name = f"{name}_{index:02d}"
        if not has_suffix_tag and not _has_known_ext(name) and ext:
            name += ext
        suffix = Path(name).suffix if _has_known_ext(name) else ""
        stem = name[: -len(suffix)] if suffix else name
        cleaned = _clean_component(stem, max_len)
        if not cleaned:
            cleaned = "未命名"
        parts[-1] = f"{cleaned}{suffix}"
        if not parts[-1]:
            raise ValueError("生成的文件名为空")

        root = self._save_root()
        dest = root.joinpath(*parts)
        root_resolved = root.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        if not dest_resolved.is_relative_to(root_resolved):
            raise ValueError("生成的目标路径越出了保存目录")
        return dest_resolved

    # -- 下载 -------------------------------------------------------------

    def _parser_api_base(self) -> str:
        metadata = self._parser_plugin()
        config = getattr(metadata, "config", None) if metadata else None
        if config is not None:
            try:
                base = str(config.get("parser_api_base_url") or "").strip()
            except Exception:
                base = ""
            if base:
                return base.rstrip("/")
        return "http://127.0.0.1:8000"

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120, connect=15)
            )
        return self._http

    async def _fetch_bytes(self, url: str) -> bytes | None:
        base = self._parser_api_base()
        candidates: list[str] = []
        if base:
            candidates.append(f"{base}/download?url={urllib.parse.quote(url, safe='')}")
        candidates.append(url)
        referer = ""
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.endswith("douyin.com"):
            referer = "https://www.douyin.com/"
        elif host.endswith("kuaishou.com"):
            referer = "https://www.kuaishou.com/"
        elif host.endswith("xiaohongshu.com"):
            referer = "https://www.xiaohongshu.com/"
        for candidate in candidates:
            headers = {"User-Agent": UA}
            if referer:
                headers["Referer"] = referer
            try:
                async with self._session().get(candidate, headers=headers) as response:
                    if response.status >= 400:
                        continue
                    data = await response.content.read()
                    if data:
                        return data
            except Exception as exc:
                self.logger.debug("下载失败 %s: %s", candidate, exc)
        return None

    # -- 历史 -------------------------------------------------------------

    def _seen_url(self, url: str) -> bool:
        key = normalize_url(url)
        if not key:
            return False
        for item in self._history:
            if item.get("status") == "ok" and normalize_url(str(item.get("url") or "")) == key:
                return True
        return False

    async def _record(
        self,
        event: AstrMessageEvent,
        *,
        status: str,
        kind: str,
        url: str,
        data: dict[str, Any],
        text_meta: dict[str, str],
        files: list[str],
        size: int,
        message: str,
    ) -> None:
        author_obj = data.get("author")
        author = ""
        if isinstance(author_obj, dict):
            author = str(author_obj.get("name") or "")
        title = str(data.get("title") or "") or str(text_meta.get("title") or "")
        if not author:
            author = str(text_meta.get("author") or "")
        _code, platform_name = self._detect_platform(url)
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
            "status": status,
            "kind": kind,
            "platform": platform_name,
            "sender_id": str(event.get_sender_id() or ""),
            "sender_name": str(event.get_sender_name() or ""),
            "title": title,
            "author": author,
            "url": url,
            "files": files,
            "size": size,
            "message": message,
        }
        self._history.insert(0, entry)
        self._history = self._history[: int(self._cfg.get("history_limit") or 200)]
        if status == "ok":
            self._stats["saved"] += 1
            self._stats["bytes"] += int(size or 0)
        elif status == "error":
            self._stats["failed"] += 1
        else:
            self._stats["skipped"] += 1
        try:
            await self._persist_history()
        except Exception:
            self.logger.exception("写入历史记录失败")

    # -- Web API ----------------------------------------------------------

    def _register_web_apis(self) -> None:
        routes = (
            (f"/{PLUGIN_NAME}/settings", self._web_settings_get, ["GET"], "读取归档设置"),
            (f"/{PLUGIN_NAME}/settings", self._web_settings_save, ["POST"], "保存归档设置"),
            (f"/{PLUGIN_NAME}/test_dir", self._web_test_dir, ["POST"], "测试保存目录"),
            (f"/{PLUGIN_NAME}/preview", self._web_preview, ["POST"], "预览命名结果"),
            (f"/{PLUGIN_NAME}/history", self._web_history, ["GET"], "读取归档记录"),
            (f"/{PLUGIN_NAME}/history/clear", self._web_history_clear, ["POST"], "清空归档记录"),
        )
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception:
                self.logger.exception("注册 Web API 失败: %s", route)

    def _parser_info(self) -> dict[str, Any]:
        metadata = self._parser_plugin()
        instance = self._parser_instance()
        return {
            "installed": metadata is not None and instance is not None,
            "hooked": getattr(instance, "_va_hook_owner", None) is self,
            "name": getattr(metadata, "name", None) if metadata else None,
            "version": getattr(metadata, "version", None) if metadata else None,
            "api_base": self._parser_api_base(),
        }

    async def _web_settings_get(self):
        self._ensure_parser_hook()
        root = self._save_root()
        return json_response(
            {
                "ok": True,
                "settings": self._cfg,
                "defaults": DEFAULT_SETTINGS,
                "placeholders": PLACEHOLDERS,
                "presets": TEMPLATE_PRESETS,
                "history": self._history[:20],
                "history_total": len(self._history),
                "stats": self._stats,
                "parser": self._parser_info(),
                "data_dir": str(self._data_dir),
                "save_root": str(root),
                "version": PLUGIN_VERSION,
            }
        )

    async def _web_settings_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return json_response({"ok": False, "message": "请求格式不正确"})
        raw = payload.get("settings")
        if not isinstance(raw, dict):
            raw = payload
        try:
            cfg = coerce_settings(raw, strict=True)
        except ValueError as exc:
            return json_response({"ok": False, "message": str(exc)})
        except Exception as exc:
            return json_response({"ok": False, "message": f"设置不合法：{exc}"})

        warning = ""
        try:
            Path(cfg["save_dir"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warning = f"目录暂时无法创建（{exc}），设置已保存，请在发送前确认目录可写"

        self._cfg = cfg
        try:
            await self._persist_settings()
        except OSError as exc:
            return json_response({"ok": False, "message": f"写入配置文件失败：{exc}"})

        self.logger.info(
            "归档设置已更新：enabled=%s 目录=%s 模板=%s 白名单=%d",
            cfg["enabled"],
            cfg["save_dir"],
            cfg["template"],
            len(cfg["whitelist"]),
        )
        return json_response({"ok": True, "settings": cfg, "warning": warning})

    async def _web_test_dir(self):
        payload = await request.json(default={})
        raw = payload.get("path") if isinstance(payload, dict) else None
        try:
            path = expand_save_dir(raw)
        except ValueError as exc:
            return json_response({"ok": False, "message": str(exc)})

        def _probe() -> dict[str, Any]:
            existed = path.exists()
            path.mkdir(parents=True, exist_ok=True)
            probe_file = path / ".video_archiver_write_test"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            usage = shutil.disk_usage(path)
            return {
                "path": str(path),
                "created": not existed,
                "free": usage.free,
                "total": usage.total,
                "writable": True,
            }

        try:
            info = await asyncio.to_thread(_probe)
        except Exception as exc:
            return json_response({"ok": False, "message": f"目录不可写：{exc}"})
        info["ok"] = True
        info["free_text"] = human_size(info["free"])
        info["total_text"] = human_size(info["total"])
        return json_response(info)

    async def _web_preview(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        template = str(payload.get("template") or self._cfg.get("template") or DEFAULT_TEMPLATE)
        try:
            template = validate_template(template)
        except ValueError as exc:
            return json_response({"ok": False, "message": str(exc)})
        sample = self._sample_values()
        sample.update(self._coerce_sample(payload.get("sample")))
        try:
            max_len = int(payload.get("max_len") or self._cfg.get("max_filename_len") or 100)
        except (TypeError, ValueError):
            max_len = int(self._cfg.get("max_filename_len") or 100)
        max_len = max(20, min(200, max_len))
        parts, unknown = render_template(template, sample, max_len)
        if not parts:
            return json_response({"ok": False, "message": "模板渲染结果为空，请检查占位符"})
        name = parts[-1]
        has_suffix_tag = any(
            tag in template for tag in ("[后缀]", "[文件后缀]", "[扩展名]")
        )
        if not has_suffix_tag and not _has_known_ext(name):
            name += ".mp4"
        relative = "/".join([*parts[:-1], name])
        return json_response(
            {
                "ok": True,
                "path": relative,
                "save_root": str(self._save_root()),
                "unknown": sorted(unknown),
            }
        )

    @staticmethod
    def _coerce_sample(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        allowed = {item["key"] for item in PLACEHOLDERS}
        result: dict[str, str] = {}
        for key, item in value.items():
            key_text = str(key)
            canonical = PLACEHOLDER_ALIASES.get(key_text)
            if canonical and canonical in allowed:
                result[canonical] = str(item or "")
        return result

    @staticmethod
    def _sample_values() -> dict[str, str]:
        now = datetime.now()
        return {
            "作者": "小小鱼",
            "作者ID": "MS4wLjABAAAAexample",
            "标题": "今天的晚霞真好看",
            "日期": now.strftime("%Y-%m-%d"),
            "时间": now.strftime("%H-%M-%S"),
            "年": now.strftime("%Y"),
            "月": now.strftime("%m"),
            "日": now.strftime("%d"),
            "时": now.strftime("%H"),
            "分": now.strftime("%M"),
            "秒": now.strftime("%S"),
            "视频平台": "抖音",
            "平台代码": "douyin",
            "原视频URL": "https://v.douyin.com/iRNBZ4k/",
            "视频ID": "7300000000000000000",
            "视频类型": "视频",
            "序号": "01",
            "发送者": "kakusei",
            "发送者QQ": "123456789",
            "群号": "987654321",
            "随机": "a1b2c3",
            "后缀": "mp4",
        }

    async def _web_history(self):
        try:
            limit = request.query.get("limit", 50, type=int)
        except Exception:
            limit = 50
        limit = max(1, min(200, int(limit or 50)))
        return json_response(
            {
                "ok": True,
                "items": self._history[:limit],
                "total": len(self._history),
                "stats": self._stats,
            }
        )

    async def _web_history_clear(self):
        self._history = []
        self._stats = {"saved": 0, "failed": 0, "skipped": 0, "bytes": 0}
        try:
            await self._persist_history()
        except OSError as exc:
            return json_response({"ok": False, "message": f"清空记录失败：{exc}"})
        return json_response({"ok": True})
