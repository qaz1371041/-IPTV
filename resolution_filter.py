"""
分辨率过滤器 - 去掉720p以下
"""
import logging

logger = logging.getLogger(__name__)


def filter_by_resolution(speed_test_results, min_height=720):
    """过滤低于 min_height 的源"""
    filtered = []
    removed = 0

    for item in speed_test_results:
        sr = item.get("speed_result", {})
        resolution = sr.get("resolution", (0, 0))
        width, height = resolution

        # 无法获取分辨率但可播放 → 保留
        if width == 0 and height == 0:
            if sr.get("playable"):
                filtered.append(item)
            continue

        if height >= min_height:
            filtered.append(item)
        else:
            removed += 1

    logger.info(f"分辨率过滤: 移除 {removed} 条(<{min_height}p), 保留 {len(filtered)} 条")
    return filtered
