import asyncio
import json
import logging
import os
from dataclasses import dataclass, is_dataclass, asdict
from datetime import datetime
from typing import Dict

import peony.oauth
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.callback_data import CallbackData
from aiohttp import ClientSession, ClientTimeout
from peony import BasePeonyClient
from peony.oauth_dance import get_oauth_token, get_access_token

CHATS_PATH = os.getenv('CHATS_PATH') or "chats.json"


@dataclass
class Chat:
    id: int
    oauth_token: Dict


bot = Bot(token=os.environ['TELEGRAM_BOT_ID'])
dp = Dispatcher(bot)
chats: Dict[str, Chat] = dict()
rule_cb = CallbackData('rule', 'chat_id', 'rule_id', 'action')
forward_tasks = dict()


class TryAgain(Exception):
    pass


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if is_dataclass(o):
            return asdict(o)
        return super().default(o)


def serialize():
    with open(CHATS_PATH, "w") as fout:
        json.dump(chats, fout, cls=EnhancedJSONEncoder)


def deserialize():
    global chats
    if os.path.exists(CHATS_PATH):
        with open(CHATS_PATH, "r") as fout:
            chats = {k: Chat(**v) for k, v in json.load(fout).items()}

    logging.info(f"{chats=}")
    for chat in chats.values():
        forward_tasks[chat.id] = asyncio.get_event_loop().create_task(subscription_loop(chat))


async def forward_tweets(chat: Chat):
    client = BasePeonyClient(**chat.oauth_token,
                             auth=peony.oauth.OAuth2Headers,
                             api_version="2",
                             suffix="")

    async with ClientSession(timeout=ClientTimeout(24 * 60 * 60)) as session:
        url = f"https://api.twitter.com/2/tweets/search/stream"
        prepared = await client.headers.prepare_request('get', url)
        params = {"tweet.fields": "lang",
                  "media.fields": "url",
                  "expansions": "attachments.media_keys,author_id",
                  "user.fields": "username"}

        async with session.get(url, headers=prepared['headers'], params=params) as response:
            logging.info(f"{response=}")
            if response.status == 429:
                # too many requests
                end = datetime.utcfromtimestamp(int(response.headers['x-rate-limit-reset']))
                seconds = (end - datetime.utcnow()).total_seconds() + 1
                await bot.send_message(chat.id, text=f"Sleeping for {seconds} seconds until {end.isoformat()}")
                await asyncio.sleep(seconds)
                raise TryAgain()

            await bot.send_message(chat.id, text=f"Subscription started")
            async for line in response.content:
                if not line.strip():
                    continue

                logging.info(f"{line=}")
                data = json.loads(line.strip())
                # print(json.dumps(data, indent=2))
                text = data["data"]["text"]
                lang = data["data"]["lang"]
                urls = [i["url"] for i in data.get("includes", {}).get("media", []) if i.get("url")]
                users = [i["username"] for i in data.get("includes", {}).get("users", []) if i.get("username")]
                text = f"{', '.join(users)}: {text}"

                if urls:
                    if len(urls) > 1:
                        media = types.MediaGroup()
                        media.attach_photo(urls[0], caption=text)
                        list(map(media.attach_photo, urls[1:]))
                        await bot.send_media_group(chat.id, media=media)
                    else:
                        await bot.send_photo(chat.id, urls[0], caption=text)
                else:
                    await bot.send_message(chat.id, text=text)


async def subscription_loop(chat: Chat):
    while True:
        try:
            await forward_tweets(chat)
        except TryAgain:
            pass
        except asyncio.CancelledError:
            break
        except:
            logging.exception(f"Subscription loop failed")
            await asyncio.sleep(1)


@dp.message_handler(commands=['login'])
async def login_handler(message: types.Message):
    token = await get_oauth_token(os.getenv('CONSUMER_KEY'), os.getenv('CONSUMER_SECRET'), "oob")
    url = "https://api.twitter.com/oauth/authorize?oauth_token=" + token['oauth_token']
    chats[str(message.chat.id)] = Chat(id=message.chat.id, oauth_token=token)
    serialize()

    await message.reply(f"Please visit the following [link]({url}) to obtain the key and paste it in the chat",
                        parse_mode='markdown')


@dp.message_handler(regexp=r'^\d{7}')
async def auth_code_handler(message: types.Message) -> None:
    token = await get_access_token(
        os.getenv('CONSUMER_KEY'),
        os.getenv('CONSUMER_SECRET'),
        oauth_verifier=message.text.strip(),
        **chats[str(message.chat.id)].oauth_token
    )

    chats[str(message.chat.id)].oauth_token = dict(
        consumer_key=os.getenv('CONSUMER_KEY'),
        consumer_secret=os.getenv('CONSUMER_SECRET'),
        access_token=token['oauth_token'],
        access_token_secret=token['oauth_token_secret']
    )
    serialize()
    forward_tasks[message.chat.id] = asyncio.create_task(subscription_loop(chats[str(message.chat.id)]))

    await message.reply(f"Successfully logged in!")


@dp.message_handler(commands=['edit'])
async def edit_rules(message: types.Message):
    client = BasePeonyClient(**chats[str(message.chat.id)].oauth_token,
                             auth=peony.oauth.OAuth2Headers,
                             api_version="2",
                             suffix="")
    async with client:
        resp = await client.api.tweets.search.stream.rules.get()

    if not resp.get('data', []):
        return await message.reply(f"You have not set up any rules, paste text in the chat to add a rule")

    markup = types.InlineKeyboardMarkup()
    for term in resp.get('data', []):
        markup.add(
            types.InlineKeyboardButton(
                term["value"],
                callback_data=rule_cb.new(chat_id=message.chat.id, rule_id=term['id'], action='delete')),
        )

    await message.reply(f'Delete subscription rules', reply_markup=markup)


@dp.callback_query_handler(rule_cb.filter(action='delete'))
async def delete_rule(query: types.CallbackQuery, callback_data: Dict[str, str]):
    await query.answer(f"Deleting rule {callback_data['rule_id']}")
    chat = chats[str(callback_data['chat_id'])]
    client = BasePeonyClient(**chat.oauth_token,
                             auth=peony.oauth.OAuth2Headers,
                             api_version="2",
                             suffix="")
    async with client:
        body = {'delete': {'ids': [int(callback_data['rule_id'])]}}
        await client.api.tweets.search.stream.rules.post(_json=body)

    await query.message.edit_text(f"Successfully deleted rule {callback_data['rule_id']}")


@dp.message_handler(regexp=r'^\D')
async def add_rule_handler(message: types.Message) -> None:
    chat = chats[str(message.chat.id)]
    client = BasePeonyClient(**chat.oauth_token,
                             auth=peony.oauth.OAuth2Headers,
                             api_version="2",
                             suffix="")
    async with client:
        data = {'add': [{'value': message.text.strip()}]}
        await client.api.tweets.search.stream.rules.post(_json=data)

    await message.reply(f"Successfully added rule {message.text}")


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s:%(levelname)s:%(name)s:%(message)s')
    logging.getLogger().setLevel(logging.INFO)
    deserialize()
    executor.start_polling(dp, skip_updates=True)
