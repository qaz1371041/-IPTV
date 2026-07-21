import asyncio
import aiohttp
import re
import logging
from config.settings import SOURCES_FILE
from utils.config import get_session, logger

logger = logging.getLogger(__name__)

async def fetch_single_source(session, url):
    """下载单个 M3U/TXT 源，返回 [(频道名, 链接, tvg-logo, group-title)] 原始数据"""
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
    except Exception as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return []
    
    entries = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            # 解析属性
            attr_str = line[len('#EXTINF:'):].split(',', 1)
            name = attr_str[-1].strip() if len(attr_str) > 1 else ''
            tvg_name = re.search(r'tvg-name="([^"]*)"', line)
            tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)
            group_title = re.search(r'group-title="([^"]*)"', line)
            url_line = lines[i+1].strip() if i+1 < len(lines) else ''
            if url_line and not url_line.startswith('#'):
                entries.append({
                    'raw_name': name or (tvg_name.group(1) if tvg_name else ''),
                    'url': url_line,
                    'logo': tvg_logo.group(1) if tvg_logo else '',
                    'group': group_title.group(1) if group_title else '',
                    'source': url
                })
            i += 2
        else:
            i += 1
    return entries

async def fetch_all_sources():
    """抓取所有上游源，返回原始频道列表"""
    if not SOURCES_FILE.exists():
        logger.error("sources.txt not found")
        return []
    with open(SOURCES_FILE) as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    async with await get_session() as session:
        tasks = [fetch_single_source(session, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_entries = []
    for res in results:
        if isinstance(res, list):
            all_entries.extend(res)
    logger.info(f"Fetched {len(all_entries)} raw entries from {len(urls)} sources")
    return all_entries
