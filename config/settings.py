import os
import pathlib

BASE_DIR = pathlib.Path(__file__).parent.parent.absolute()
CONFIG_DIR = BASE_DIR / 'config'
OUTPUT_DIR = BASE_DIR / 'output'
OUTPUT_DIR.mkdir(exist_ok=True)

# 输入文件
SOURCES_FILE = CONFIG_DIR / 'sources.txt'
EPG_SOURCES_FILE = CONFIG_DIR / 'epg.txt'
ALIAS_FILE = CONFIG_DIR / 'alias.txt'
DEMO_FILE = CONFIG_DIR / 'demo.txt'
BLACKLIST_FILE = CONFIG_DIR / 'blacklist.txt'
WHITELIST_FILE = CONFIG_DIR / 'whitelist.txt'
SOURCE_CAT_FILE = CONFIG_DIR / 'source-cat.txt'
CHANNEL_MODEL_FILE = CONFIG_DIR / 'Channel_model.txt'
ICONS_INDEX_FILE = CONFIG_DIR / 'icons_index.txt'

# 输出文件
M3U_OUTPUT = OUTPUT_DIR / 'live.m3u'
TXT_OUTPUT = OUTPUT_DIR / 'live.txt'
EPG_OUTPUT = OUTPUT_DIR / 'epg.xml.gz'
AI_CACHE_FILE = OUTPUT_DIR / 'ai_cache.json'
STATE_FILE = OUTPUT_DIR / 'ci_state.json'

# 测速配置
MAX_WORKERS = 50
MIN_BANDWIDTH_KBPS = 2048       # 2 Mbps = 256 KB/s
SPEEDTEST_DURATION = 2          # 下载测试持续时间（秒）
MPEG_SYNC_BYTE = 0x47
FFPROBE_TIMEOUT = 5

# AI 配置（使用 NVIDIA NIM 免费额度，可更换）
AI_ENABLED = True
AI_API_KEY = os.environ.get('NVIDIA_API_KEY', '')
AI_MODEL = "meta/llama-3.1-70b-instruct"
AI_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# 日志
LOG_FILE = OUTPUT_DIR / 'pipeline.log'

# 其他开关
AUTO_APPEND_UNKNOWNS = False   # 是否自动将未知频道追加到 demo.txt
