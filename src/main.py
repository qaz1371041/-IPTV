#!/usr/bin/env python3
import sys, os, configparser
from collections import OrderedDict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetch import fetch_all_sources
from src.match import match_template
from src.speedtest import batch_speedtest
from src.output import generate_output

def log(msg):
    from datetime import datetime, timezone, timedelta
    t = datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M:%S')
    print(f'[{t}] {msg}')

def main():
    log("=" * 60)
    log("🚀 IPTV 直播源自动更新 启动")
    log("=" * 60)

    cfg = configparser.ConfigParser()
    cfg.read('config/config.ini', encoding='utf-8')

    template_file  = cfg.get('Settings', 'template_file', fallback='demo.txt')
    sources_file   = cfg.get('Settings', 'sources_file', fallback='config/sources.txt')
    output_dir     = cfg.get('Settings', 'output_dir', fallback='output')
    
    timeout        = cfg.getint('SpeedTest', 'timeout', fallback=8)
    max_src        = cfg.getint('SpeedTest', 'max_sources_per_channel', fallback=15)
    threads        = cfg.getint('SpeedTest', 'threads', fallback=15)
    min_speed      = cfg.getint('SpeedTest', 'min_speed', fallback=50)
    min_resolution = cfg.getint('Filter', 'min_resolution', fallback=720)
    max_keep       = cfg.getint('Filter', 'max_keep_per_channel', fallback=5)

    log("\n📋 Step1: 解析模板 demo.txt (自动合并同名分类与去重)")
    template = parse_template(template_file)
    total = sum(len(chs) for _, chs in template)
    log(f"   {len(template)} 个分类, {total} 个去重后的频道")

    log("\n📥 Step2: 抓取上游源 (失效源将标记#到 config/sources.txt)")
    all_channels = fetch_all_sources(sources_file)
    log(f"   总计 {len(all_channels)} 条原始记录")

    log("\n🔍 Step3: 模板智能匹配")
    matched = match_template(template, all_channels)

    log("\n⚡ Step4: 并发测速")
    results = batch_speedtest(matched, max_src, timeout, threads)

    log("\n📝 Step5: 生成纯净输出")
    generate_output(results, output_dir, min_speed, min_resolution, max_keep)

    log("\n✅ 全部完成!")

def parse_template(filepath):
    categories_dict = OrderedDict()
    cur_cat = "未分类"
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            if line.endswith(',#genre#'):
                cur_cat = line.replace(',#genre#', '').strip()
                if cur_cat not in categories_dict:
                    categories_dict[cur_cat] = []
            else:
                if cur_cat not in categories_dict:
                    categories_dict[cur_cat] = []
                if line not in categories_dict[cur_cat]:
                    categories_dict[cur_cat].append(line)
    return list(categories_dict.items())

if __name__ == '__main__':
    main()
