import logging
import asyncio
import urllib.parse
import json
import os
import re
import random
import time
import gc 

xiaomusic = None
log = logging.getLogger("xiaomusic")

# ================= 配置区域 =================
CACHE_DIR_NAME = "download"
CACHE_DIR_ABS = f"/app/music/{CACHE_DIR_NAME}"
TEMP_LIST_NAME = "临时搜索列表" # 内存里的临时歌单名

# 网易云 API 配置
NETEASE_API_URL = "http://netease-api:3000"
PROXY_PARAM = "&level=exhigh&proxy=http://unblock-netease:8080"

async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=15) as response:
            return await response.json()
    except Exception as e:
        log.warning(f"Fetch Json Error: {e}")
        return None

def ensure_cache_dir():
    try:
        if not os.path.exists(CACHE_DIR_ABS):
            os.makedirs(CACHE_DIR_ABS)
    except: pass

def get_safe_filename(song_name):
    safe = re.sub(r'[\\/*?:"<>|]', "", song_name)
    return f"{safe}.mp3"

async def search_song_netease(keyword):
    global xiaomusic
    try:
        encoded_kw = urllib.parse.quote(keyword)
        url = f"{NETEASE_API_URL}/cloudsearch?keywords={encoded_kw}&type=1{PROXY_PARAM}"
        data = await fetch_json(xiaomusic.session, url)
        
        if not data or 'result' not in data or 'songs' not in data['result']: return None
        songs = data['result']['songs']
        if not songs: return None
            
        top_song = songs[0]
        artist = top_song['ar'][0]['name'] if top_song.get('ar') else "未知"
        song_name = top_song['name']
        full_name = f"{song_name}-{artist}"
        full_name = full_name.replace("/", "&").replace("\\", "&").replace("'", "").replace('"', "")
        
        return {"id": str(top_song['id']), "name": full_name}
    except Exception as e:
        log.error(f"搜索失败: {e}")
        return None

# =========================================================
# 【V8.0 核心】纯内存注入，不修改 setting.json
# =========================================================
def inject_to_memory_list(song_name):
    """只把歌曲名塞进主程序的内存列表，不保存到文件"""
    global xiaomusic
    try:
        if not xiaomusic: return False
        
        # 1. 确保内存里有这个临时列表
        if TEMP_LIST_NAME not in xiaomusic.music_list:
            xiaomusic.music_list[TEMP_LIST_NAME] = []
            
        current_list = xiaomusic.music_list[TEMP_LIST_NAME]
        
        # 2. 如果歌不在列表里，插到最前面
        if song_name not in current_list:
            current_list.insert(0, song_name)
            
        # 3. 限制内存列表长度 (防止内存泄露，只存最近20首)
        if len(current_list) > 20:
            xiaomusic.music_list[TEMP_LIST_NAME] = current_list[:20]
            
        log.info(f"🧠 [内存操作] 已将【{song_name}】注入主程序内存列表")
        return True
    except Exception as e:
        log.error(f"内存注入失败: {e}")
        return False

async def download_song(song_id, song_name):
    global xiaomusic
    ensure_cache_dir()
    file_name = get_safe_filename(song_name)
    file_path_abs = os.path.join(CACHE_DIR_ABS, file_name)
    clean_name = file_name[:-4]

    if os.path.exists(file_path_abs) and os.path.getsize(file_path_abs) > 1024:
        log.info(f"⚡ 命中本地缓存: {file_name}")
        # 【关键】即使命中缓存，也要把文件路径注册给主程序
        # 否则主程序只知道歌名，找不到对应的 mp3 文件
        if xiaomusic and song_name not in xiaomusic.all_music:
             music_path_root = xiaomusic.config.music_path
             full_path = os.path.normpath(os.path.join(music_path_root, CACHE_DIR_NAME, file_name))
             xiaomusic.all_music[song_name] = full_path
             log.info(f"🔧 [内存操作] 注册文件路径: {song_name}")
        return True

    real_url = ""
    try:
        url_api = f"{NETEASE_API_URL}/song/url?id={song_id}&br=320000{PROXY_PARAM}"
        u_data = await fetch_json(xiaomusic.session, url_api)
        if u_data and 'data' in u_data:
            real_url = u_data['data'][0].get('url')
    except: pass

    if not real_url:
        log.warning("无法获取下载链接")
        return False

    try:
        api_port = xiaomusic.config.port
        api_url = f"http://127.0.0.1:{api_port}/downloadonemusic"
        payload = {"name": clean_name, "url": real_url}
        async with xiaomusic.session.post(api_url, json=payload) as resp:
            await resp.json()

        for _ in range(60):
            if os.path.exists(file_path_abs) and os.path.getsize(file_path_abs) > 1024:
                log.info(f"✅ 下载完成: {file_name}")
                # 下载完成后，手动注册进 all_music 字典
                if xiaomusic:
                    music_path_root = xiaomusic.config.music_path
                    full_path = os.path.normpath(os.path.join(music_path_root, CACHE_DIR_NAME, file_name))
                    xiaomusic.all_music[song_name] = full_path
                    log.info(f"🔧 [内存操作] 新文件注册成功: {song_name}")
                return True
            await asyncio.sleep(1)
        return False
    except Exception as e:
        log.error(f"下载异常: {e}")
        return False

