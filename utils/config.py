"""
🧠 config.py —— 配置加载、会话池、频道解析、工具函数
                自动判断直播/点播，无需手动标记
"""
import os
import re
import asyncio
import aiohttp
from typing import Dict, List, Tuple, Set, Optional
from dataclasses import dataclass, field
from urllib.parse import urljoin

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    SOURCES_FILE, DEMO_FILE, BLACKLIST_FILE,
    MAX_CONCURRENCY, REQUEST_TIMEOUT, AUTO_VOD_KEYWORDS
)


# ─────────────────────────────────────────────────────
#  数据结构
# ─────────────────────────────────────────────────────
@dataclass
class Channel:
    name: str
    genre: str = ""
    is_vod: bool = False


@dataclass
class SourceEntry:
    name: str
    url: str
    speed: float = 0.0
    width: int = 0
    height: int = 0
    resolution: str = ""
    group: str = ""         # 上游源中的 group-title
    is_vod: bool = False    # 是否自动判定为点播


# ─────────────────────────────────────────────────────
#  自动 VOD 判断
# ─────────────────────────────────────────────────────
def _auto_detect_vod(name: str, group: str = "", url: str = "") -> bool:
    """
    全自动判断一个频道是直播还是点播
    
    判断逻辑（命中任一即为点播）：
    1. 频道名 / group-title 包含 AUTO_VOD_KEYWORDS 中的关键词
    2. demo.txt 中该频道所在的分类名包含关键词
    3. URL 路径特征（如 /vod/、/movie/ 等）
    
    不需要用户手动加任何标记！
    """
    text = f"{name} {group} {url}".lower()

    # 关键词匹配
    for kw in AUTO_VOD_KEYWORDS:
        if kw.lower() in text:
            return True

    # URL 路径特征
    vod_url_patterns = ["/vod/", "/movie/", "/video/", "/replay/", "/catchup/"]
    url_lower = url.lower()
    for p in vod_url_patterns:
        if p in url_lower:
            return True

    return False


def _is_genre_vod(genre_name: str) -> bool:
    """判断 demo.txt 中的分类名是否暗示点播"""
    for kw in AUTO_VOD_KEYWORDS:
        if kw.lower() in genre_name.lower():
            return True
    return False


# ─────────────────────────────────────────────────────
#  Demo 模板解析
# ─────────────────────────────────────────────────────
def parse_demo(path: str = DEMO_FILE) -> List[Tuple[str, List[Channel]]]:
    """
    解析 demo.txt，返回 [(genre_name, [Channel, ...]), ...]
    
    自动判断逻辑：
    - 分类名包含关键词 → 该分类下所有频道 is_vod = True
    - 单个频道名包含关键词 → 该频道 is_vod = True
    """
    sections: List[Tuple[str, List[Channel]]] = []
    current_genre = "未分类"
    current_channels: List[Channel] = []

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            if ",#genre#" in line:
                if current_channels:
                    sections.append((current_genre, current_channels))
                current_genre = line.replace(",#genre#", "").strip()
                current_channels = []
                continue

            # 自动判断：分类名 OR 频道名
            genre_is_vod = _is_genre_vod(current_genre)
            name_is_vod = _auto_detect_vod(line)

            ch = Channel(
                name=line,
                genre=current_genre,
                is_vod=genre_is_vod or name_is_vod
            )
            current_channels.append(ch)

    if current_channels:
        sections.append((current_genre, current_channels))

    return sections


# ─────────────────────────────────────────────────────
#  黑名单加载
# ─────────────────────────────────────────────────────
def load_blacklist(path: str = BLACKLIST_FILE) -> Set[str]:
    bl: Set[str] = set()
    if not os.path.exists(path):
        return bl
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                bl.add(line.lower())
    return bl


# ─────────────────────────────────────────────────────
#  上游源加载
# ─────────────────────────────────────────────────────
def load_source_urls(path: str = SOURCES_FILE) -> List[str]:
    urls: List[str] = []
    if not os.path.exists(path):
        return urls
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


