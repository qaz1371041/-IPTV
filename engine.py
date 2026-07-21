import os, re, io, gzip, json, time, logging, subprocess, shutil
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests
import urllib3

urllib3.disable_warnings()
logging.getLogger("urllib3").setLevel(logging.ERROR)

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config")
OUTPUT = os.path.join(BASE, "output")

TIMEOUT = 8
MAX_WORKERS = 80
MIN_HEIGHT = 720
FFPROBE_TIMEOUT = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
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

# ═══════════════════════════════════════
#  频道名归一化 —— 核心匹配逻辑
# ═══════════════════════════════════════

# 要剥离的后缀/噪声词
_NOISE_WORDS = [
    "高清", "超清", "标清", "蓝光", "原画",
    "hd", "fhd", "uhd", "4k", "8k", "sd", "h265", "h264", "hevc", "avc",
    "频道", "电视台", "卫视台", "tv",
    "（主）", "（备）", "(主)", "(备)",
    "ipv6", "ipv4",
    "组播", "单播",
]

# 中文数字映射
_CN_NUM = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
    "零": "0", "〇": "0",
}

# CCTV 别名表
_CCTV_MAP = {
    "cctv1":  "cctv1综合",      "cctv-1": "cctv1综合",
    "cctv2":  "cctv2财经",      "cctv-2": "cctv2财经",
    "cctv3":  "cctv3综艺",      "cctv-3": "cctv3综艺",
    "cctv4":  "cctv4中文国际",   "cctv-4": "cctv4中文国际",
    "cctv5":  "cctv5体育",      "cctv-5": "cctv5体育",
    "cctv5+": "cctv5+体育赛事", "cctv-5+": "cctv5+体育赛事",
    "cctv5＋": "cctv5+体育赛事",
    "cctv6":  "cctv6电影",      "cctv-6": "cctv6电影",
    "cctv7":  "cctv7国防军事",   "cctv-7": "cctv7国防军事",
    "cctv8":  "cctv8电视剧",     "cctv-8": "cctv8电视剧",
    "cctv9":  "cctv9纪录",      "cctv-9": "cctv9纪录",
    "cctv10": "cctv10科教",     "cctv-10": "cctv10科教",
    "cctv11": "cctv11戏曲",     "cctv-11": "cctv11戏曲",
    "cctv12": "cctv12社会与法",  "cctv-12": "cctv12社会与法",
    "cctv13": "cctv13新闻",     "cctv-13": "cctv13新闻",
    "cctv14": "cctv14少儿",     "cctv-14": "cctv14少儿",
    "cctv15": "cctv15音乐",     "cctv-15": "cctv15音乐",
    "cctv16": "cctv16奥林匹克",  "cctv-16": "cctv16奥林匹克",
    "cctv17": "cctv17农业农村",  "cctv-17": "cctv17农业农村",
}


def _normalize_name(raw: str) -> str:
    """
    把频道名归一化为纯小写、无空格、无后缀、无标点的最小形式。
    用于模糊匹配比对。
    """
    n = raw.strip().lower()

    # 全角→半角
    n = n.replace("＋", "+").replace("－", "-").replace("（", "(").replace("）", ")")

    # 中文数字→阿拉伯数字 (仅对 CCTV 后面的数字)
    def _cn2num(m):
        prefix = m.group(1)
        cn = m.group(2)
        return prefix + _CN_NUM.get(cn, cn)
    n = re.sub(r'(cctv|cctv-)([一二三四五六七八九十])', _cn2num, n)

    # 去噪声词
    for w in _NOISE_WORDS:
        n = n.replace(w, "")

    # 去标点符号和空格
    n = re.sub(r'[\s\-_.,:;，。：；、()\[\]【】《》"\'\"\/|\\#*！!？?@&]+', '', n)

    return n.strip()


def _build_index(alive_list, alias_map):
    """
    从存活频道列表构建三级索引:
      exact_index:       原始名 -> {url, speed}
      normalized_index:  归一化名 -> {url, speed}
      keyword_index:     关键词 -> [{url, speed, name}]
    """
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

        # 关键词索引 —— 提取核心标识
        # CCTV 系列: "cctv1", "cctv5+" 等
        m = re.search(r'(cctv\d+\+?)', norm)
        if m:
            keyword_index[m.group(1)].append(data)

        # 卫视系列: "湖南", "浙江", "江苏" 等
        m = re.search(r'([\u4e00-\u9fff]{2,4})(卫视|tv)', norm)
        if m:
            keyword_index[m.group(1) + "卫视"].append(data)

        # 省级频道: 提取省份名
        provinces = ["北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
                     "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
                     "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
                     "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海",
                     "天津"]
        for prov in provinces:
            if prov in name:
                keyword_index[prov].append(data)

    return exact_index, normalized_index, keyword_index


