import asyncio
import json
import logging
import math
import mimetypes
import os
import secrets
import shutil
import subprocess
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from bot.helper.chats import get_chats, post_playlist, posts_chat, posts_db_file
from bot.helper.database import Database
from bot.helper.search import search
from bot.helper.thumbnail import get_image
from bot.telegram import work_loads, multi_clients
from aiohttp_session import get_session
from bot.config import Telegram
from bot.helper.exceptions import FIleNotFound, InvalidHash
from bot.helper.index import get_files, posts_file
from bot.server.custom_dl import ByteStreamer
from bot.server.render_template import render_page
from bot.helper.cache import rm_cache

from bot.telegram import StreamBot
from bot.helper.media_tracks import probe_media_tracks, clear_media_tracks_cache
from bot.server.file_properties import get_file_ids

SUBTITLE_CACHE = {}
AUDIO_LOCK = asyncio.Lock()
SUBTITLE_EXTRACTION_TASKS = {}


def shift_vtt_timestamps(vtt_text: str, start_sec: float) -> str:
    if start_sec <= 0:
        return vtt_text

    def parse_time(ts_str: str) -> float:
        parts = ts_str.split(':')
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(ts_str)

    def format_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    shifted = ["WEBVTT\n"]
    for line in vtt_text.splitlines():
        if "-->" in line:
            parts = line.split("-->")
            if len(parts) == 2:
                try:
                    t1 = parse_time(parts[0].strip()) + start_sec
                    t2 = parse_time(parts[1].strip()) + start_sec
                    shifted.append(f"{format_time(t1)} --> {format_time(t2)}")
                except Exception:
                    shifted.append(line)
            else:
                shifted.append(line)
        elif line.strip() and not line.startswith("WEBVTT"):
            shifted.append(line)
        elif not line.strip():
            shifted.append("")

    return "\n".join(shifted)


async def run_full_subtitle_extraction(stream_url: str, stream_index: int, full_vtt_path: str, cache_key: str):
    temp_path = f"{full_vtt_path}.tmp.{os.getpid()}"
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return
    cmd = [
        ffmpeg_bin,
        "-y", "-v", "error",
        "-i", stream_url,
        "-map", f"0:{stream_index}",
        "-f", "webvtt",
        temp_path
    ]
    try:
        proc_res = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            timeout=1800.0
        )
        if proc_res.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            os.replace(temp_path, full_vtt_path)
            with open(full_vtt_path, "r", encoding="utf-8", errors="ignore") as f:
                SUBTITLE_CACHE[cache_key] = f.read()
            logging.info(f"Full subtitle extraction completed for {cache_key}")
    except Exception as e:
        logging.warning(f"Error in background subtitle extraction: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    finally:
        SUBTITLE_EXTRACTION_TASKS.pop(cache_key, None)


async def validate_media_hash(chat_id: int, message_id: int, secure_hash: str):
    if not secure_hash:
        raise InvalidHash
    try:
        if StreamBot and (getattr(StreamBot, "is_connected", False) or hasattr(StreamBot, "get_messages")):
            file_data = await get_file_ids(StreamBot, chat_id=chat_id, message_id=message_id)
            if file_data and getattr(file_data, "unique_id", "")[:6] != secure_hash:
                raise InvalidHash
    except (InvalidHash, FIleNotFound):
        raise
    except Exception as e:
        logging.warning(f"Error validating media hash: {e}")

client_cache = {}

routes = web.RouteTableDef()
db = Database()

@routes.get('/api/logo', allow_head=True)
@routes.get('/logo.png', allow_head=True)
async def logo_handler(request):
    return web.FileResponse('bot/server/static/xibflix_wordmark.png')

@routes.get('/api/poster', allow_head=True)
async def poster_handler(request):
    return web.FileResponse('bot/server/static/xibflix.png')


@routes.get('/login')
async def login_form(request):
    session = await get_session(request)
    redirect_url = session.get('redirect_url', '/')
    return web.Response(text=await render_page(None, None, route='login', redirect_url=redirect_url), content_type='text/html')


@routes.post('/login')
async def login_route(request):
    session = await get_session(request)
    if 'user' in session:
        return web.HTTPFound('/')
    data = await request.post()
    username = data.get('username')
    password = data.get('password')
    error_message = None
    if (username == Telegram.USERNAME and password == Telegram.PASSWORD) or (username == Telegram.ADMIN_USERNAME and password == Telegram.ADMIN_PASSWORD):
        session['user'] = username
        if 'redirect_url' not in session:
            session['redirect_url'] = '/'
        redirect_url = session['redirect_url']
        del session['redirect_url']
        return web.HTTPFound(redirect_url)
    else:
        error_message = "Invalid username or password"
    return web.Response(text=await render_page(None, None, route='login', msg=error_message), content_type='text/html')


@routes.post('/logout')
async def logout_route(request):
    session = await get_session(request)
    session.pop('user', None)
    return web.HTTPFound('/login')


@routes.post('/create')
async def create_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})
    data = await request.post()
    folderName = data.get('folderName')
    thumbnail = data.get('thumbnail')
    parent_dir = data.get('parent_dir')
    parent_dir = parent_dir.split('db=')[-1] if 'db=' in parent_dir else 'root'
    await db.create_folder(parent_dir, folderName, thumbnail)
    if parent_dir == 'root':
        return web.HTTPFound('/')
    else:
        return web.HTTPFound(f'/playlist?db={parent_dir}')


