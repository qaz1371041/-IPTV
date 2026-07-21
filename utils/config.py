import asyncio
import aiohttp
import logging
from config.settings import LOG_FILE, MAX_WORKERS

logger = logging.getLogger('iptv')
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(fh)

# 全局 session 池
async def get_session():
    timeout = aiohttp.ClientTimeout(total=10)
    return aiohttp.ClientSession(timeout=timeout)

# 线程池（用于 ffprobe 等阻塞操作）
from concurrent.futures import ThreadPoolExecutor
thread_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
