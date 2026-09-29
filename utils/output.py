"""
📝 output.py —— 生成 live.m3u / live.txt 输出文件
输出内容严格对齐 demo.txt 结构
"""
import os
from typing import List, Tuple

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import OUTPUT_DIR, OUTPUT_M3U, OUTPUT_TXT, CUSTOM_SOURCE_URL
from utils.config import Channel, SourceEntry


# 类型：(genre_name, [(Channel, [SourceEntry]), ...])
SectionType = Tuple[str, List[Tuple[Channel, List[SourceEntry]]]]


def generate_output(
    sections: List[SectionType],
    update_time: str
):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    _write_txt(sections, update_time)
    _write_m3u(sections, update_time)

    print(f"\n📄 输出完成:")
    print(f"   TXT → {OUTPUT_TXT}")
    print(f"   M3U → {OUTPUT_M3U}")


def _write_txt(sections: List[SectionType], update_time: str):
    lines: List[str] = []

    # 更新时间行
    lines.append("🕘️更新时间,#genre#")
    lines.append(f"{update_time},{CUSTOM_SOURCE_URL}")
    lines.append("")

    for genre_name, channels in sections:
        lines.append(f"{genre_name},#genre#")

        for ch, sources in channels:
            if sources:
                for src in sources:
                    lines.append(f"{ch.name},{src.url}")

        lines.append("")

    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_m3u(sections: List[SectionType], update_time: str):
    lines: List[str] = []
    lines.append("#EXTM3U")

    lines.append(f'#EXTINF:-1 group-title="🕘️更新时间",{update_time}')
    lines.append(CUSTOM_SOURCE_URL)

    for genre_name, channels in sections:
        for ch, sources in channels:
            if sources:
                for src in sources:
                    tvg_name = ch.name
                    group = genre_name

                    res_info = ""
                    if src.resolution:
                        res_info = f" {src.resolution}"

                    display_name = f"{ch.name}{res_info}"

                    lines.append(
                        f'#EXTINF:-1 group-title="{group}" '
                        f'tvg-name="{tvg_name}",{display_name}'
                    )
                    lines.append(src.url)

    with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def print_summary(sections: List[SectionType]):
    total_channels = 0
    total_sources = 0
    empty_channels = 0

    print("\n" + "=" * 60)
    print("📊 统计摘要")
    print("=" * 60)

    for genre_name, channels in sections:
        genre_sources = 0
        genre_empty = 0
        for ch, sources in channels:
            total_channels += 1
            if sources:
                total_sources += len(sources)
                genre_sources += len(sources)
            else:
                empty_channels += 1
                genre_empty += 1

        if genre_sources > 0:
            print(f"  {genre_name}: {genre_sources} 条可用源"
                  + (f" ({genre_empty} 频道无源)" if genre_empty else ""))

    print(f"\n  📺 总频道数: {total_channels}")
    print(f"  🔗 总可用源: {total_sources}")
    print(f"  ❌ 无源频道: {empty_channels}")
    print("=" * 60)
