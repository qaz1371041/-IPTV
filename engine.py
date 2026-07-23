#!/usr/bin/env python3
"""
IPTV 直播源聚合引擎
功能：从多个源抓取 → 模板匹配 → 去重 → 测速 → 输出
"""

import re
import os
import time
import logging
import difflib
import hashlib
import requests
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("iptv-engine")

# ============================================================
# 配置
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

MAX_WORKERS = 20          # 并发抓取线程数
MAX_URLS_PER_CHANNEL = 5  # 每频道最多保留URL数
REQUEST_TIMEOUT = 15      # 请求超时(秒)
SPEED_TEST_TIMEOUT = 5    # 测速超时(秒)
DIFFLIB_CUTOFF = 0.6     # difflib 相似度阈值

# ============================================================
# 归一化函数
# ============================================================
_NOISE_WORDS = [
    # 画质
    "高清", "超清", "标清", "蓝光", "原画", "普清", "流畅",
    "hd", "fhd", "uhd", "4k", "8k", "sd", "h265", "h264", "hevc", "avc",
    # 类型
    "频道", "电视台", "卫视台", "tv", "直播",
    # 线路
    "（主）", "（备）", "(主)", "(备)", "主", "备", "主备", "备用", "主线", "备线",
    # 协议
    "ipv6", "ipv4", "组播", "单播",
    # 频道内容后缀
    "综合", "财经", "综艺", "中文国际", "体育", "体育赛事", "电影",
    "国防军事", "电视剧", "纪录", "科教", "戏曲", "社会与法",
    "新闻", "少儿", "音乐", "农业农村", "奥林匹克",
    # 其他
    "咪咕", "itv", "北联", "电信", "东联", "高码", "高码率",
    "广西", "梅州", "汝阳", "山东", "上海", "斯特", "四川",
    "太原", "天津", "影视", "浙江", "重庆",
]

# 按长度降序排列，确保长词优先匹配
_NOISE_WORDS.sort(key=len, reverse=True)


def _normalize_name(name: str) -> str:
    """
    将频道名归一化为可比较的标准形式。
    例: "CCTV-1综合高清" → "cctv1"
        "CCTV1综合"      → "cctv1"
        "CCTV-1"         → "cctv1"
    """
    if not name:
        return ""

    s = name.strip().lower()

    # 去掉特殊字符（保留中文、字母、数字、+）
    s = re.sub(r'[^\w\u4e00-\u9fff+＋]', '', s)

    # 去掉噪声词
    changed = True
    while changed:
        changed = False
        for word in _NOISE_WORDS:
            if word in s:
                s = s.replace(word, '')
                changed = True

    # 去掉多余空格
    s = s.strip()

    return s


