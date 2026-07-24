"""
模板解析器 - 解析 demo.txt
"""
import os
import logging

logger = logging.getLogger(__name__)


def parse_template(template_file="demo.txt"):
    """
    解析 demo.txt
    
    返回: {"央视频道": ["CCTV-1", "CCTV-2", ...], ...}
    """
    if not os.path.exists(template_file):
        raise FileNotFoundError(f"模板文件不存在: {template_file}")

    template = {}
    current_category = None

    with open(template_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if ",#genre#" in line:
                current_category = line.replace(",#genre#", "").strip()
                if current_category not in template:
                    template[current_category] = []
            else:
                if current_category is None:
                    current_category = "未分类"
                    template[current_category] = []
                channel_name = line.strip()
                if channel_name:
                    template[current_category].append(channel_name)

    total = sum(len(v) for v in template.values())
    logger.info(f"模板解析: {len(template)} 分类, {total} 频道")
    return template
