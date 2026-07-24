"""
模糊匹配器 - 将模板频道与抓取到的源进行智能匹配
"""
import difflib
import re
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def normalize_channel_name(name):
    """标准化频道名称"""
    name = name.strip().lower()
    name = re.sub(r'[-_\s·.．]', '', name)
    name = re.sub(r'(频道|台|高清|hd|fhd|4k|标清|sd)$', '', name)
    return name


def calculate_similarity(name1, name2):
    """计算相似度"""
    n1 = normalize_channel_name(name1)
    n2 = normalize_channel_name(name2)
    return difflib.SequenceMatcher(None, n1, n2).ratio()


def match_channels(template, all_channels, cutoff=0.6, max_matches_per_channel=5):
    """
    模糊匹配
    
    返回: {"分类": {"频道名": [{"name", "url", "source", "similarity"}, ...]}}
    """
    source_names = list(set(ch["name"] for ch in all_channels))
    source_map = defaultdict(list)
    for ch in all_channels:
        source_map[ch["name"]].append(ch)

    # 建立标准化名称 → 原始名称的映射
    norm_to_original = {}
    for sn in source_names:
        norm_to_original[normalize_channel_name(sn)] = sn

    normalized_source_names = list(norm_to_original.keys())

    matched_result = {}
    total_matched = 0
    total_template = 0

    for category, channel_names in template.items():
        matched_result[category] = {}

        for template_name in channel_names:
            total_template += 1
            matches = []

            close_matches = difflib.get_close_matches(
                normalize_channel_name(template_name),
                normalized_source_names,
                n=max_matches_per_channel * 3,
                cutoff=cutoff
            )

            for norm_match in close_matches:
                original_name = norm_to_original.get(norm_match)
                if original_name:
                    similarity = calculate_similarity(template_name, original_name)
                    for ch_data in source_map[original_name]:
                        matches.append({
                            "name": original_name,
                            "url": ch_data["url"],
                            "source": ch_data["source"],
                            "similarity": round(similarity, 4)
                        })

            matches.sort(key=lambda x: x["similarity"], reverse=True)
            matches = matches[:max_matches_per_channel]

            if matches:
                matched_result[category][template_name] = matches
                total_matched += 1
            else:
                matched_result[category][template_name] = []

    logger.info(f"匹配: {total_matched}/{total_template}")
    return matched_result
