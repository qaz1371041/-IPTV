#!/usr/bin/env python3
"""
🚀 main.py —— IPTV 直播源自动聚合、测速、过滤、排序
"""
import asyncio
import time
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import OUTPUT_DIR, STRICT_PROBE, CUSTOM_SOURCE_URL
from utils.config import (
    parse_demo, load_blacklist, load_source_urls,
    fetch_all_sources, find_matching_sources,
    mark_dead_sources,
    session_pool, SourceEntry, Channel
)
from utils.speedtest import speedtest_all
from utils.output import generate_output, print_summary


async def main():
    start_time = time.monotonic()
    now = datetime.now()
    update_time = now.strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 60)
    print(f"🚀 IPTV 自动更新开始 | {update_time}")
    print("=" * 60)

    # ── 步骤 1：解析 demo.txt 模板 ──
    print("\n📋 步骤 1/5：解析频道模板 (demo.txt)...")
    demo_sections = parse_demo()
    total_demo_channels = sum(len(chs) for _, chs in demo_sections)
    vod_count = sum(
        sum(1 for ch in chs if ch.is_vod)
        for _, chs in demo_sections
    )
    print(f"   ✅ 加载 {len(demo_sections)} 个分类, {total_demo_channels} 个频道")
    print(f"   🎬 自动识别点播频道: {vod_count} 个")

    # ── 步骤 2：加载黑名单 ──
    print("\n🚫 步骤 2/5：加载黑名单...")
    blacklist = load_blacklist()
    print(f"   ✅ 黑名单关键词: {len(blacklist)} 条")

    # ── 步骤 3：拉取上游源 ──
    print("\n🌐 步骤 3/5：拉取上游直播源...")
    source_urls = load_source_urls()

    # ✅ 追加自定义源
    if CUSTOM_SOURCE_URL and CUSTOM_SOURCE_URL not in source_urls:
        source_urls.append(CUSTOM_SOURCE_URL)
        print(f"   ➕ 追加自定义源: {CUSTOM_SOURCE_URL}")

    print(f"   📡 活跃上游源数量: {len(source_urls)} 个")

    channel_map, dead_urls = await fetch_all_sources(source_urls)
    total_raw = sum(len(v) for v in channel_map.values())
    print(f"   ✅ 解析到 {len(channel_map)} 个不同频道, 共 {total_raw} 条源")

    # ── 步骤 3.5：标记死链 ──
    if dead_urls:
        print(f"\n💀 步骤 3.5/5：标记 {len(dead_urls)} 条上游死链...")
        mark_dead_sources(dead_urls)
    else:
        print(f"\n✅ 步骤 3.5/5：无上游死链，跳过标记")

    # ── 步骤 4：匹配 + 测速 ──
    print("\n⚡ 步骤 4/5：频道匹配 + 并发测速 + 分辨率探测...")
    mode_str = "严格" if STRICT_PROBE else "宽松"
    print(f"   🔧 并发: 50 | 测速: 1MB | 最低速度: 50KB/s")
    print(f"   📐 分辨率: ≥720p | ffprobe 超时: 15s | 模式: {mode_str}")
    print(f"   ⚠️  严格模式: ffprobe 探测失败 = 直接丢弃（杜绝假源）")

    # 收集所有需要测速的源
    all_test_map = {}
    vod_urls = set()

    for genre_name, channels in demo_sections:
        for ch in channels:
            matched = find_matching_sources(
                ch.name, channel_map, blacklist, demo_channel=ch
            )
            for src in matched:
                if src.url not in all_test_map:
                    all_test_map[src.url] = src
                if src.is_vod or ch.is_vod:
                    vod_urls.add(src.url)

    live_sources = [s for url, s in all_test_map.items() if url not in vod_urls]
    vod_sources = [s for url, s in all_test_map.items() if url in vod_urls]

    total_before = len(live_sources) + len(vod_sources)

    print(f"\n   📺 直播源测速: {len(live_sources)} 条（含 ffprobe 分辨率探测）...")
    live_passed = []
    if live_sources:
        live_passed = await speedtest_all(live_sources, is_vod=False)
    print(f"      → 通过: {len(live_passed)}/{len(live_sources)} 条")

    print(f"   🎬 点播源测速: {len(vod_sources)} 条（跳过分辨率探测）...")
    vod_passed = []
    if vod_sources:
        vod_passed = await speedtest_all(vod_sources, is_vod=True)
    print(f"      → 通过: {len(vod_passed)}/{len(vod_sources)} 条")

    total_after = len(live_passed) + len(vod_passed)
    print(f"\n   📊 测速总结: {total_before} 条 → {total_after} 条通过"
          f"（淘汰 {total_before - total_after} 条假源/死源/低速源）")

    # ── 组装最终输出 ──
    final_sections = []
    for genre_name, channels in demo_sections:
        genre_output = []
        for ch in channels:
            matched = find_matching_sources(
                ch.name, channel_map, blacklist, demo_channel=ch
            )
            passed = [s for s in matched if s.speed > 0]
            passed.sort(key=lambda x: x.speed, reverse=True)
            genre_output.append((ch, passed))
        final_sections.append((genre_name, genre_output))

    # ── 步骤 5：输出 ──
    print(f"\n📝 步骤 5/5：生成输出文件...")
    generate_output(final_sections, update_time)
    print_summary(final_sections)

    await session_pool.close()

    elapsed = time.monotonic() - start_time
    print(f"\n⏱️  总耗时: {elapsed:.1f} 秒")
    print("🎉 完成！")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
