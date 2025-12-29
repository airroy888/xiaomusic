import logging
import random
import asyncio
import urllib.parse
import json
import os
import re
import time

xiaomusic = None
log = logging.getLogger("xiaomusic")

# ================= 配置区域 =================
DATA_FILE = "/app/conf/music_list.json"
CACHE_DIR_NAME = "download"
CACHE_DIR_ABS = f"/app/music/{CACHE_DIR_NAME}" 
LOG_FILE_PATH = "/app/xiaomusic.log.txt"

# 网易云 API 配置
NETEASE_API_URL = "http://netease-api:3000"
PROXY_PARAM = "&level=exhigh&proxy=http://unblock-netease:8080"

# 全局变量
next_song_cache = None
current_download_task = None 
# 【V6.5 新增】插件私有定时器仓库，解决主程序丢失引用导致的“幽灵定时器”问题
plugin_timers = {} 

async def fetch_json(session, url):
    """通用网络请求工具"""
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
    except Exception as e:
        log.error(f"创建缓存目录失败: {e}")

def get_safe_filename(song_name):
    """清理文件名中的非法字符"""
    safe = re.sub(r'[\\/*?:"<>|]', "", song_name)
    return f"{safe}.mp3"

async def download_song(song_id, song_name):
    """核心下载逻辑"""
    global xiaomusic
    ensure_cache_dir()
    
    file_name = get_safe_filename(song_name)
    file_path_abs = os.path.join(CACHE_DIR_ABS, file_name)
    clean_name = file_name[:-4]

    # 1. 本地缓存检查
    if os.path.exists(file_path_abs) and os.path.getsize(file_path_abs) > 1024:
        if xiaomusic and song_name not in xiaomusic.all_music:
             music_path_root = xiaomusic.config.music_path
             full_path = os.path.normpath(os.path.join(music_path_root, CACHE_DIR_NAME, file_name))
             xiaomusic.all_music[song_name] = full_path
             log.info(f"🔧 [播放插件] 修复内存索引: {song_name}")
        log.info(f"⚡ 命中本地缓存: {file_name}")
        return file_path_abs

    log.info(f"📥 [播放插件] 准备下载: {song_name} (ID:{song_id})")
    
    # 2. 获取直链
    real_url = ""
    try:
        url_api = f"{NETEASE_API_URL}/song/url?id={song_id}&br=320000{PROXY_PARAM}"
        u_data = await fetch_json(xiaomusic.session, url_api)
        if u_data and 'data' in u_data:
            real_url = u_data['data'][0].get('url')
    except Exception as e:
        log.warning(f"获取链接失败: {e}")
        return None

    if not real_url:
        log.warning(f"无有效链接: {song_name}")
        return None

    # 3. API 下载
    try:
        api_port = xiaomusic.config.port
        api_url = f"http://127.0.0.1:{api_port}/downloadonemusic"
        payload = {"name": clean_name, "url": real_url}
        log.info(f"📡 调用API下载: {clean_name}")
        async with xiaomusic.session.post(api_url, json=payload) as resp:
            if resp.status != 200:
                log.error(f"API下载请求失败: {resp.status}")
                return None
            else:
                await resp.json()

        # 4. 等待完成
        for _ in range(60):
            if os.path.exists(file_path_abs) and os.path.getsize(file_path_abs) > 1024:
                log.info(f"✅ 下载完成: {file_path_abs}")
                if xiaomusic:
                    music_path_root = xiaomusic.config.music_path
                    full_path = os.path.normpath(os.path.join(music_path_root, CACHE_DIR_NAME, file_name))
                    if os.path.exists(full_path):
                        xiaomusic.all_music[song_name] = full_path
                    else:
                        xiaomusic.all_music[song_name] = file_path_abs
                return file_path_abs
            await asyncio.sleep(1)
        return None
    except Exception as e:
        log.error(f"API下载流程出错: {e}")
        return None

async def pre_download_next(target_musics, did):
    """后台静默预下载"""
    global next_song_cache, xiaomusic
    if xiaomusic and did in xiaomusic.devices:
        dev = xiaomusic.devices[did]
        if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop':
            return

    try:
        log.info("⏳ 缓冲中：等待 40秒 后开始预下载下一首...")
        await asyncio.sleep(40)
    except asyncio.CancelledError:
        return
    
    if xiaomusic and did in xiaomusic.devices:
        dev = xiaomusic.devices[did]
        if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop':
            return

    try:
        next_song = random.choice(target_musics)
        log.info(f"🚀 [后台] 开始预下载: {next_song['name']}")
        await download_song(next_song['id'], next_song['name'])
        
        # 直接存入内存，切歌时直接读取
        next_song_cache = {
            "name": next_song['name'],
            "path": "", 
            "id": next_song['id']
        }
    except Exception as e:
        log.warning(f"预下载异常: {e}")

