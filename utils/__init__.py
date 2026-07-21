import asyncio
import logging
import os
import pathlib
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import aiohttp

# ---------- 全局路径 ----------
BASE_DIR = pathlib.Path(__file__).parent.parent.absolute()
CONFIG_DIR = BASE_DIR / 'config'
OUTPUT_DIR = BASE_DIR / 'output'
OUTPUT_DIR.mkdir(exist_ok=True)

SOURCES_FILE = CONFIG_DIR / 'sources.txt'
EPG_SOURCES_FILE = CONFIG_DIR / 'epg.txt'
ALIAS_FILE = CONFIG_DIR / 'alias.txt'
DEMO_FILE = CONFIG_DIR / 'demo.txt'
BLACKLIST_FILE = CONFIG_DIR / 'blacklist.txt'
WHITELIST_FILE = CONFIG_DIR / 'whitelist.txt'

M3U_OUTPUT = OUTPUT_DIR / 'live.m3u'
TXT_OUTPUT = OUTPUT_DIR / 'live.txt'
EPG_OUTPUT = OUTPUT_DIR / 'epg.xml.gz'
AI_CACHE_FILE = OUTPUT_DIR / 'ai_cache.json'
STATE_FILE = OUTPUT_DIR / 'ci_state.json'

# ---------- 测速配置 ----------
MAX_WORKERS = 50
MIN_BANDWIDTH_KBPS = 2048          # 2 Mbps
SPEEDTEST_DURATION = 2             # 秒
MPEG_SYNC_BYTE = 0x47
FFPROBE_TIMEOUT = 5

# ---------- AI 配置（可选） ----------
AI_ENABLED = bool(os.environ.get('NVIDIA_API_KEY'))
AI_API_KEY = os.environ.get('NVIDIA_API_KEY', '')
AI_MODEL = "meta/llama-3.1-70b-instruct"
AI_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('iptv')

# ---------- 异步工具 ----------
async def get_session():
    timeout = aiohttp.ClientTimeout(total=15)
    return aiohttp.ClientSession(timeout=timeout)

thread_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------- 文件加载器 ----------
def load_lines(path):
    if not path.exists():
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return [
            line.strip() for line in f
            if line.strip() and not line.startswith('#')
        ]

def load_alias_map():
    """返回 {标准名: [别名列表]}"""
    alias = {}
    for line in load_lines(ALIAS_FILE):
        if ':' in line:
            std, names = line.split(':', 1)
            alias[std.strip()] = [n.strip() for n in names.split('|') if n.strip()]
    return alias

def load_blacklist():
    return set(load_lines(BLACKLIST_FILE))

def load_whitelist():
    whitelist = set(load_lines(WHITELIST_FILE))
    return whitelist if whitelist else None

def load_demo_template():
    """解析 demo.txt → (OrderedDict{分类:[频道名]}, dict{频道名:分类})"""
    categories = OrderedDict()
    ch_to_cat = {}
    current_cat = None
    if not DEMO_FILE.exists():
        logger.warning("demo.txt not found")
        return categories, ch_to_cat
    with open(DEMO_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 分类头：分类名,#genre# 或 # 分类名,#genre#
            cat_match = re.match(r'^(?:#\s*)?(.+?),#genre#$', line)
            if cat_match:
                current_cat = cat_match.group(1).strip()
                categories[current_cat] = []
            elif current_cat:
                ch_name = re.sub(r'#.*', '', line).strip()
                if ch_name:
                    categories[current_cat].append(ch_name)
                    ch_to_cat[ch_name] = current_cat
    return categories, ch_to_cat
