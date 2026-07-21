import gzip
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from utils import EPG_SOURCES_FILE, EPG_OUTPUT, get_session, logger

async def download_epg(session, url):
    """下载并解析 EPG，返回 {channel_id: [(title, start, stop), ...]}"""
    try:
        async with session.get(url, timeout=30) as resp:
            if resp.status != 200:
                return {}
            data = await resp.read()
            if url.endswith('.gz'):
                data = gzip.decompress(data)
            root = ET.fromstring(data)
            programmes = {}
            for prog in root.findall('programme'):
                ch_id = prog.get('channel')
                title = prog.findtext('title', default='')
                if not title or title in ('精彩节目', '未提供节目表'):
                    continue
                start = prog.get('start', '')
                stop = prog.get('stop', '')
                programmes.setdefault(ch_id, []).append((title, start, stop))
            return programmes
    except Exception as e:
        logger.warning(f"EPG download failed {url}: {e}")
        return {}

async def merge_epg(valid_channel_names):
    """合并 EPG，只保留在 valid_channel_names 中的频道节目"""
    if not EPG_SOURCES_FILE.exists():
        return
    urls = [line.strip() for line in open(EPG_SOURCES_FILE) if line.strip()]
    async with await get_session() as session:
        tasks = [download_epg(session, url) for url in urls]
        results = await asyncio.gather(*tasks)

    merged = {}
    for prog_dict in results:
        for ch_id, progs in prog_dict.items():
            if ch_id in valid_channel_names:
                merged.setdefault(ch_id, []).extend(progs)

    # 生成 XML
    tv = ET.Element('tv')
    for ch in valid_channel_names:
        ET.SubElement(tv, 'channel', id=ch)
    for ch, progs in merged.items():
        for title, start, stop in progs:
            elem = ET.SubElement(tv, 'programme', channel=ch, start=start, stop=stop)
            ET.SubElement(elem, 'title').text = title

    tree = ET.ElementTree(tv)
    with gzip.open(EPG_OUTPUT, 'wb') as f:
        tree.write(f, encoding='utf-8', xml_declaration=True)
    logger.info(f"EPG merged: {EPG_OUTPUT}")
