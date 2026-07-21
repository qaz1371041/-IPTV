import gzip
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from config.settings import EPG_SOURCES_FILE, EPG_OUTPUT
from utils.config import get_session, logger

async def download_epg(session, url):
    """下载并解析 EPG XML，返回节目列表 {channel_id: [(title, start, stop)]}"""
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

async def merge_epg(channel_names):
    """下载所有 EPG 源，筛选出在 channel_names 中的频道节目，合并写入 epg.xml.gz"""
    if not EPG_SOURCES_FILE.exists():
        return
    with open(EPG_SOURCES_FILE) as f:
        urls = [line.strip() for line in f if line.strip()]
    
    async with await get_session() as session:
        tasks = [download_epg(session, url) for url in urls]
        results = await asyncio.gather(*tasks)
    
    # 合并，这里简单按频道名去重
    merged = {}
    for prog_dict in results:
        for ch_id, progs in prog_dict.items():
            if ch_id in channel_names:
                merged.setdefault(ch_id, []).extend(progs)
    
    # 生成 XML
    tv = ET.Element('tv')
    for ch_name in channel_names:
        ET.SubElement(tv, 'channel', id=ch_name)
    for ch_name, progs in merged.items():
        for title, start, stop in progs:
            ET.SubElement(tv, 'programme', channel=ch_name, start=start, stop=stop).text = title
    
    tree = ET.ElementTree(tv)
    with gzip.open(EPG_OUTPUT, 'wb') as f:
        tree.write(f, encoding='utf-8', xml_declaration=True)
    logger.info(f"EPG merged: {EPG_OUTPUT}")
