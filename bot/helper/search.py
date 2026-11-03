import re
from bot.config import Telegram
from bot.helper.database import Database
from bot.telegram import StreamBot, UserBot
from bot import LOGGER
from bot.helper.media import is_media
from bot.helper.cache import rm_cache
from os.path import splitext
from bot.helper.file_size import get_readable_file_size

db = Database()
async def search(chat_id, query, page):
    if Telegram.SESSION_STRING == '':
        posts = await db.search_tgfiles(id=chat_id, query=query, page=page)
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
                    LOGGER.info(f"Purged deleted file from search index: chat={chat_id}, msg_id={del_id}")

                if deleted_ids:
                    rm_cache(str(chat_id))

                return verified + other_posts
        except Exception as e:
            LOGGER.warning(f"Error verifying search files with Telegram: {e}")
            return posts
        return posts
    posts = []
    async for post in UserBot.search_messages(chat_id=int(chat_id), limit=50, query=str(query), offset=(int(page) - 1) * 50):
        file = post.video or post.document
        if not file:
            continue
        title = file.file_name or post.caption or file.file_id
        title, _ = splitext(title)
        title = re.sub(r'[.,|_\',]', ' ', title)
        posts.append({"msg_id": post.id, "title": title,
                     "hash": file.file_unique_id[:6], "size": get_readable_file_size(file.file_size), "type": file.mime_type})
    return posts
