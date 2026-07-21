from utils import load_demo_template, M3U_OUTPUT, TXT_OUTPUT, logger

def write_output(channels):
    """channels: [{'name': str, 'url': str, 'logo': str, 'group': str}]"""
    demo_cats, demo_ch2cat = load_demo_template()
    name_dict = {ch['name']: ch for ch in channels}

    with open(M3U_OUTPUT, 'w', encoding='utf-8') as m3u, \
         open(TXT_OUTPUT, 'w', encoding='utf-8') as txt:
        m3u.write('#EXTM3U\n')
        for cat, ch_list in demo_cats.items():
            for ch_name in ch_list:
                if ch_name in name_dict:
                    entry = name_dict[ch_name]
                    m3u.write(
                        f'#EXTINF:-1 group-title="{cat}" '
                        f'tvg-logo="{entry["logo"]}" tvg-name="{ch_name}",{ch_name}\n'
                    )
                    m3u.write(f'{entry["url"]}\n')
                    txt.write(f'{ch_name},{entry["url"]}\n')
    logger.info(f"Output written: {len(name_dict)} channels to {M3U_OUTPUT}")