def install_hook(xm):
    if hasattr(xm, '_web_hook_installed'): return
    old_func = xm.do_check_cmd
    async def new_func(did="", query="", ctrl_panel=True, **kwargs):
        xm._last_web_query = query
        await old_func(did, query, ctrl_panel, **kwargs)
    xm.do_check_cmd = new_func
    xm._web_hook_installed = True
    log.info("💉 [搜索插件] Web指令监听器已安装")

async def search_play_song(did=None, arg1=None, **kwargs):
    global xiaomusic
    query = arg1
    
    if not xiaomusic:
        try:
            for obj in gc.get_objects():
                if hasattr(obj, 'do_check_cmd') and hasattr(obj, 'plugin_manager'):
                    xiaomusic = obj
                    break
        except: pass

    if not xiaomusic:
        log.error("❌ 无法获取主程序实例")
        return

    install_hook(xiaomusic)

    current_did = did
    if not current_did:
        if hasattr(xiaomusic, 'get_cur_did'):
            current_did = xiaomusic.get_cur_did()
        if not current_did and xiaomusic.devices:
            current_did = list(xiaomusic.devices.keys())[0]

    if not query:
        if hasattr(xiaomusic, '_last_web_query') and xiaomusic._last_web_query:
            raw_cmd = xiaomusic._last_web_query
            xiaomusic._last_web_query = None 
            for kw in ["播放歌曲", "搜索播放", "搜索", "播放"]:
                if kw in raw_cmd:
                    query = raw_cmd.replace(kw, "").strip()
                    log.info(f"🔄 [Web指令] 提取歌名: {query}")
                    break
        
        if not query and xiaomusic.last_record:
            try:
                last_cmd = xiaomusic.last_record.get('query', '')
                for kw in ["播放歌曲", "搜索播放", "搜索", "播放"]:
                    if kw in last_cmd:
                        temp_q = last_cmd.replace(kw, "").strip()
                        if temp_q and len(temp_q) < 20:
                            query = temp_q
                            log.info(f"🔄 [语音指令] 提取歌名: {query}")
                            break
            except: pass

    if not current_did or not query:
        if not query:
            log.warning("⚠️ 首次运行钩子正在安装，请【再次点击】搜索播放")
        return

    log.info(f"🔍 搜索: {query}")

    song_info = await search_song_netease(query)
    if not song_info:
        log.warning(f"未找到: {query}")
        return

    # 1. 下载 (并注册文件路径到内存)
    if not await download_song(song_info['id'], song_info['name']):
        log.warning("下载失败")
        return

    # 2. 注入内存列表 (关键步骤：不碰配置文件)
    inject_to_memory_list(song_info['name'])

    try:
        api_port = xiaomusic.config.port
        play_url = f"http://127.0.0.1:{api_port}/playmusiclist"
        
        clean_name = song_info['name']
        if clean_name.endswith('.mp3'): clean_name = clean_name[:-4]

        payload = {
            "did": current_did,
            "listname": TEMP_LIST_NAME,
            "musicname": clean_name
        }
        
        log.info(f"▶️ 请求播放歌单: {TEMP_LIST_NAME} -> {clean_name}")
        async with xiaomusic.session.post(play_url, json=payload) as resp:
            if resp.status == 200:
                log.info("✅ 播放指令已下发")
            else:
                log.error(f"播放请求失败: {resp.status}")
    except Exception as e:
        log.error(f"播放异常: {e}")