#!/usr/bin/env python3
"""
核心引擎 - 适配 alias.txt (主名,别名1,别名2,re:正则) + demo.txt 多分类复用
"""
import os, re, gzip, json, time, logging, subprocess
from typing import List, Dict, Tuple, Set
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ============ 路径 ============
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config")
OUTPUT = os.path.join(BASE, "output")

# ============ 常量 ============
THREADS = 50
TIMEOUT = 10
MIN_MBPS = 2.0
SAMPLE_PER_HOST = 3
HOST_DEAD_RATIO = 0.8
TS_SYNC = 0x47
EPG_KEYWORDS = ["未提供节目表", "精彩节目", "暂无节目"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ============================================================
#  工具
# ============================================================
def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[429,500,502,503])
    s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=100))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=100))
    s.headers["User-Agent"] = UA
    return s

def _read(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

def _norm_url(url: str) -> str:
    url = url.strip()
    url = re.sub(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)",
                 r"https://raw.githubusercontent.com/\1/\2/\3", url)
    url = re.sub(r"https?://gitee\.com/([^/]+)/([^/]+)/blob/(.+)",
                 r"https://gitee.com/\1/\2/raw/\3", url)
    return url


# ============================================================
#  别名引擎 (适配: 主名,别名1,别名2,re:正则)
# ============================================================
class AliasEngine:
    """
    格式: 主名,别名1,别名2,re:正则,...
    第一个逗号前 = 标准名(主名)
    后续 = 别名 (re:开头为正则)
    """
    def __init__(self):
        self.exact: Dict[str, str] = {}      # alias_lower -> 主名
        self.regex: List[Tuple[re.Pattern, str]] = []  # (pattern, 主名)
        self._load()

    def _load(self):
        path = os.path.join(CONFIG, "alias.txt")
        if not os.path.exists(path):
            logger.warning("alias.txt 不存在")
            return

        count_exact = 0
        count_regex = 0

        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "," not in line:
                    continue

                parts = line.split(",")
                standard = parts[0].strip()
                if not standard:
                    continue

                # 主名自身映射
                self.exact[standard.lower()] = standard
                count_exact += 1

                # 别名
                for alias in parts[1:]:
                    alias = alias.strip()
                    if not alias:
                        continue
                    if alias.startswith("re:"):
                        # 正则别名
                        pattern_str = alias[3:]
                        try:
                            pat = re.compile(pattern_str)
                            self.regex.append((pat, standard))
                            count_regex += 1
                        except re.error as e:
                            logger.debug(f"正则错误: {pattern_str} -> {e}")
                    else:
                        self.exact[alias.lower()] = standard
                        count_exact += 1

        logger.info(f"别名引擎: {count_exact} 精确, {count_regex} 正则")

    def resolve(self, name: str) -> str:
        """频道名 → 标准名"""
        name_stripped = name.strip()
        low = name_stripped.lower()

        # 1. 精确匹配
        if low in self.exact:
            return self.exact[low]

        # 2. 正则匹配
        for pat, std in self.regex:
            if pat.search(name_stripped):
                return std

        return name_stripped

    def add_alias(self, alias: str, standard: str):
        """追加新别名到文件"""
        path = os.path.join(CONFIG, "alias.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{standard},{alias}\n")
        self.exact[alias.lower()] = standard


