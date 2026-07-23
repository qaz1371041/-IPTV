#!/usr/bin/env python3
"""IPTV 列表生成器 - 入口"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta

os.environ.setdefault('TZ', 'Asia/Shanghai')

CST = timezone(timedelta(hours=8))


class CSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=CST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(CSTFormatter('%(asctime)s [%(levelname)s] %(message)s'))

log = logging.getLogger("iptv")
log.addHandler(handler)
log.setLevel(logging.INFO)


def main():
    from engine import Engine

    log.info("=" * 60)
    log.info("  IPTV 列表生成器 v2.1")
    log.info("  时间: %s", datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S CST'))
    log.info("=" * 60)

    engine = Engine()
    engine.fetch()
    engine.speedtest()
    engine.categorize()
    engine.write_output()
    engine.process_epg()

    log.info("=" * 60)
    log.info("  全部完成！")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
