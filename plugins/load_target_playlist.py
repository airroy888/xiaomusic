import logging
import urllib.parse
import json
import asyncio
import os

xiaomusic = None 
log = logging.getLogger("xiaomusic")

# ================= 配置区域 =================
API_URL = "http://netease-api:3000"
COOKIE_FILE = "/app/conf/netease.txt"
DATA_FILE = "/app/conf/music_list.json"

async def fetch_json(session, url):
    """通用网络请求工具"""
    try:
        async with session.get(url, timeout=30) as response:
            return await response.json()
    except Exception as e:
        log.error(f"请求失败: {url}, 错误: {e}")
        return None

def get_netease_cookie():
    """读取本地网易云 Cookie"""
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except: pass
    return ""

# =========================================================
# 插件入口：加载网易云歌单
# 指令示例: exec#load_target_playlist('歌单ID', '自定义名称')
# =========================================================
async def load_target_playlist(playlist_id, custom_name="默认歌单"):
    global xiaomusic
    log.info(f"插件执行: 加载歌单 ID: {playlist_id}")
    
    raw_cookie = get_netease_cookie()
    encoded_cookie = urllib.parse.quote(raw_cookie)
    
    try:
        # 1. 请求歌单详情
        detail_url = f"{API_URL}/playlist/detail?id={playlist_id}&cookie={encoded_cookie}"
        data = await fetch_json(xiaomusic.session, detail_url)
        
        if not data or data.get('code') != 200 or 'playlist' not in data:
            return "歌单解析失败"

        tracks = data['playlist']['tracks']
        musics_payload = []
        song_names_list = []
        
        # 2. 解析歌曲信息
        for t in tracks:
            tid = str(t['id'])
            song_name = t['name']
            ar_name = "未知"
            if 'ar' in t and t['ar']: ar_name = t['ar'][0]['name']
            elif 'artists' in t and t['artists']: ar_name = t['artists'][0]['name']
            
            # 去除文件名中的非法字符，防止保存文件失败
            full_name = f"{song_name}-{ar_name}".replace("/", "&").replace("\\", "&").replace("'", "").replace('"', "")
            
            musics_payload.append({
                "name": full_name,
                "url": "",  # 空 URL，依靠本地下载播放
                "id": tid
            })
            song_names_list.append(full_name)
        
        # 3. 写入 JSON 文件
        try:
            current_data = []
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    try: current_data = json.load(f)
                    except: current_data = []
            
            # 覆盖同名歌单
            new_data = [pl for pl in current_data if pl.get('name') != custom_name]
            new_data.append({"name": custom_name, "musics": musics_payload})
            
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, ensure_ascii=False, indent=4)
            
            # 4. 刷新内存并保存配置
            xiaomusic.music_list[custom_name] = song_names_list
            if hasattr(xiaomusic, 'save_cur_config'):
                xiaomusic.save_cur_config()
                
            log.info(f"✅ 歌单更新完成: {custom_name} ({len(musics_payload)}首)")
            
        except Exception as e:
            log.error(f"❌ 保存失败: {e}")
            return f"保存失败: {str(e)}"

        return f"更新完成: {len(musics_payload)}首"

    except Exception as e:
        log.exception(f"加载出错: {e}")
        return f"加载出错: {str(e)}"