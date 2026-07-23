"""
IPTV Engine v3.0
流程: 抓取 → 模板匹配 → 过滤死链 → 过滤低画质 → 测速排序 → 输出 → EPG

特性:
- 先匹配demo.txt模板，只测需要的URL（大幅减少测速量）
- 正则别名支持 (re: 前缀)
- 连续3次不可达 → 自动注释 sources.txt
- 低画质过滤只针对直播源，播放源全部保留
- 归一化增强
- 每个频道所有可播放线路全部收录，按速度排序
- 缓存机制（24小时）
- EPG合并输出
"""

import os
import re
import gzip
import json
import time
import random
import logging
import subprocess
import shutil
import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone, timedelta

import requests
import urllib3

urllib3.disable_warnings()
logging.getLogger("urllib3").setLevel(logging.ERROR)

# ==================== 全局配置 ====================
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config")
OUTPUT = os.path.join(BASE, "output")
CACHE_DIR = os.path.join(BASE, "cache")

# 网络参数
QUICK_TIMEOUT = 8       # 阶段A: 快速连通性超时
DEEP_TIMEOUT = 12       # 阶段B: 深度验证超时
FFPROBE_TIMEOUT = 15    # ffprobe超时
QUICK_WORKERS = 80      # 阶段A并发
DEEP_WORKERS = 30       # 阶段B并发
PROBE_WORKERS = 15      # ffprobe并发
MAX_RETRIES = 1         # 重试次数
RETRY_DELAY = 1         # 重试间隔
REQUEST_JITTER = (0.05, 0.2)

# 画质参数（只针对直播源）
MIN_HEIGHT = 720
MIN_BITRATE = 1500000

# 源管理
MAX_SOURCE_FAIL = 3
CACHE_TTL = 86400
MAX_URLS_PER_CHANNEL = 5  # 每个频道最多保留线路数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Connection": "keep-alive",
}

log = logging.getLogger("iptv")
HAS_FFPROBE = shutil.which("ffprobe") is not None

EPG_SOURCES = [
    "https://live.fanmingming.com/e.xml.gz",
    "http://epg.51zmt.top:8000/e.xml.gz",
    "https://e.erw.cc/all.xml.gz",
    "https://raw.githubusercontent.com/taksssss/tv/main/epg/51zmt.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/51zmt.xml.gz",
]

# ==================== 直播源 vs 播放源 ====================
LIVE_KEYWORDS = [
    "cctv", "央视", "卫视", "频道", "tv",
    "北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
    "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
    "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
    "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海", "天津",
    "新闻", "综合", "公共", "都市", "生活", "科教", "少儿", "音乐",
    "体育", "影视", "文艺", "财经", "农业", "军事", "纪录",
]

PLAYBACK_KEYWORDS = [
    "电影", "电视剧", "综艺", "动漫", "动画", "纪录片",
    "剧场", "影院", "大片", "热播", "点播", "回放",
    "经典", "老片", "港片", "美剧", "韩剧", "日剧",
    "咪咕", "游戏", "电竞", "演唱会", "演出", "晚会",
    "音乐剧", "话剧", "歌剧", "相声", "小品",
]


def _is_live_source(name: str, group: str) -> bool:
    """判断是否为直播源（央视/卫视/地方台），播放源返回False"""
    text = (name + " " + group).lower()
    for kw in PLAYBACK_KEYWORDS:
        if kw in text:
            return False
    for kw in LIVE_KEYWORDS:
        if kw in text:
            return True
    return True  # 默认当直播源处理


# ==================== 工具函数 ====================

_NOISE_WORDS = [
    "高清", "超清", "标清", "蓝光", "原画",
    "hd", "fhd", "uhd", "4k", "8k", "sd", "h265", "h264", "hevc", "avc",
    "频道", "电视台", "卫视台", "tv",
    "（主）", "（备）", "(主)", "(备)",
    "ipv6", "ipv4", "组播", "单播",
]

