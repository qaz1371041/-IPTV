"""
IPTV 主入口 v3.0
流程: 抓取 → 模板匹配 → 过滤死链 → 过滤低画质 → 测速排序 → 输出 → EPG
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

# 日志配置
tz = timezone(timedelta(hours=8))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("iptv")


def main():
    start = time.time()
    log.info("=" * 60)
    log.info("IPTV Engine v3.0 启动")
    log.info("时间: %s", datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S CST"))
    log.info("=" * 60)

    from engine import Engine

    engine = Engine()

    # 阶段1: 抓取
    engine.fetch()

    # 阶段2: 模板匹配（只保留demo.txt需要的）
    engine.match_template()

    # 阶段3: 过滤死链
    engine.filter_dead()

    # 阶段4: 过滤低画质（只针对直播源）
    engine.filter_quality()

    # 阶段5: 测速排序
    engine.speedtest()

    # 阶段6: 输出
    engine.write_output()

    # 阶段7: EPG
    engine.process_epg()

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("全部完成! 耗时: %.1f 秒 (%.1f 分钟)", elapsed, elapsed / 60)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
