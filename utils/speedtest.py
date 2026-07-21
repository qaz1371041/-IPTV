import asyncio
import aiohttp
import time
import struct
import subprocess
from config.settings import *
from utils.config import get_session, thread_pool, logger

async def test_single_channel(session, entry):
    """测速 + MPEG 同步字节校验 + 分辨率探测，返回有效频道或 None"""
    url = entry['url']
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            # 读取数据并测速
            start = time.time()
            data = b''
            total = 0
            async for chunk in resp.content.iter_chunked(4096):
                data += chunk
                total += len(chunk)
                if time.time() - start >= SPEEDTEST_DURATION:
                    break
            duration = time.time() - start
            if duration == 0 or total == 0:
                return None
            bitrate = (total * 8) / (duration * 1000)  # kbps
            if bitrate < MIN_BANDWIDTH_KBPS:
                return None
            # MPEG-TS 同步字节校验 (可选，针对 .ts 流)
            if b'TS' in data[:4] or data[0] == 0x47:
                # 简单检查第一个字节
                if data[0] != MPEG_SYNC_BYTE:
                    return None
            # 分辨率探测
            resolution = await detect_resolution(url)
            entry['bitrate'] = bitrate
            entry['resolution'] = resolution
            return entry
    except Exception as e:
        return None

async def detect_resolution(url):
    """使用 ffprobe 探测视频分辨率"""
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=width,height', '-of', 'csv=p=0', url]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=FFPROBE_TIMEOUT)
        if stdout:
            w, h = stdout.decode().strip().split(',')
            return f"{w}x{h}"
    except:
        pass
    return ''

async def speedtest_entries(entries, blacklist=None, whitelist=None):
    """并发测速，返回有效频道列表"""
    blacklist = blacklist or set()
    whitelist = whitelist or set()
    valid = []
    async with await get_session() as session:
        tasks = []
        for entry in entries:
            # 黑/白名单过滤
            if whitelist:
                if entry['raw_name'] not in whitelist and entry['url'] not in whitelist:
                    continue
            elif entry['raw_name'] in blacklist or entry['url'] in blacklist:
                continue
            tasks.append(test_single_channel(session, entry))
        
        # 服务器级预筛：按 host 分组，抽样测试
        # (简化实现，直接全量测试)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, dict):
                valid.append(res)
    logger.info(f"Speedtest done: {len(valid)} valid channels out of {len(entries)}")
    return valid
