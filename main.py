#!/usr/bin/env python3
"""IPTV 纯净直播源 - 入口"""
import argparse, time, logging, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["fetch","speed","output","all"], default="all")
    args = parser.parse_args()
    t0 = time.time()
    e = Engine()

    if args.stage in ("fetch", "all"):
        e.fetch()
    if args.stage in ("speed", "all"):
        e.speedtest()
    if args.stage in ("output", "all"):
        e.categorize()
        e.write_output()
        e.process_epg()

    logger.info(f"✅ 完成 {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
