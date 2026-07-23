#!/usr/bin/env python3
"""
IPTV 直播源聚合引擎
核心: 模板驱动 + difflib模糊匹配 + 全量保留测速通过的源
"""

import re
import os
import sys
import time
import logging
import difflib
import requests
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("iptv")

# ============================================================
# 路径
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

DEMO_FILE = os.path.join(CONFIG_DIR, "demo.txt")
ALIAS_FILE = os.path.join(CONFIG_DIR, "alias.txt")
SOURCES_FILE = os.path.join(CONFIG_DIR, "sources.txt")
BLACKLIST_FILE = os.path.join(CONFIG_DIR, "blacklist.txt")

OUTPUT_M3U = os.path.join(OUTPUT_DIR, "iptv.m3u")
OUTPUT_TXT = os.path.join(OUTPUT_DIR, "iptv.txt")

# ============================================================
# 参数
# ============================================================
MAX_WORKERS = 30           # 并发线程
REQUEST_TIMEOUT = 15       # 抓取超时(秒)
SPEED_TIMEOUT = 8          # 测速超时(秒)
SPEED_MIN_BYTES = 1024     # 测速最少读到1KB才算存活
DIFFLIB_CUTOFF = 0.6      # 模糊匹配阈值

# ============================================================
# 归一化
# ============================================================
_NOISE = sorted([
    "高清", "超清", "标清", "蓝光", "原画", "普清", "流畅",
    "hd", "fhd", "uhd", "4k", "8k", "sd", "h265", "h264", "hevc", "avc",
    "频道", "电视台", "卫视台", "tv", "直播",
    "（主）", "（备）", "(主)", "(备)", "主备", "备用", "主线", "备线",
    "ipv6", "ipv4", "组播", "单播",
    "综合", "财经", "综艺", "中文国际", "体育", "体育赛事", "电影",
    "国防军事", "电视剧", "纪录", "科教", "戏曲", "社会与法",
    "新闻", "少儿", "音乐", "农业农村", "奥林匹克",
    "咪咕", "itv", "北联", "电信", "东联", "高码", "高码率",
    "广西", "梅州", "汝阳", "山东", "上海", "斯特", "四川",
    "太原", "天津", "影视", "浙江", "重庆",
], key=len, reverse=True)


def norm(name: str) -> str:
    """归一化频道名: 'CCTV-1综合高清' → 'cctv1'"""
    if not name:
        return ""
    s = name.strip().lower()
    s = re.sub(r'[^\w\u4e00-\u9fff+＋]', '', s)
    changed = True
    while changed:
        changed = False
        for w in _NOISE:
            if w in s:
                s = s.replace(w, '')
                changed = True
    return s.strip()


# ============================================================
# 1. 解析 demo.txt
# ============================================================
def parse_template():
    """
    返回:
      genres: OrderedDict {"央视频道": ["CCTV-1","CCTV-2",...], ...}
      names:  ["CCTV-1", "CCTV-2", ...]  扁平列表
    """
    genres = OrderedDict()
    names = []
    cur = "未分类"

    if not os.path.exists(DEMO_FILE):
        log.error("❌ demo.txt 不存在: %s", DEMO_FILE)
        sys.exit(1)

    with open(DEMO_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.endswith(',#genre#'):
                cur = line.replace(',#genre#', '').strip()
                genres.setdefault(cur, [])
                continue
            name = line.split(',')[0].strip()
            if name:
                genres.setdefault(cur, []).append(name)
                names.append(name)

    log.info("📋 demo.txt: %d 个分类, %d 个频道", len(genres), len(names))
    return genres, names


# ============================================================
# 2. 解析 alias.txt
# ============================================================
def parse_alias():
    """返回 (alias_map, regex_list)"""
    alias_map = {}       # {别名小写: 主名}
    regex_list = []      # [(compiled, 主名)]

    if not os.path.exists(ALIAS_FILE):
        log.info("alias.txt 不存在，跳过")
        return alias_map, regex_list

    with open(ALIAS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 2:
                continue
            main = parts[0]
            for a in parts[1:]:
                if not a:
                    continue
                if a.startswith('re:'):
                    try:
                        regex_list.append((re.compile(a[3:]), main))
                    except re.error:
                        pass
                else:
                    alias_map[a.strip().lower()] = main

    log.info("📖 alias: %d 个别名, %d 个正则", len(alias_map), len(regex_list))
    return alias_map, regex_list


# ============================================================
# 3. 加载黑名单
# ============================================================
def load_blacklist():
    bl = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    bl.add(line)
    if bl:
        log.info("🚫 黑名单: %d 条", len(bl))
    return bl


# ============================================================
# 4. 抓取所有源
# ============================================================
def fetch_sources():
    """返回 [(频道名, URL), ...]"""
    urls = []
    if not os.path.exists(SOURCES_FILE):
        log.error("❌ sources.txt 不存在!")
        sys.exit(1)

    with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(line)

    if not urls:
        log.error("❌ sources.txt 为空!")
        sys.exit(1)

    log.info("🌐 抓取 %d 个源...", len(urls))
    all_ch = []
    ok = fail = 0

    def _fetch(url):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
            })
            r.raise_for_status()
            r.encoding = r.apparent_encoding or 'utf-8'
            text = r.text
            if '#EXTM3U' in text[:200] or '#EXTINF' in text[:500]:
                return _parse_m3u(text)
            return _parse_txt(text)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_fetch, u): u for u in urls}
        for fut in as_completed(futs):
            try:
                entries = fut.result()
                if entries:
                    all_ch.extend(entries)
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1

    log.info("🌐 完成: 成功 %d, 失败 %d, 共 %d 条URL", ok, fail, len(all_ch))
    return all_ch


