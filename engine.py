import os
import re
import io
import gzip
import json
import time
import logging
import subprocess
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import requests
import urllib3

urllib3.disable_warnings()
logging.getLogger("urllib3").setLevel(logging.ERROR)

# ==================== 全局配置 ====================
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config")
OUTPUT = os.path.join(BASE, "output")

TIMEOUT = 10         # 测速超时(秒)
MAX_WORKERS = 60     # 并发线程数（降低避免被限速）
MIN_HEIGHT = 720     # 最低分辨率
FFPROBE_TIMEOUT = 12 # ffprobe 超时(秒)

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
    "https://gitee.com/taksssss/tv/raw/main/epg/112114.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/51zmt.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/old.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/e.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/e2.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/e3.xml.gz",
]

_NOISE_WORDS = [
    "高清", "超清", "标清", "蓝光", "原画",
    "hd", "fhd", "uhd", "4k", "8k", "sd", "h265", "h264", "hevc", "avc",
    "频道", "电视台", "卫视台", "tv",
    "（主）", "（备）", "(主)", "(备)",
    "ipv6", "ipv4", "组播", "单播",
]

_CN_NUM = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
    "零": "0", "〇": "0",
}


# ==================== 工具函数 ====================

def _normalize_name(raw: str) -> str:
    """将频道名归一化，用于模糊匹配"""
    n = raw.strip().lower()
    n = n.replace("＋", "+").replace("－", "-").replace("（", "(").replace("）", ")")

    def _cn2num(m):
        return m.group(1) + _CN_NUM.get(m.group(2), m.group(2))

    n = re.sub(r'(cctv|cctv-)([一二三四五六七八九十])', _cn2num, n)

    for w in _NOISE_WORDS:
        n = n.replace(w, "")

    n = re.sub(r'[\s\-_.,:;，。：；、()\[\]【】《》"\'\"\/|\\#*！!？?@&]+', '', n)
    return n.strip()