def _match_channel(demo_name, alias_map, exact_index, normalized_index, keyword_index):
    """
    多级匹配策略，返回 (url, match_type) 或 (None, None)
    match_type: "exact" / "alias" / "normalized" / "keyword" / "fuzzy"
    """
    # 1) 精确匹配
    if demo_name in exact_index:
        return exact_index[demo_name]["url"], "exact"

    # 2) 别名精确匹配
    aliased = alias_map.get(demo_name)
    if aliased and aliased in exact_index:
        return exact_index[aliased]["url"], "alias"

    # 3) 归一化匹配
    norm = _normalize_name(demo_name)
    if norm and norm in normalized_index:
        return normalized_index[norm]["url"], "normalized"

    # 4) 别名归一化匹配
    if aliased:
        norm_alias = _normalize_name(aliased)
        if norm_alias and norm_alias in normalized_index:
            return normalized_index[norm_alias]["url"], "alias+normalized"

    # 5) 关键词匹配
    #    CCTV 系列
    m = re.search(r'(cctv\d+\+?)', norm)
    if m:
        kw = m.group(1)
        if kw in keyword_index and keyword_index[kw]:
            # 进一步缩小范围: 如果 demo 名含 "体育"，优先选含 "体育" 的
            best = keyword_index[kw][0]
            for item in keyword_index[kw]:
                if item["speed"] < best["speed"]:
                    best = item
            return best["url"], "keyword"

    # 6) 卫视/省份关键词
    for prov in ["北京", "上海", "广东", "深圳", "浙江", "江苏", "湖南", "湖北",
                 "四川", "重庆", "山东", "河南", "河北", "福建", "安徽", "江西",
                 "辽宁", "吉林", "黑龙江", "陕西", "甘肃", "云南", "贵州",
                 "广西", "海南", "山西", "内蒙古", "新疆", "西藏", "宁夏", "青海",
                 "天津"]:
        if prov in demo_name:
            kw = prov + "卫视" if "卫视" in demo_name else prov
            if kw in keyword_index and keyword_index[kw]:
                best = min(keyword_index[kw], key=lambda x: x["speed"])
                return best["url"], "keyword"

    # 7) 子串模糊匹配 (demo 名是某个存活频道名的子串，或反过来)
    if len(norm) >= 2:
        for bnorm, bdata in normalized_index.items():
            if norm in bnorm or bnorm in norm:
                return bdata["url"], "fuzzy"

    return None, None


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    return s