def _parse_m3u(text):
    entries = []
    name = None
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith('#EXTINF'):
            m = re.search(r',(.+)$', line)
            name = m.group(1).strip() if m else None
        elif line.startswith('#'):
            continue
        elif name and (line.startswith('http') or line.startswith('rtmp')):
            entries.append((name, line))
            name = None
    return entries


def _parse_txt(text):
    entries = []
    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#') or ',#genre#' in line:
            continue
        if ',' in line:
            parts = line.split(',', 1)
            name = parts[0].strip()
            if not name:
                continue
            for url in parts[1].split('#'):
                url = url.strip()
                if url.startswith('http') or url.startswith('rtmp'):
                    entries.append((name, url))
    return entries


# ============================================================
# 5. 模糊匹配（核心）
# ============================================================
def match(target_names, source_channels, alias_map, regex_list):
    """
    返回: [(目标频道名, URL), ...]
    匹配优先级: 精确 > 别名 > 归一化 > 正则 > difflib
    """
    log.info("🎯 匹配: %d 目标 vs %d 源URL", len(target_names), len(source_channels))

    # 索引
    t_lower = {n.strip().lower(): n for n in target_names}
    t_norm = {}
    for n in target_names:
        k = norm(n)
        if k:
            t_norm[k] = n

    # alias → target
    a2t = {}
    for alias_low, std in alias_map.items():
        std_l = std.strip().lower()
        std_n = norm(std)
        target = t_lower.get(std_l) or t_norm.get(std_n)
        if not target:
            for t in target_names:
                tl = t.strip().lower()
                tn = norm(t)
                if len(std_l) >= 3 and len(tl) >= 3 and (std_l in tl or tl in std_l):
                    target = t
                    break
                if std_n and tn and len(std_n) >= 3 and len(tn) >= 3:
                    if std_n in tn or tn in std_n:
                        target = t
                        break
        if target:
            a2t[alias_low] = target

    # regex → target
    r2t = []
    for pat, std in regex_list:
        std_l = std.strip().lower()
        std_n = norm(std)
        target = t_lower.get(std_l) or t_norm.get(std_n)
        if not target:
            for t in target_names:
                tn = norm(t)
                if std_n and tn and (std_n in tn or tn in std_n):
                    target = t
                    break
        if target:
            r2t.append((pat, target))

    # difflib 列表
    t_name_list = [n.strip() for n in target_names]
    t_norm_list = [norm(n) for n in target_names]

    log.info("   索引: %d 别名, %d 正则, %d 归一化", len(a2t), len(r2t), len(t_norm))

    # 匹配
    matched = []
    st = {"exact": 0, "alias": 0, "norm": 0, "regex": 0, "fuzzy": 0, "skip": 0}

    for src_name, url in source_channels:
        name = src_name.strip()
        nl = name.lower()
        nn = norm(name)
        target = None

        # 1 精确
        if nl in t_lower:
            target = t_lower[nl]
            st["exact"] += 1
        # 2 别名
        elif nl in a2t:
            target = a2t[nl]
            st["alias"] += 1
        # 3 归一化
        elif nn and nn in t_norm:
            target = t_norm[nn]
            st["norm"] += 1

        # 4 正则
        if not target:
            for pat, t in r2t:
                try:
                    if pat.search(name):
                        target = t
                        st["regex"] += 1
                        break
                except Exception:
                    pass

        # 5 ★ difflib ★
        if not target:
            if nn and len(nn) >= 2:
                c = difflib.get_close_matches(nn, t_norm_list, n=1, cutoff=DIFFLIB_CUTOFF)
                if c and c[0]:
                    target = t_norm.get(c[0])
                    if target:
                        st["fuzzy"] += 1
            if not target and len(name) >= 2:
                c = difflib.get_close_matches(name, t_name_list, n=1, cutoff=DIFFLIB_CUTOFF)
                if c:
                    target = c[0]
                    st["fuzzy"] += 1

        if target:
            matched.append((target, url))
        else:
            st["skip"] += 1

    log.info("🎯 结果: 精确%d 别名%d 归一化%d 正则%d 模糊%d | 跳过%d",
             st["exact"], st["alias"], st["norm"], st["regex"], st["fuzzy"], st["skip"])
    log.info("   匹配URL: %d 条", len(matched))

    # 覆盖统计
    hit = set(t for t, _ in matched)
    log.info("   覆盖频道: %d/%d", len(hit), len(target_names))
    miss = set(target_names) - hit
    if miss:
        log.warning("   ⚠️ 未匹配(%d): %s", len(miss), ", ".join(sorted(miss)[:20]))

    return matched


