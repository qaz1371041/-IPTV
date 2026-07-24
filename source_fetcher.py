"""
多源抓取器 - 从多个直播源URL并发抓取频道数据
"""
import requests
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = [
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv4.m3u",
    "https://live.fanmingming.com/tv/m3u/ipv6.m3u",
    "https://live.fanmingming.com/tv/m3u/ipv4.m3u",
]


def load_sources_from_file(filepath="config/sources.txt"):
    """从文件加载源列表，跳过 # 开头的行"""
    sources = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
    except FileNotFoundError:
        logger.warning(f"源文件不存在: {filepath}，使用默认源")
    return sources if sources else DEFAULT_SOURCES


def fetch_single_source(url, timeout=15):
    """抓取单个源"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.encoding = "utf-8"
        if resp.status_code == 200:
            return url, resp.text
        return url, None
    except Exception as e:
        logger.warning(f"抓取失败 {url}: {e}")
        return url, None


def parse_m3u_content(content):
    """解析 M3U 格式"""
    channels = []
    lines = content.strip().split("\n")
    current_name = None

    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            match = re.search(r',(.+)$', line)
            if match:
                current_name = match.group(1).strip()
        elif line and not line.startswith("#") and current_name:
            channels.append({"name": current_name, "url": line.strip()})
            current_name = None

    return channels


def parse_txt_content(content):
    """解析 TXT 格式（频道名,链接）"""
    channels = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or ",#genre#" in line:
            continue
        parts = line.split(",", 1)
        if len(parts) == 2:
            name = parts[0].strip()
            url = parts[1].strip()
            if url.startswith("http"):
                channels.append({"name": name, "url": url})
    return channels


def parse_source_content(content):
    """智能判断格式并解析"""
    if not content:
        return []
    if "#EXTM3U" in content or "#EXTINF:" in content:
        return parse_m3u_content(content)
    return parse_txt_content(content)


def fetch_all_sources(source_urls=None, max_workers=20):
    """并发抓取所有源"""
    if source_urls is None:
        source_urls = load_sources_from_file()

    all_channels = []
    logger.info(f"抓取 {len(source_urls)} 个源...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_single_source, url): url
            for url in source_urls
        }

        for future in as_completed(futures):
            url, content = future.result()
            if content:
                channels = parse_source_content(content)
                for ch in channels:
                    ch["source"] = url
                all_channels.extend(channels)
                logger.info(f"  ✓ {url} → {len(channels)} 频道")
            else:
                logger.warning(f"  ✗ {url} → 失败")

    logger.info(f"共抓取 {len(all_channels)} 条")
    return all_channels
