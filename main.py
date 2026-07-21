import asyncio
import sys
from utils.config import logger
from utils.fetcher import fetch_all_sources
from utils.speedtest import speedtest_entries
from utils.loaders import load_blacklist, load_whitelist, load_demo_template
from utils.categorizer import Categorizer
from utils.output import write_output
from utils.epg import merge_epg
from config.settings import AI_ENABLED

async def main():
    # 1. 抓取上游源
    raw_entries = await fetch_all_sources()
    if not raw_entries:
        logger.error("No entries fetched")
        return

    # 2. 测速过滤
    blacklist = load_blacklist()
    whitelist = load_whitelist()
    valid_entries = await speedtest_entries(raw_entries, blacklist, whitelist)
    if not valid_entries:
        logger.warning("No valid streams after speedtest")
        return

    # 3. 分类标准化
    categorizer = Categorizer()
    final_channels = []
    demo_cats, demo_ch2cat = load_demo_template()
    for entry in valid_entries:
        raw_name = entry['raw_name']
        std_name, cat, logo = categorizer.resolve(raw_name, entry.get('source', ''))
        # 如果分类不在 demo 中，统一归为“其他”
        if cat not in demo_cats:
            cat = '其他'
        final_channels.append({
            'name': std_name,
            'url': entry['url'],
            'logo': logo or entry.get('logo', ''),
            'group': cat
        })

    # 4. 输出 M3U/TXT
    write_output(final_channels)

    # 5. EPG 合并
    channel_names = {ch['name'] for ch in final_channels}
    await merge_epg(channel_names)

if __name__ == '__main__':
    asyncio.run(main())
