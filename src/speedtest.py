import requests, m3u8, time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings()

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
VOD_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.flv', '.wmv', '.mov', '.ts', '.mp3')

def is_vod_url(url):
    path = url.split('?')[0].lower()
    return any(path.endswith(ext) for ext in VOD_EXTENSIONS)

def test_one(url, timeout=8):
    result = {'playable': False, 'speed': 0.0, 'resolution': (0, 0), 'is_vod': is_vod_url(url)}
    try:
        if result['is_vod']:
            resp = requests.head(url, headers=HEADERS, timeout=timeout, verify=False, allow_redirects=True)
            if resp.status_code == 200:
                result['playable'] = True
                result['speed'] = 500.0
            return result

        resp = requests.get(url, headers=HEADERS, timeout=timeout, stream=True, verify=False, allow_redirects=True)
        if resp.status_code != 200: return result

        ctype = resp.headers.get('Content-Type', '')
        is_hls = ('mpegurl' in ctype or '.m3u8' in url.split('?')[0])

        if is_hls:
            m3u8_obj = m3u8.loads(resp.text, uri=url)
            if m3u8_obj.playlists:
                best = max(m3u8_obj.playlists, key=lambda p: (p.stream_info.resolution or (0, 0))[1])
                if best.stream_info.resolution: result['resolution'] = best.stream_info.resolution
                sub_resp = requests.get(best.absolute_uri, headers=HEADERS, timeout=timeout, verify=False)
                m3u8_obj = m3u8.loads(sub_resp.text, uri=best.absolute_uri)
            
            if m3u8_obj.segments:
                total_bytes = 0
                t0 = time.time()
                for seg in m3u8_obj.segments[:2]:
                    seg_resp = requests.get(seg.absolute_uri, headers=HEADERS, timeout=timeout, stream=True, verify=False)
                    total_bytes += len(seg_resp.raw.read(512 * 1024))
                    seg_resp.close()
                elapsed = time.time() - t0
                if elapsed > 0 and total_bytes > 0:
                    result['speed'] = round((total_bytes / 1024) / elapsed, 1)
                    result['playable'] = True
        else:
            total = 0
            t0 = time.time()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total >= 1024 * 1024 or (time.time() - t0) > timeout: break
            elapsed = time.time() - t0
            if elapsed > 0 and total > 0:
                result['speed'] = round((total / 1024) / elapsed, 1)
                result['playable'] = True
    except:
        pass
    return result

def batch_speedtest(matched, max_sources, timeout, threads):
    results = {}
    for category, channels in matched.items():
        results[category] = {}
        for ch_name, sources in channels.items():
            srcs = sources[:max_sources]
            if not srcs:
                results[category][ch_name] = []
                continue
            
            tested = []
            with ThreadPoolExecutor(max_workers=threads) as pool:
                futures = {pool.submit(test_one, url, timeout): url for _, url in srcs}
                for f in as_completed(futures):
                    url = futures[f]
                    try: r = f.result()
                    except: r = {'playable': False, 'speed': 0, 'resolution': (0,0), 'is_vod': False}
                    tested.append({'url': url, 'result': r})
            
            tested.sort(key=lambda x: x['result']['speed'], reverse=True)
            results[category][ch_name] = tested
    return results
