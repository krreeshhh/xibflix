from os.path import splitext
import re
from bot.config import Telegram
from bot.helper.database import Database
from bot.telegram import StreamBot, UserBot
from bot.helper.file_size import get_readable_file_size
from bot import LOGGER
from bot.helper.cache import get_cache, save_cache, rm_cache
from bot.helper.media import is_media
from asyncio import gather

db = Database()


async def fetch_message(chat_id, message_id):
    try:
        message = await StreamBot.get_messages(chat_id, message_id)
        return message
    except Exception as e:
        return None


async def get_messages(chat_id, first_message_id, last_message_id, batch_size=50):
    messages = []
    current_message_id = first_message_id
    while current_message_id <= last_message_id:
        batch_message_ids = list(range(current_message_id, min(current_message_id + batch_size, last_message_id + 1)))
        tasks = [fetch_message(chat_id, message_id) for message_id in batch_message_ids]
        batch_messages = await gather(*tasks)
        for message in batch_messages:
            if message:
                if file := message.video or message.document:
                    title = file.file_name or message.caption or file.file_id
                    title, _ = splitext(title)
                    title = re.sub(r'[.,|_\',]', ' ', title)
                    messages.append({"msg_id": message.id, "title": title,
                                     "hash": file.file_unique_id[:6], "size": get_readable_file_size(file.file_size),
                                     "type": file.mime_type, "chat_id": str(chat_id)})
        current_message_id += batch_size
    return messages


async def get_files(chat_id, page=1):
    if Telegram.SESSION_STRING == '':
        posts = await db.list_tgfiles(id=chat_id, page=page)
        if not posts:
            return []
        try:
            int_chat_id = int(chat_id)
            msg_ids = []
            post_by_id = {}
            other_posts = []
            for p in posts:
                mid = p.get("msg_id")
                if mid is not None and str(mid).isdigit():
                    m_int = int(mid)
                    msg_ids.append(m_int)
                    post_by_id[m_int] = p
                else:
                    other_posts.append(p)

            if msg_ids:
                tg_messages = await StreamBot.get_messages(int_chat_id, msg_ids)
                if not isinstance(tg_messages, list):
                    tg_messages = [tg_messages]

                tg_map = {m.id: m for m in tg_messages if m and not m.empty}
                verified = []
                deleted_ids = []
                for mid in msg_ids:
                    tg_msg = tg_map.get(mid)
                    if tg_msg and is_media(tg_msg):
                        verified.append(post_by_id[mid])
                    else:
                        deleted_ids.append(mid)

                for del_id in deleted_ids:
                    db.delete_file(chat_id=str(chat_id), msg_id=str(del_id))
                    db.delete_playlist_file(chat_id=str(chat_id), file_id=str(del_id))
                    LOGGER.info(f"Purged deleted file from index: chat={chat_id}, msg_id={del_id}")

                if deleted_ids:
                    rm_cache(str(chat_id))

                return verified + other_posts
        except Exception as e:
            LOGGER.warning(f"Error verifying files with Telegram: {e}")
            return posts
        return posts
    if cache := get_cache(chat_id, int(page)):
        return cache
    posts = []
    async for post in UserBot.get_chat_history(chat_id=int(chat_id), limit=50, offset=(int(page) - 1) * 50):
        file = post.video or post.document
        if not file:
            continue
        title = file.file_name or post.caption or file.file_id
        title, _ = splitext(title)
        title = re.sub(r'[.,|_\',]', ' ', title)
        posts.append({"msg_id": post.id, "title": title,
                    "hash": file.file_unique_id[:6], "size": get_readable_file_size(file.file_size), "type": file.mime_type})
    save_cache(chat_id, {"posts": posts}, page)
    return posts

async def posts_file(posts, chat_id):
    phtml = """
            <div class="col">
                <div class="card netflix-shelf-card mb-3">
                    <input type="checkbox" class="admin-only form-check-input position-absolute top-0 end-0 m-2"
                        onchange="checkSendButton()" id="selectCheckbox"
                        data-id="{id}|{hash}|{title}|{size}|{type}|{img}">
                    <a href="/watch/{chat_id}?id={id}&hash={hash}" class="shelf-card-link">
                        <div class="card-poster-wrapper">
                            <img src="https://cdn.jsdelivr.net/gh/weebzone/weebzone/data/Surf-TG/src/loading.gif" class="card-poster-img lzy_img"
                                data-src="{img}" alt="{title}">
                            <div class="card-badge-overlay">
                                <span class="badge-shelf-tag">{type}</span>
                                <span class="badge-shelf-tag highlight">{size}</span>
                            </div>
                        </div>
                        <div class="card-info-footer">
                            <h6 class="card-shelf-title" title="{title}">{title}</h6>
                        </div>
                    </a>
                </div>
            </div>
"""
    return ''.join(phtml.format(chat_id=str(chat_id).replace("-100", ""), id=post["msg_id"], img=f"/api/thumb/{chat_id}?id={post['msg_id']}", title=post["title"], hash=post["hash"], size=post['size'], type=post['type']) for post in posts)
