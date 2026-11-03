from asyncio import gather, create_task
from bot.helper.database import Database
from bot.telegram import StreamBot
from bot.config import Telegram

db = Database()

async def get_chats():
    AUTH_CHANNEL = await db.get_variable('auth_channel')
    if AUTH_CHANNEL is None or AUTH_CHANNEL.strip() == '':
        AUTH_CHANNEL = Telegram.AUTH_CHANNEL
    else:
        AUTH_CHANNEL = [channel.strip() for channel in AUTH_CHANNEL.split(",")]
    
    return [{"chat-id": chat.id, "title": chat.title or chat.first_name, "type": chat.type.name} for chat in await gather(*[create_task(StreamBot.get_chat(int(channel_id))) for channel_id in AUTH_CHANNEL])]


async def posts_chat(channels):
    phtml = """
            <div class="col channel-card">
                <a href="/channel/{cid}" class="shelf-card-link">
                    <div class="card netflix-shelf-card mb-3">
                        <div class="card-poster-wrapper">
                            <img src="https://cdn.jsdelivr.net/gh/weebzone/weebzone/data/Surf-TG/src/loading.gif" class="card-poster-img lzy_img"
                                data-src="{img}" alt="{title}">
                            <div class="card-badge-overlay">
                                <span class="badge-shelf-tag"><i class="bi bi-broadcast me-1"></i>{ctype}</span>
                            </div>
                        </div>
                        <div class="card-info-footer">
                            <h6 class="card-shelf-title" title="{title}">{title}</h6>
                        </div>
                    </div>
                </a>
            </div>
"""
    return ''.join(phtml.format(cid=str(channel["chat-id"]).replace("-100", ""), img=f"/api/thumb/{channel['chat-id']}", title=channel["title"], ctype=channel['type']) for channel in channels)


async def post_playlist(playlists):
    dhtml = """
    <div class="col">
        <div class="card netflix-shelf-card mb-3">
            <a href="" onclick="openEditPopupForm(event, '{img}', '{ctype}', '{cid}', '{title}')"
                class="admin-only edit-card-btn position-absolute top-0 end-0 m-2" data-bs-toggle="modal" data-bs-target="#editFolderModal">
                <i class="bi bi-pencil-square"></i>
            </a>
            <a href="/playlist?db={cid}" class="shelf-card-link">
                <div class="card-poster-wrapper">
                    <img src="{img}" class="card-poster-img lzy_img" data-src="{img}" alt="{title}">
                    <div class="card-badge-overlay">
                        <span class="badge-shelf-tag"><i class="bi bi-folder-fill me-1"></i>Collection</span>
                    </div>
                </div>
                <div class="card-info-footer">
                    <h6 class="card-shelf-title" title="{title}">{title}</h6>
                </div>
            </a>
        </div>
    </div>
    """
    return ''.join(dhtml.format(
        cid=playlist["_id"],
        img=playlist["thumbnail"] if playlist.get("thumbnail") and playlist["thumbnail"].strip() else "/api/logo",
        title=playlist["name"],
        ctype=playlist['parent_folder']
    ) for playlist in playlists)


async def posts_db_file(posts):
    phtml = """
    <div class="col">
        <div class="card netflix-shelf-card mb-3">
            <a href=""
                onclick="openPostEditPopupForm(event, '{img}', '{type}', '{size}', '{title}', '{cid}', '{ctype}')"
                class="admin-only edit-card-btn position-absolute top-0 end-0 m-2" data-bs-toggle="modal" data-bs-target="#editModal">
                <i class="bi bi-pencil-square"></i>
            </a>
            <a href="/watch/{chat_id}?id={id}&hash={hash}" class="shelf-card-link">
                <div class="card-poster-wrapper">
                    <img src="https://cdn.jsdelivr.net/gh/weebzone/weebzone/data/Surf-TG/src/loading.gif" data-src="{img}"
                        class="card-poster-img lzy_img" alt="{title}">
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
    return ''.join(phtml.format(cid=post["_id"], chat_id=str(post["chat_id"]).replace("-100", ""), id=post["file_id"], img=post.get("thumbnail") or "/api/logo", title=post["name"], hash=post["hash"], size=post['size'], type=post['file_type'], ctype=post["parent_folder"]) for post in posts)