# ============================================================
# 6. 去重（同频道同URL只留一条）
# ============================================================
def dedup(matched):
    seen = set()
    result = []
    for target, url in matched:
        key = (target, url)
        if key not in seen:
            seen.add(key)
            result.append((target, url))
    log.info("🔄 去重: %d → %d", len(matched), len(result))
    return result


# ============================================================
# 7. 测速（全部保留通过的）
# ============================================================
def speed_test(entries):
    """
    测速，所有存活的URL全部保留，不限数量。
    返回: [(target, url, speed), ...] 按频道分组、组内按速度降序
    """
    log.info("⚡ 测速 %d 条URL (超时%ds, 最少%d bytes)...",
             len(entries), SPEED_TIMEOUT, SPEED_MIN_BYTES)

    def _test(item):
        target, url = item
        try:
            start = time.time()
            r = requests.get(url, stream=True, timeout=SPEED_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0"
            })
            # 读取数据
            data = r.raw.read(204800)  # 读200KB
            elapsed = time.time() - start
            if elapsed > 0 and len(data) >= SPEED_MIN_BYTES:
                speed = len(data) / elapsed  # bytes/s
                return (target, url, speed)
            return (target, url, 0)
        except Exception:
            return (target, url, 0)

    results = []
    done = 0
    total = len(entries)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(_test, e) for e in entries]
        for f in as_completed(futs):
            results.append(f.result())
            done += 1
            if done % 200 == 0 or done == total:
                log.info("   进度: %d/%d", done, total)

    # 过滤: 只保留 speed > 0 的（全部保留！）
    alive = [(t, u, s) for t, u, s in results if s > 0]
    dead = len(results) - len(alive)

    log.info("⚡ 测速完成: 存活 %d, 死亡 %d", len(alive), dead)

    # 按频道分组，组内按速度降序
    groups = defaultdict(list)
    for t, u, s in alive:
        groups[t].append((u, s))

    final = []
    for t, urls in groups.items():
        urls.sort(key=lambda x: x[1], reverse=True)
        for u, s in urls:
            final.append((t, u, s))

    log.info("   最终保留: %d 条URL, %d 个频道", len(final), len(groups))
    return final


# ============================================================
# 8. 输出
# ============================================================
def output(entries, genres):
    """生成 M3U + TXT，按 demo.txt 分类顺序"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 按频道聚合URL
    ch_urls = OrderedDict()
    for target, url, speed in entries:
        ch_urls.setdefault(target, []).append(url)

    # 频道→分类
    ch_genre = {}
    for g, names in genres.items():
        for n in names:
            ch_genre[n] = g

    # M3U
    m3u = ["#EXTM3U"]
    for g, names in genres.items():
        for n in names:
            if n in ch_urls:
                for url in ch_urls[n]:
                    m3u.append(f'#EXTINF:-1 tvg-name="{n}" group-title="{g}",{n}')
                    m3u.append(url)

    with open(OUTPUT_M3U, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u))

    # TXT
    txt = []
    for g, names in genres.items():
        has = [n for n in names if n in ch_urls]
        if not has:
            continue
        txt.append(f"{g},#genre#")
        for n in has:
            txt.append(f"{n},{'#'.join(ch_urls[n])}")

    with open(OUTPUT_TXT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt))

    log.info("📁 输出:")
    log.info("   %s (%d 行)", OUTPUT_M3U, len(m3u))
    log.info("   %s (%d 行)", OUTPUT_TXT, len(txt))
    log.info("   频道: %d, URL: %d", len(ch_urls), len(entries))


# ============================================================
# 主流程
# ============================================================
def main():
    log.info("=" * 60)
    log.info("🚀 IPTV 聚合引擎启动")
    log.info("   所有测速通过的源全部保留")
    log.info("=" * 60)
    t0 = time.time()

    # 1. 解析模板
    genres, target_names = parse_template()

    # 2. 解析别名
    alias_map, regex_list = parse_alias()

    # 3. 加载黑名单
    blacklist = load_blacklist()

    # 4. 抓取
    source_channels = fetch_sources()
    if not source_channels:
        log.error("❌ 无数据，退出")
        sys.exit(1)

    # 过滤黑名单
    if blacklist:
        before = len(source_channels)
        source_channels = [(n, u) for n, u in source_channels if u not in blacklist]
        log.info("🚫 黑名单过滤: %d → %d", before, len(source_channels))

    # 5. 模糊匹配
    matched = match(target_names, source_channels, alias_map, regex_list)
    if not matched:
        log.error("❌ 无匹配，退出")
        sys.exit(1)

    # 6. 去重
    deduped = dedup(matched)

    # 7. 测速（全部保留通过的）
    final = speed_test(deduped)
    if not final:
        log.error("❌ 测速后无存活URL，退出")
        sys.exit(1)

    # 8. 输出
    output(final, genres)

    log.info("=" * 60)
    log.info("✅ 完成! 耗时 %.1f 秒", time.time() - t0)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
