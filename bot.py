import asyncio
import json
import logging
import os
from dataclasses import dataclass, is_dataclass, asdict
from typing import Dict

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.callback_data import CallbackData
from aiohttp import ClientSession
from tweety.bot import Twitter, UserTweets, Tweet
from tweety.builder import UrlBuilder

CHATS_PATH = os.getenv('CHATS_PATH') or "chats.json"

TWEET_PARAMS = {"tweet.fields": "lang",
                "media.fields": "url",
                "expansions": "attachments.media_keys,author_id",
                "user.fields": "username"}


@dataclass
class Chat:
    id: int
    last_sent_id: Dict[str, int]
    subscriptions: Dict[str, int]


bot = Bot(token=os.environ['TELEGRAM_BOT_ID'])
dp = Dispatcher(bot)
chats: Dict[str, Chat] = dict()
rule_cb = CallbackData('rule', 'chat_id', 'rule_id', 'action')
search_cb = CallbackData('search', 'chat_id', 'rule_id')
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


async def send_tweet(data: Tweet, chat: Chat):
    urls = [m['direct_url'] for m in data.media]
    text = f"{data.author.username}: {data.text}"

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


async def get_tweets(user_id: int):
    async with ClientSession() as session:
        async def _call(p: dict):
            async with session.request(
                    method=p['method'].lower(),
                    url=p['url'],
                    headers=p['headers']) as r:
                return await r.json()

        builder = UrlBuilder()
        token = await _call(builder.get_guest_token())

        builder.guest_token = token['guest_token']

        data = await _call(builder.user_tweets(user_id=user_id, replies=False, cursor=None))

    result = list()
    for entry in UserTweets._get_entries(data):
        result.extend([Tweet(data, t, None) for t in UserTweets._get_tweet_content_key(entry)])

    return result


async def forward_tweets():
    while True:
        to_send = list()
        for chat in chats.values():
            results = await asyncio.gather(*list(map(get_tweets, chat.subscriptions.values())))
            for tweets, user_id in zip(results, chat.subscriptions.values()):
                last_sent = chat.last_sent_id.get(str(user_id), 0)
                tweets = filter(lambda x: x.media, tweets)
                tweets = filter(lambda x: int(x.id) > last_sent, tweets)
                for tweet in tweets:
                    to_send.append((chat, tweet, user_id))

        for chat, tweet, user_id in reversed(to_send):
            await send_tweet(tweet, chat)
            chat.last_sent_id[str(user_id)] = max(int(tweet.id), chat.last_sent_id.get(str(user_id), 0))

        if to_send:
            serialize()

        await asyncio.sleep(60)


async def subscription_loop():
    while True:
        try:
            await forward_tweets()
        except TryAgain:
            pass
        except asyncio.CancelledError:
            break
        except:
            logging.exception(f"Subscription loop failed")
            await asyncio.sleep(1)


@dp.message_handler(commands=['edit'])
async def edit_rules(message: types.Message):
    markup = types.InlineKeyboardMarkup()
    chat = chats[str(message.chat.id)]
    for name in chat.subscriptions.keys():
        markup.add(
            types.InlineKeyboardButton(
                name,
                callback_data=rule_cb.new(chat_id=message.chat.id, rule_id=name, action='delete')),
        )

    await message.reply(f'Delete subscription rules', reply_markup=markup)


@dp.callback_query_handler(rule_cb.filter(action='delete'))
async def delete_rule(query: types.CallbackQuery, callback_data: Dict[str, str]):
    await query.answer(f"Deleting rule {callback_data['rule_id']}")
    chat = chats[str(callback_data['chat_id'])]
    chat.subscriptions.pop(callback_data['rule_id'])
    serialize()
    await query.message.edit_text(f"Successfully deleted rule {callback_data['rule_id']}")


@dp.message_handler(commands=['search'])
async def search_menu(message: types.Message):
    if str(message.chat.id) not in chats:
        chats[str(message.chat.id)] = Chat(id=message.chat.id, last_sent_id={}, subscriptions={})
    chat = chats[str(message.chat.id)]
    if not chat.subscriptions:
        return await message.reply(f"You have not set up any rules, paste text in the chat to add a rule")

    markup = types.InlineKeyboardMarkup()
    for term in chat.subscriptions.keys():
        markup.add(
            types.InlineKeyboardButton(
                term,
                callback_data=search_cb.new(chat_id=message.chat.id, rule_id=term)),
        )

    await message.reply(f'Pick a rule to use for search', reply_markup=markup)


@dp.callback_query_handler(search_cb.filter())
async def search_by_rule(query: types.CallbackQuery, callback_data: Dict[str, str]):
    await query.answer(f"Fetching rules")

    chat = chats[str(callback_data['chat_id'])]
    api = Twitter()
    for name in chat.subscriptions.keys():
        for tweet in api.get_tweets(name):
            if not tweet.media:
                continue

            await send_tweet(tweet, chat)


@dp.message_handler(regexp=r'^[\D/]')
async def add_rule_handler(message: types.Message) -> None:
    if str(message.chat.id) not in chats:
        chats[str(message.chat.id)] = Chat(id=message.chat.id, last_sent_id={}, subscriptions={})

    chat = chats[str(message.chat.id)]
    chat.subscriptions[message.text] = Twitter().get_user_info(message.text).rest_id

    serialize()

    await message.reply(f"Successfully added rule {message.text}")


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s:%(levelname)s:%(name)s:%(message)s')
    logging.getLogger().setLevel(logging.INFO)
    deserialize()
    asyncio.get_event_loop().create_task(subscription_loop())
    executor.start_polling(dp, skip_updates=True)
