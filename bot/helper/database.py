import certifi
from pymongo import DESCENDING, MongoClient
from bot.config import Telegram
from bot import LOGGER
import re


class Database:
    def __init__(self):
        MONGODB_URI = Telegram.DATABASE_URL
        self.mongo_client = MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["surftg"]
        self.config = self.db["config"]
        self.files = self.db["files"]
        try:
            if "playlist" in self.db.list_collection_names():
                self.db.drop_collection("playlist")
                LOGGER.info("Dropped legacy playlist collection from MongoDB")
        except Exception:
            pass

    async def update_config(self, theme, auth_channel):
        bot_id = Telegram.BOT_TOKEN.split(":", 1)[0]
        config = self.config.find_one({"_id": bot_id})
        if config is None:
            result = self.config.insert_one(
                {"_id": bot_id, "theme": theme, "auth_channel": auth_channel})
            return result.inserted_id is not None
        else:
            result = self.config.update_one({"_id": bot_id}, {
                "$set": {"theme": theme, "auth_channel": auth_channel}})
            return result.modified_count > 0

    async def get_variable(self, key):
        bot_id = Telegram.BOT_TOKEN.split(":", 1)[0]
        config = self.config.find_one({"_id": bot_id})
        return config.get(key) if config is not None else None

    async def list_tgfiles(self, id, page=1, per_page=50):
        query = {'chat_id': id}
        offset = (int(page) - 1) * per_page
        mydoc = self.files.find(query).sort(
            'msg_id', DESCENDING).skip(offset).limit(per_page)
        return list(mydoc)

    async def add_tgfiles(self, chat_id, file_id, hash, name, size, file_type):
        if fetch_old := self.files.find_one({"chat_id": chat_id, "hash": hash}):
            return
        file = {"chat_id": chat_id, "msg_id": file_id,
                "hash": hash, "title": name, "size": size, "type": file_type}
        self.files.insert_one(file)


    async def search_tgfiles(self, id, query, page=1, per_page=50):
        words = re.findall(r'\w+', query.lower())
        regex_pattern = '.*'.join(f'(?=.*{re.escape(word)})' for word in words)
        regex_query = {'$regex': f'.*{regex_pattern}.*', '$options': 'i'}
        query = {'chat_id': id, 'title': regex_query}
        offset = (int(page) - 1) * per_page
        mydoc = self.files.find(query).sort(
            'msg_id', DESCENDING).skip(offset).limit(per_page)
        return list(mydoc)
    
    async def add_btgfiles(self, data):
        result = self.files.insert_many(data)

    def _get_variants(self, val):
        if val is None:
            return []
        s_val = str(val).strip()
        variants = {s_val}
        if s_val.startswith("-100"):
            variants.add(s_val[4:])
        elif s_val.startswith("-"):
            variants.add(s_val[1:])
        else:
            variants.add(f"-100{s_val}")
        for v in list(variants):
            if v.lstrip('-').isdigit():
                try:
                    variants.add(int(v))
                except Exception:
                    pass
        return list(variants)

    def delete_file(self, chat_id=None, msg_id=None, hash=None):
        query = {}
        if chat_id is not None:
            query["chat_id"] = {"$in": self._get_variants(chat_id)}
        if msg_id is not None:
            s_msg = str(msg_id)
            query["msg_id"] = {"$in": [s_msg, int(s_msg)] if s_msg.isdigit() else [s_msg]}
        if hash is not None:
            query["hash"] = str(hash)
        if not query:
            return False
        try:
            res = self.files.delete_many(query)
            if res.deleted_count > 0:
                LOGGER.info(f"Deleted {res.deleted_count} file(s) from database index for chat={chat_id}, msg_id={msg_id}")
            return res.deleted_count > 0
        except Exception as e:
            LOGGER.error(f"delete_file error: {e}")
            return False

    def delete_file_by_msg_id(self, msg_id):
        if msg_id is None:
            return False
        s_msg = str(msg_id)
        query = {"msg_id": {"$in": [s_msg, int(s_msg)] if s_msg.isdigit() else [s_msg]}}
        try:
            res = self.files.delete_many(query)
            return res.deleted_count > 0
        except Exception as e:
            LOGGER.error(f"delete_file_by_msg_id error: {e}")
            return False