def _name_similarity(a: str, b: str) -> float:
    """计算两个频道名的相似度 (0.0~1.0)"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) == 0:
        return 0.0
    # 检查较短的是否是较长的子串
    if shorter in longer:
        return len(shorter) / len(longer)
    # 计算字符交集相似度
    set_a = set(a)
    set_b = set(b)
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _is_video_data(data: bytes) -> bool:
    """检查数据是否是真实视频流（而非 HTML 错误页等）"""
    if not data or len(data) < 8:
        return False

    # 检查是否是 HTML（错误页面）
    text_start = data[:64].decode('utf-8', errors='ignore').strip().lower()
    html_indicators = ['<!doctype', '<html', '<head', '<body', '<!DOCTYPE',
                       '{"code"', '{"error"', '{"status"', '403', '404',
                       'not found', 'forbidden', 'unauthorized']
    for indicator in html_indicators:
        if indicator.lower() in text_start:
            return False

    # MPEG-TS 流: 第一个字节是 0x47 (sync byte)
    if data[0] == 0x47:
        return True

    # FLV 流: 前 3 字节是 "FLV"
    if data[:3] == b'FLV':
        return True

    # MP4/fMP4: 前 4-8 字节包含 "ftyp" 或 "moof" 或 "mdat"
    if b'ftyp' in data[:12] or b'moof' in data[:12] or b'mdat' in data[:12]:
        return True

    # 如果数据量足够大且不是纯文本，也认为是视频
    if len(data) >= 4096:
        # 检查是否大部分是二进制数据（非可打印字符）
        printable = sum(1 for b in data[:256] if 32 <= b <= 126 or b in (9, 10, 13))
        if printable / min(len(data), 256) < 0.5:
            return True

    return False


def _build_index(alive_list, alias_map):
    """构建多级匹配索引"""
    exact_index = {}
    normalized_index = {}
    keyword_index = defaultdict(list)

    for a in alive_list:
        name = a["item"]["name"]
        name = alias_map.get(name, name)
        url = a["item"]["url"]
        speed = a["speed"]
        data = {"url": url, "speed": speed, "name": name}

        # 精确索引
        if name not in exact_index or speed < exact_index[name]["speed"]:
            exact_index[name] = data

        # 归一化索引
        norm = _normalize_name(name)
        if norm and (norm not in normalized_index or speed < normalized_index[norm]["speed"]):
            normalized_index[norm] = data

        # 关键词索引
        m = re.search(r'(cctv\d+\+?)', norm)
        if m:
            keyword_index[m.group(1)].append(data)

        m = re.search(r'([\u4e00-\u9fff]{2,4})(卫视|tv)', norm)
        if m:
            keyword_index[m.group(1) + "卫视"].append(data)

        provinces = [
            "北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
            "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
            "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
            "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海", "天津",
        ]
        for prov in provinces:
            if prov in name:
                keyword_index[prov].append(data)

    return exact_index, normalized_index, keyword_index


def _match_channel(demo_name, alias_map, exact_index, normalized_index, keyword_index):
    """6级匹配引擎（去掉了危险的模糊子串匹配）"""
    # Level 1: 精确匹配
    if demo_name in exact_index:
        return exact_index[demo_name]["url"], "exact"

    # Level 2: 别名精确匹配
    aliased = alias_map.get(demo_name)
    if aliased and aliased in exact_index:
        return exact_index[aliased]["url"], "alias"

    # Level 3: 归一化匹配
    norm = _normalize_name(demo_name)
    if norm and norm in normalized_index:
        return normalized_index[norm]["url"], "normalized"

    # Level 4: 别名归一化匹配
    if aliased:
        norm_alias = _normalize_name(aliased)
        if norm_alias and norm_alias in normalized_index:
            return normalized_index[norm_alias]["url"], "alias+normalized"

    # Level 5: CCTV关键词匹配（要求名字相似度高）
    m = re.search(r'(cctv\d+\+?)', norm)
    if m:
        kw = m.group(1)
        if kw in keyword_index and keyword_index[kw]:
            # 选速度最快且名字最相似的
            candidates = keyword_index[kw]
            best = None
            best_score = -1
            for c in candidates:
                sim = _name_similarity(norm, _normalize_name(c["name"]))
                score = sim * 100 - c["speed"] * 0.01
                if score > best_score:
                    best_score = score
                    best = c
            if best and best_score > 30:
                return best["url"], "keyword"

    # Level 6: 省份/卫视关键词匹配（要求名字相似度高）
    for prov in ["北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
                 "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
                 "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
                 "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海", "天津"]:
        if prov in demo_name:
            kw = prov + "卫视" if "卫视" in demo_name else prov
            if kw in keyword_index and keyword_index[kw]:
                candidates = keyword_index[kw]
                best = None
                best_score = -1
                for c in candidates:
                    sim = _name_similarity(norm, _normalize_name(c["name"]))
                    score = sim * 100 - c["speed"] * 0.01
                    if score > best_score:
                        best_score = score
                        best = c
                if best and best_score > 30:
                    return best["url"], "keyword"

    # ★ 不再做模糊子串匹配，避免货不对板
    return None, None


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    return s


# ==================== 核心引擎 ====================

class Engine:

    def __init__(self):
        self.all_entries = []
        self.alive = []
        self.classified = []
        self.dead_sources = []

    # ---------- 阶段1: 抓取 ----------
    def fetch(self):
        log.info("=" * 50)
        log.info("阶段1: 抓取 + 死源屏蔽")
        log.info("=" * 50)

        alive_sources, self.dead_sources = self._load_sources()
        if not alive_sources:
            log.error("所有源均不可达！")
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
            log.info("  [%s]: %d 个频道", url[:50], len(entries))
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

    # ---------- 阶段2: 测速（真正的播放验证） ----------
    def speedtest(self):
        if not self.all_entries:
            log.warning("无条目可测速")
            return

        session = _session()
        alive = []
        dead_count = 0
        fake_count = 0

        log.info("=" * 50)
        log.info("阶段2: 深度测速 - 验证真实可播放 (并发=%d, 超时=%ds)",
                 MAX_WORKERS, TIMEOUT)
        log.info("=" * 50)
        log.info("待测: %d", len(self.all_entries))
        log.info("ffprobe: %s", "✅ 可用" if HAS_FFPROBE else "❌ 未安装(将使用轻量验证)")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._deep_check_url, e, session): e for e in self.all_entries}
            done = 0
            for future in as_completed(futures):
                done += 1
                item, speed, status = future.result()
                if status == "alive":
                    alive.append({"item": item, "speed": speed, "resolution": None})
                elif status == "fake":
                    fake_count += 1
                else:
                    dead_count += 1
                if done % 50 == 0 or done == len(self.all_entries):
                    log.info("  进度: %d/%d  ✅可播放: %d  ❌不可达: %d  ⚠️假源: %d",
                             done, len(self.all_entries), len(alive), dead_count, fake_count)

        log.info("测速完成: ✅可播放 %d / ❌不可达 %d / ⚠️假源(有响应但非视频) %d",
                 len(alive), dead_count, fake_count)

        # 分辨率探测（ffprobe 深度验证）
        if HAS_FFPROBE and alive:
            log.info("-" * 50)
            log.info("ffprobe 深度验证 + 分辨率探测 (最低 %dp, 共 %d 个)", MIN_HEIGHT, len(alive))
            log.info("-" * 50)

            verified = []
            low_res = 0
            probe_fail = 0

            def _probe(entry):
                res = self._probe_resolution(entry["item"]["url"])
                return (entry, res)

            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = {pool.submit(_probe, a): a for a in alive}
                done = 0
                for future in as_completed(futures):
                    done += 1
                    entry, res = future.result()
                    if res:
                        entry["resolution"] = res
                        if res[1] >= MIN_HEIGHT:
                            verified.append(entry)
                        else:
                            low_res += 1
                    else:
                        # ffprobe 无法解析 → 淘汰（不可播放）
                        probe_fail += 1
                    if done % 50 == 0 or done == len(alive):
                        log.info("  探测: %d/%d  通过: %d  低分辨率: %d  不可播放: %d",
                                 done, len(alive), len(verified), low_res, probe_fail)

            alive = verified
            log.info("深度验证后: 可播放 %d / 低分辨率淘汰 %d / 不可播放淘汰 %d",
                     len(alive), low_res, probe_fail)
        elif not HAS_FFPROBE:
            log.info("⚠️ ffprobe 未安装，跳过深度验证（仅做了轻量级视频头检测）")

        alive.sort(key=lambda x: x["speed"])
        self.alive = alive

    # ---------- 阶段3: 分类 ----------
    def categorize(self):
        log.info("=" * 50)
        log.info("阶段3: 分类 (demo.txt 驱动 + 自动匹配)")
        log.info("=" * 50)

        demo = self._load_demo()
        alias_map = self._load_alias()

        if self.alive:
            log.info("📊 存活频道样本 (前15个):")
            for a in self.alive[:15]:
                log.info("   %s → %s", a["item"]["name"], a["item"]["url"][:60])

        if demo:
            log.info("📋 demo.txt 样本 (前15个):")
            for d in demo[:15]:
                log.info("   [%s] %s", d["group"], d["name"])

        if not demo:
            log.warning("⚠️ demo.txt 为空或不存在，将输出全部存活频道")
            result = []
            for a in self.alive:
                group = a["item"].get("group", "") or "未分类"
                result.append((group, a["item"]["name"], a["item"]["url"]))
            self.classified = result
            log.info("Fallback 输出: %d 个频道", len(result))
            return

        exact_index, normalized_index, keyword_index = _build_index(self.alive, alias_map)
        log.info("索引构建完成: 精确=%d, 归一化=%d, 关键词=%d",
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

            url, match_type = _match_channel(
                name, alias_map, exact_index, normalized_index, keyword_index
            )

            if url:
                if "alias" in match_type:
                    stats["alias"] += 1
                elif match_type in stats:
                    stats[match_type] += 1
                result.append((group, name, url))
            else:
                stats["miss"] += 1
                misses.append(name)

        self.classified = result

        log.info("═══ 匹配结果 ═══")
        log.info("  ✅ 精确匹配: %d", stats["exact"])
        log.info("  ✅ 别名匹配: %d", stats["alias"])
        log.info("  ✅ 归一化匹配: %d", stats["normalized"])
        log.info("  ✅ 关键词匹配: %d", stats["keyword"])
        log.info("  ❌ 未匹配: %d", stats["miss"])
        log.info("  总计输出: %d 个频道", len(result))

        if misses:
            log.warning("未匹配的频道 (%d 个):", len(misses))
            for m in misses[:30]:
                log.warning("   - %s", m)

    # ---------- 阶段4: 输出 ----------
    def write_output(self):
        log.info("=" * 50)
        log.info("阶段4: 输出")
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

        log.info("输出: %s (%d 个频道)", txt_path, count)
        log.info("输出: %s", m3u_path)

    # ---------- 阶段5: EPG ----------
    def process_epg(self):
        log.info("=" * 50)
        log.info("阶段5: EPG")
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
                log.info("  [%s]: %d 条", src[:50], pg_count)
                all_texts.append(text)
            except Exception as e:
                log.warning("  EPG失败: %s", e)

        if not all_texts:
            log.warning("EPG: 无数据")
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
        log.info("EPG: %d 条 → %s", total, epg_path)

    # ==================== 内部方法 ====================

    def _check_source_alive(self, url, session):
        try:
            resp = session.get(url, timeout=15, stream=True)
            resp.raise_for_status()
            chunk = resp.raw.read(512)
            resp.close()
            return bool(chunk)
        except Exception:
            return False

    def _load_sources(self):
        path = os.path.join(CONFIG, "sources.txt")
        if not os.path.exists(path):
            log.error("config/sources.txt 不存在!")
            return [], []

        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        session = _session()
        alive_sources = []
        dead_sources = []
        new_lines = []

        log.info("检测上游源可达性...")

        for line in lines:
            raw = line.rstrip("\n")
            stripped = raw.strip()

            if not stripped or stripped.startswith("#"):
                new_lines.append(raw)
                continue

            if self._check_source_alive(stripped, session):
                alive_sources.append(stripped)
                new_lines.append(raw)
                log.info("  ✅ %s", stripped[:70])
            else:
                dead_sources.append(stripped)
                new_lines.append(f"# {raw}  # 死链已屏蔽")
                log.warning("  ❌ %s → 已屏蔽", stripped[:70])

        if dead_sources:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
            log.info("已屏蔽 %d 个死源，回写 sources.txt", len(dead_sources))

        return alive_sources, dead_sources

    def _fetch_source(self, url, session):
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            return resp.text
        except Exception:
            return None

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
        """解析直播源txt格式（仅用于sources，不用于demo）"""
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

    # =====================================================
    # ★ 核心重写：深度测速 - 验证真实可播放
    # =====================================================

    def _deep_check_url(self, item, session):
        """
        深度验证频道是否可播放
        返回: (item, speed_ms, status)
        status: "alive"=可播放, "dead"=不可达, "fake"=有响应但非视频
        """
        url = item["url"]
        try:
            t0 = time.time()

            # 根据 URL 类型选择不同的验证策略
            url_lower = url.lower()

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
        """
        验证 m3u8 流：
        1. 获取 m3u8 播放列表
        2. 如果是 master playlist，追踪到子 playlist
        3. 找到第一个 .ts 视频片段
        4. 下载并验证 .ts 片段是真实视频数据
        """
        # Step 1: 获取 m3u8 播放列表
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', '').lower()
        content = resp.text

        # 基本检查：应该是 m3u8 内容
        if '#EXTM3U' not in content and '#EXTINF' not in content:
            # 可能返回了 HTML 错误页
            return (item, -1, "fake")

        # Step 2: 处理 master playlist（嵌套 m3u8）
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

        # Step 3: 找到第一个 .ts 视频片段
        ts_url = self._extract_first_ts(content, url)
        if not ts_url:
            return (item, -1, "fake")

        # Step 4: 下载 .ts 片段并验证是真实视频
        try:
            ts_resp = session.get(ts_url, timeout=TIMEOUT, stream=True)
            ts_resp.raise_for_status()
            ts_data = ts_resp.raw.read(8192)
            ts_resp.close()

            if not ts_data or len(ts_data) < 100:
                return (item, -1, "fake")

            if not _is_video_data(ts_data):
                return (item, -1, "fake")

            speed = round((time.time() - t0) * 1000)
            return (item, speed, "alive")

        except Exception:
            return (item, -1, "dead")

    def _check_direct_stream(self, item, url, session, t0):
        """
        验证直链流（.flv, .ts, .mp4 等）：
        1. 请求流 URL
        2. 读取足够的数据（64KB）
        3. 验证是真实视频数据（检查文件头魔数）
        """
        resp = session.get(url, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()

        # 检查 Content-Type
        content_type = resp.headers.get('Content-Type', '').lower()

        # 如果明确是 text/html，大概率是错误页面
        if 'text/html' in content_type:
            # 读一点内容确认
            data = resp.raw.read(512)
            resp.close()
            if _is_video_data(data):
                # 有些服务器 Content-Type 设错了，但数据是对的
                speed = round((time.time() - t0) * 1000)
                return (item, speed, "alive")
            return (item, -1, "fake")

        # 读取 64KB 数据用于验证
        data = resp.raw.read(65536)
        resp.close()

        if not data or len(data) < 100:
            return (item, -1, "dead")

        # 验证是否是真实视频数据
        if not _is_video_data(data):
            return (item, -1, "fake")

        speed = round((time.time() - t0) * 1000)
        return (item, speed, "alive")

    def _extract_sub_playlist(self, content, base_url):
        """从 master playlist 中提取子播放列表 URL"""
        lines = content.strip().split('\n')
        best_bw = -1
        best_url = None

        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith('#EXT-X-STREAM-INF'):
                # 提取带宽信息，优先选带宽最高的
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(bw_match.group(1)) if bw_match else 0

                # 下一行是 URL
                if i + 1 < len(lines):
                    sub_path = lines[i + 1].strip()
                    if sub_path and not sub_path.startswith('#'):
                        if sub_path.startswith('http'):
                            sub_url = sub_path
                        else:
                            sub_url = urljoin(base_url, sub_path)
                        if bw >= best_bw:
                            best_bw = bw
                            best_url = sub_url

        return best_url

    def _extract_first_ts(self, content, base_url):
        """从 m3u8 播放列表中提取第一个 .ts 片段 URL"""
        lines = content.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                # 这是一个媒体片段 URL
                if line.startswith('http'):
                    return line
                else:
                    return urljoin(base_url, line)
        return None

    def _probe_resolution(self, url):
        """使用 ffprobe 探测分辨率（同时也验证了流是否可播放）"""
        if not HAS_FFPROBE:
            return None
        try:
            cmd = [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-analyzeduration", "5000000",  # 分析5秒
                "-probesize", "5000000",          # 探测5MB
                "-i", url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    w = stream.get("width", 0)
                    h = stream.get("height", 0)
                    if w and h:
                        return (w, h)
            return None
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

    # =====================================================
    # ★ demo.txt 解析器
    # =====================================================
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
            log.info("  分组(%d个): %s", len(groups), groups)
            log.info("  前10个: %s", [(e["group"], e["name"]) for e in entries[:10]])
        else:
            log.warning("⚠️ demo.txt 解析结果为空！请检查文件格式")

        return entries

    def _load_alias(self):
        path = os.path.join(CONFIG, "alias.txt")
        alias_map = {}
        if not os.path.exists(path):
            return alias_map
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) >= 2:
                    main_name = parts[0].strip()
                    for alt in parts[1:]:
                        alt = alt.strip()
                        if alt:
                            alias_map[alt] = main_name
        log.info("alias.txt 加载: %d 条别名映射", len(alias_map))
        return alias_map
