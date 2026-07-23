"""
IPTV Engine v2.3

修复:
  1. 归一化增强：去掉 (720p)/(1080i)/[FHD] 等分辨率后缀
  2. 别名双向映射：标准名↔别名 都能匹配
  3. 反向索引：标准名→所有别名频道
  4. 匹配引擎重写：6级精准匹配
  5. 连续3次不可达 → 自动注释 sources.txt
  6. 低画质过滤只针对直播源
  7. EPG源更新
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

TIMEOUT = 15
MAX_WORKERS = 25
MAX_RETRIES = 2
RETRY_DELAY = 2
REQUEST_JITTER = (0.1, 0.5)

MIN_HEIGHT = 720
MIN_BITRATE = 1500000
FFPROBE_TIMEOUT = 15

MAX_SOURCE_FAIL = 3
CACHE_TTL = 86400
MAX_URLS_PER_CHANNEL = 3

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
    "https://e.erw.cc/all.xml.gz",
    "https://epg.51zmt.top:8000/e.xml.gz",
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
    "咪咕", "游戏", "电竞",
]


def _is_live_source(name: str, group: str) -> bool:
    text = (name + " " + group).lower()
    for kw in PLAYBACK_KEYWORDS:
        if kw in text:
            return False
    for kw in LIVE_KEYWORDS:
        if kw in text:
            return True
    return True


# ==================== 工具函数 ====================

_NOISE_WORDS = [
    "高清", "超清", "标清", "蓝光", "原画",
    "hd", "fhd", "uhd", "4k", "8k", "sd", "h265", "h264", "hevc", "avc",
    "频道", "电视台", "卫视台", "tv",
    "（主）", "（备）", "(主)", "(备)",
    "ipv6", "ipv4", "组播", "单播",
]

# ★ 新增：分辨率/编码后缀正则（用于归一化时清洗）
_RES_SUFFIX_RE = re.compile(
    r'[\s]*[\(\[（【]?\s*'
    r'(?:'
    r'\d{3,4}\s*[pi]'          # 720p, 1080i, 1080p, 2160p
    r'|\d{3,4}\s*[x×*]\s*\d{3,4}'  # 1280x720, 1920×1080
    r'|uhd|fhd|qhd|hdr|sdr'   # 编码/画质标记
    r'|h\.?26[45]|hevc|avc'   # 编码格式
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
    """
    频道名归一化（增强版）
    去掉分辨率后缀、噪声词、特殊符号
    """
    n = raw.strip().lower()
    n = n.replace("＋", "+").replace("－", "-").replace("（", "(").replace("）", ")")

    # 中文数字 → 阿拉伯数字
    def _cn2num(m):
        return m.group(1) + _CN_NUM.get(m.group(2), m.group(2))
    n = re.sub(r'(cctv|cctv-)([一二三四五六七八九十])', _cn2num, n)

    # ★ 去掉分辨率/编码后缀（可多次，处理 "CCTV-3 (720p) [FHD]" 这种情况）
    for _ in range(3):
        new_n = _RES_SUFFIX_RE.sub('', n).strip()
        if new_n == n:
            break
        n = new_n

    # 去掉噪声词
    for w in _NOISE_WORDS:
        n = n.replace(w, "")

    # 去掉所有特殊符号
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
        pool_connections=30, pool_maxsize=30, max_retries=0,
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
        self.all_entries = []
        self.alive = []
        self.classified = []
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
            log.warning("  🔒 已永久注释: %s", url[:70])

    # ==================== 阶段1: 抓取 ====================

    def fetch(self):
        log.info("=" * 50)
        log.info("阶段1: 抓取 + 源可达性检测")
        log.info("=" * 50)

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

        rules = self._load_rules()
        self.all_entries = self._apply_rules(self.all_entries, rules)
        log.info("规则过滤后: %d", len(self.all_entries))

        seen = set()
        unique = []
        for e in self.all_entries:
            key = (e["name"], e["url"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        self.all_entries = unique
        log.info("去重后: %d", len(self.all_entries))

    # ==================== 阶段2: 深度测速 ====================

    def speedtest(self):
        if not self.all_entries:
            log.warning("无条目可测速")
            return

        session = _session()
        alive = []
        dead_count = 0
        fake_count = 0
        cached_count = 0

        log.info("=" * 50)
        log.info("阶段2: 深度测速 (并发=%d, 超时=%ds, 重试=%d次)",
                 MAX_WORKERS, TIMEOUT, MAX_RETRIES)
        log.info("=" * 50)
        log.info("待测: %d | ffprobe: %s",
                 len(self.all_entries), "✅" if HAS_FFPROBE else "❌")

        to_test = []
        for e in self.all_entries:
            url_key = _url_hash(e["url"])
            if url_key in self._cache:
                cached = self._cache[url_key]
                if cached.get("status") == "alive":
                    alive.append({
                        "item": e, "speed": cached["speed"],
                        "resolution": cached.get("resolution"),
                        "bitrate": cached.get("bitrate", 0),
                    })
                    cached_count += 1
                else:
                    dead_count += 1
            else:
                to_test.append(e)

        if cached_count > 0:
            log.info("缓存命中: %d 个 (跳过测速)", cached_count)
        log.info("实际需测: %d 个", len(to_test))

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._deep_check_with_retry, e, session): e
                       for e in to_test}
            done = 0
            for future in as_completed(futures):
                done += 1
                item, speed, status = future.result()
                url_key = _url_hash(item["url"])

                if status == "alive":
                    alive.append({"item": item, "speed": speed,
                                  "resolution": None, "bitrate": 0})
                    self._cache[url_key] = {
                        "status": "alive", "speed": speed, "timestamp": time.time(),
                    }
                elif status == "fake":
                    fake_count += 1
                    self._cache[url_key] = {"status": "fake", "timestamp": time.time()}
                else:
                    dead_count += 1
                    self._cache[url_key] = {"status": "dead", "timestamp": time.time()}

                if done % 50 == 0 or done == len(to_test):
                    log.info("  进度: %d/%d  ✅%d  ❌%d  ⚠️假源%d",
                             done, len(to_test), len(alive) - cached_count,
                             dead_count, fake_count)

        log.info("测速完成: ✅%d (缓存%d+新测%d) / ❌%d / ⚠️假源%d",
                 len(alive), cached_count, len(alive) - cached_count,
                 dead_count, fake_count)

        # ffprobe 深度验证
        if HAS_FFPROBE and alive:
            log.info("-" * 50)
            log.info("ffprobe 深度验证 (直播源: ≥%dp/≥%.1fMbps, 播放源: 不过滤)",
                     MIN_HEIGHT, MIN_BITRATE / 1_000_000)
            log.info("-" * 50)

            verified = []
            low_res = 0
            low_bitrate = 0
            probe_fail = 0
            playback_skip = 0

            def _probe(entry):
                res = self._probe_resolution(entry["item"]["url"])
                return (entry, res)

            with ThreadPoolExecutor(max_workers=15) as pool:
                futures = {pool.submit(_probe, a): a for a in alive}
                done = 0
                for future in as_completed(futures):
                    done += 1
                    entry, res = future.result()
                    url_key = _url_hash(entry["item"]["url"])
                    name = entry["item"]["name"]
                    group = entry["item"].get("group", "")
                    is_live = _is_live_source(name, group)

                    if res:
                        entry["resolution"] = (res["width"], res["height"])
                        entry["bitrate"] = res["bitrate"]

                        if is_live:
                            if res["height"] < MIN_HEIGHT:
                                low_res += 1
                                self._cache[url_key]["status"] = "low_res"
                                continue
                            if res["bitrate"] > 0 and res["bitrate"] < MIN_BITRATE:
                                low_bitrate += 1
                                self._cache[url_key]["status"] = "low_bitrate"
                                continue
                        else:
                            playback_skip += 1

                        verified.append(entry)
                        self._cache[url_key]["resolution"] = [res["width"], res["height"]]
                        self._cache[url_key]["bitrate"] = res["bitrate"]
                    else:
                        if is_live:
                            probe_fail += 1
                            self._cache[url_key]["status"] = "probe_fail"
                        else:
                            playback_skip += 1
                            verified.append(entry)

                    if done % 30 == 0 or done == len(alive):
                        log.info("  探测: %d/%d  ✅%d  低分辨率%d  低码率%d  不可播放%d  播放源跳过%d",
                                 done, len(alive), len(verified),
                                 low_res, low_bitrate, probe_fail, playback_skip)

            alive = verified
            log.info("深度验证后: ✅%d / 低分辨率淘汰%d / 低码率淘汰%d / 不可播放%d / 播放源保留%d",
                     len(alive), low_res, low_bitrate, probe_fail, playback_skip)
        elif not HAS_FFPROBE:
            log.info("⚠️ ffprobe 未安装，跳过深度验证")

        alive.sort(key=lambda x: x["speed"])
        self.alive = alive
        self._save_cache()

    # ==================== 阶段3: 精准匹配 ====================

    def categorize(self):
        log.info("=" * 50)
        log.info("阶段3: 精准匹配 demo.txt 模板")
        log.info("=" * 50)

        demo = self._load_demo()
        alias_map, reverse_alias = self._load_alias()

        if self.alive:
            log.info("📊 存活频道样本 (前15):")
            for a in self.alive[:15]:
                res_str = f" {a['resolution'][0]}x{a['resolution'][1]}" if a.get("resolution") else ""
                br_str = f" {a['bitrate'] / 1_000_000:.1f}Mbps" if a.get("bitrate") else ""
                log.info("   %s (%dms%s%s)", a["item"]["name"], a["speed"], res_str, br_str)

        if not demo:
            log.warning("⚠️ demo.txt 为空，输出全部存活频道")
            result = []
            for a in self.alive:
                group = a["item"].get("group", "") or "未分类"
                result.append((group, a["item"]["name"], a["item"]["url"]))
            self.classified = result
            return

        # ★ 构建索引（使用双向别名）
        exact_index, normalized_index, keyword_index = self._build_index(
            self.alive, alias_map, reverse_alias
        )

        log.info("索引: 精确=%d, 归一化=%d, 关键词=%d",
                 len(exact_index), len(normalized_index), len(keyword_index))

        result = []
        current_group = ""
        seen_in_group = set()
        stats = {"exact": 0, "alias": 0, "normalized": 0, "keyword": 0, "miss": 0}
        misses = []

        for d in demo:
            name = d["name"]
            group = d["group"]

            if group != current_group:
                current_group = group
                seen_in_group = set()
            if name in seen_in_group:
                continue
            seen_in_group.add(name)

            url, match_type = self._match_channel(
                name, alias_map, reverse_alias,
                exact_index, normalized_index, keyword_index
            )

            if url:
                if "alias" in (match_type or ""):
                    stats["alias"] += 1
                elif match_type in stats:
                    stats[match_type] += 1
                result.append((group, name, url))
            else:
                stats["miss"] += 1
                misses.append(name)

        self.classified = result

        log.info("═══ 匹配结果 ═══")
        log.info("  ✅ 精确: %d | 别名: %d | 归一化: %d | 关键词: %d",
                 stats["exact"], stats["alias"], stats["normalized"], stats["keyword"])
        log.info("  ❌ 未匹配: %d | 总输出: %d", stats["miss"], len(result))

        if misses:
            log.warning("未匹配频道 (%d): %s", len(misses),
                        ", ".join(misses[:30]) + ("..." if len(misses) > 30 else ""))

    # ==================== 阶段4: 输出 ====================

    def write_output(self):
        log.info("=" * 50)
        log.info("阶段4: 输出文件")
        log.info("=" * 50)

        os.makedirs(OUTPUT, exist_ok=True)
        txt_path = os.path.join(OUTPUT, "iptv.txt")
        m3u_path = os.path.join(OUTPUT, "iptv.m3u")
        count = 0

        with open(txt_path, "w", encoding="utf-8") as f:
            current_group = ""
            for group, name, url in self.classified:
                if group != current_group:
                    current_group = group
                    f.write(f"\n{group},#genre#\n")
                f.write(f"{name},{url}\n")
                count += 1

        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for group, name, url in self.classified:
                f.write(f'#EXTINF:-1 group-title="{group}",{name}\n')
                f.write(f"{url}\n")

        log.info("✅ %s (%d 个频道)", txt_path, count)
        log.info("✅ %s", m3u_path)

    # ==================== 阶段5: EPG ====================

    def process_epg(self):
        log.info("=" * 50)
        log.info("阶段5: EPG 节目单")
        log.info("=" * 50)

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
                    log.info("  ✅ [%s]: %d 条节目", src[:60], pg_count)
                    all_texts.append(text)
                else:
                    log.warning("  ⚠️ [%s]: 无节目数据", src[:60])
            except Exception as e:
                log.warning("  ❌ [%s]: %s", src[:60], str(e)[:80])

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
        log.info("✅ EPG: %d 条 → %s", total, epg_path)

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

        log.info("检测上游源可达性 (连续%d次不可达 → 永久注释)...", MAX_SOURCE_FAIL)

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not _is_valid_source_url(stripped):
                log.warning("  ⚠️ 无效URL格式，跳过: %s", stripped[:60])
                continue

            if self._check_source_alive(stripped, session):
                alive_sources.append(stripped)
                if stripped in fail_count:
                    log.info("  ✅ %s (恢复可达，重置计数)", stripped[:60])
                else:
                    log.info("  ✅ %s", stripped[:60])
            else:
                count = fail_count.get(stripped, 0) + 1
                new_fail_count[stripped] = count
                if count >= MAX_SOURCE_FAIL:
                    log.warning("  🔒 %s (连续%d次不可达，永久排除)",
                                stripped[:60], count)
                    self._comment_out_source(stripped)
                    del new_fail_count[stripped]
                else:
                    log.warning("  ❌ %s (第%d/%d次不可达)",
                                stripped[:60], count, MAX_SOURCE_FAIL)

        self._save_fail_count(new_fail_count)
        log.info("源检测完成: ✅可达 %d 个", len(alive_sources))
        return alive_sources

    def _check_source_alive(self, url, session) -> bool:
        try:
            resp = session.get(url, timeout=TIMEOUT, stream=True)
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
                resp = session.get(url, timeout=TIMEOUT)
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

    # ==================== 深度测速 ====================

    def _deep_check_with_retry(self, item, session):
        for attempt in range(MAX_RETRIES + 1):
            try:
                time.sleep(random.uniform(*REQUEST_JITTER))
                result = self._deep_check_url(item, session)
                _, _, status = result
                if status in ("alive", "fake"):
                    return result
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                return result
            except Exception:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                return (item, -1, "dead")
        return (item, -1, "dead")

    def _deep_check_url(self, item, session):
        url = item["url"]
        url_lower = url.lower()
        try:
            t0 = time.time()
            if '.m3u8' in url_lower or 'm3u8' in url_lower:
                return self._check_m3u8_stream(item, url, session, t0)
            else:
                return self._check_direct_stream(item, url, session, t0)
        except requests.exceptions.Timeout:
            return (item, -1, "dead")
        except requests.exceptions.ConnectionError:
            return (item, -1, "dead")
        except Exception:
            return (item, -1, "dead")

    def _check_m3u8_stream(self, item, url, session, t0):
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        content = resp.text
        if '#EXTM3U' not in content and '#EXTINF' not in content:
            return (item, -1, "fake")
        if '#EXT-X-STREAM-INF' in content:
            sub_url = self._extract_sub_playlist(content, url)
            if not sub_url:
                return (item, -1, "fake")
            try:
                resp = session.get(sub_url, timeout=TIMEOUT)
                resp.raise_for_status()
                content = resp.text
            except Exception:
                return (item, -1, "dead")
        ts_url = self._extract_first_ts(content, url)
        if not ts_url:
            return (item, -1, "fake")
        try:
            ts_resp = session.get(ts_url, timeout=TIMEOUT, stream=True)
            ts_resp.raise_for_status()
            ts_data = ts_resp.raw.read(8192)
            ts_resp.close()
            if not ts_data or len(ts_data) < 188:
                return (item, -1, "fake")
            if not _is_video_data(ts_data):
                return (item, -1, "fake")
            speed = round((time.time() - t0) * 1000)
            return (item, speed, "alive")
        except Exception:
            return (item, -1, "dead")

    def _check_direct_stream(self, item, url, session, t0):
        resp = session.get(url, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            data = resp.raw.read(512)
            resp.close()
            if _is_video_data(data):
                speed = round((time.time() - t0) * 1000)
                return (item, speed, "alive")
            return (item, -1, "fake")
        data = resp.raw.read(65536)
        resp.close()
        if not data or len(data) < 188:
            return (item, -1, "dead")
        if not _is_video_data(data):
            return (item, -1, "fake")
        speed = round((time.time() - t0) * 1000)
        return (item, speed, "alive")

    def _extract_sub_playlist(self, content, base_url):
        lines = content.strip().split('\n')
        best_bw = -1
        best_url = None
        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF'):
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(bw_match.group(1)) if bw_match else 0
                if i + 1 < len(lines):
                    sub_path = lines[i + 1].strip()
                    if sub_path and not sub_path.startswith('#'):
                        sub_url = sub_path if sub_path.startswith('http') else urljoin(base_url, sub_path)
                        if bw >= best_bw:
                            best_bw = bw
                            best_url = sub_url
        return best_url

    def _extract_first_ts(self, content, base_url):
        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                return line if line.startswith('http') else urljoin(base_url, line)
        return None

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

    # ==================== ★ 匹配索引（重写） ====================

    def _build_index(self, alive_list, alias_map, reverse_alias):
        """
        构建多级匹配索引（双向别名版）

        alias_map:     别名 → 标准名  (如 "CCTV9" → "CCTV-9")
        reverse_alias: 标准名 → [别名列表]  (如 "CCTV-9" → ["CCTV9", "央视九套", ...])
        """
        exact_index = {}
        normalized_index = {}
        keyword_index = defaultdict(list)

        for a in alive_list:
            name = a["item"]["name"]
            url = a["item"]["url"]
            speed = a["speed"]

            # 通过别名映射到标准名
            std_name = alias_map.get(name, name)

            data = {"url": url, "speed": speed, "name": std_name, "orig_name": name}

            # ★ 精确索引：标准名 + 原始名 + 所有别名变体
            keys = set([std_name, name])
            # 如果原始名是某个标准名的别名，也把标准名加入
            if name in alias_map:
                keys.add(alias_map[name])
            # 如果标准名有反向别名，也加入
            if std_name in reverse_alias:
                for alt in reverse_alias[std_name]:
                    keys.add(alt)

            for key in keys:
                if key not in exact_index or speed < exact_index[key]["speed"]:
                    exact_index[key] = data

            # ★ 归一化索引：所有变体都归一化
            norm_keys = set()
            for key in keys:
                nk = _normalize_name(key)
                if nk:
                    norm_keys.add(nk)

            for nk in norm_keys:
                if nk not in normalized_index or speed < normalized_index[nk]["speed"]:
                    normalized_index[nk] = data

            # 关键词索引
            norm = _normalize_name(std_name)
            m = re.search(r'(cctv\d+\+?)', norm)
            if m:
                keyword_index[m.group(1)].append(data)

            m = re.search(r'([\u4e00-\u9fff]{2,4})(卫视)', norm)
            if m:
                keyword_index[m.group(1) + "卫视"].append(data)

            provinces = [
                "北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
                "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
                "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
                "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海", "天津",
            ]
            for prov in provinces:
                if prov in std_name or prov in name:
                    keyword_index[prov].append(data)

        return exact_index, normalized_index, keyword_index

    def _match_channel(self, demo_name, alias_map, reverse_alias,
                       exact_index, normalized_index, keyword_index):
        """
        6级精准匹配引擎（双向别名版）
        """
        # Level 1: 精确匹配（demo名直接在索引中）
        if demo_name in exact_index:
            return exact_index[demo_name]["url"], "exact"

        # Level 2: demo名是标准名 → 查反向别名 → 在索引中找
        if demo_name in reverse_alias:
            for alt in reverse_alias[demo_name]:
                if alt in exact_index:
                    return exact_index[alt]["url"], "alias"

        # Level 3: demo名是别名 → 映射到标准名 → 在索引中找
        aliased = alias_map.get(demo_name)
        if aliased and aliased in exact_index:
            return exact_index[aliased]["url"], "alias"

        # Level 4: 归一化匹配
        norm = _normalize_name(demo_name)
        if norm and norm in normalized_index:
            return normalized_index[norm]["url"], "normalized"

        # Level 5: 别名归一化
        if aliased:
            norm_alias = _normalize_name(aliased)
            if norm_alias and norm_alias in normalized_index:
                return normalized_index[norm_alias]["url"], "alias+normalized"

        # Level 5b: 反向别名归一化
        if demo_name in reverse_alias:
            for alt in reverse_alias[demo_name]:
                norm_alt = _normalize_name(alt)
                if norm_alt and norm_alt in normalized_index:
                    return normalized_index[norm_alt]["url"], "alias+normalized"

        # Level 6: 关键词匹配（相似度>40%）
        m = re.search(r'(cctv\d+\+?)', norm)
        if m:
            kw = m.group(1)
            if kw in keyword_index and keyword_index[kw]:
                best, best_score = None, -1
                for c in keyword_index[kw]:
                    sim = _name_similarity(norm, _normalize_name(c["name"]))
                    score = sim * 100 - c["speed"] * 0.001
                    if score > best_score:
                        best_score = score
                        best = c
                if best and best_score > 40:
                    return best["url"], "keyword"

        provinces = [
            "北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
            "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
            "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
            "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海", "天津",
        ]
        for prov in provinces:
            if prov in demo_name:
                kw = prov + "卫视" if "卫视" in demo_name else prov
                if kw in keyword_index and keyword_index[kw]:
                    best, best_score = None, -1
                    for c in keyword_index[kw]:
                        sim = _name_similarity(norm, _normalize_name(c["name"]))
                        score = sim * 100 - c["speed"] * 0.001
                        if score > best_score:
                            best_score = score
                            best = c
                    if best and best_score > 40:
                        return best["url"], "keyword"

        return None, None

    # ==================== 配置文件加载 ====================

    def _load_demo(self):
        path = os.path.join(CONFIG, "demo.txt")
        if not os.path.exists(path):
            log.warning("config/demo.txt 不存在")
            return []
        entries = []
        group = ""
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if ",#genre#" in line:
                    group = line.split(",")[0].strip()
                    continue
                if "," in line:
                    name = line.split(",", 1)[0].strip()
                else:
                    name = line.strip()
                if name:
                    entries.append({"name": name, "url": "", "group": group})
        log.info("demo.txt 加载: %d 个频道模板", len(entries))
        if entries:
            groups = list(dict.fromkeys(e["group"] for e in entries))
            log.info("  分组(%d): %s", len(groups), groups[:10])
        return entries

    def _load_alias(self):
        """
        加载 alias.txt（Guovin/iptv-api 格式）
        格式: 标准名,别名1,别名2,...

        返回:
          alias_map:     别名 → 标准名
          reverse_alias: 标准名 → [别名列表]
        """
        path = os.path.join(CONFIG, "alias.txt")
        alias_map = {}
        reverse_alias = defaultdict(list)

        if not os.path.exists(path):
            log.warning("config/alias.txt 不存在")
            return alias_map, reverse_alias

        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if len(parts) >= 2:
                    std_name = parts[0]
                    for alt in parts[1:]:
                        alias_map[alt] = std_name
                        reverse_alias[std_name].append(alt)

        log.info("alias.txt: %d 条别名映射, %d 个标准名有反向索引",
                 len(alias_map), len(reverse_alias))
        return alias_map, reverse_alias
