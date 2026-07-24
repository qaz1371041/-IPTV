"""
测速模块 - ffprobe 测速 + 可播放性验证
"""
import subprocess
import json
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import SKIP_TEST_KEYWORDS, SPEED_TEST_TIMEOUT

logger = logging.getLogger(__name__)


def should_skip_channel(channel_name):
    """判断是否跳过（电影/演唱会等不测速）"""
    for keyword in SKIP_TEST_KEYWORDS:
        if keyword in channel_name:
            return True
    return False


def test_speed_ffprobe(url, timeout=None):
    """使用 ffprobe 测试"""
    if timeout is None:
        timeout = SPEED_TEST_TIMEOUT

    result = {
        "playable": False,
        "speed": 0,
        "resolution": (0, 0),
        "response_time": 0,
        "error": None
    }

    start_time = time.time()

    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-rw_timeout", f"{timeout * 1000000}",
            "-timeout", f"{timeout * 1000000}",
            url
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5
        )

        result["response_time"] = round(time.time() - start_time, 3)

        if proc.returncode == 0 and proc.stdout:
            try:
                info = json.loads(proc.stdout)

                for stream in info.get("streams", []):
                    if stream.get("codec_type") == "video":
                        width = int(stream.get("width", 0))
                        height = int(stream.get("height", 0))
                        result["resolution"] = (width, height)
                        result["playable"] = True
                        break

                format_info = info.get("format", {})
                bit_rate = format_info.get("bit_rate")
                if bit_rate:
                    result["speed"] = int(bit_rate) // 1000

                if result["speed"] == 0:
                    for stream in info.get("streams", []):
                        if stream.get("codec_type") == "video":
                            br = stream.get("bit_rate")
                            if br:
                                result["speed"] = int(br) // 1000
                                break

                if not result["playable"]:
                    result["error"] = "无视频流"

            except json.JSONDecodeError:
                result["error"] = "JSON解析失败"
        else:
            result["error"] = proc.stderr[:200] if proc.stderr else "ffprobe错误"

    except subprocess.TimeoutExpired:
        result["response_time"] = timeout
        result["error"] = "超时"
    except FileNotFoundError:
        result["error"] = "ffprobe未安装"
    except Exception as e:
        result["error"] = str(e)

    return result


def test_speed_requests(url, timeout=None):
    """备用：requests 连通性测试"""
    if timeout is None:
        timeout = SPEED_TEST_TIMEOUT

    result = {
        "playable": False,
        "speed": 0,
        "resolution": (0, 0),
        "response_time": 0,
        "error": None
    }

    start_time = time.time()

    try:
        headers = {"User-Agent": "Mozilla/5.0", "Range": "bytes=0-2048"}
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        result["response_time"] = round(time.time() - start_time, 3)

        if resp.status_code in (200, 206):
            result["playable"] = True
            content_length = len(resp.content)
            if result["response_time"] > 0:
                result["speed"] = int(content_length * 8 / result["response_time"] / 1000)
        else:
            result["error"] = f"HTTP {resp.status_code}"

    except requests.Timeout:
        result["response_time"] = timeout
        result["error"] = "超时"
    except Exception as e:
        result["error"] = str(e)

    return result


def speed_test_channels(matched_result, max_workers=10, use_ffprobe=True):
    """对所有匹配频道测速"""
    test_func = test_speed_ffprobe if use_ffprobe else test_speed_requests

    test_tasks = []
    for category, channels in matched_result.items():
        for template_name, matches in channels.items():
            if should_skip_channel(template_name):
                logger.info(f"  ⏭ 跳过: {template_name}")
                continue
            for match in matches:
                test_tasks.append({
                    "category": category,
                    "template_name": template_name,
                    "match": match
                })

    logger.info(f"测速: {len(test_tasks)} 条, 并发: {max_workers}")

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for task in test_tasks:
            future = executor.submit(test_func, task["match"]["url"])
            futures[future] = task

        completed = 0
        for future in as_completed(futures):
            completed += 1
            task = futures[future]
            task["speed_result"] = future.result()
            results.append(task)

            if completed % 20 == 0:
                logger.info(f"  进度: {completed}/{len(test_tasks)}")

    logger.info(f"测速完成: {len(results)} 条")
    return results
