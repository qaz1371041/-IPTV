import asyncio
import re
import aiohttp
from utils import SOURCES_FILE, get_session, logger

async def fetch_single(session, url):
    """下载单个 M3U/TXT 源，返回原始频道列表"""
    entries = []
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
    except Exception as e:
        logger.debug(f"Fetch failed {url}: {e}")
        return []

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            # 解析属性
            name = ''
           if ',' in line:         # ✅ 正确
                name = line.split(',')[-1].strip()
            tvg_name = re.search(r'tvg-name="([^"]*)"', line)
            tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)
            group_title = re.search(r'group-title="([^"]*)"', line)

            if i + 1 < len(lines):
                url_line = lines[i+1].strip()
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
    """抓取所有上游源"""
    if not SOURCES_FILE.exists():
        logger.error("sources.txt not found")
        return []
    urls = [line.strip() for line in open(SOURCES_FILE) if line.strip() and not line.startswith('#')]
    async with await get_session() as session:
        tasks = [fetch_single(session, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_entries = []
    for res in results:
        if isinstance(res, list):
            all_entries.extend(res)
    logger.info(f"Fetched {len(all_entries)} raw channels from {len(urls)} sources")
    return all_entries