@routes.post('/delete')
async def delete_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})
    data = await request.json()
    id = data.get('delete_id')
    parent = data.get('parent')
    if not (success := db.delete(id)):
        return web.HTTPInternalServerError()
    if parent == 'root':
        return web.HTTPFound('/')
    else:
        return web.HTTPFound(f'/playlist?db={parent}')


@routes.post('/edit')
async def editFolder_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})
    data = await request.post()
    folderName = data.get('folderName')
    thumbnail = data.get('thumbnail')
    id = data.get('folder_id')
    parent = data.get('parent')
    success = await db.edit(id, folderName, thumbnail)
    if not success:
        return web.HTTPInternalServerError()
    if parent == 'root':
        return web.HTTPFound('/')
    else:
        return web.HTTPFound(f'/playlist?db={parent}')


@routes.post('/edit_post')
async def editPost_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})
    data = await request.post()
    fileName = data.get('fileName')
    thumbnail = data.get('filethumbnail')
    id = data.get('file_id')
    parent = data.get('file_folder_id')
    success = await db.edit(id, fileName, thumbnail)
    if not success:
        return web.HTTPInternalServerError()
    if parent == 'root':
        return web.HTTPFound('/')
    else:
        return web.HTTPFound(f'/playlist?db={parent}')


@routes.get('/searchDbFol')
async def searchDbFolder_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})
    query = request.query.get('query', '')
    folder_names = await db.search_DbFolder(query)
    return web.json_response(folder_names)


@routes.post('/send')
async def send_route(request):
    data = await request.post()
    chat_id = data.get('chatId')
    chat_id = f"-100{chat_id}"
    folder_id = data.get('folderId')
    selected_ids = data.get('selectedIds')
    if not all([chat_id, folder_id, selected_ids]):
        return {'error': 'Missing required data in request'}

    formatted_entries = []
    for entry in selected_ids.split(','):
        file_id, hash, filename, size, file_type, thumbnail = entry.split('|')
        formatted_entries.append({
            'chat_id': chat_id,
            'parent_folder': folder_id,
            'file_id': file_id,
            'hash': hash,
            'name': filename,
            'size': size,
            'file_type': file_type,
            'thumbnail': thumbnail,
            'type': 'file'
        })

    json_data = json.dumps(formatted_entries)
    data = json.loads(json_data)
    await db.add_json(data)
    if folder_id == 'root':
        return web.HTTPFound('/')
    else:
        return web.HTTPFound(f'/playlist?db={folder_id}')


@routes.get('/reload')
async def reload_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})

    chat_id = request.query.get('chatId', '')
    if chat_id == 'home':
        rm_cache()
        return web.HTTPFound('/')
    else:
        rm_cache(f"-100{chat_id}")
        return web.HTTPFound(f'/channel/{chat_id}')