# ============================================================
#  Demo 模板 (支持同一频道出现在多个分类)
# ============================================================
class Demo:
    """
    解析 demo.txt，支持同一频道在多个分类中出现。
    输出时每个分类独立，频道可重复出现在不同分类。
    """
    def __init__(self):
        self.path = os.path.join(CONFIG, "demo.txt")
        # 有序分类列表: [{"name": "📺央视频道", "channels": ["CCTV1", ...]}]
        self.cats: List[Dict] = []
        # 频道 → 出现在哪些分类 (一对多)
        self.ch2cats: Dict[str, List[str]] = defaultdict(list)
        # 每个分类内的频道顺序
        self.cat_order: Dict[str, Dict[str, int]] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            logger.warning("demo.txt 不存在")
            return

        cur = None
        with open(self.path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.endswith(",#genre#"):
                    cat_name = line[:-8].strip()
                    cur = {"name": cat_name, "channels": []}
                    self.cats.append(cur)
                    self.cat_order[cat_name] = {}
                elif cur is not None:
                    idx = len(cur["channels"])
                    cur["channels"].append(line)
                    self.ch2cats[line].append(cur["name"])
                    self.cat_order[cur["name"]][line] = idx

        total_ch = sum(len(c["channels"]) for c in self.cats)
        logger.info(f"Demo: {len(self.cats)} 分类, {total_ch} 条目, {len(self.ch2cats)} 唯一频道")

    def get_cats_for_channel(self, std_name: str) -> List[str]:
        """获取频道所属的所有分类"""
        return self.ch2cats.get(std_name, [])

    def get_order(self, cat_name: str, ch_name: str) -> int:
        """获取频道在分类中的排序位置"""
        return self.cat_order.get(cat_name, {}).get(ch_name, 99999)

    def add_channel(self, name: str, cat: str):
        """自进化: 追加新频道到分类"""
        for c in self.cats:
            if c["name"] == cat:
                if name not in c["channels"]:
                    idx = len(c["channels"])
                    c["channels"].append(name)
                    self.ch2cats[name].append(cat)
                    self.cat_order[cat][name] = idx
                return
        # 新分类
        self.cats.append({"name": cat, "channels": [name]})
        self.ch2cats[name].append(cat)
        self.cat_order[cat] = {name: 0}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for c in self.cats:
                f.write(f"{c['name']},#genre#\n")
                for ch in c["channels"]:
                    f.write(f"{ch}\n")
                f.write("\n")


# ============================================================
#  黑白名单
# ============================================================
class Rules:
    def __init__(self):
        self.black: List[re.Pattern] = []
        self.white: Set[str] = set()
        self._load()

    def _load(self):
        path = os.path.join(CONFIG, "rules.txt")
        if not os.path.exists(path):
            return
        section = ""
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line == "[black]":
                    section = "black"; continue
                elif line == "[white]":
                    section = "white"; continue

                if section == "black":
                    try:
                        self.black.append(re.compile(line, re.I))
                    except re.error:
                        self.black.append(re.compile(re.escape(line), re.I))
                elif section == "white":
                    self.white.add(line.lower())

        logger.info(f"规则: {len(self.black)} 黑名单, {len(self.white)} 白名单")

    def is_black(self, name: str, url: str) -> bool:
        for p in self.black:
            if p.search(name) or p.search(url):
                return True
        return False

    def is_white(self, name: str) -> bool:
        return name.lower() in self.white


# ============================================================
#  图标
# ============================================================
class Icons:
    def __init__(self):
        self.map: Dict[str, str] = {}
        for line in _read(os.path.join(CONFIG, "icons.txt")):
            if "=" in line:
                k, v = line.split("=", 1)
                self.map[k.strip()] = v.strip()
    def get(self, name: str) -> str:
        return self.map.get(name, "")


# ============================================================
#  主引擎
# ============================================================
class Engine:
    def __init__(self):
        os.makedirs(OUTPUT, exist_ok=True)
        self.alias = AliasEngine()
        self.demo = Demo()
        self.rules = Rules()
        self.icons = Icons()
        self.session = _session()
        self.pool = ThreadPoolExecutor(max_workers=THREADS)
        self.channels: List[Dict] = []   # 抓取后
        self.alive: List[Dict] = []      # 测速后
        # AI 缓存
        self.cache_path = os.path.join(OUTPUT, "cache.json")
        self.cache: Dict = {}
        if os.path.exists(self.cache_path):
            try:
                self.cache = json.load(open(self.cache_path, encoding="utf-8"))
            except Exception:
                pass

    # ==================== 阶段1: 抓取 ====================
    def fetch(self):
        logger.info("=" * 50)
        logger.info("阶段1: 抓取")
        logger.info("=" * 50)

        urls = [_norm_url(u) for u in _read(os.path.join(CONFIG, "sources.txt"))]
        logger.info(f"源: {len(urls)}")

        futs = {self.pool.submit(self._fetch_one, u): u for u in urls}
        for fut in as_completed(futs):
            try:
                self.channels.extend(fut.result())
            except Exception as e:
                logger.warning(f"源异常: {e}")

        # 过滤
        before = len(self.channels)
        self.channels = [
            ch for ch in self.channels
            if not self.rules.is_black(ch["name"], ch["url"])
            and ch["name"].strip()
            and not re.match(r"^\d+$", ch["name"].strip())
        ]
        logger.info(f"抓取: {before} → 过滤: {len(self.channels)}")

    def _fetch_one(self, url: str) -> List[Dict]:
        try:
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            text = r.text
        except Exception as e:
            logger.warning(f"  失败 [{url[:50]}]: {e}")
            return []
        if "#EXTINF:" in text[:500]:
            return self._parse_m3u(text, url)
        return self._parse_txt(text, url)

    def _parse_m3u(self, text: str, src: str) -> List[Dict]:
        result = []
        name, logo, group = "", "", ""
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("#EXTINF:"):
                m = re.search(r",(.+)$", line)
                name = m.group(1).strip() if m else ""
                m2 = re.search(r'tvg-logo="([^"]*)"', line)
                logo = m2.group(1) if m2 else ""
                m3 = re.search(r'group-title="([^"]*)"', line)
                group = m3.group(1) if m3 else ""
            elif line and not line.startswith("#") and line.startswith("http"):
                if name:
                    result.append({"name": name, "url": line, "logo": logo, "group": group, "src": src})
                name, logo, group = "", "", ""
        return result

    def _parse_txt(self, text: str, src: str) -> List[Dict]:
        result = []
        group = ""
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith(",#genre#"):
                group = line[:-8].strip()
                continue
            if "," in line:
                name, urls_str = line.split(",", 1)
                name = name.strip()
                for u in urls_str.split("#"):
                    u = u.strip()
                    if u.startswith("http"):
                        result.append({"name": name, "url": u, "logo": "", "group": group, "src": src})
        return result

    # ==================== 阶段2: 测速 ====================
    def speedtest(self):
        logger.info("=" * 50)
        logger.info(f"阶段2: 测速 ({THREADS}线程)")
        logger.info("=" * 50)
        if not self.channels:
            return

        # 按host分组预筛
        hosts: Dict[str, List[Dict]] = defaultdict(list)
        for ch in self.channels:
            hosts[urlparse(ch["url"]).netloc].append(ch)

        dead_hosts = self._prefilter(hosts)
        logger.info(f"死亡服务器: {len(dead_hosts)}/{len(hosts)}")

        candidates = [
            ch for ch in self.channels
            if urlparse(ch["url"]).netloc not in dead_hosts
            or self.rules.is_white(ch["name"])
        ]
        logger.info(f"待测: {len(candidates)}")

        futs = {self.pool.submit(self._test_one, ch["url"]): ch for ch in candidates}
        done = 0
        for fut in as_completed(futs):
            ch = futs[fut]
            done += 1
            try:
                speed, valid, res = fut.result()
            except Exception:
                speed, valid, res = 0, False, ""

            is_wl = self.rules.is_white(ch["name"])
            if (speed >= MIN_MBPS and valid) or (is_wl and speed > 0):
                ch["speed"] = round(speed, 2)
                ch["resolution"] = res
                self.alive.append(ch)

            if done % 200 == 0:
                logger.info(f"  进度: {done}/{len(candidates)} 存活: {len(self.alive)}")

        self.alive.sort(key=lambda x: x.get("speed", 0), reverse=True)
        logger.info(f"存活: {len(self.alive)}/{len(candidates)}")

        stats = defaultdict(int)
        for ch in self.alive:
            stats[ch.get("resolution") or "未知"] += 1
        logger.info(f"分辨率: {dict(stats)}")

    def _prefilter(self, hosts: Dict[str, List]) -> Set[str]:
        dead = set()
        def _chk(url):
            try:
                return self.session.head(url, timeout=5, allow_redirects=True).status_code < 400
            except Exception:
                return False
        for host, chs in hosts.items():
            samples = chs[:SAMPLE_PER_HOST]
            results = [f.result() for f in [self.pool.submit(_chk, c["url"]) for c in samples]]
            if results and sum(results) / len(results) < (1 - HOST_DEAD_RATIO):
                dead.add(host)
        return dead

    def _test_one(self, url: str) -> Tuple[float, bool, str]:
        try:
            t0 = time.time()
            r = self.session.get(url, timeout=TIMEOUT, stream=True)
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(8192):
                data += chunk
                if len(data) >= 1048576:
                    break
            r.close()
            elapsed = max(time.time() - t0, 0.001)
            speed = len(data) * 8 / elapsed / 1e6
            # TS 0x47 校验
            valid = len(data) >= 188 and all(
                data[i] == TS_SYNC for i in range(0, min(len(data), 940), 188)
            )
            res = self._probe(url)
            return speed, valid, res
        except Exception:
            return 0.0, False, ""

    def _probe(self, url: str) -> str:
        try:
            out = subprocess.run(
                ["ffprobe","-v","quiet","-select_streams","v:0",
                 "-show_entries","stream=height","-of","csv=p=0",
                 "-rw_timeout","5000000","-analyzeduration","2000000",url],
                capture_output=True, text=True, timeout=8
            ).stdout.strip()
            h = int(out.split("\n")[0]) if out else 0
            if h >= 2160: return "4K"
            if h >= 1080: return "1080p"
            if h >= 720: return "720p"
            if h >= 480: return "480p"
        except Exception:
            pass
        return ""

    # ==================== 阶段3: 分类 (demo.txt 驱动) ====================
    def categorize(self):
        """
        核心逻辑:
        1. 别名标准化 → 得到标准名
        2. 同名去重 (取最快)
        3. 按 demo.txt 分配: 一个频道可出现在多个分类
        4. 未匹配的 → AI分类 → 自进化追加到 demo.txt
        """
        logger.info("=" * 50)
        logger.info("阶段3: 分类 (demo.txt 驱动)")
        logger.info("=" * 50)
        if not self.alive:
            return

        # 1. 别名标准化
        for ch in self.alive:
            ch["std"] = self.alias.resolve(ch["name"])

        # 2. 去重: 同标准名取速度最快的 (保留多个URL备选)
        best: Dict[str, Dict] = {}
        for ch in self.alive:
            k = ch["std"]
            if k not in best or ch.get("speed", 0) > best[k].get("speed", 0):
                best[k] = ch
        unique = list(best.values())
        logger.info(f"去重: {len(self.alive)} → {len(unique)} 唯一频道")

        # 3. 按 demo.txt 分配 (一对多)
        # 结果: OrderedDict { 分类名: [频道dict, ...] }
        result: Dict[str, List[Dict]] = OrderedDict()
        for cat in self.demo.cats:
            result[cat["name"]] = []

        matched_names: Set[str] = set()
        unmatched: List[Dict] = []

        for ch in unique:
            std = ch["std"]
            cats = self.demo.get_cats_for_channel(std)
            if cats:
                matched_names.add(std)
                for cat_name in cats:
                    result[cat_name].append(ch)
            else:
                unmatched.append(ch)

        matched_count = sum(len(v) for v in result.values())
        logger.info(f"匹配: {len(matched_names)} 频道 → {matched_count} 条目")
        logger.info(f"未匹配: {len(unmatched)}")

        # 4. AI 兜底 + 自进化
        if unmatched:
            logger.info(f"AI 分类: {len(unmatched)} 个")
            from ai import AIHelper
            ai = AIHelper(self.cache, self.cache_path)
            new_count = 0
            for ch in unmatched:
                std = ch["std"]
                cat = ai.classify(std)
                ch["cat"] = cat
                # 追加到 demo.txt (自进化)
                self.demo.add_channel(std, cat)
                if cat not in result:
                    result[cat] = []
                result[cat].append(ch)
                new_count += 1
            ai.save()
            self.demo.save()
            logger.info(f"自进化: +{new_count} 频道写入 demo.txt")

        # 5. 每个分类内按 demo.txt 顺序排序
        for cat_name in result:
            result[cat_name].sort(
                key=lambda x: self.demo.get_order(cat_name, x["std"])
            )

        # 6. 移除空分类
        self.categorized = OrderedDict(
            (k, v) for k, v in result.items() if v
        )

        total = sum(len(v) for v in self.categorized.values())
        logger.info(f"最终: {len(self.categorized)} 分类, {total} 条目")

    # ==================== 阶段4: 输出 ====================
    def write_output(self):
        logger.info("=" * 50)
        logger.info("阶段4: 输出")
        logger.info("=" * 50)
        if not hasattr(self, "categorized") or not self.categorized:
            return

        # --- M3U ---
        m3u_path = os.path.join(OUTPUT, "live.m3u")
        lines = ["#EXTM3U"]
        for cat, chs in self.categorized.items():
            for ch in chs:
                name = ch["std"]
                logo = ch.get("logo") or self.icons.get(name)
                res = ch.get("resolution", "")
                display = f"{name} [{res}]" if res else name
                attrs = f'tvg-name="{name}" group-title="{cat}"'
                if logo:
                    attrs += f' tvg-logo="{logo}"'
                lines.append(f"#EXTINF:-1 {attrs},{display}")
                lines.append(ch["url"])
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # --- TXT ---
        txt_path = os.path.join(OUTPUT, "live.txt")
        lines = []
        for cat, chs in self.categorized.items():
            lines.append(f"{cat},#genre#")
            for ch in chs:
                lines.append(f"{ch['std']},{ch['url']}")
            lines.append("")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        total = sum(len(v) for v in self.categorized.values())
        logger.info(f"✅ {total} 条目 → live.m3u + live.txt")

    # ==================== 阶段5: EPG ====================
    def process_epg(self):
        logger.info("=" * 50)
        logger.info("阶段5: EPG")
        logger.info("=" * 50)
        urls = _read(os.path.join(CONFIG, "epg.txt"))
        if not urls:
            logger.info("无EPG源")
            return

        all_progs = []
        for url in urls:
            url = _norm_url(url)
            try:
                r = self.session.get(url, timeout=30)
                r.raise_for_status()
                if url.endswith(".gz") or r.headers.get("Content-Encoding") == "gzip":
                    text = gzip.decompress(r.content).decode("utf-8")
                else:
                    text = r.text
                progs = self._parse_epg(text)
                all_progs.extend(progs)
                logger.info(f"  [{url[:40]}]: {len(progs)} 条")
            except Exception as e:
                logger.warning(f"  EPG失败: {e}")

        if not all_progs:
            return

        # 去重
        seen = set()
        merged = []
        for p in all_progs:
            key = (p["ch_id"], p["start"], p["title"])
            if key not in seen:
                seen.add(key)
                merged.append(p)

        # 写 XML.GZ
        root = ET.Element("tv")
        seen_ch = set()
        for p in merged:
            if p["ch_id"] not in seen_ch:
                seen_ch.add(p["ch_id"])
                ch_el = ET.SubElement(root, "channel", id=p["ch_id"])
                ET.SubElement(ch_el, "display-name").text = p["ch_name"]
        for p in merged:
            el = ET.SubElement(root, "programme", start=p["start"], stop=p["stop"], channel=p["ch_id"])
            t = ET.SubElement(el, "title", lang="zh")
            t.text = p["title"]

        out_path = os.path.join(OUTPUT, "epg.xml.gz")
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            f.write(ET.tostring(root, encoding="unicode", xml_declaration=True))
        logger.info(f"EPG: {len(all_progs)} → {len(merged)} → {out_path}")

    def _parse_epg(self, xml_str: str) -> List[Dict]:
        progs = []
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            return progs
        ch_map = {}
        for ch in root.findall(".//channel"):
            cid = ch.get("id", "")
            dn = ch.find("display-name")
            ch_map[cid] = dn.text if dn is not None and dn.text else cid
        for p in root.findall(".//programme"):
            title_el = p.find("title")
            title = title_el.text if title_el is not None else ""
            if not title or any(kw in title for kw in EPG_KEYWORDS):
                continue
            progs.append({
                "ch_id": p.get("channel", ""),
                "ch_name": ch_map.get(p.get("channel", ""), ""),
                "start": p.get("start", ""),
                "stop": p.get("stop", ""),
                "title": title,
            })
        return progs