class Engine:

    def __init__(self):
        self.all_entries = []
        self.alive = []
        self.classified = []
        self.dead_sources = []

    # ═══════════════════════════════════════
    #  阶段1: 抓取
    # ═══════════════════════════════════════

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

    # ═══════════════════════════════════════
    #  阶段2: 测速
    # ═══════════════════════════════════════

    def speedtest(self):
        if not self.all_entries:
            log.warning("无条目可测速")
            return

        session = _session()
        alive = []
        dead_count = 0

        log.info("=" * 50)
        log.info("阶段2: 全量测速 (并发=%d, 超时=%ds)", MAX_WORKERS, TIMEOUT)
        log.info("=" * 50)
        log.info("待测: %d", len(self.all_entries))

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self._check_url, e, session): e for e in self.all_entries}
            done = 0
            for future in as_completed(futures):
                done += 1
                item, speed = future.result()
                if speed > 0:
                    alive.append({"item": item, "speed": speed, "resolution": None})
                else:
                    dead_count += 1
                if done % 50 == 0 or done == len(self.all_entries):
                    log.info("  进度: %d/%d  存活: %d  死亡: %d",
                             done, len(self.all_entries), len(alive), dead_count)

        log.info("测速完成: 存活 %d / 死亡 %d", len(alive), dead_count)

        if HAS_FFPROBE and alive:
            log.info("-" * 50)
            log.info("分辨率探测 (最低 %dp, 共 %d 个)", MIN_HEIGHT, len(alive))
            log.info("-" * 50)

            def _probe(entry):
                res = self._probe_resolution(entry["item"]["url"])
                return (entry, res)

            low_res = 0
            with ThreadPoolExecutor(max_workers=30) as pool:
                futures = {pool.submit(_probe, a): a for a in alive}
                done = 0
                for future in as_completed(futures):
                    done += 1
                    entry, res = future.result()
                    if res:
                        entry["resolution"] = res
                        if res[1] < MIN_HEIGHT:
                            low_res += 1
                    if done % 50 == 0 or done == len(alive):
                        log.info("  探测: %d/%d  低分辨率淘汰: %d", done, len(alive), low_res)

            alive = [a for a in alive
                     if a["resolution"] is None or a["resolution"][1] >= MIN_HEIGHT]
            log.info("分辨率过滤后: 存活 %d / 淘汰 %d", len(alive), low_res)
        else:
            if not HAS_FFPROBE:
                log.info("⚠️ ffprobe 未安装，跳过分辨率探测")

        alive.sort(key=lambda x: x["speed"])
        self.alive = alive

    # ═══════════════════════════════════════
    #  阶段3: 分类（超级自动匹配）
    # ═══════════════════════════════════════

    def categorize(self):
        log.info("=" * 50)
        log.info("阶段3: 分类 (demo.txt 驱动 + 自动匹配)")
        log.info("=" * 50)

        demo = self._load_demo()
        alias_map = self._load_alias()

        # ─── 打印诊断信息 ───
        if self.alive:
            log.info("📊 存活频道样本 (前15个):")
            for a in self.alive[:15]:
                log.info("   %s → %s", a["item"]["name"], a["item"]["url"][:60])

        if demo:
            log.info("📋 demo.txt 样本 (前15个):")
            for d in demo[:15]:
                log.info("   [%s] %s", d["group"], d["name"])

        # ─── 如果 demo.txt 为空，直接输出全部存活频道 ───
        if not demo:
            log.warning("⚠️ demo.txt 为空或不存在，将输出全部存活频道")
            result = []
            for a in self.alive:
                group = a["item"].get("group", "") or "未分类"
                result.append((group, a["item"]["name"], a["item"]["url"]))
            self.classified = result
            log.info("Fallback 输出: %d 个频道", len(result))
            return

        # ─── 构建三级索引 ───
        exact_index, normalized_index, keyword_index = _build_index(self.alive, alias_map)
        log.info("索引构建完成: 精确=%d, 归一化=%d, 关键词=%d",
                 len(exact_index), len(normalized_index), len(keyword_index))

        # ─── 逐条匹配 ───
        result = []
        current_group = ""
        seen_in_group = set()
        stats = {"exact": 0, "alias": 0, "normalized": 0, "keyword": 0, "fuzzy": 0, "miss": 0}
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
                # 归一化统计
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
        log.info("  ✅ 模糊匹配: %d", stats["fuzzy"])
        log.info("  ❌ 未匹配: %d", stats["miss"])
        log.info("  总计输出: %d 个频道", len(result))

        if misses:
            log.warning("未匹配的频道 (%d 个):", len(misses))
            for m in misses[:20]:
                log.warning("   - %s", m)

    # ═══════════════════════════════════════
    #  阶段4: 输出
    # ═══════════════════════════════════════

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

    # ═══════════════════════════════════════
    #  阶段5: EPG
    # ═══════════════════════════════════════

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

    # ═══════════════════════════════════════
    #  内部方法
    # ═══════════════════════════════════════

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
                parts = line.split(",", 1)
                name = parts[0].strip()
                rest = parts[1].strip()
                # demo.txt 可能只有频道名没有 URL
                if rest and not rest.startswith("http") and rest != "":
                    # 这不是 URL，可能是 demo 模板，只取频道名
                    entries.append({"name": name, "url": "", "group": group})
                else:
                    entries.append({"name": name, "url": rest, "group": group})
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

    def _check_url(self, item, session):
        url = item["url"]
        try:
            t0 = time.time()
            resp = session.get(url, timeout=TIMEOUT, stream=True)
            resp.raise_for_status()
            chunk = resp.raw.read(2048)
            resp.close()
            if not chunk:
                return (item, -1)
            speed = round((time.time() - t0) * 1000)
            return (item, speed)
        except Exception:
            return (item, -1)

    def _probe_resolution(self, url):
        if not HAS_FFPROBE:
            return None
        try:
            cmd = [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-i", url,
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
        except Exception:
            return None

    def _load_demo(self):
        path = os.path.join(CONFIG, "demo.txt")
        if not os.path.exists(path):
            log.warning("config/demo.txt 不存在")
            return []
        with open(path, encoding="utf-8") as f:
            content = f.read()
        entries = self._parse_txt(content)
        log.info("demo.txt 加载: %d 个频道模板", len(entries))
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
