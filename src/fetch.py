import requests, time, re
import urllib3
urllib3.disable_warnings()

def fetch_all_sources(sources_file, timeout=15):
    with open(sources_file, 'r', encoding='utf-8') as f:
        raw_lines = f.readlines()

    all_channels = []
    new_lines = [] 
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue

        url_match = re.search(r'(https?://[^\s]+)', stripped)
        if not url_match:
            new_lines.append(line)
            continue
            
        url = url_match.group(1)
        print(f"   抓取: {url[:60]}...")
        
        is_valid = False
        try:
            resp = requests.get(url, headers=headers, timeout=timeout+5, verify=False)
            resp.encoding = 'utf-8'
            if resp.status_code == 200 and len(resp.text) > 50:
                is_valid = True
                channels = _parse_content(resp.text)
                print(f"     ✓ {len(channels)} 条")
                all_channels.extend(channels)
            else:
                print(f"     ✗ HTTP {resp.status_code}")
        except Exception as e:
            print(f"     ✗ 失败: {str(e)[:30]}")

        # 核心：失效加 #，有效保持原样
        if is_valid:
            new_lines.append(line)
        else:
            new_lines.append(f"# {stripped}\n")
        time.sleep(0.3)

    with open(sources_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print(f"   ✓ {sources_file} 已更新")
    return all_channels

def _parse_content(content):
    channels = []
    if '#EXTINF' in content:
        lines = content.strip().split('\n')
        name = None
        for line in lines:
            line = line.strip()
            if line.startswith('#EXTINF:'):
                tvg_name = re.search(r'tvg-name="([^"]+)"', line)
                if tvg_name: name = tvg_name.group(1)
                else:
                    parts = line.split(',')
                    name = parts[-1].strip() if len(parts) >= 2 else "Unknown"
            elif line and not line.startswith('#') and name:
                channels.append((name, line))
                name = None
    else:
        for line in content.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('#') or line.endswith(',#genre#'): continue
            if ',' in line:
                parts = line.split(',', 1)
                name = parts[0].strip()
                url = parts[1].strip()
                if url.startswith('http'):
                    channels.append((name, url))
    return channels
