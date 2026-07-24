"""
-IPTV 主程序
模板驱动 + 模糊匹配 + 测速 + 分辨率过滤 + 失效源标记
"""
import logging
import sys
import time
from datetime import datetime

from config import *
from template_parser import parse_template
from source_fetcher import fetch_all_sources, load_sources_from_file
from fuzzy_matcher import match_channels
from speed_tester import speed_test_channels
from resolution_filter import filter_by_resolution
from output_generator import generate_output


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8", mode="w")
        ]
    )


def main():
    setup_logging()
    logger = logging.getLogger("main")

    start_time = time.time()
    logger.info("=" * 60)
    logger.info(f"🚀 -IPTV 启动 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ===== 第一步：解析模板 =====
    logger.info("\n📋 [1/6] 解析频道模板...")
    try:
        template = parse_template(TEMPLATE_FILE)
    except FileNotFoundError as e:
        logger.error(f"❌ {e}")
        sys.exit(1)

    total_channels = sum(len(v) for v in template.values())
    logger.info(f"  → {len(template)} 个分类, {total_channels} 个频道")

    # ===== 第二步：抓取上游源 =====
    logger.info("\n🌐 [2/6] 抓取上游直播源...")
    source_urls = load_sources_from_file(SOURCES_FILE)
    all_channels = fetch_all_sources(source_urls, max_workers=MAX_FETCH_WORKERS)

    if not all_channels:
        logger.error("❌ 未抓取到任何频道数据")
        sys.exit(1)

    logger.info(f"  → 共抓取 {len(all_channels)} 条频道数据")

    # ===== 第三步：模糊匹配 =====
    logger.info("\n🎯 [3/6] 模糊匹配频道...")
    matched_result = match_channels(
        template,
        all_channels,
        cutoff=MATCH_CUTOFF,
        max_matches_per_channel=MAX_MATCHES_PER_CHANNEL
    )

    matched_count = sum(
        1 for cat in matched_result.values()
        for ch_list in cat.values()
        if ch_list
    )
    logger.info(f"  → 匹配成功: {matched_count}/{total_channels}")

    # ===== 第四步：测速 =====
    logger.info("\n⚡ [4/6] 测速验证...")
    speed_results = speed_test_channels(
        matched_result,
        max_workers=SPEED_TEST_WORKERS,
        use_ffprobe=USE_FFPROBE
    )

    # ===== 第五步：分辨率过滤 =====
    logger.info(f"\n🔍 [5/6] 过滤低于 {MIN_RESOLUTION_HEIGHT}p 的源...")
    filtered_results = filter_by_resolution(
        speed_results,
        min_height=MIN_RESOLUTION_HEIGHT
    )

    # ===== 第六步：生成输出 =====
    logger.info("\n📝 [6/6] 生成输出文件...")
    m3u_path, txt_path = generate_output(
        filtered_results,
        template,
        output_dir=OUTPUT_DIR,
        sources_file=SOURCES_FILE
    )

    # ===== 完成 =====
    elapsed = round(time.time() - start_time, 2)
    logger.info("\n" + "=" * 60)
    logger.info(f"✅ 完成! 耗时: {elapsed}s")
    logger.info(f"  📄 M3U: {m3u_path}")
    logger.info(f"  📄 TXT: {txt_path}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
