import asyncio
import json
import logging
import os
from dataclasses import dataclass, is_dataclass, asdict, field
from typing import Dict, List

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
    filters: Dict[str, List[str]] = field(default_factory=lambda: {})
    awaiting_filter: str = ''


bot = Bot(token=os.environ['TELEGRAM_BOT_ID'])
dp = Dispatcher(bot)
chats: Dict[str, Chat] = dict()
edit_subscription = CallbackData('subscription', 'chat_id', 'name', 'action')
edit_filter = CallbackData('filter', 'chat_id', 'subscription', 'action', 'idx')
search_cb = CallbackData('search', 'chat_id', 'subscription')
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


async def get_tweets(user_id: int) -> List[Tweet]:
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
            for tweets, (user_name, user_id) in zip(results, chat.subscriptions.items()):
                filters = chat.filters.get(user_name, [])

                last_sent = chat.last_sent_id.get(str(user_id), 0)
                tweets = filter(lambda x: x.media, tweets)
                tweets = filter(lambda x: int(x.id) > last_sent, tweets)
                tweets = filter(lambda x: not filters or any(map(lambda term: term.lower() in x.text.lower(), filters)),
                                tweets)

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
                callback_data=edit_subscription.new(chat_id=message.chat.id, name=name, action='edit')),
        )

    await message.reply(f'Delete subscription rules', reply_markup=markup)


@dp.callback_query_handler(edit_subscription.filter(action='edit'))
async def edit_rule(query: types.CallbackQuery, callback_data: Dict[str, str]):
    await query.answer(f"Editing filters for subscription {callback_data['name']}")
    chat = chats[str(callback_data['chat_id'])]

    markup = types.InlineKeyboardMarkup()
    for idx, filter_term in enumerate(chat.filters.get(callback_data['name'], [])):
        markup.add(
            types.InlineKeyboardButton(
                f"Delete {filter_term}",
                callback_data=edit_filter.new(chat_id=callback_data['chat_id'],
                                              subscription=callback_data['name'],
                                              idx=idx,
                                              action='delete')),
        )

    markup.add(
        types.InlineKeyboardButton(
            f"Add new filter",
            callback_data=edit_filter.new(chat_id=callback_data['chat_id'],
                                          subscription=callback_data['name'],
                                          idx=0,
                                          action='add')),
    )

    await query.message.reply(f'Edit filters', reply_markup=markup)


@dp.callback_query_handler(edit_filter.filter(action='add'))
async def add_filter(query: types.CallbackQuery, callback_data: Dict[str, str]):
    await query.message.reply(f"Send your filter text")
    chat = chats[str(callback_data['chat_id'])]
    chat.awaiting_filter = callback_data['subscription']
    serialize()


@dp.callback_query_handler(edit_filter.filter(action='delete'))
async def delete_filter(query: types.CallbackQuery, callback_data: Dict[str, str]):
    chat = chats[str(callback_data['chat_id'])]
    chat.filters[callback_data['subscription']].pop(int(callback_data['idx']))
    serialize()
    await query.message.reply(f"Removed {callback_data['idx']} index from filters")


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
                callback_data=search_cb.new(chat_id=message.chat.id, subscription=term)),
        )

    await message.reply(f'Pick a rule to use for search', reply_markup=markup)


@dp.callback_query_handler(search_cb.filter())
async def search_by_rule(query: types.CallbackQuery, callback_data: Dict[str, str]):
    await query.answer(f"Fetching tweets")

    chat = chats[str(callback_data['chat_id'])]
    subscription = chat.subscriptions[callback_data['subscription']]
    filters = chat.filters.get(callback_data['subscription'], [])

    api = Twitter()

    for tweet in api.get_tweets(subscription):
        if not tweet.media:
            continue

        if not filters or any(map(lambda term: term.lower() in tweet.text.lower(), filters)):
            await send_tweet(tweet, chat)
            break


@dp.message_handler()
async def handle_input(message: types.Message) -> None:
    if str(message.chat.id) not in chats:
        chats[str(message.chat.id)] = Chat(id=message.chat.id, last_sent_id={}, subscriptions={})

    chat = chats[str(message.chat.id)]

    if chat.awaiting_filter:
        chat.filters[chat.awaiting_filter] = chat.filters.get(chat.awaiting_filter, []) + [message.text]
        chat.awaiting_filter = ''
        await message.reply(f"Successfully added filter {message.text}")
    else:
        chat.subscriptions[message.text] = Twitter().get_user_info(message.text).rest_id
        await message.reply(f"Successfully added subscription {message.text}")

    serialize()


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s:%(levelname)s:%(name)s:%(message)s')
    logging.getLogger().setLevel(logging.INFO)
    deserialize()
    asyncio.get_event_loop().create_task(subscription_loop())
    executor.start_polling(dp, skip_updates=True)
