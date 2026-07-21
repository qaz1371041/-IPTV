import asyncio
from utils import logger, load_blacklist, load_whitelist
from utils.fetcher import fetch_all_sources
from utils.speedtest import speedtest_entries
from utils.categorizer import Categorizer
from utils.output import write_output
from utils.epg import merge_epg

async def main():
    # 1. 抓取所有源
    entries = await fetch_all_sources()
    if not entries:
        logger.error("No entries fetched, exiting.")
        return

    # 2. 测速过滤
    blacklist = load_blacklist()
    whitelist = load_whitelist()
    valid = await speedtest_entries(entries, blacklist, whitelist)
    if not valid:
        logger.warning("No valid streams after speedtest.")
        return

    # 3. 分类标准化（严格匹配 demo.txt）
    categorizer = Categorizer()
    final_channels = []
    for entry in valid:
        std_name, cat = categorizer.resolve(entry['raw_name'], entry.get('source', ''))
        final_channels.append({
            'name': std_name,
            'url': entry['url'],
            'logo': entry.get('logo', ''),
            'group': cat
        })

    # 4. 输出 M3U/TXT
    write_output(final_channels)

    # 5. 合并 EPG
    channel_names = {ch['name'] for ch in final_channels}
    await merge_epg(channel_names)

if __name__ == '__main__':
    asyncio.run(main())