_RES_SUFFIX_RE = re.compile(
    r'[\s]*[\(\[（【]?\s*'
    r'(?:'
    r'\d{3,4}\s*[pi]'
    r'|\d{3,4}\s*[x×*]\s*\d{3,4}'
    r'|uhd|fhd|qhd|hdr|sdr'
    r'|h\.?26[45]|hevc|avc'
    r'|mpeg-?[24]'
    r'|主|备|主备|备用|主线|备线'
    r'|ipv[46]'
    r')'
    r'\s*[\)\]）】]?'
    r'[\s]*$',
    re.IGNORECASE
)

_CN_NUM = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
    "零": "0", "〇": "0",
}


def _normalize_name(raw: str) -> str:
    """归一化频道名：去噪、统一格式"""
    n = raw.strip().lower()
    n = n.replace("＋", "+").replace("－", "-").replace("（", "(").replace("）", ")")

    def _cn2num(m):
        return m.group(1) + _CN_NUM.get(m.group(2), m.group(2))
    n = re.sub(r'(cctv|cctv-)([一二三四五六七八九十])', _cn2num, n)

    for _ in range(3):
        new_n = _RES_SUFFIX_RE.sub('', n).strip()
        if new_n == n:
            break
        n = new_n

    for w in _NOISE_WORDS:
        n = n.replace(w, "")

    n = re.sub(r'[\s\-_.,:;，。：；、()\[\]【】《》"\'\"\/|\\#*！!？?@&]+', '', n)
    return n.strip()


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) == 0:
        return 0.0
    if shorter in longer:
        return len(shorter) / len(longer)
    set_a, set_b = set(a), set(b)
    union = set_a | set_b
    return len(set_a & set_b) / len(union) if union else 0.0


def _is_video_data(data: bytes) -> bool:
    """判断数据是否为真实视频流"""
    if not data or len(data) < 8:
        return False
    text_start = data[:256].decode('utf-8', errors='ignore').strip().lower()
    for indicator in ['<!doctype', '<html', '<head', '<body',
                      '{"code"', '{"error"', '{"status"', '{"msg"',
                      'not found', 'forbidden', 'unauthorized', 'access denied']:
        if indicator in text_start:
            return False
    if data[0] == 0x47:
        return True
    if data[:3] == b'FLV':
        return True
    if b'ftyp' in data[:16] or b'moof' in data[:16] or b'mdat' in data[:16]:
        return True
    if len(data) >= 4096:
        printable = sum(1 for b in data[:512] if 32 <= b <= 126 or b in (9, 10, 13))
        if printable / min(len(data), 512) < 0.4:
            return True
    return False


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=50, pool_maxsize=50, max_retries=0,
    )
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s


def _is_valid_source_url(line: str) -> bool:
    line = line.strip()
    if not line or line.startswith("#"):
        return False
    if not re.match(r'^https?://', line, re.IGNORECASE):
        return False
    try:
        parsed = urlparse(line)
        if not parsed.scheme or not parsed.netloc:
            return False
    except Exception:
        return False
    return True


# ==================== 核心引擎 ====================