def auto_next_callback(did, custom_name):
    """
    定时器回调
    【V6.5】增加私有字典清理逻辑
    """
    global plugin_timers
    
    # 任务触发后，从私有字典中移除句柄，避免残留
    if did in plugin_timers:
        plugin_timers.pop(did, None)

    if xiaomusic and did in xiaomusic.devices:
        dev = xiaomusic.devices[did]
        if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop':
            return
        if hasattr(dev, 'status') and dev.status == 0:
            return
        # 这里不再检查 dev.next_timer is None，因为我们使用 plugin_timers 作为权威依据

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    log.info(f"⏰ 定时器触发切歌 -> {did}")
    asyncio.run_coroutine_threadsafe(
        play_target_list(custom_name=custom_name, did=did, is_auto_next=True),
        loop
    )

# =========================================================
# 插件入口：合并了【播放歌单】和【切歌】逻辑
# =========================================================
async def play_target_list(custom_name="默认歌单", did=None, is_auto_next=False, **kwargs):
    global xiaomusic, next_song_cache, current_download_task, plugin_timers
    
    # 1. 确定设备 ID
    current_did = did
    if not current_did:
        if hasattr(xiaomusic, 'get_cur_did'):
            current_did = xiaomusic.get_cur_did()
            if current_did:
                log.info(f"🎯 内存定位设备ID: {current_did}")
        if not current_did and xiaomusic.devices:
            current_did = list(xiaomusic.devices.keys())[0]

    if not current_did: 
        log.error("❌ 未找到有效设备ID")
        return "设备未连接"

    # ================= 1. 强力清场 (V6.5 核心修复) =================
    # 无论何时调用播放，首先检查私有账本，强制杀死旧的定时任务
    # 这一步能彻底解决“主程序丢失引用导致的幽灵定时器”问题
    if current_did in plugin_timers:
        try:
            handle = plugin_timers[current_did]
            handle.cancel()
            log.info(f"🛡️ [强力清场] 已移除该设备旧的定时器 (私有句柄)")
        except Exception as e:
            log.warning(f"定时器清理异常: {e}")
        finally:
            plugin_timers.pop(current_did, None)
            
    # 双重保险：尝试清除官方变量 (即使它可能是 None)
    if current_did in xiaomusic.devices:
        dev = xiaomusic.devices[current_did]
        if hasattr(dev, 'cancel_next_timer'):
            dev.cancel_next_timer()

    # ================= 2. 切歌逻辑 =================
    if custom_name == 'CUT':
        log.info(f"✂️ 收到切歌指令 -> {current_did}")
        if current_did in xiaomusic.devices:
            dev = xiaomusic.devices[current_did]
            
            # 【V6.1 核心修复】切歌前强制清除上一首的定时器 (已合并到上方强力清场逻辑中)
            
            # 1. 强制重置 PLAY 状态
            if hasattr(dev, '_last_cmd'):
                dev._last_cmd = 'play'
                log.info("▶️ [切歌] 重置设备指令状态为 PLAY")
            
            # 2. 自动获取当前歌单
            current_pl = dev.cur_playlist
            if current_pl and current_pl in xiaomusic.music_list:
                custom_name = current_pl
                log.info(f"🔄 继承当前歌单: {custom_name}")
            else:
                custom_name = "抖音热门歌曲"
                log.warning(f"⚠️ 无当前歌单，兜底使用: {custom_name}")
            
            # 3. 移除 TTS，追求极致响应速度
    # ================================================

    log.info(f"🎵 执行播放: {custom_name} -> {current_did}")

    # 2. 状态检查 (自动播放时才检查停止，切歌/手动播放不检查)
    if current_did in xiaomusic.devices:
        dev = xiaomusic.devices[current_did]
        
        if is_auto_next:
            if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop':
                log.info("🛑 自动播放检测到 STOP 指令，不再播放")
                return
            if hasattr(dev, 'status') and dev.status == 0:
                log.info("🛑 自动播放检测到手动停止，不再播放")
                return
        elif custom_name != 'CUT': 
            # 如果不是切歌逻辑（切歌上面已经重置过了），是普通手动播放指令
            dev._last_cmd = 'play'

    if not os.path.exists(DATA_FILE): return "无歌单文件"
    
    try:
        # 3. 读取歌单
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            playlists = json.load(f)
        target_musics = []
        for pl in playlists:
            if pl.get('name') == custom_name:
                target_musics = pl.get('musics', [])
                break
        if not target_musics: return "歌单为空"

        # 4. 选歌 (天然共享内存缓存!)
        song_to_play = None
        if next_song_cache:
            log.info(f"⚡ 使用预下载: {next_song_cache['name']}")
            song_to_play = next_song_cache
            next_song_cache = None # 用完即焚
        else:
            log.info("🎲 无缓存，随机选择")
            song_to_play = random.choice(target_musics)

        # 刷新播放列表顺序
        all_names = [m.get('name') for m in target_musics]
        random.shuffle(all_names)
        target_name = song_to_play['name']
        if target_name in all_names:
            all_names.remove(target_name)
            all_names.insert(0, target_name)
        xiaomusic.music_list[custom_name] = all_names

        # 5. 下载
        play_path_rel = await download_song(song_to_play['id'], target_name)

        if not play_path_rel:
            log.warning("下载失败，重试...")
            await asyncio.sleep(1)
            # 递归重试
            return await play_target_list(custom_name, current_did, is_auto_next=True)

        # 6. 下载后二次检查停止状态
        if current_did in xiaomusic.devices:
            dev = xiaomusic.devices[current_did]
            if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop':
                log.info("🛑 下载后检测到 STOP 指令，取消播放")
                return

        # 7. 调用 API 播放
        log.info(f"▶️ 调用歌单播放API: {target_name} in {custom_name}")
        
        try:
            api_port = xiaomusic.config.port
            play_list_url = f"http://127.0.0.1:{api_port}/playmusiclist"
            play_payload = {
                "did": current_did,
                "listname": custom_name,
                "musicname": target_name 
            }
            async with xiaomusic.session.post(play_list_url, json=play_payload) as resp:
                if resp.status == 200:
                    log.info("✅ 歌单播放指令下发成功")
        except Exception as e_play:
            log.error(f"API播放异常: {e_play}")
        
        # 8. 更新状态 & 定时器
        if current_did in xiaomusic.devices:
            dev = xiaomusic.devices[current_did]
            dev.cur_music = target_name
            dev.cur_playlist = custom_name
            try: dev.cur_music_idx = xiaomusic.music_list[custom_name].index(target_name)
            except: pass
            
            if hasattr(xiaomusic, 'save_cur_config'):
                xiaomusic.save_cur_config()

            duration = 0
            offset = 0
            for _ in range(20): 
                if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop': break
                if hasattr(dev, 'get_offset_duration'):
                    offset, duration = dev.get_offset_duration()
                else:
                    duration = getattr(dev, 'cur_music_length', 0)
                if duration > 0: break 
                await asyncio.sleep(0.5)
            
            if hasattr(dev, '_last_cmd') and dev._last_cmd == 'stop':
                log.info("🛑 轮询后检测到 STOP 指令，不设置定时器")
                return

            log.info(f"⏱️ 探测结果: duration={duration}, offset={offset}")

            if duration > 0:
                # 不再调用 dev.cancel_next_timer()，因为我们在开头已经强力清理过了
                remaining = duration - offset
                next_delay = max(remaining - 3, 1)
                log.info(f"⏰ 智能设置定时器: {next_delay}s")
                
                try:
                    loop = asyncio.get_running_loop()
                    # 【V6.5 核心】创建任务，并存入私有字典
                    handle = loop.call_later(
                        next_delay, 
                        auto_next_callback, 
                        current_did, 
                        custom_name
                    )
                    plugin_timers[current_did] = handle
                    
                    # 同时也赋值给 dev.next_timer，保持对 Web UI 显示的兼容性
                    # 但我们不再依赖它来取消任务
                    dev.next_timer = handle
                except Exception as e_loop:
                    log.error(f"❌ 定时器错误: {e_loop}")
            else:
                log.warning("⚠️ 获取时长超时")

        # 9. 启动后台预下载 (存入 shared 内存变量)
        if current_download_task and not current_download_task.done():
            current_download_task.cancel()
        current_download_task = asyncio.create_task(pre_download_next(target_musics, current_did))

        return f"播放: {target_name}"

    except Exception as e:
        log.error(f"流程异常: {e}")
        return f"出错: {str(e)}"