@routes.post('/config')
async def editConfig_route(request):
    session = await get_session(request)
    if (username := session.get('user')) != Telegram.ADMIN_USERNAME:
        return web.json_response({'msg': 'Who the hell you are'})
    data = await request.post()
    channel = data.get('channel')
    theme = data.get('theme')
    success = await db.update_config(theme=theme, auth_channel=channel)
    if not success:
        return web.HTTPInternalServerError()
    return web.HTTPFound('/')



@routes.get('/')
async def home_route(request):
    session = await get_session(request)
    if username := session.get('user'):
        try:
            channels = await get_chats()
            playlists = await db.get_Dbfolder()
            phtml = await posts_chat(channels)
            dhtml = await post_playlist(playlists)
            is_admin = username == Telegram.ADMIN_USERNAME
            return web.Response(text=await render_page(None, None, route='home', html=phtml, playlist=dhtml, is_admin=is_admin), content_type='text/html')
        except Exception as e:
            logging.critical(e.with_traceback(None))
            raise web.HTTPInternalServerError(text=str(e)) from e
    else:
        session['redirect_url'] = request.path_qs
        return web.HTTPFound('/login')


@routes.get('/playlist')
async def playlist_route(request):
    session = await get_session(request)
    if username := session.get('user'):
        try:
            parent_id = request.query.get('db')
            page = request.query.get('page', '1')
            playlists = await db.get_Dbfolder(parent_id, page=page)
            files = await db.get_dbFiles(parent_id, page=page)
            text = await db.get_info(parent_id)
            dhtml = await post_playlist(playlists)
            dphtml = await posts_db_file(files)
            is_admin = username == Telegram.ADMIN_USERNAME
            return web.Response(text=await render_page(parent_id, None, route='playlist', playlist=dhtml, database=dphtml, msg=text, is_admin=is_admin), content_type='text/html')
        except Exception as e:
            logging.critical(e.with_traceback(None))
            raise web.HTTPInternalServerError(text=str(e)) from e
    else:
        session['redirect_url'] = request.path_qs
        return web.HTTPFound('/login')


@routes.get('/search/db/{parent}')
async def dbsearch_route(request):
    session = await get_session(request)
    if username := session.get('user'):
        parent = request.match_info['parent']
        page = request.query.get('page', '1')
        query = request.query.get('q')
        is_admin = username == Telegram.ADMIN_USERNAME
        try:
            files = await db.search_dbfiles(id=parent, page=page, query=query)
            dphtml = await posts_db_file(files)
            name = await db.get_info(parent)
            text = f"{name} - {query}"
            return web.Response(text=await render_page(parent, None, route='playlist', database=dphtml, msg=text, is_admin=is_admin), content_type='text/html')
        except Exception as e:
            logging.critical(e.with_traceback(None))
            raise web.HTTPInternalServerError(text=str(e)) from e
    else:
        session['redirect_url'] = request.path_qs
        return web.HTTPFound('/login')


@routes.get('/channel/{chat_id}')
async def channel_route(request):
    session = await get_session(request)
    if username := session.get('user'):
        chat_id = request.match_info['chat_id']
        chat_id = f"-100{chat_id}"
        page = request.query.get('page', '1')
        is_admin = username == Telegram.ADMIN_USERNAME
        try:
            posts = await get_files(chat_id, page=page)
            phtml = await posts_file(posts, chat_id)
            chat = await StreamBot.get_chat(int(chat_id))
            return web.Response(text=await render_page(None, None, route='index', html=phtml, msg=chat.title, chat_id=chat_id.replace("-100", ""), is_admin=is_admin), content_type='text/html')
        except Exception as e:
            logging.critical(e.with_traceback(None))
            raise web.HTTPInternalServerError(text=str(e)) from e
    else:
        session['redirect_url'] = request.path_qs
        return web.HTTPFound('/login')


