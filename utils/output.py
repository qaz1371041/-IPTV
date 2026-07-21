from config.settings import M3U_OUTPUT, TXT_OUTPUT
from utils.loaders import load_demo_template
from utils.config import logger

def write_output(valid_channels):
    """
    valid_channels: list of dicts，包含 'name', 'url', 'logo', 'group'
    输出严格按照 demo.txt 的分类和顺序
    """
    demo_cats, demo_ch2cat = load_demo_template()
    # 构建 name -> entry 映射
    name_to_entry = {}
    for ch in valid_channels:
        name_to_entry[ch['name']] = ch
    
    with open(M3U_OUTPUT, 'w', encoding='utf-8') as m3u, open(TXT_OUTPUT, 'w', encoding='utf-8') as txt:
        m3u.write('#EXTM3U\n')
        for cat, ch_list in demo_cats.items():
            for ch_name in ch_list:
                if ch_name in name_to_entry:
                    entry = name_to_entry[ch_name]
                    m3u.write(f'#EXTINF:-1 group-title="{cat}" tvg-logo="{entry.get("logo","")}" tvg-name="{ch_name}",{ch_name}\n')
                    m3u.write(f'{entry["url"]}\n')
                    txt.write(f'{ch_name},{entry["url"]}\n')
    logger.info(f"Output written: {len(name_to_entry)} channels in {M3U_OUTPUT}")
