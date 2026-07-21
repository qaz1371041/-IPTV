import asyncio
import json
import sys
from config.settings import STATE_FILE

async def stage_fetch():
    from utils.fetcher import fetch_all_sources
    return await fetch_all_sources()

async def stage_speedtest(raw_entries):
    from utils.speedtest import speedtest_entries
    from utils.loaders import load_blacklist, load_whitelist
    return await speedtest_entries(raw_entries, load_blacklist(), load_whitelist())

async def stage_output(valid_entries):
    from utils.categorizer import Categorizer
    from utils.output import write_output
    from utils.epg import merge_epg
    from utils.loaders import load_demo_template

    categorizer = Categorizer()
    final_channels = []
    demo_cats, _ = load_demo_template()
    for entry in valid_entries:
        raw_name = entry['raw_name']
        std_name, cat, logo = categorizer.resolve(raw_name, entry.get('source', ''))
        if cat not in demo_cats:
            cat = '其他'
        final_channels.append({
            'name': std_name,
            'url': entry['url'],
            'logo': logo or entry.get('logo', ''),
            'group': cat
        })
    write_output(final_channels)
    channel_names = {ch['name'] for ch in final_channels}
    await merge_epg(channel_names)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

async def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else 'all'
    state = load_state()

    if stage == 'fetch' or stage == 'all':
        entries = await stage_fetch()
        state['raw_count'] = len(entries)
        # 缓存 raw entries 需要序列化，这里仅保存 URL 列表用于下一阶段重新抓取？不现实。
        # 在 CI 中我们分阶段运行但传递数据不便，建议在 CI 中合并为单个阶段运行 main.py
        # 这里仅为示例，实际 CI 可以直接调用 main.py
        print(f"Fetch done: {state['raw_count']} entries")
        save_state(state)

    if stage == 'speedtest':
        # 需要 raw entries，从文件加载？简化处理，直接重新抓取
        print("Speedtest not fully implemented in CI mode, run main.py instead")
    if stage == 'output':
        print("Output not fully implemented in CI mode")

if __name__ == '__main__':
    asyncio.run(main())