# ============================================================
# 主引擎类
# ============================================================
class IPTVEngine:
    def __init__(self):
        self.all_entries = []       # 所有抓取的条目
        self.matched_entries = []   # 匹配后的条目
        self.final_entries = []     # 最终输出
        self.blacklist = set()      # 黑名单URL

    # --------------------------------------------------------
    # 阶段0: 加载配置
    # --------------------------------------------------------
    def _load_sources(self) -> list:
        """加载直播源URL列表"""
        sources = []
        if not os.path.exists(SOURCES_FILE):
            log.warning("sources.txt 不存在: %s", SOURCES_FILE)
            return sources

        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)

        log.info("加载源: %d 个", len(sources))
        return sources

    def _load_blacklist(self):
        """加载黑名单"""
        if not os.path.exists(BLACKLIST_FILE):
            return
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self.blacklist.add(line)
        log.info("黑名单: %d 条", len(self.blacklist))

    def _load_demo(self) -> list:
        """
        解析 demo.txt 为频道模板列表。
        格式:
            分类名,#genre#
            频道名1
            频道名2
            ...
        返回: [{"name": "CCTV-1", "genre": "央视频道"}, ...]
        """
        demo = []
        if not os.path.exists(DEMO_FILE):
            log.warning("demo.txt 不存在: %s", DEMO_FILE)
            return demo

        current_genre = "未分类"
        with open(DEMO_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # 分类行: "📺央视频道,#genre#"
                if ",#genre#" in line:
                    current_genre = line.replace(",#genre#", "").strip()
                    continue

                # 频道名行（可能带URL: "CCTV-1,http://xxx"）
                parts = line.split(",", 1)
                name = parts[0].strip()
                if name:
                    demo.append({"name": name, "genre": current_genre})

        log.info("demo.txt 加载: %d 个频道模板", len(demo))
        return demo

    def _load_alias(self):
        """
        解析 alias.txt。
        格式: 主名,别名1,别名2,re:正则,...
        返回: (alias_map, reverse_alias, regex_patterns)
            alias_map: {别名小写: 主名}
            reverse_alias: {主名: [别名列表]}
            regex_patterns: [(compiled_pattern, 主名)]
        """
        alias_map = {}
        reverse_alias = defaultdict(list)
        regex_patterns = []

        if not os.path.exists(ALIAS_FILE):
            log.warning("alias.txt 不存在: %s", ALIAS_FILE)
            return alias_map, reverse_alias, regex_patterns

        with open(ALIAS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue

                main_name = parts[0]
                aliases = parts[1:]

                for alias in aliases:
                    if not alias:
                        continue

                    if alias.startswith("re:"):
                        # 正则表达式
                        pattern_str = alias[3:]
                        try:
                            pattern = re.compile(pattern_str)
                            regex_patterns.append((pattern, main_name))
                        except re.error as e:
                            log.warning("正则编译失败 [%s]: %s", pattern_str, e)
                    else:
                        # 普通别名
                        alias_lower = alias.strip().lower()
                        alias_map[alias_lower] = main_name
                        reverse_alias[main_name].append(alias)

        log.info("别名加载: %d 个映射, %d 个正则, %d 个标准频道",
                 len(alias_map), len(regex_patterns), len(reverse_alias))
        return alias_map, reverse_alias, regex_patterns

    # --------------------------------------------------------
    # 阶段1: 抓取源
    # --------------------------------------------------------
    def fetch_all_sources(self):
        """并发抓取所有源"""
        log.info("=" * 60)
        log.info("阶段1: 抓取直播源")
        log.info("=" * 60)

        sources = self._load_sources()
        self._load_blacklist()

        if not sources:
            log.error("没有可用的源！")
            return

        all_entries = []
        success_count = 0
        fail_count = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._fetch_single_source, url): url
                for url in sources
            }

            for future in as_completed(futures):
                url = futures[future]
                try:
                    entries = future.result()
                    if entries:
                        all_entries.extend(entries)
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    log.debug("抓取失败 [%s]: %s", url[:60], e)

        # 过滤黑名单
        if self.blacklist:
            before = len(all_entries)
            all_entries = [
                e for e in all_entries
                if e["url"] not in self.blacklist
            ]
            log.info("黑名单过滤: %d → %d", before, len(all_entries))

        self.all_entries = all_entries
        log.info("抓取完成: 成功 %d 源, 失败 %d 源, 共 %d 条URL",
                 success_count, fail_count, len(all_entries))

    def _fetch_single_source(self, url: str) -> list:
        """抓取单个源，返回条目列表"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            }
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            content = resp.text

            # 判断格式
            if "#EXTM3U" in content[:200] or "#EXTINF" in content[:500]:
                return self._parse_m3u(content, url)
            else:
                return self._parse_txt(content, url)

        except Exception as e:
            log.debug("请求失败 [%s]: %s", url[:60], e)
            return []

    def _parse_m3u(self, content: str, source_url: str) -> list:
        """解析 M3U 格式"""
        entries = []
        lines = content.split("\n")
        current_name = None
        current_group = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXTINF"):
                # 提取频道名
                # #EXTINF:-1 tvg-name="CCTV1" group-title="央视",CCTV-1
                name_match = re.search(r',(.+)$', line)
                if name_match:
                    current_name = name_match.group(1).strip()

                # 提取 group-title
                group_match = re.search(r'group-title="([^"]*)"', line)
                if group_match:
                    current_group = group_match.group(1)

            elif line.startswith("#"):
                continue

            elif current_name and (line.startswith("http") or line.startswith("rtmp")):
                entries.append({
                    "name": current_name,
                    "url": line.strip(),
                    "genre": current_group,
                    "source": source_url,
                })
                current_name = None

        return entries

    def _parse_txt(self, content: str, source_url: str) -> list:
        """解析 TXT 格式"""
        entries = []
        current_genre = ""

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # 分类行: "央视频道,#genre#"
            if ",#genre#" in line:
                current_genre = line.replace(",#genre#", "").strip()
                continue

            # 频道行: "CCTV-1,http://xxx" 或 "CCTV-1,http://a#http://b"
            if "," in line:
                parts = line.split(",", 1)
                name = parts[0].strip()
                urls_str = parts[1].strip()

                if not name:
                    continue

                # 多个URL用 # 分隔
                urls = [u.strip() for u in urls_str.split("#") if u.strip()]
                for url in urls:
                    if url.startswith("http") or url.startswith("rtmp"):
                        entries.append({
                            "name": name,
                            "url": url,
                            "genre": current_genre,
                            "source": source_url,
                        })

        return entries

    # --------------------------------------------------------
    # 阶段2: 模板匹配（核心！）
    # --------------------------------------------------------
    def match_template(self):
        """
        将上游源条目匹配到 demo.txt 模板。
        匹配优先级: 精确 > 别名 > 归一化 > 正则 > difflib相似度
        """
        log.info("=" * 60)
        log.info("阶段2: 模板匹配 (只保留demo.txt需要的频道)")
        log.info("=" * 60)

        demo = self._load_demo()
        if not demo:
            log.warning("demo.txt 为空，保留全部频道")
            self.matched_entries = [(e["name"], e) for e in self.all_entries]
            return

        alias_map, reverse_alias, regex_patterns = self._load_alias()

        # ========== 构建索引 ==========

        # demo频道名 → 小写索引
        demo_names_lower = {}
        for d in demo:
            demo_names_lower[d["name"].strip().lower()] = d["name"]

        # 归一化索引
        demo_norm_index = {}
        for d in demo:
            norm = _normalize_name(d["name"])
            if norm:
                demo_norm_index[norm] = d["name"]

        # ★ difflib 用的列表 ★
        demo_name_list = [d["name"].strip() for d in demo]
        demo_norm_list = [_normalize_name(d["name"]) for d in demo]

        # alias → demo 映射
        alias_to_demo = {}
        for alias_lower, standard_name in alias_map.items():
            std_lower = standard_name.strip().lower()
            std_norm = _normalize_name(standard_name)

            target = None
            # 精确
            if std_lower in demo_names_lower:
                target = demo_names_lower[std_lower]
            # 归一化
            elif std_norm and std_norm in demo_norm_index:
                target = demo_norm_index[std_norm]
            # 包含关系
            else:
                for d in demo:
                    d_lower = d["name"].strip().lower()
                    d_norm = _normalize_name(d["name"])
                    if len(std_lower) >= 3 and len(d_lower) >= 3:
                        if std_lower in d_lower or d_lower in std_lower:
                            target = d["name"]
                            break
                    if std_norm and d_norm and len(std_norm) >= 3 and len(d_norm) >= 3:
                        if std_norm in d_norm or d_norm in std_norm:
                            target = d["name"]
                            break

            if target:
                alias_to_demo[alias_lower] = target

        # 正则 → demo 映射
        regex_to_demo = []
        for pattern, standard_name in regex_patterns:
            std_lower = standard_name.strip().lower()
            std_norm = _normalize_name(standard_name)

            target = None
            if std_lower in demo_names_lower:
                target = demo_names_lower[std_lower]
            elif std_norm and std_norm in demo_norm_index:
                target = demo_norm_index[std_norm]
            else:
                for d in demo:
                    d_norm = _normalize_name(d["name"])
                    if std_norm and d_norm:
                        if std_norm in d_norm or d_norm in std_norm:
                            target = d["name"]
                            break

            if target:
                regex_to_demo.append((pattern, target))

        log.info("索引构建: %d 个别名→demo, %d 个正则→demo, "
                 "%d 个归一化key, %d 个demo频道",
                 len(alias_to_demo), len(regex_to_demo),
                 len(demo_norm_index), len(demo_name_list))

        # ========== 遍历所有上游条目进行匹配 ==========
        matched = []
        stats = {
            "exact": 0, "alias": 0, "norm": 0,
            "regex": 0, "difflib": 0, "skip": 0
        }

        for entry in self.all_entries:
            name = entry["name"].strip()
            name_lower = name.lower()
            name_norm = _normalize_name(name)
            matched_demo_name = None

            # 1. 精确匹配
            if name_lower in demo_names_lower:
                matched_demo_name = demo_names_lower[name_lower]
                stats["exact"] += 1

            # 2. 别名匹配
            elif name_lower in alias_to_demo:
                matched_demo_name = alias_to_demo[name_lower]
                stats["alias"] += 1

            # 3. 归一化匹配
            elif name_norm and name_norm in demo_norm_index:
                matched_demo_name = demo_norm_index[name_norm]
                stats["norm"] += 1

            # 4. 正则匹配
            if not matched_demo_name:
                for pattern, target in regex_to_demo:
                    try:
                        if pattern.search(name):
                            matched_demo_name = target
                            stats["regex"] += 1
                            break
                    except Exception:
                        continue

            # 5. ★ difflib 相似度匹配（iptv-api-1 的核心方法）★
            if not matched_demo_name:
                # 先用归一化名匹配（更快，字符串更短）
                if name_norm and len(name_norm) >= 2:
                    close = difflib.get_close_matches(
                        name_norm, demo_norm_list, n=1, cutoff=DIFFLIB_CUTOFF
                    )
                    if close and close[0]:
                        matched_demo_name = demo_norm_index.get(close[0])
                        if matched_demo_name:
                            stats["difflib"] += 1

                # 再用原始名匹配
                if not matched_demo_name and len(name) >= 2:
                    close = difflib.get_close_matches(
                        name, demo_name_list, n=1, cutoff=DIFFLIB_CUTOFF
                    )
                    if close:
                        matched_demo_name = close[0]
                        stats["difflib"] += 1

            # 记录结果
            if matched_demo_name:
                matched.append((matched_demo_name, entry))
            else:
                stats["skip"] += 1

        self.matched_entries = matched

        # ========== 日志输出 ==========
        log.info("匹配结果:")
        log.info("  精确: %d | 别名: %d | 归一化: %d | 正则: %d | difflib: %d",
                 stats["exact"], stats["alias"], stats["norm"],
                 stats["regex"], stats["difflib"])
        log.info("  总匹配: %d | 跳过(不需要): %d", len(matched), stats["skip"])

        # 每频道统计
        channel_count = defaultdict(int)
        for demo_name, _ in matched:
            channel_count[demo_name] += 1

        log.info("  模板频道: %d/%d 个有匹配, 平均每频道 %.1f 条URL",
                 len(channel_count), len(demo),
                 len(matched) / max(len(channel_count), 1))

        # 未匹配频道
        all_demo_names = set(d["name"] for d in demo)
        unmatched = all_demo_names - set(channel_count.keys())
        if unmatched:
            log.warning("  未匹配到URL的频道 (%d): %s",
                        len(unmatched), ", ".join(sorted(unmatched)[:30]))

        # 调试：匹配过少时打印上游样本
        if len(matched) < len(demo):
            log.warning("  ⚠️ 匹配数 < 模板频道数！上游源频道名样本 (前20):")
            sample = list(set(e["name"] for e in self.all_entries[:500]))[:20]
            for s in sample:
                log.warning("    [%s] → norm=[%s]", s, _normalize_name(s))

    # --------------------------------------------------------
    # 阶段3: 去重 + 排序
    # --------------------------------------------------------
    def deduplicate(self):
        """去重：同一频道下相同URL只保留一条"""
        log.info("=" * 60)
        log.info("阶段3: 去重 + 排序")
        log.info("=" * 60)

        # 按 demo 顺序分组
        channel_urls = OrderedDict()  # {demo_name: {url: entry}}
        for demo_name, entry in self.matched_entries:
            if demo_name not in channel_urls:
                channel_urls[demo_name] = OrderedDict()
            url = entry["url"]
            if url not in channel_urls[demo_name]:
                channel_urls[demo_name][url] = entry

        # 每频道限制URL数
        final = []
        for demo_name, url_dict in channel_urls.items():
            urls = list(url_dict.values())[:MAX_URLS_PER_CHANNEL]
            for entry in urls:
                final.append((demo_name, entry))

        self.final_entries = final
        log.info("去重后: %d 条URL, %d 个频道",
                 len(final), len(channel_urls))

    # --------------------------------------------------------
    # 阶段4: 测速（可选）
    # --------------------------------------------------------
    def speed_test(self):
        """对URL进行测速，按速度排序"""
        log.info("=" * 60)
        log.info("阶段4: 测速")
        log.info("=" * 60)

        # 按频道分组
        channel_entries = defaultdict(list)
        for demo_name, entry in self.final_entries:
            channel_entries[demo_name].append(entry)

        # 并发测速
        all_urls = [(demo_name, entry) for demo_name, entry in self.final_entries]
        speed_results = {}

        def test_url(item):
            demo_name, entry = item
            url = entry["url"]
            speed = self._measure_speed(url)
            return (demo_name, url, speed)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(test_url, item) for item in all_urls]
            for future in as_completed(futures):
                try:
                    demo_name, url, speed = future.result()
                    speed_results[(demo_name, url)] = speed
                except Exception:
                    pass

        # 按频道内速度排序
        final_sorted = []
        for demo_name in channel_entries:
            entries = channel_entries[demo_name]
            entries.sort(
                key=lambda e: speed_results.get((demo_name, e["url"]), 0),
                reverse=True
            )
            for entry in entries[:MAX_URLS_PER_CHANNEL]:
                final_sorted.append((demo_name, entry))

        self.final_entries = final_sorted
        alive = sum(1 for v in speed_results.values() if v > 0)
        log.info("测速完成: %d/%d 个URL存活", alive, len(speed_results))

    def _measure_speed(self, url: str) -> float:
        """测量URL下载速度 (bytes/s)，失败返回0"""
        try:
            start = time.time()
            resp = requests.get(
                url,
                stream=True,
                timeout=SPEED_TEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            # 只读前 100KB
            data = resp.raw.read(102400)
            elapsed = time.time() - start
            if elapsed > 0 and len(data) > 0:
                return len(data) / elapsed
            return 0
        except Exception:
            return 0

    # --------------------------------------------------------
    # 阶段5: 输出
    # --------------------------------------------------------
    def output(self):
        """输出 M3U 和 TXT 文件"""
        log.info("=" * 60)
        log.info("阶段5: 输出文件")
        log.info("=" * 60)

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # 按频道分组（保持demo顺序）
        channel_entries = OrderedDict()
        for demo_name, entry in self.final_entries:
            if demo_name not in channel_entries:
                channel_entries[demo_name] = []
            channel_entries[demo_name].append(entry)

        # 获取 genre 信息
        demo = self._load_demo()
        genre_map = {d["name"]: d["genre"] for d in demo}

        # 输出 M3U
        m3u_lines = ["#EXTM3U"]
        for demo_name, entries in channel_entries.items():
            genre = genre_map.get(demo_name, "未分类")
            for entry in entries:
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-name="{demo_name}" '
                    f'group-title="{genre}",{demo_name}'
                )
                m3u_lines.append(entry["url"])

        with open(OUTPUT_M3U, "w", encoding="utf-8") as f:
            f.write("\n".join(m3u_lines))

        # 输出 TXT
        txt_lines = []
        current_genre = None
        for demo_name, entries in channel_entries.items():
            genre = genre_map.get(demo_name, "未分类")
            if genre != current_genre:
                txt_lines.append(f"{genre},#genre#")
                current_genre = genre
            urls = "#".join(e["url"] for e in entries)
            txt_lines.append(f"{demo_name},{urls}")

        with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        log.info("输出完成:")
        log.info("  M3U: %s (%d 行)", OUTPUT_M3U, len(m3u_lines))
        log.info("  TXT: %s (%d 行)", OUTPUT_TXT, len(txt_lines))
        log.info("  频道: %d 个, URL: %d 条",
                 len(channel_entries), len(self.final_entries))

    # --------------------------------------------------------
    # 主流程
    # --------------------------------------------------------
    def run(self, skip_speed_test=False):
        """执行完整流程"""
        log.info("🚀 IPTV 直播源聚合引擎启动")
        log.info("=" * 60)

        start_time = time.time()

        # 阶段1: 抓取
        self.fetch_all_sources()
        if not self.all_entries:
            log.error("没有抓取到任何数据，退出")
            return

        # 阶段2: 模板匹配
        self.match_template()
        if not self.matched_entries:
            log.error("没有匹配到任何频道，退出")
            return

        # 阶段3: 去重
        self.deduplicate()

        # 阶段4: 测速（可选）
        if not skip_speed_test:
            self.speed_test()

        # 阶段5: 输出
        self.output()

        elapsed = time.time() - start_time
        log.info("=" * 60)
        log.info("✅ 全部完成! 耗时 %.1f 秒", elapsed)
        log.info("=" * 60)


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IPTV 直播源聚合引擎")
    parser.add_argument("--no-speed", action="store_true",
                        help="跳过测速阶段")
    args = parser.parse_args()

    engine = IPTVEngine()
    engine.run(skip_speed_test=args.no_speed)
