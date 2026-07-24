"""
输出生成器
- 输出文件只保留可播放链接（按速度排序）
- 失效源只在 config/sources.txt 中标记 #
"""
import os
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def check_upstream_alive(source_url, timeout=5):
    """检查上游源是否存活"""
    import requests
    try:
        resp = requests.head(source_url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 400
    except:
        return False


def mark_dead_sources_in_file(dead_sources, sources_file="config/sources.txt"):
    """在 sources.txt 中给失效源加 #"""
    if not dead_sources or not os.path.exists(sources_file):
        return

    with open(sources_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated = False
    new_lines = []

    for line in lines:
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        if stripped in dead_sources:
            new_lines.append(f"# {stripped}  # [失效] 自动标记\n")
            updated = True
            logger.info(f"  ⚠️ 标记失效: {stripped}")
        else:
            new_lines.append(line)

    if updated:
        with open(sources_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        logger.info(f"  sources.txt 已更新")


def generate_output(speed_test_results, template, output_dir="output", sources_file="config/sources.txt"):
    """
    生成输出:
    - 只输出可播放链接
    - 按速度排序（快→慢）
    - 失效源只在 sources.txt 标记
    """
    os.makedirs(output_dir, exist_ok=True)

    # 按频道分组
    channel_results = defaultdict(list)
    for item in speed_test_results:
        key = (item["category"], item["template_name"])
        channel_results[key].append(item)

    # 按速度排序
    for key in channel_results:
        channel_results[key].sort(
            key=lambda x: x.get("speed_result", {}).get("speed", 0),
            reverse=True
        )

    # 检查上游存活
    upstream_cache = {}
    dead_sources = set()

    def is_upstream_alive(source_url):
        if not source_url:
            return True
        if source_url not in upstream_cache:
            alive = check_upstream_alive(source_url)
            upstream_cache[source_url] = alive
            if not alive:
                dead_sources.add(source_url)
        return upstream_cache[source_url]

    # 生成输出
    txt_path = os.path.join(output_dir, "result.txt")
    m3u_path = os.path.join(output_dir, "result.m3u")

    txt_lines = []
    m3u_lines = ["#EXTM3U"]
    playable_count = 0

    for category, channel_names in template.items():
        txt_lines.append(f"{category},#genre#")

        for channel_name in channel_names:
            key = (category, channel_name)
            items = channel_results.get(key, [])

            if not items:
                continue

            for item in items:
                sr = item.get("speed_result", {})
                url = item["match"]["url"]
                source = item["match"].get("source", "")
                speed = sr.get("speed", 0)
                resolution = sr.get("resolution", (0, 0))
                playable = sr.get("playable", False)

                # 不可播放 → 不输出
                if not playable:
                    continue

                # 上游失效 → 不输出
                if not is_upstream_alive(source):
                    continue

                # ✅ 可播放 + 上游正常 → 输出
                res_str = f"{resolution[0]}x{resolution[1]}" if resolution[0] > 0 else ""

                txt_lines.append(f"{channel_name},{url}")

                display = channel_name
                if res_str:
                    display += f" | {res_str}"
                if speed:
                    display += f" | {speed}kbps"
                m3u_lines.append(f"#EXTINF:-1 group-title=\"{category}\",{display}")
                m3u_lines.append(url)
                playable_count += 1

    # 写入文件
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))

    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_lines))

    # 在 sources.txt 中标记失效源
    if dead_sources:
        logger.info(f"标记 {len(dead_sources)} 个失效源到 sources.txt...")
        mark_dead_sources_in_file(dead_sources, sources_file)

    logger.info(f"输出: {playable_count} 条可播放")
    logger.info(f"  TXT: {txt_path}")
    logger.info(f"  M3U: {m3u_path}")
    if dead_sources:
        logger.info(f"  失效源: {len(dead_sources)} 个 (已标记在 sources.txt)")

    return m3u_path, txt_path
