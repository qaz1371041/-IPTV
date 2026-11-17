"""
⚙️  统一配置参数
"""
import os

# ─── 路径 ───────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

SOURCES_FILE   = os.path.join(CONFIG_DIR, "sources.txt")
DEMO_FILE      = os.path.join(CONFIG_DIR, "demo.txt")
BLACKLIST_FILE = os.path.join(CONFIG_DIR, "blacklist.txt")

OUTPUT_M3U = os.path.join(OUTPUT_DIR, "live.m3u")
OUTPUT_TXT = os.path.join(OUTPUT_DIR, "live.txt")

# ─── 网络 ───────────────────────────────────────────
MAX_CONCURRENCY   = 50
REQUEST_TIMEOUT   = 10
SPEEDTEST_BYTES   = 1_048_576
MIN_SPEED_KBPS    = 50

# ─── 分辨率 & ffprobe ───────────────────────────────
MIN_HEIGHT        = 720
FFPROBE_TIMEOUT   = 15
FFPROBE_RETRY     = 1

# ─── 严格模式 ───────────────────────────────────────
STRICT_PROBE      = True

# ─── 自动 VOD 判断关键词 ─────────────────────────────
AUTO_VOD_KEYWORDS = [
    "电影", "剧场", "影院", "轮播", "专区", "系列",
    "演唱会", "点播", "追剧", "影视", "动漫",
    "咪咕播",
    "淘剧", "淘娱乐", "淘电影",
    "埋堆堆", "不挤影院", "私人影院", "放映厅",
    "欢笑影院", "喜乐影院", "蘑菇影厅", "嫣然影厅",
    "NewTV",
    "电视剧三", "电视剧四", "电视剧七", "电视剧八", "电视剧十一",
]

# ─── 输出 ───────────────────────────────────────────
CUSTOM_SOURCE_URL = "http://xjj1.716888.xyz/fenlei/4k/4k.php"
