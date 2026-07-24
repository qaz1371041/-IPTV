"""
全局配置
"""

# ===== 模板 =====
TEMPLATE_FILE = "demo.txt"

# ===== 源 =====
SOURCES_FILE = "config/sources.txt"
MAX_FETCH_WORKERS = 20

# ===== 匹配 =====
MATCH_CUTOFF = 0.6
MAX_MATCHES_PER_CHANNEL = 5

# ===== 测速 =====
SPEED_TEST_WORKERS = 10
SPEED_TEST_TIMEOUT = 10
USE_FFPROBE = True

# ===== 分辨率 =====
MIN_RESOLUTION_HEIGHT = 720

# ===== 跳过测速的关键词（电影/演唱会等不测）=====
SKIP_TEST_KEYWORDS = [
    "电影", "演唱会", "综艺", "纪录片", "动画", "少儿",
    "剧场", "影院", "点播", "回放", "影视", "剧集",
    "MTV", "MV", "音乐", "卡拉OK", "动漫", "卡通"
]

# ===== 输出 =====
OUTPUT_DIR = "output"

# ===== 日志 =====
LOG_LEVEL = "INFO"
LOG_FILE = "iptv.log"