@routes.get('/search/{chat_id}')
async def search_route(request):
    session = await get_session(request)
    if username := session.get('user'):
        chat_id = request.match_info['chat_id']
        chat_id = f"-100{chat_id}"
        page = request.query.get('page', '1')
        query = request.query.get('q')
        is_admin = username == Telegram.ADMIN_USERNAME
        try:
            posts = await search(chat_id, page=page, query=query)
            phtml = await posts_file(posts, chat_id)
            chat = await StreamBot.get_chat(int(chat_id))
            text = f"{chat.title} - {query}"
            return web.Response(text=await render_page(None, None, route='index', html=phtml, msg=text, chat_id=chat_id.replace("-100", ""), is_admin=is_admin), content_type='text/html')
        except Exception as e:
            logging.critical(e.with_traceback(None))
            raise web.HTTPInternalServerError(text=str(e)) from e
    else:
        session['redirect_url'] = request.path_qs
        return web.HTTPFound('/login')


@routes.get('/api/thumb/{chat_id}', allow_head=True)
async def get_thumbnail(request):
    chat_id = request.match_info['chat_id']
    if message_id := request.query.get('id'):
        img = await get_image(chat_id, message_id)
    else:
        img = await get_image(chat_id, None)
    response = web.FileResponse(img)
    response.content_type = "image/jpeg"
    return response

@routes.get('/api/tracks/{chat_id}', allow_head=True)
async def api_tracks_handler(request: web.Request):
    try:
        raw_chat = request.match_info['chat_id']
        if not raw_chat.lstrip('-').isdigit():
            raise web.HTTPNotFound()
        chat_id = f"-100{raw_chat}" if not raw_chat.startswith('-100') else raw_chat
        chat_id = int(chat_id)

        message_id = request.query.get('id')
        secure_hash = request.query.get('hash')
        if not message_id or not secure_hash or not message_id.isdigit():
            raise web.HTTPBadRequest(text="Missing or invalid id/hash parameter")
        message_id = int(message_id)

        await validate_media_hash(chat_id, message_id, secure_hash)

        custom_stream = request.query.get('stream_url')
        local_port = request.host.split(':')[-1] if ':' in request.host else Telegram.PORT
        stream_url = custom_stream or f"http://127.0.0.1:{local_port}/{chat_id}/probe?id={message_id}&hash={secure_hash}"
        tracks_data = await probe_media_tracks(chat_id, message_id, secure_hash, stream_url=stream_url)
        return web.json_response(tracks_data, headers={"Access-Control-Allow-Origin": "*"})
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message) from e
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message) from e
    except web.HTTPException:
        raise
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e)) from e


