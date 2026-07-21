import asyncio
import sys
from utils import logger, load_blacklist, load_whitelist, load_demo_template
from utils.fetcher import fetch_all_sources
from utils.speedtest import speedtest_entries
from utils.categorizer import Categorizer
from utils.output import write_output
from utils.epg import merge_epg

async def main():
    print("=" * 60)
    print("📡 诊断模式：开始排查 IPTV 流水线")
    
    # 1. 抓取
    print("\n➡️ 1. 正在抓取上游源...")
    entries = await fetch_all_sources()
    print(f"   抓取到 {len(entries)} 个原始频道")
    if not entries:
        print("❌ 抓取失败：请检查 config/sources.txt 是否有有效链接，且链接能直接返回文本")
        return

    # 2. 测速
    print("\n➡️ 2. 开始测速过滤...")
    blacklist = load_blacklist()
    whitelist = load_whitelist()
    valid = await speedtest_entries(entries, blacklist, whitelist)
    print(f"   测速通过 {len(valid)} 个频道")
    if not valid:
        print("❌ 测速全部未通过：可能所有链接失效或速度低于阈值，可临时降低 MIN_BANDWIDTH_KBPS 测试")
        return

    # 3. 打印前几个测速通过的频道名，方便对照
    print("\n   测速通过的频道示例（前5个）：")
    for ch in valid[:5]:
        print(f"   - 原始名: {ch['raw_name']} | URL: {ch['url'][:60]}... | 来源: {ch.get('source','')}")

    # 4. 分类匹配
    print("\n➡️ 3. 分类匹配（对照 demo.txt）...")
    categorizer = Categorizer()
    final_channels = []
    matched = 0
    unmatched = []
    for entry in valid:
        raw = entry['raw_name']
        std_name, cat = categorizer.resolve(raw, entry.get('source', ''))
        # 记录是否匹配 demo 中的频道
        in_demo = std_name in categorizer.demo_ch2cat
        if in_demo:
            matched += 1
        else:
            unmatched.append((raw, std_name, cat))
        final_channels.append({
            'name': std_name,
            'url': entry['url'],
            'logo': entry.get('logo', ''),
            'group': cat
        })

    print(f"   成功匹配 demo.txt 的频道数: {matched}")
    print(f"   未匹配到 demo.txt 的频道数: {len(unmatched)}")
    if unmatched:
        print("   前几个未匹配频道（原始名 → 标准化名 → 分类）：")
        for raw, std, cat in unmatched[:10]:
            print(f"   - {raw} → {std} → {cat}")

    # 5. 输出
    print("\n➡️ 4. 生成输出...")
    write_output(final_channels)  # 这里默认只输出 demo 中存在的，可临时开启诊断输出
    print("   输出完成，请检查 output/live.m3u 文件")

    # 6. EPG
    print("\n➡️ 5. 合并 EPG...")
    channel_names = {ch['name'] for ch in final_channels}
    await merge_epg(channel_names)
    print("   完成")

    print("\n✅ 诊断完成，根据以上数字可定位问题。")

if __name__ == '__main__':
    asyncio.run(main())
