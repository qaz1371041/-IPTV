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


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    return s


# ═══════════════════════════════════════════
#  阶段1: 抓取（自动屏蔽死源）
# ═══════════════════════════════════════════

def check_source_alive(url, session):
    try:
        resp = session.get(url, timeout=15, stream=True)
        resp.raise_for_status()
        chunk = resp.raw.read(512)
        resp.close()
        return bool(chunk)
    except Exception:
        return False


def load_sources():
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

        if check_source_alive(stripped, session):
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


def fetch_source(url, session):
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def parse_m3u(text):
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


def parse_txt(text):
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


def load_rules():
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


def apply_rules(entries, rules):
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


# ═══════════════════════════════════════════
#  阶段2: 全量测速 + 分辨率探测
# ═══════════════════════════════════════════

def check_url(item, session):
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


def probe_resolution(url):
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


def test_all(entries):
    session = _session()
    alive = []
    dead_count = 0

    log.info("=" * 50)
    log.info("阶段2: 全量测速 (并发=%d, 超时=%ds)", MAX_WORKERS, TIMEOUT)
    log.info("=" * 50)
    log.info("待测: %d", len(entries))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_url, e, session): e for e in entries}
        done = 0
        for future in as_completed(futures):
            done += 1
            item, speed = future.result()
            if speed > 0:
                alive.append({"item": item, "speed": speed, "resolution": None})
            else:
                dead_count += 1
            if done % 50 == 0 or done == len(entries):
                log.info("  进度: %d/%d  存活: %d  死亡: %d",
                         done, len(entries), len(alive), dead_count)

    log.info("测速完成: 存活 %d / 死亡 %d", len(alive), dead_count)

    if HAS_FFPROBE and alive:
        log.info("-" * 50)
        log.info("分辨率探测 (最低 %dp, 共 %d 个)", MIN_HEIGHT, len(alive))
        log.info("-" * 50)

        def _probe(entry):
            res = probe_resolution(entry["item"]["url"])
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
    return alive


# ═══════════════════════════════════════════
#  阶段3: 分类 (demo.txt 驱动)
# ═══════════════════════════════════════════

def load_demo():
    path = os.path.join(CONFIG, "demo.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return parse_txt(f.read())


def load_alias():
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
    return alias_map


def classify(alive_entries):
    demo = load_demo()
    alias_map = load_alias()

    best = {}
    for a in alive_entries:
        name = a["item"]["name"]
        name = alias_map.get(name, name)
        if name not in best or a["speed"] < best[name]["speed"]:
            best[name] = a["item"]["url"]

    result = []
    current_group = ""
    seen = set()

    for d in demo:
        name = d["name"]
        group = d["group"]

        if group != current_group:
            current_group = group
            seen = set()

        if name in seen:
            continue
        seen.add(name)

        if name in best:
            result.append((group, name, best[name]))

    return result


# ═══════════════════════════════════════════
#  阶段4: 输出（只有活频道）
# ═══════════════════════════════════════════

def write_output(classified):
    os.makedirs(OUTPUT, exist_ok=True)

    txt_path = os.path.join(OUTPUT, "iptv.txt")
    m3u_path = os.path.join(OUTPUT, "iptv.m3u")

    count = 0

    with open(txt_path, "w", encoding="utf-8") as f:
        current_group = ""
        for group, name, url in classified:
            if group != current_group:
                current_group = group
                f.write(f"\n{group},#genre#\n")
            f.write(f"{name},{url}\n")
            count += 1

    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for group, name, url in classified:
            f.write(f'#EXTINF:-1 group-title="{group}",{name}\n')
            f.write(f"{url}\n")

    log.info("输出: %s (%d 个频道)", txt_path, count)
    log.info("输出: %s", m3u_path)
    return count


# ═══════════════════════════════════════════
#  阶段5: EPG
# ═══════════════════════════════════════════

EPG_SOURCES = [
    "https://gitee.com/taksssss/tv/raw/main/epg/112114.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/51zmt.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/old.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/e.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/e2.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/e3.xml.gz",
]


def fetch_epg():
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


# ═══════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════

def run():
    t_start = time.time()

    log.info("=" * 50)
    log.info("阶段1: 抓取 + 死源屏蔽")
    log.info("=" * 50)

    alive_sources, dead_sources = load_sources()

    if not alive_sources:
        log.error("所有源均不可达！")
        return

    session = _session()
    all_entries = []

    for url in alive_sources:
        text = fetch_source(url, session)
        if not text:
            continue
        if "#EXTM3U" in text[:100] or "#EXTINF" in text[:500]:
            entries = parse_m3u(text)
        else:
            entries = parse_txt(text)
        log.info("  [%s]: %d 个频道", url[:50], len(entries))
        all_entries.extend(entries)

    log.info("总计抓取: %d 个条目", len(all_entries))

    rules = load_rules()
    all_entries = apply_rules(all_entries, rules)
    log.info("规则过滤后: %d", len(all_entries))

    seen = set()
    unique = []
    for e in all_entries:
        key = (e["name"], e["url"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    all_entries = unique
    log.info("去重后: %d", len(all_entries))

    alive = test_all(all_entries)

    log.info("=" * 50)
    log.info("阶段3: 分类 (demo.txt 驱动)")
    log.info("=" * 50)
    classified = classify(alive)

    log.info("=" * 50)
    log.info("阶段4: 输出")
    log.info("=" * 50)
    count = write_output(classified)

    log.info("=" * 50)
    log.info("阶段5: EPG")
    log.info("=" * 50)
    fetch_epg()

    elapsed = round(time.time() - t_start, 1)
    log.info("✅ 完成 %.1fs  频道: %d  屏蔽源: %d", elapsed, count, len(dead_sources))