@routes.get('/api/subtitles/{chat_id}', allow_head=True)
async def api_subtitles_handler(request: web.Request):
    try:
        raw_chat = request.match_info['chat_id']
        if not raw_chat.lstrip('-').isdigit():
            raise web.HTTPNotFound()
        chat_id = f"-100{raw_chat}" if not raw_chat.startswith('-100') else raw_chat
        chat_id = int(chat_id)

        message_id = request.query.get('id')
        secure_hash = request.query.get('hash')
        stream_param = request.query.get('stream')
        if not message_id or not secure_hash or not message_id.isdigit() or stream_param is None or not stream_param.isdigit():
            raise web.HTTPBadRequest(text="Missing or invalid query parameters")
        message_id = int(message_id)
        stream_index = int(stream_param)

        await validate_media_hash(chat_id, message_id, secure_hash)

        cache_key = f"{chat_id}_{message_id}_{stream_index}"
        vtt_dir = os.path.join("cache", "subtitles")
        os.makedirs(vtt_dir, exist_ok=True)
        full_vtt_path = os.path.join(vtt_dir, f"sub_{chat_id}_{message_id}_{stream_index}.vtt")

        # 1. Check if complete file is cached on disk
        if os.path.exists(full_vtt_path) and os.path.getsize(full_vtt_path) > 0:
            if cache_key not in SUBTITLE_CACHE:
                try:
                    with open(full_vtt_path, 'r', encoding='utf-8', errors='ignore') as f:
                        SUBTITLE_CACHE[cache_key] = f.read()
                except Exception:
                    pass
            return web.FileResponse(
                full_vtt_path,
                headers={
                    "Content-Type": "text/vtt",
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                }
            )

        if cache_key in SUBTITLE_CACHE:
            return web.Response(
                text=SUBTITLE_CACHE[cache_key],
                content_type="text/vtt",
                charset="utf-8",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                }
            )

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise web.HTTPInternalServerError(text="ffmpeg not found on server")

        custom_stream = request.query.get('stream_url')
        local_port = Telegram.PORT
        stream_url = custom_stream or f"http://127.0.0.1:{local_port}/{chat_id}/stream?id={message_id}&hash={secure_hash}"

        # 2. Launch background task for full movie subtitle extraction
        if cache_key not in SUBTITLE_EXTRACTION_TASKS:
            task = asyncio.create_task(
                run_full_subtitle_extraction(stream_url, stream_index, full_vtt_path, cache_key)
            )
            SUBTITLE_EXTRACTION_TASKS[cache_key] = task

        # 3. For immediate response: extract a 90-second window starting at 'time'
        time_param = request.query.get('time')
        start_sec = max(0.0, float(time_param)) if time_param is not None else 0.0
        window_duration = "30"
        cmd = [
            ffmpeg_bin,
            "-y", "-v", "error",
            "-ss", str(int(start_sec)),
            "-i", stream_url,
            "-map", f"0:{stream_index}",
            "-t", window_duration,
            "-f", "webvtt",
            "pipe:1"
        ]

        try:
            proc_res = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=45.0
            )
            stdout = proc_res.stdout
            stderr = proc_res.stderr
            returncode = proc_res.returncode
        except subprocess.TimeoutExpired:
            return web.Response(
                text="WEBVTT\n\n",
                content_type="text/vtt",
                charset="utf-8",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        vtt_raw = stdout.decode('utf-8', errors='ignore') if stdout else ""
        if not vtt_raw.startswith("WEBVTT"):
            if returncode != 0:
                err_msg = stderr.decode('utf-8', errors='ignore') if stderr else ""
                logging.warning(f"ffmpeg subtitle conversion error: {err_msg}")
                return web.Response(
                    text="WEBVTT\n\n",
                    content_type="text/vtt",
                    charset="utf-8",
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            vtt_raw = "WEBVTT\n\n" + vtt_raw

        shifted_vtt = shift_vtt_timestamps(vtt_raw, start_sec)
        if start_sec == 0:
            SUBTITLE_CACHE[cache_key] = shifted_vtt
        return web.Response(
            text=shifted_vtt,
            content_type="text/vtt",
            charset="utf-8",
            headers={
                "Cache-Control": "public, max-age=60",
                "Access-Control-Allow-Origin": "*",
            }
        )
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message) from e
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message) from e
    except web.HTTPException:
        raise
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e)) from e


