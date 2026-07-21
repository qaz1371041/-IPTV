import asyncio
import re
import aiohttp
from utils import SOURCES_FILE, get_session, logger

async def fetch_single(session, url):
    """下载单个 M3U 或 TXT 源，返回原始频道列表"""
    entries = []
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()
    except Exception as e:
        logger.debug(f"Fetch failed {url}: {e}")
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    # 自动判断格式：如果第一行是 #EXTM3U 或前几行含有 #EXTINF，则按 M3U 处理
    is_m3u = lines[0].startswith('#EXTM3U') or any('#EXTINF' in l for l in lines[:5])

    if is_m3u:
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('#EXTINF:'):
                # 解析 M3U 扩展信息
                name = ''
                if ',' in line:
                    name = line.split(',')[-1].strip()
                tvg_name = re.search(r'tvg-name="([^"]*)"', line)
                tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)
                group_title = re.search(r'group-title="([^"]*)"', line)

                if i + 1 < len(lines):
                    url_line = lines[i+1]
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
    else:
        # 纯 TXT 格式：每行 "频道名,URL" 或 "频道名,URL,台标,分组"
        for line in lines:
            if ',' in line:
                parts = line.split(',')
                ch_name = parts[0].strip()
                stream_url = parts[1].strip() if len(parts) > 1 else ''
                logo = parts[2].strip() if len(parts) > 2 else ''
                group = parts[3].strip() if len(parts) > 3 else ''
                if stream_url and not stream_url.startswith('#'):
                    entries.append({
                        'raw_name': ch_name,
                        'url': stream_url,
                        'logo': logo,
                        'group': group,
                        'source': url
                    })
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