class Engine:

    def __init__(self):
        self.all_entries = []       # 抓取的全部条目
        self.matched_entries = []   # 模板匹配后的条目
        self.alive = []             # 存活条目
        self.classified = []        # 最终分类结果
        self._cache = self._load_cache()

    # ==================== 缓存 ====================

    def _load_cache(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_file = os.path.join(CACHE_DIR, "speedtest_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                now = time.time()
                valid = {k: v for k, v in cache.items()
                         if now - v.get("timestamp", 0) < CACHE_TTL}
                log.info("缓存加载: %d 条有效 / %d 条过期",
                         len(valid), len(cache) - len(valid))
                return valid
            except Exception as e:
                log.warning("缓存加载失败: %s", e)
        return {}

    def _save_cache(self):
        cache_file = os.path.join(CACHE_DIR, "speedtest_cache.json")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
            log.info("缓存已保存: %d 条", len(self._cache))
        except Exception as e:
            log.warning("缓存保存失败: %s", e)

    # ==================== 源失败计数 ====================

    def _load_fail_count(self) -> dict:
        path = os.path.join(CACHE_DIR, "source_fail_count.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_fail_count(self, data: dict):
        path = os.path.join(CACHE_DIR, "source_fail_count.json")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _comment_out_source(self, url: str):
        path = os.path.join(CONFIG, "sources.txt")
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        commented = False
        for line in lines:
            stripped = line.strip()
            if stripped == url and not commented:
                new_lines.append(f"# [连续{MAX_SOURCE_FAIL}次不可达，自动排除] {url}\n")
                commented = True
            else:
                new_lines.append(line)
        if commented:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            log.warning("  已永久注释: %s", url[:70])

    # ==================== 阶段1: 抓取 ====================

    def fetch(self):
        log.info("=" * 60)
        log.info("阶段1: 抓取上游源")
        log.info("=" * 60)

        alive_sources = self._load_sources()
        if not alive_sources:
            log.error("所有源均不可达！请检查 config/sources.txt")
            return

        session = _session()
        self.all_entries = []

        for url in alive_sources:
            text = self._fetch_source(url, session)
            if not text:
                continue
            if "#EXTM3U" in text[:100] or "#EXTINF" in text[:500]:
                entries = self._parse_m3u(text)
            else:
                entries = self._parse_txt(text)
            log.info("  [%s]: %d 个频道", url[:60], len(entries))
            self.all_entries.extend(entries)

        log.info("总计抓取: %d 个条目", len(self.all_entries))

        # 规则过滤
        rules = self._load_rules()
        self.all_entries = self._apply_rules(self.all_entries, rules)
        log.info("规则过滤后: %d", len(self.all_entries))

        # URL去重
        seen = set()
        unique = []
        for e in self.all_entries:
            key = (e["name"], e["url"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        self.all_entries = unique
        log.info("去重后: %d", len(self.all_entries))

    # ==================== 阶段2: 模板匹配 ====================

    def match_template(self):
        log.info("=" * 60)
        log.info("阶段2: 模板匹配 (只保留demo.txt需要的频道)")
        log.info("=" * 60)

        demo = self._load_demo()
        if not demo:
            log.warning("demo.txt 为空，保留全部频道")
            self.matched_entries = [(e["name"], e) for e in self.all_entries]
            return

        alias_map, reverse_alias, regex_patterns = self._load_alias()

        # 构建demo频道索引
        demo_names = set()
        demo_normalized = {}
        for d in demo:
            name = d["name"]
            demo_names.add(name.lower())
            norm = _normalize_name(name)
            if norm:
                demo_normalized[norm] = name

        # 构建正则匹配器（将正则映射到demo标准名）
        regex_to_demo = []
        for pattern, standard_name in regex_patterns:
            if standard_name.lower() in demo_names:
                regex_to_demo.append((pattern, standard_name))

        # 构建别名到demo名的映射
        alias_to_demo = {}
        for alias_lower, standard_name in alias_map.items():
            if standard_name.lower() in demo_names:
                alias_to_demo[alias_lower] = standard_name

        # 遍历所有条目，匹配模板
        matched = []
        match_stats = {"exact": 0, "alias": 0, "regex": 0, "normalized": 0, "skip": 0}

        for entry in self.all_entries:
            name = entry["name"]
            name_lower = name.strip().lower()
            matched_demo_name = None

            # 1. 精确匹配
            if name_lower in demo_names:
                matched_demo_name = name_lower
                match_stats["exact"] += 1

            # 2. 别名匹配
            elif name_lower in alias_to_demo:
                matched_demo_name = alias_to_demo[name_lower]
                match_stats["alias"] += 1

            # 3. 正则匹配
            else:
                for pattern, standard_name in regex_to_demo:
                    try:
                        if pattern.search(name):
                            matched_demo_name = standard_name
                            match_stats["regex"] += 1
                            break
                    except Exception:
                        continue

            # 4. 归一化匹配
            if not matched_demo_name:
                norm = _normalize_name(name)
                if norm and norm in demo_normalized:
                    matched_demo_name = demo_normalized[norm]
                    match_stats["normalized"] += 1

            if matched_demo_name:
                matched.append((matched_demo_name, entry))
            else:
                match_stats["skip"] += 1

        self.matched_entries = matched

        log.info("匹配结果:")
        log.info("  精确: %d | 别名: %d | 正则: %d | 归一化: %d",
                 match_stats["exact"], match_stats["alias"],
                 match_stats["regex"], match_stats["normalized"])
        log.info("  匹配: %d | 跳过(不需要): %d",
                 len(matched), match_stats["skip"])

        # 统计每个模板频道匹配到多少条URL
        channel_count = defaultdict(int)
        for demo_name, _ in matched:
            channel_count[demo_name] += 1
        log.info("  模板频道: %d 个, 平均每频道 %.1f 条URL",
                 len(channel_count),
                 len(matched) / max(len(channel_count), 1))

    # ==================== 阶段3: 过滤死链 ====================

    def filter_dead(self):
        if not self.matched_entries:
            log.warning("无匹配条目")
            return

        log.info("=" * 60)
        log.info("阶段3: 过滤死链 (快速连通性检测, 并发=%d, 超时=%ds)",
                 QUICK_WORKERS, QUICK_TIMEOUT)
        log.info("=" * 60)

        session = _session()
        alive = []
        dead_count = 0
        fake_count = 0
        cached_count = 0

        # URL去重（同一URL只测一次）
        url_seen = {}
        unique_entries = []
        for demo_name, entry in self.matched_entries:
            url = entry["url"]
            if url not in url_seen:
                url_seen[url] = True
                unique_entries.append((demo_name, entry))

        log.info("URL去重: %d -> %d", len(self.matched_entries), len(unique_entries))

        # 缓存分离
        to_test = []
        for demo_name, entry in unique_entries:
            url_key = _url_hash(entry["url"])
            if url_key in self._cache:
                cached = self._cache[url_key]
                if cached.get("status") == "alive":
                    alive.append((demo_name, entry))
                    cached_count += 1
                else:
                    dead_count += 1
            else:
                to_test.append((demo_name, entry))

        log.info("缓存命中: %d | 待测: %d", cached_count, len(to_test))

        # 快速连通性检测
        with ThreadPoolExecutor(max_workers=QUICK_WORKERS) as pool:
            futures = {
                pool.submit(self._quick_check, entry, session): (demo_name, entry)
                for demo_name, entry in to_test
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                demo_name, entry = futures[future]
                try:
                    status = future.result()
                except Exception:
                    status = "dead"

                url_key = _url_hash(entry["url"])
                if status == "alive":
                    alive.append((demo_name, entry))
                    self._cache[url_key] = {"status": "alive", "timestamp": time.time()}
                elif status == "fake":
                    fake_count += 1
                    self._cache[url_key] = {"status": "fake", "timestamp": time.time()}
                else:
                    dead_count += 1
                    self._cache[url_key] = {"status": "dead", "timestamp": time.time()}

                if done % 200 == 0 or done == len(to_test):
                    log.info("  进度: %d/%d  alive=%d  dead=%d  fake=%d",
                             done, len(to_test), len(alive) - cached_count,
                             dead_count, fake_count)

        log.info("死链过滤完成: alive=%d (cache=%d+new=%d) / dead=%d / fake=%d",
                 len(alive), cached_count, len(alive) - cached_count,
                 dead_count, fake_count)

        self.alive = alive
        self._save_cache()

    # ==================== 阶段4: 过滤低画质 ====================

    def filter_quality(self):
        if not self.alive:
            log.warning("无存活条目")
            return

        if not HAS_FFPROBE:
            log.info("ffprobe 未安装，跳过画质过滤")
            return

        log.info("=" * 60)
        log.info("阶段4: 画质过滤 (直播源: >=%dp/>=%.1fMbps, 播放源: 不过滤)",
                 MIN_HEIGHT, MIN_BITRATE / 1_000_000)
        log.info("=" * 60)

        verified = []
        low_res = 0
        low_bitrate = 0
        probe_fail = 0
        playback_keep = 0

        def _probe(item):
            demo_name, entry = item
            res = self._probe_resolution(entry["url"])
            return (demo_name, entry, res)

        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            futures = {pool.submit(_probe, item): item for item in self.alive}
            done = 0
            for future in as_completed(futures):
                done += 1
                demo_name, entry, res = future.result()
                url_key = _url_hash(entry["url"])
                name = entry["name"]
                group = entry.get("group", "")
                is_live = _is_live_source(name, group)

                if res:
                    if is_live:
                        # 直播源：过滤低画质
                        if res["height"] < MIN_HEIGHT:
                            low_res += 1
                            self._cache[url_key] = {"status": "low_res", "timestamp": time.time()}
                            continue
                        if res["bitrate"] > 0 and res["bitrate"] < MIN_BITRATE:
                            low_bitrate += 1
                            self._cache[url_key] = {"status": "low_bitrate", "timestamp": time.time()}
                            continue
                        verified.append((demo_name, entry, res))
                    else:
                        # 播放源：不过滤，全部保留
                        playback_keep += 1
                        verified.append((demo_name, entry, res))
                else:
                    if is_live:
                        # 直播源探测失败：保守保留（可能是ffprobe超时）
                        probe_fail += 1
                        verified.append((demo_name, entry, None))
                    else:
                        # 播放源探测失败：保留
                        playback_keep += 1
                        verified.append((demo_name, entry, None))

                if done % 100 == 0 or done == len(self.alive):
                    log.info("  探测: %d/%d  ok=%d  low_res=%d  low_br=%d  fail=%d  playback=%d",
                             done, len(self.alive), len(verified),
                             low_res, low_bitrate, probe_fail, playback_keep)

        self.alive = verified
        log.info("画质过滤完成: 保留=%d / low_res=%d / low_br=%d / probe_fail=%d / playback=%d",
                 len(verified), low_res, low_bitrate, probe_fail, playback_keep)
        self._save_cache()

    # ==================== 阶段5: 测速排序 ====================

    def speedtest(self):
        if not self.alive:
            log.warning("无存活条目")
            return

        log.info("=" * 60)
        log.info("阶段5: 测速排序 (并发=%d, 超时=%ds)", DEEP_WORKERS, DEEP_TIMEOUT)
        log.info("=" * 60)

        session = _session()
        results = []  # (demo_name, entry, speed, resolution, bitrate)

        def _speed_test(item):
            demo_name, entry, res = item
            speed = self._measure_speed(entry["url"], session)
            return (demo_name, entry, speed, res)

        with ThreadPoolExecutor(max_workers=DEEP_WORKERS) as pool:
            futures = {pool.submit(_speed_test, item): item for item in self.alive}
            done = 0
            for future in as_completed(futures):
                done += 1
                try:
                    demo_name, entry, speed, res = future.result()
                except Exception:
                    demo_name, entry, res = futures[future]
                    speed = -1

                if speed > 0:
                    results.append({
                        "demo_name": demo_name,
                        "entry": entry,
                        "speed": speed,
                        "resolution": (res["width"], res["height"]) if res else None,
                        "bitrate": res["bitrate"] if res else 0,
                    })

                if done % 100 == 0 or done == len(self.alive):
                    log.info("  测速: %d/%d  有效=%d", done, len(self.alive), len(results))

        # 按demo_name分组，每组按速度排序
        channel_urls = defaultdict(list)
        for r in results:
            channel_urls[r["demo_name"]].append(r)

        # 每个频道按速度排序，保留前N条
        classified = []
        for demo_name, urls in channel_urls.items():
            urls.sort(key=lambda x: x["speed"])
            for u in urls[:MAX_URLS_PER_CHANNEL]:
                group = u["entry"].get("group", "") or "未分类"
                classified.append((group, demo_name, u["entry"]["url"], u["speed"]))

        self.classified = classified
        log.info("测速完成: %d 个频道, %d 条线路",
                 len(channel_urls), len(classified))

        # 打印样本
        log.info("样本 (前10):")
        for group, name, url, speed in classified[:10]:
            log.info("  %s - %s (%dms)", group, name, speed)

    # ==================== 阶段6: 输出 ====================

    def write_output(self):
        log.info("=" * 60)
        log.info("阶段6: 输出文件")
        log.info("=" * 60)

        os.makedirs(OUTPUT, exist_ok=True)
        txt_path = os.path.join(OUTPUT, "iptv.txt")
        m3u_path = os.path.join(OUTPUT, "iptv.m3u")

        # 按demo.txt模板顺序输出
        demo = self._load_demo()
        demo_order = []
        if demo:
            seen_groups = []
            for d in demo:
                if d["group"] not in seen_groups:
                    seen_groups.append(d["group"])
                demo_order.append((d["group"], d["name"]))

        # 构建输出索引
        output_index = defaultdict(list)
        for group, name, url, speed in self.classified:
            output_index[(group, name)].append((url, speed))

        count = 0
        with open(txt_path, "w", encoding="utf-8") as f:
            current_group = ""
            if demo_order:
                for group, name in demo_order:
                    if group != current_group:
                        current_group = group
                        f.write(f"\n{group},#genre#\n")
                    key = (group, name)
                    if key in output_index:
                        urls = output_index[key]
                        urls.sort(key=lambda x: x[1])  # 按速度排序
                        for url, speed in urls:
                            f.write(f"{name},{url}\n")
                            count += 1
            else:
                # 无模板，按分组输出
                grouped = defaultdict(list)
                for group, name, url, speed in self.classified:
                    grouped[group].append((name, url, speed))
                for group, items in grouped.items():
                    f.write(f"\n{group},#genre#\n")
                    items.sort(key=lambda x: x[2])
                    for name, url, speed in items:
                        f.write(f"{name},{url}\n")
                        count += 1

        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            if demo_order:
                for group, name in demo_order:
                    key = (group, name)
                    if key in output_index:
                        urls = output_index[key]
                        urls.sort(key=lambda x: x[1])
                        for url, speed in urls:
                            f.write(f'#EXTINF:-1 group-title="{group}",{name}\n')
                            f.write(f"{url}\n")
            else:
                for group, name, url, speed in self.classified:
                    f.write(f'#EXTINF:-1 group-title="{group}",{name}\n')
                    f.write(f"{url}\n")

        log.info("OK %s (%d 条线路)", txt_path, count)
        log.info("OK %s", m3u_path)

    # ==================== 阶段7: EPG ====================

    def process_epg(self):
        log.info("=" * 60)
        log.info("阶段7: EPG 节目单")
        log.info("=" * 60)

        session = _session()
        all_texts = []

        for src in EPG_SOURCES:
            try:
                resp = session.get(src, timeout=30)
                resp.raise_for_status()
                data = resp.content
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
                text = data.decode("utf-8", errors="ignore")
                pg_count = len(re.findall(r'<programme ', text))
                if pg_count > 0:
                    log.info("  OK [%s]: %d 条节目", src[:60], pg_count)
                    all_texts.append(text)
                else:
                    log.warning("  WARN [%s]: 无节目数据", src[:60])
            except Exception as e:
                log.warning("  FAIL [%s]: %s", src[:60], str(e)[:80])

        if not all_texts:
            log.warning("EPG: 所有源均失败，跳过")
            return

        os.makedirs(OUTPUT, exist_ok=True)
        epg_path = os.path.join(OUTPUT, "epg.xml.gz")
        merged = '<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'
        for text in all_texts:
            channels = re.findall(r'<channel[\s\S]*?</channel>', text)
            programmes = re.findall(r'<programme[\s\S]*?</programme>', text)
            for ch in channels:
                merged += ch + "\n"
            for pg in programmes:
                merged += pg + "\n"
        merged += "</tv>\n"

        with gzip.open(epg_path, "wt", encoding="utf-8") as f:
            f.write(merged)

        total = sum(len(re.findall(r'<programme ', t)) for t in all_texts)
        log.info("OK EPG: %d 条 -> %s", total, epg_path)

    # ==================== 源加载 ====================

    def _load_sources(self) -> list:
        path = os.path.join(CONFIG, "sources.txt")
        if not os.path.exists(path):
            log.error("config/sources.txt 不存在!")
            return []

        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        fail_count = self._load_fail_count()
        session = _session()
        alive_sources = []
        new_fail_count = {}

        log.info("检测上游源可达性 (连续%d次不可达 -> 永久注释)...", MAX_SOURCE_FAIL)

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not _is_valid_source_url(stripped):
                log.warning("  无效URL格式，跳过: %s", stripped[:60])
                continue

            if self._check_source_alive(stripped, session):
                alive_sources.append(stripped)
                if stripped in fail_count:
                    log.info("  OK %s (恢复可达，重置计数)", stripped[:60])
                else:
                    log.info("  OK %s", stripped[:60])
            else:
                count = fail_count.get(stripped, 0) + 1
                new_fail_count[stripped] = count
                if count >= MAX_SOURCE_FAIL:
                    log.warning("  LOCK %s (连续%d次不可达，永久排除)",
                                stripped[:60], count)
                    self._comment_out_source(stripped)
                else:
                    log.warning("  FAIL %s (第%d/%d次不可达)",
                                stripped[:60], count, MAX_SOURCE_FAIL)

        self._save_fail_count(new_fail_count)
        log.info("源检测完成: 可达 %d 个", len(alive_sources))
        return alive_sources

    def _check_source_alive(self, url, session) -> bool:
        try:
            resp = session.get(url, timeout=QUICK_TIMEOUT, stream=True)
            if resp.status_code != 200:
                resp.close()
                return False
            content_type = resp.headers.get('Content-Type', '').lower()
            chunk = resp.raw.read(4096)
            resp.close()
            if len(chunk) < 10:
                return False
            if 'text/html' in content_type:
                text = chunk.decode('utf-8', errors='ignore').lower()
                if any(x in text for x in ['<!doctype', '<html', '404 not found',
                                            '403 forbidden', 'access denied']):
                    return False
            text = chunk.decode('utf-8', errors='ignore')
            if '#EXTM3U' in text or '#EXTINF' in text or '#EXT-X-' in text:
                return True
            if ',http' in text or ',#genre#' in text:
                return True
            if text.strip().startswith('{') or text.strip().startswith('['):
                return True
            if len(chunk) > 100:
                return True
            return False
        except Exception:
            return False

    def _fetch_source(self, url, session):
        for attempt in range(MAX_RETRIES + 1):
            try:
                time.sleep(random.uniform(*REQUEST_JITTER))
                resp = session.get(url, timeout=DEEP_TIMEOUT)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                else:
                    log.warning("  获取失败 [%s]: %s", url[:50], str(e)[:60])
        return None

    # ==================== 解析 ====================

    def _parse_m3u(self, text):
        entries = []
        lines = text.strip().split("\n")
        name = None
        group = ""
        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF:"):
                m = re.search(r',(.+)$', line)
                name = m.group(1).strip() if m else ""
                gm = re.search(r'group-title="([^"]*)"', line)
                group = gm.group(1) if gm else ""
            elif line and not line.startswith("#") and name:
                entries.append({"name": name, "url": line, "group": group})
                name = None
                group = ""
        return entries

    def _parse_txt(self, text):
        entries = []
        group = ""
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ",#genre#" in line:
                group = line.split(",")[0].strip()
                continue
            if "," in line:
                name, url = line.split(",", 1)
                entries.append({"name": name.strip(), "url": url.strip(), "group": group})
        return entries

    def _load_rules(self):
        rules = {"replace": [], "black": [], "white": []}
        path = os.path.join(CONFIG, "rules.txt")
        if not os.path.exists(path):
            return rules
        section = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].lower()
                    continue
                if section == "replace" and "->" in line:
                    old, new = line.split("->", 1)
                    rules["replace"].append((old.strip(), new.strip()))
                elif section == "black":
                    rules["black"].append(line)
                elif section == "white":
                    rules["white"].append(line)
        return rules

    def _apply_rules(self, entries, rules):
        result = []
        for e in entries:
            name, url = e["name"], e["url"]
            for old, new in rules["replace"]:
                name = name.replace(old, new)
            if any(kw in name or kw in url for kw in rules["black"]):
                continue
            e["name"] = name
            result.append(e)
        return result

    # ==================== 快速连通性检测 ====================

    def _quick_check(self, entry, session):
        """快速检测：HEAD请求或GET前几字节，不下载完整视频"""
        url = entry["url"]
        try:
            # 先尝试HEAD
            resp = session.head(url, timeout=QUICK_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                ct = resp.headers.get('Content-Type', '').lower()
                if 'text/html' in ct:
                    return "fake"
                return "alive"
            elif resp.status_code in (405, 501, 403):
                # HEAD不支持，尝试GET前几字节
                resp2 = session.get(url, timeout=QUICK_TIMEOUT, stream=True)
                if resp2.status_code == 200:
                    chunk = resp2.raw.read(1024)
                    resp2.close()
                    ct = resp2.headers.get('Content-Type', '').lower()
                    if 'text/html' in ct:
                        return "fake"
                    if len(chunk) > 0:
                        return "alive"
                else:
                    resp2.close()
                return "dead"
            else:
                return "dead"
        except requests.exceptions.Timeout:
            return "dead"
        except requests.exceptions.ConnectionError:
            return "dead"
        except Exception:
            return "dead"

    # ==================== 测速 ====================

    def _measure_speed(self, url, session):
        """测量下载速度（下载前64KB计算时间）"""
        try:
            t0 = time.time()
            resp = session.get(url, timeout=DEEP_TIMEOUT, stream=True)
            resp.raise_for_status()

            # 检查Content-Type
            ct = resp.headers.get('Content-Type', '').lower()
            if 'text/html' in ct:
                resp.close()
                return -1

            # 下载前64KB
            data = resp.raw.read(65536)
            resp.close()

            if not data or len(data) < 188:
                return -1

            # 验证是否为视频数据
            if not _is_video_data(data):
                return -1

            elapsed = time.time() - t0
            if elapsed <= 0:
                return 1
            return round(elapsed * 1000)  # 返回毫秒
        except Exception:
            return -1

    # ==================== ffprobe ====================

    def _probe_resolution(self, url):
        if not HAS_FFPROBE:
            return None
        try:
            cmd = [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                "-analyzeduration", "5000000",
                "-probesize", "5000000",
                "-i", url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            width = height = bitrate = 0
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    width = stream.get("width", 0)
                    height = stream.get("height", 0)
                    br = stream.get("bit_rate")
                    if br:
                        bitrate = int(br)
                    break
            if not bitrate:
                fmt = data.get("format", {})
                br = fmt.get("bit_rate")
                if br:
                    bitrate = int(br)
            if width and height:
                return {"width": width, "height": height, "bitrate": bitrate}
            return None
        except (subprocess.TimeoutExpired, Exception):
            return None

    # ==================== 别名加载 ====================

    def _load_alias(self):
        """
        加载 alias.txt，返回:
        - alias_map: {别名小写: 标准名}
        - reverse_alias: {标准名小写: [别名列表]}
        - regex_patterns: [(compiled_regex, 标准名), ...]
        """
        alias_map = {}
        reverse_alias = defaultdict(list)
        regex_patterns = []

        path = os.path.join(CONFIG, "alias.txt")
        if not os.path.exists(path):
            log.warning("config/alias.txt 不存在，跳过别名")
            return alias_map, reverse_alias, regex_patterns

        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                standard_name = parts[0]
                aliases = parts[1:]

                alias_map[standard_name.lower()] = standard_name
                reverse_alias[standard_name.lower()].append(standard_name)

                for alias in aliases:
                    if not alias:
                        continue
                    if alias.startswith("re:"):
                        regex_str = alias[3:]
                        try:
                            compiled = re.compile(regex_str)
                            regex_patterns.append((compiled, standard_name))
                        except re.error as e:
                            log.warning("正则编译失败 [%s]: %s", regex_str[:40], e)
                    else:
                        alias_map[alias.lower()] = standard_name
                        reverse_alias[standard_name.lower()].append(alias)

        log.info("别名加载: %d 个映射, %d 个正则, %d 个标准频道",
                 len(alias_map), len(regex_patterns), len(reverse_alias))
        return alias_map, reverse_alias, regex_patterns

    # ==================== demo.txt 加载 ====================

    def _load_demo(self):
        path = os.path.join(CONFIG, "demo.txt")
        if not os.path.exists(path):
            log.warning("config/demo.txt 不存在")
            return []

        entries = []
        group = ""
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ",#genre#" in line:
                    group = line.split(",")[0].strip()
                    continue
                if "," in line:
                    name = line.split(",")[0].strip()
                    if name:
                        entries.append({"name": name, "group": group})
        log.info("demo.txt 加载: %d 个频道模板", len(entries))
        return entries