@routes.get('/api/audio/{chat_id}', allow_head=True)
async def api_audio_handler(request: web.Request):
    try:
        raw_chat = request.match_info['chat_id']
        if not raw_chat.lstrip('-').isdigit():
            raise web.HTTPNotFound()
        chat_id = f"-100{raw_chat}" if not raw_chat.startswith('-100') else raw_chat
        chat_id = int(chat_id)

        message_id = request.query.get('id')
        secure_hash = request.query.get('hash')
        stream_param = request.query.get('stream')
        if not message_id or not secure_hash or not message_id.isdigit() or stream_param is None or not stream_param.isdigit():
            raise web.HTTPBadRequest(text="Missing or invalid query parameters")
        message_id = int(message_id)
        stream_index = int(stream_param)

        await validate_media_hash(chat_id, message_id, secure_hash)

        audio_dir = os.path.join("cache", "audio")
        os.makedirs(audio_dir, exist_ok=True)
        audio_file = f"audio_{chat_id}_{message_id}_{stream_index}.m4a"
        audio_path = os.path.join(audio_dir, audio_file)
        temp_path = f"{audio_path}.tmp"
        if not os.path.exists(audio_path):
            async with AUDIO_LOCK:
                if not os.path.exists(audio_path):
                    ffmpeg_bin = shutil.which("ffmpeg")
                    if not ffmpeg_bin:
                        raise web.HTTPInternalServerError(text="ffmpeg not found on server")

                    custom_stream = request.query.get('stream_url')
                    local_port = request.host.split(':')[-1] if ':' in request.host else Telegram.PORT
                    stream_url = custom_stream or f"http://127.0.0.1:{local_port}/{chat_id}/stream?id={message_id}&hash={secure_hash}"

                    cmd = [
                        ffmpeg_bin,
                        "-y", "-v", "error",
                        "-i", stream_url,
                        "-map", f"0:{stream_index}",
                        "-c:a", "aac",
                        "-b:a", "192k",
                        "-f", "mp4",
                        temp_path
                    ]

                    try:
                        proc_res = await asyncio.to_thread(
                            subprocess.run,
                            cmd,
                            capture_output=True,
                            timeout=60.0
                        )
                        stderr = proc_res.stderr
                        returncode = proc_res.returncode
                    except subprocess.TimeoutExpired:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        raise web.HTTPGatewayTimeout(text="Audio extraction timed out")

                    if returncode != 0 or not os.path.exists(temp_path):
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        err_msg = stderr.decode('utf-8', errors='ignore') if stderr else ""
                        logging.warning(f"ffmpeg audio extraction error: {err_msg}")
                        raise web.HTTPInternalServerError(text="Audio track extraction failed")
                    os.replace(temp_path, audio_path)

                    # Keep last 5 audio files
                    try:
                        all_audio_files = [
                            os.path.join(audio_dir, f)
                            for f in os.listdir(audio_dir)
                            if f.startswith("audio_") and f.endswith(".m4a")
                        ]
                        if len(all_audio_files) > 5:
                            all_audio_files.sort(key=lambda p: os.path.getmtime(p))
                            for old_file in all_audio_files[:-5]:
                                try:
                                    os.remove(old_file)
                                except OSError:
                                    pass
                    except Exception as e:
                        logging.warning(f"Error pruning audio cache: {e}")

        response = web.FileResponse(audio_path)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message) from e
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message) from e
    except web.HTTPException:
        raise
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e)) from e


@routes.get('/watch/{chat_id}', allow_head=True)
async def stream_handler_watch(request: web.Request):
    session = await get_session(request)
    if username := session.get('user'):
        try:
            chat_id = request.match_info['chat_id']
            chat_id = f"-100{chat_id}"
            message_id = request.query.get('id')
            secure_hash = request.query.get('hash')
            return web.Response(text=await render_page(message_id, secure_hash, chat_id=chat_id), content_type='text/html')
        except InvalidHash as e:
            raise web.HTTPForbidden(text=e.message) from e
        except FIleNotFound as e:
            db.delete_file(chat_id=chat_id, msg_id=message_id, hash=secure_hash)
            raise web.HTTPNotFound(text=e.message) from e
        except (AttributeError, BadStatusLine, ConnectionResetError):
            pass
        except Exception as e:
            logging.critical(e.with_traceback(None))
            raise web.HTTPInternalServerError(text=str(e)) from e
    else:
        session['redirect_url'] = request.path_qs
        return web.HTTPFound('/login')


@routes.get('/{chat_id}/{encoded_name}', allow_head=True)
async def stream_handler(request: web.Request):
    try:
        raw_chat = request.match_info['chat_id']
        if not raw_chat.lstrip('-').isdigit():
            raise web.HTTPNotFound()
        chat_id = f"-100{raw_chat}" if not raw_chat.startswith('-100') else raw_chat
        message_id = request.query.get('id')
        #name = request.match_info['encoded_name']
        secure_hash = request.query.get('hash')
        return await media_streamer(request, int(chat_id), int(message_id), secure_hash)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message) from e
    except FIleNotFound as e:
        db.delete_file(chat_id=chat_id, msg_id=message_id, hash=secure_hash)
        raise web.HTTPNotFound(text=e.message) from e
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


class_cache = {}


async def media_streamer(request: web.Request, chat_id: int, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    if request.remote in ("127.0.0.1", "::1", "localhost") or not Telegram.MULTI_CLIENT or not multi_clients:
        index = 0
        faster_client = multi_clients[0]
    else:
        index = min(work_loads, key=work_loads.get)
        faster_client = multi_clients[index]

    if Telegram.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(chat_id=chat_id, message_id=id)
    logging.debug("after calling get_file_properties")

    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - \
        math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
