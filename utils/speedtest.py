"""utils/speedtest.py - 直播源测速、假源过滤与分辨率探测"""
import asyncio
import time
import json
import subprocess

import aiohttp

from config.settings import (
    MAX_CONCURRENCY, SPEEDTEST_BYTES, MIN_SPEED_KBPS,
    FFPROBE_TIMEOUT, STRICT_PROBE, MIN_HEIGHT
)

# 假源 Content-Type 黑名单
_FAKE_CT = ("text/html", "text/plain", "text/xml", "image/", "application/json")


# ─── HTTP 测速 + 假源过滤 ────────────────────────────────────────
async def _test_speed(session, url: str, timeout: int) -> dict:
    t0 = time.monotonic()
    total = 0
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                               ssl=False) as resp:
            if resp.status != 200:
                return {"speed_kbps": 0, "error": f"HTTP {resp.status}"}
            ct = (resp.headers.get("Content-Type", "") or "").lower()
            if any(f in ct for f in _FAKE_CT):
                return {"speed_kbps": 0, "error": f"fake({ct})"}
            async for chunk in resp.content.iter_chunked(8192):
                total += len(chunk)
                if total >= SPEEDTEST_BYTES:
                    break
    except asyncio.TimeoutError:
        return {"speed_kbps": 0, "error": "timeout"}
    except Exception as e:
        return {"speed_kbps": 0, "error": str(e)[:80]}
    elapsed = time.monotonic() - t0
    kbps = (total / 1024 / elapsed) if elapsed > 0 else 0
    return {"speed_kbps": round(kbps, 1), "error": None}


# ─── ffprobe 分辨率探测 ──────────────────────────────────────────
def _probe_resolution(url: str) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0",
             "-timeout", str(FFPROBE_TIMEOUT * 1_000_000), url],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT
        )
        data = json.loads(result.stdout)
        for s in data.get("streams", []):
            w, h = int(s.get("width", 0)), int(s.get("height", 0))
            if w and h:
                return {"width": w, "height": h, "error": None}
        return {"width": 0, "height": 0, "error": "no_video"}
    except subprocess.TimeoutExpired:
        return {"width": 0, "height": 0, "error": "timeout"}
    except Exception as e:
        return {"width": 0, "height": 0, "error": str(e)[:80]}


# ─── 主函数（优化版：防点播超时/OOM） ──────────────────────────────
async def speedtest_all(sources: list, is_vod: bool = False) -> list:
    """
    对所有传入的 SourceEntry 进行测速。
    就地修改 src.speed / src.width / src.height，返回通过的列表。
    """
    if not sources:
        return []

    total = len(sources)
    done = 0
    
    # ★ 优化：点播源降低并发，防止 OOM 和超时
    vod_mode = is_vod
    current_concurrency = 20 if vod_mode else MAX_CONCURRENCY
    current_timeout = 8 if vod_mode else 15  # 点播 8 秒连不上就算了
    
    sem = asyncio.Semaphore(current_concurrency)
    print(f"      ⚙️  当前模式: {'点播(降级并发)' if vod_mode else '直播(全速)'} | 并发: {current_concurrency} | 超时: {current_timeout}s")

    # ═══ 第一轮：HTTP 测速 ═══
    async def _phase1(src):
        nonlocal done
        async with sem:
            # 使用动态超时时间
            r = await _test_speed(session, src.url, timeout=current_timeout)
            done += 1
            if done % 500 == 0 or done == total:
                print(f"      📊 测速进度: {done}/{total}")
            src.speed = r["speed_kbps"] if r["speed_kbps"] >= MIN_SPEED_KBPS else 0

    connector = aiohttp.TCPConnector(
        limit=current_concurrency, 
        limit_per_host=3 if vod_mode else 5, # 点播限制单主机连接数
        force_close=True
    )
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [_phase1(s) for s in sources]
        await asyncio.gather(*tasks, return_exceptions=True)

    speed_passed = [s for s in sources if s.speed > 0]
    print(f"      ✅ HTTP测速: {len(speed_passed)}/{total} 通过")

    # ★ 点播源直接返回，绝对不跑 ffprobe！
    if not speed_passed or vod_mode:
        return speed_passed

    # ═══ 第二轮：ffprobe 分辨率探测（仅直播） ═══
    print(f"      📐 ffprobe 分辨率探测: {len(speed_passed)} 条...")
    probe_done = 0
    probe_sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _phase2(src):
        nonlocal probe_done
        async with probe_sem:
            info = await asyncio.get_event_loop().run_in_executor(
                None, _probe_resolution, src.url)
            probe_done += 1
            if probe_done % 200 == 0 or probe_done == len(speed_passed):
                print(f"      📊 ffprobe 进度: {probe_done}/{len(speed_passed)}")
            if STRICT_PROBE and (info["error"] or info["height"] < MIN_HEIGHT):
                src.speed = 0
                return
            src.width = info.get("width", 0)
            src.height = info.get("height", 0)

    tasks2 = [_phase2(s) for s in speed_passed]
    await asyncio.gather(*tasks2, return_exceptions=True)

    final = [s for s in speed_passed if s.speed > 0]
    print(f"      ✅ ffprobe: {len(final)} 条最终通过")
    return final
