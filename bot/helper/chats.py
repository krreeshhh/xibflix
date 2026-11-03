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

