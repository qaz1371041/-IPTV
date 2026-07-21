import asyncio
import time
import subprocess
from utils import (
    get_session, thread_pool, logger,
    MAX_WORKERS, MIN_BANDWIDTH_KBPS, SPEEDTEST_DURATION,
    MPEG_SYNC_BYTE, FFPROBE_TIMEOUT
)

async def detect_resolution(url):
    """使用 ffprobe 探测分辨率"""
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'csv=p=0', url
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=FFPROBE_TIMEOUT)
        if stdout:
            w, h = stdout.decode().strip().split(',')
            return f"{w}x{h}"
    except:
        pass
    return ''

async def test_channel(session, entry):
    """测速 + 同步字节校验 + 分辨率，返回有效频道或 None"""
    url = entry['url']
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = b''
            total = 0
            start = time.time()
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
            # 简单 MPEG-TS 同步字节校验
            if data and data[0] != MPEG_SYNC_BYTE and b'TS' in data[:4]:
                return None

            resolution = await detect_resolution(url)
            entry['bitrate'] = bitrate
            entry['resolution'] = resolution
            return entry
    except:
        return None

async def speedtest_entries(entries, blacklist, whitelist):
    """并发测速，返回有效频道列表"""
    valid = []
    async with await get_session() as session:
        tasks = []
        for entry in entries:
            name = entry['raw_name']
            url = entry['url']
            # 白名单优先
            if whitelist is not None:
                if name not in whitelist and url not in whitelist:
                    continue
            else:
                if name in blacklist or url in blacklist:
                    continue
            tasks.append(test_channel(session, entry))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, dict):
                valid.append(res)
    logger.info(f"Speedtest done: {len(valid)} valid out of {len(entries)}")
    return valid