# ─────────────────────────────────────────────────────
#  上游源拉取 & 解析
# ─────────────────────────────────────────────────────
async def fetch_all_sources(
    source_urls: List[str]
) -> Tuple[Dict[str, List[SourceEntry]], set]:
    """
    并发拉取所有上游源
    返回 (channel_map, dead_urls)
    """
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENCY, ssl=False)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT * 2)

    all_entries: List[SourceEntry] = []
    dead_urls: set = set()

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [_fetch_one(session, url, sem) for url in source_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for url, content in results:
        if isinstance(content, Exception) or content is None:
            print(f"  ⛔ 上游源死链/超时: {url}")
            dead_urls.add(url)
            continue

        if not content.strip():
            print(f"  ⛔ 上游源内容为空: {url}")
            dead_urls.add(url)
            continue

        entries = _parse_source_content(content)
        if not entries:
            print(f"  ⛔ 上游源解析无频道: {url}")
            dead_urls.add(url)
            continue

        print(f"  ✅ {url} → 解析到 {len(entries)} 条频道")
        all_entries.extend(entries)

    channel_map: Dict[str, List[SourceEntry]] = {}
    for entry in all_entries:
        key = _normalize_name(entry.name)
        channel_map.setdefault(key, []).append(entry)

    return channel_map, dead_urls


async def _fetch_one(session: aiohttp.ClientSession, url: str,
                     sem: asyncio.Semaphore) -> Tuple[str, Optional[str]]:
    async with sem:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    return (url, text)
                else:
                    return (url, None)
        except Exception:
            return (url, None)


def _parse_source_content(content: str) -> List[SourceEntry]:
    """
    解析 M3U 或 TXT 格式的上游源
    自动提取 group-title 并判断 VOD
    """
    entries: List[SourceEntry] = []
    lines = content.strip().splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if not line or ",#genre#" in line:
            i += 1
            continue

        # ── M3U 格式 ──
        if line.startswith("#EXTINF"):
            name = ""
            group = ""

            # 提取 tvg-name
            m_name = re.search(r'tvg-name="([^"]*)"', line)
            if m_name and m_name.group(1):
                name = m_name.group(1).strip()

            # 提取 group-title（上游源的分类信息！）
            m_group = re.search(r'group-title="([^"]*)"', line)
            if m_group and m_group.group(1):
                group = m_group.group(1).strip()

            # 如果没取到 tvg-name，用逗号后面的文字
            if not name:
                parts = line.split(",", 1)
                if len(parts) > 1:
                    name = parts[1].strip()

            # 下一行是 URL
            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    entry = SourceEntry(name=name, url=url, group=group)
                    # ★ 自动判断 VOD：用频道名 + group-title + URL 三重检测
                    entry.is_vod = _auto_detect_vod(name, group, url)
                    entries.append(entry)
            i += 1
            continue

        # ── TXT 格式：频道名,URL ──
        if "," in line and not line.startswith("#"):
            parts = line.split(",", 1)
            if len(parts) == 2:
                name = parts[0].strip()
                url = parts[1].strip()
                if name and url and (url.startswith("http") or url.startswith("rtmp")):
                    entry = SourceEntry(name=name, url=url)
                    entry.is_vod = _auto_detect_vod(name, "", url)
                    entries.append(entry)
                    i += 1
                    continue

        i += 1

    return entries


# ─────────────────────────────────────────────────────
#  频道名标准化 & 匹配 (★ 核心修复区 ★)
# ─────────────────────────────────────────────────────
def _normalize_name(name: str) -> str:
    n = name.strip()
    # 去掉所有常见特殊字符（新增 +、:、#、@、&、'、"）
    n = re.sub(r'[\s\-\_\.\|\/\\（）()\[\]【】\+\:\#\@\&\'\"]', '', n)
    # 去掉尾部质量/类型标签
    n = re.sub(r'(综合|频道|高清|超清|HD|FHD|UHD|4K|8K|标清|流畅|直播|电视)$',
               '', n, flags=re.IGNORECASE)
    return n.lower()


def find_matching_sources(
    demo_name: str,
    channel_map: Dict[str, List[SourceEntry]],
    blacklist: Set[str],
    demo_channel: Channel = None
) -> List[SourceEntry]:
    """
    根据 demo 频道名匹配上游源，自动过滤黑名单
    
    匹配规则（从严格到宽松）：
    1. 精确匹配（标准化后完全相同）
    2. 数字频道：前缀+数字完全相同（如 cctv5 只匹配 cctv5，不匹配 cctv50）
    3. 中文频道：前缀完全相同（如 湖南卫视 只匹配 湖南卫视，不匹配 湖南卫视剧场）
    """
    norm = _normalize_name(demo_name)
    matches: List[SourceEntry] = []
    seen_urls: Set[str] = set()

    # 提取 demo 名的数字和文字部分
    demo_nums = re.findall(r'\d+', norm)
    demo_alpha = re.sub(r'\d+', '', norm)  # 去掉数字后的纯文字部分

    for key, entries in channel_map.items():
        matched = False

        # ① 精确匹配
        if key == norm:
            matched = True

        # ② 模糊匹配（收紧规则，杜绝子串误匹配）
        else:
            key_nums = re.findall(r'\d+', key)
            key_alpha = re.sub(r'\d+', '', key)

            # 情况A：双方都有数字 → 文字前缀相同 + 数字完全相同
            if demo_nums and key_nums:
                if demo_alpha == key_alpha and demo_nums == key_nums:
                    matched = True

            # 情况B：双方都没数字 → 文字部分必须完全相同
            elif not demo_nums and not key_nums:
                if demo_alpha == key_alpha:
                    matched = True

            # 情况C：一方有数字一方没有 → 不匹配
            # （"cctv" 不应该匹配 "cctv1"，"湖南卫视" 不应该匹配 "湖南卫视2"）

        if matched:
            for e in entries:
                if e.url not in seen_urls:
                    seen_urls.add(e.url)
                    # 如果 demo 分类是点播，继承给匹配到的源
                    if demo_channel and demo_channel.is_vod:
                        e.is_vod = True
                    matches.append(e)

    # 黑名单过滤
    filtered = []
    for e in matches:
        name_lower = e.name.lower()
        url_lower = e.url.lower()
        if any(bw in name_lower or bw in url_lower for bw in blacklist):
            continue
        filtered.append(e)

    return filtered


# ─────────────────────────────────────────────────────
#  会话池
# ─────────────────────────────────────────────────────
class SessionPool:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=MAX_CONCURRENCY,
                ssl=False,
                ttl_dns_cache=300,
            )
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 IPTV-Checker/2.0"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


session_pool = SessionPool()


# ─────────────────────────────────────────────────────
#  上游死链标记 & 持久化
# ─────────────────────────────────────────────────────
def mark_dead_sources(
    dead_urls: set,
    path: str = SOURCES_FILE
):
    """将死链在 sources.txt 中用 # [DEAD] 标记，下次自动跳过"""
    if not dead_urls:
        return

    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    modified = False
    new_lines = []
    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#") or not stripped:
            new_lines.append(line)
            continue

        if stripped in dead_urls:
            new_lines.append(f"# [DEAD] {stripped}\n")
            modified = True
            print(f"  💀 已标记死链: {stripped}")
        else:
            new_lines.append(line)

    if modified:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"  ✅ sources.txt 已更新，死链已用 # 标记")
    else:
        print(f"  ℹ️  无需更新 sources.txt")
