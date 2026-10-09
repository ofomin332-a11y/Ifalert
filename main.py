import asyncio, logging, os, re, html
from datetime import datetime
from urllib.parse import urlparse
import aiohttp
from telegram import Bot
from telegram.error import TelegramError

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('if-alert-monitor')

BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
API_TOKEN = os.environ['ALERTS_API_TOKEN']
POLL_SECONDS = max(10, int(os.getenv('POLL_SECONDS', '10')))
CHANNEL_POLL_SECONDS = max(30, int(os.getenv('CHANNEL_POLL_SECONDS', '60')))
API_URL = 'https://api.alerts.in.ua/v1/alerts/active.json'
IF_UID = '13'

# Public Telegram channel pages (t.me/s/<handle>). Add more comma-separated handles
# through TELEGRAM_CHANNELS. Do not include @ or URLs.
DEFAULT_CHANNELS = [
    'martsinkiv_online',  # Руслан Марцінків
    'mrada_if_ua',        # Івано-Франківська міська рада
    'onyshchuksvitlana',  # Голова Івано-Франківської ОВА
    'totallzrada',        # Тотальна Зрада (неофіційний агрегатор)
]
CHANNELS = list(dict.fromkeys(
    x.strip().lstrip('@').strip('/')
    for x in (','.join(DEFAULT_CHANNELS) + ',' + os.getenv('TELEGRAM_CHANNELS', '')).split(',')
    if x.strip()
))
# For a council website/RSS or the exact "Західний" channel, add a verified URL/feed
# in OFFICIAL_FEEDS after confirming the correct public endpoint.
OFFICIAL_FEEDS = [x.strip() for x in os.getenv('OFFICIAL_FEEDS', '').split(',') if x.strip()]
MAX_MESSAGE_CHARS = 3500

def fmt(v):
    if not v: return 'не вказано'
    try: return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone().strftime('%d.%m.%Y %H:%M:%S')
    except (ValueError, TypeError): return str(v)

def relevant(a):
    return str(a.get('location_oblast_uid','')) == IF_UID or (
        str(a.get('location_uid','')) == IF_UID and a.get('location_type') == 'oblast'
    )

def key(a):
    return str(a.get('id') or '|'.join(str(a.get(k,'')) for k in ('location_uid','alert_type','started_at')))

def place(a):
    return a.get('location_title') or a.get('location_raion') or a.get('location_oblast') or 'Івано-Франківщина'

def threats(a):
    items = a.get('threats')
    if not isinstance(items, list) or not items:
        return 'ℹ️ Тип загрози в цьому записі не уточнено.'
    vals = [str(x.get('source_message') or x.get('threat_type') or 'повітряна загроза')
            for x in items if isinstance(x, dict)]
    return '🛰️ <b>Дані про загрозу:</b>\n' + '\n'.join('• ' + html.escape(v) for v in vals)

def start_text(a):
    notes = str(a.get('notes') or '').strip()
    extra = f'\n📝 Примітка джерела: {html.escape(notes)}' if notes else ''
    return (f"🔴 <b>НОВЕ ОФІЦІЙНЕ СПОВІЩЕННЯ</b>\n📍 Локація: <b>{html.escape(str(place(a)))}</b>"
            f"\n⚠️ Тип: {html.escape(str(a.get('alert_type','загроза')))}"
            f"\n🕒 Початок за джерелом: {fmt(a.get('started_at'))}\n{threats(a)}"
            f'\n🔗 <a href="https://devs.alerts.in.ua/">Джерело: Alerts.in.ua API</a>{extra}'
            '\n\nПід час тривоги прямуйте в укриття. Бот не визначає самостійно траєкторії.')

def update_text(a):
    return (f"🔄 <b>ОНОВЛЕННЯ ЗАГРОЗИ</b>\n📍 Локація: <b>{html.escape(str(place(a)))}</b>"
            f"\n🕒 Оновлено в джерелі: {fmt(a.get('updated_at'))}\n{threats(a)}"
            '\n🔗 <a href="https://devs.alerts.in.ua/">Джерело: Alerts.in.ua API</a>')

def end_text(a):
    return (f"🟢 <b>ОНОВЛЕННЯ СТАТУСУ</b>\n📍 Локація: <b>{html.escape(str(place(a)))}</b>"
            '\nℹ️ Запис більше не присутній у списку активних загроз API.'
            f"\n🕒 Перевірено: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M:%S')}"
            '\nПеревірте офіційне сповіщення про відбій.')

async def fetch_alerts(session, etag):
    headers = {'Authorization': f'Bearer {API_TOKEN}'}
    if etag: headers['If-None-Match'] = etag
    async with session.get(API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status == 304: return None, etag
        r.raise_for_status()
        new_etag = r.headers.get('ETag', etag)
        data = await r.json(content_type=None)
    alerts = data.get('alerts') if isinstance(data, dict) else None
    if not isinstance(alerts, list): raise ValueError('Unexpected API response; preserving previous state')
    return [a for a in alerts if isinstance(a, dict) and relevant(a)], new_etag

async def send(bot, text):
    await bot.send_message(chat_id=CHAT_ID, text=text[:MAX_MESSAGE_CHARS],
                           parse_mode='HTML', disable_web_page_preview=True)

# Parse public Telegram web previews only. This is not the Telegram Bot API and may
# fail if Telegram changes page markup or restricts access; errors are logged.
POST_RE = re.compile(r'<div class="tgme_widget_message_wrap[^"]*"[^>]*>.*?</div>\s*</div>\s*</div>', re.S)
def extract_posts(page, handle):
    posts = []
    for block in POST_RE.findall(page):
        m = re.search(r'data-post="([^"]+)"', block)
        if not m: continue
        post_id = m.group(1)
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        when = tm.group(1) if tm else 'час не вказано'
        body_m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not body_m: continue
        body = body_m.group(1)
        body = re.sub(r'<br\s*/?>', '\n', body)
        body = re.sub(r'<[^>]+>', '', body)
        body = html.unescape(body).strip()
        body = re.sub(r'\n{3,}', '\n\n', body)
        if body:
            posts.append((post_id, when, body, f'https://t.me/{post_id}'))
    return posts[-20:]

def should_forward(handle, body):
    # Keep safety alerts, public local-authority updates, and potentially relevant regional
    # news. This is a keyword filter, not a fact-checker.
    t = body.casefold()
    keywords = ('івано-франків', 'прикарпат', 'тривог', 'повітрян', 'ракета', 'шахед',
                'дрон', 'бпла', 'вибух', 'обстріл', 'відбій', 'укрит', 'марцінків',
                'міська рада', 'обласна рада', 'ова', 'енерг', 'відключ')
    if handle in ('martsinkiv_online', 'mrada_if_ua', 'onyshchuksvitlana'):
        return True
    return any(k in t for k in keywords)

async def poll_channels(session, bot, seen):
    for handle in CHANNELS:
        url = f'https://t.me/s/{handle}'
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                page = await r.text()
            posts = extract_posts(page, handle)
            # First successful read establishes baseline to avoid dumping old posts.
            if handle not in seen:
                seen[handle] = {p[0] for p in posts}
                log.info('Channel baseline established: @%s (%d recent posts)', handle, len(posts))
                continue
            known = seen[handle]
            new_posts = [p for p in posts if p[0] not in known]
            for post_id, when, body, link in new_posts:
                known.add(post_id)
                if should_forward(handle, body):
                    text = (f'📣 <b>НОВА ПУБЛІКАЦІЯ</b>\n'
                            f'📡 Джерело: <b>@{html.escape(handle)}</b>\n'
                            f'🕒 Час публікації: {html.escape(when)}\n\n'
                            f'{html.escape(body[:2600])}\n\n🔗 <a href="{link}">Відкрити оригінал</a>')
                    try:
                        await send(bot, text)
                    except TelegramError:
                        log.exception('Telegram forward failed for @%s', handle)
            # Limit memory; channel page only exposes recent posts.
            if len(known) > 500: seen[handle] = set(list(known)[-250:])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Public Telegram source poll failed: @%s', handle)

async def main():
    bot = Bot(token=BOT_TOKEN)
    previous = None
    fingerprints = {}
    etag = None
    seen_channels = {}
    last_channel_poll = 0.0
    async with aiohttp.ClientSession(headers={'User-Agent':'Mozilla/5.0 IF-Alert-Monitor/3.0'}) as session:
        while True:
            try:
                alerts, etag = await fetch_alerts(session, etag)
                if alerts is not None:
                    current = {key(a): a for a in alerts}
                    fp = {k: repr((a.get('alert_type'), a.get('alert_level'), a.get('threats'), a.get('notes')))
                          for k, a in current.items()}
                    if previous is None:
                        previous, fingerprints = current, fp
                        log.info('Baseline established: %d active Ivano-Frankivsk alert(s)', len(current))
                    else:
                        for k, a in current.items():
                            try:
                                if k not in previous: await send(bot, start_text(a))
                                elif fp.get(k) != fingerprints.get(k): await send(bot, update_text(a))
                            except TelegramError: log.exception('Telegram alert send failed')
                        for k, a in previous.items():
                            if k not in current:
                                try: await send(bot, end_text(a))
                                except TelegramError: log.exception('Telegram status update failed')
                        previous, fingerprints = current, fp
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Alert API polling failed; preserving previous known state')

            now = asyncio.get_running_loop().time()
            if now - last_channel_poll >= CHANNEL_POLL_SECONDS:
                await poll_channels(session, bot, seen_channels)
                last_channel_poll = now
            await asyncio.sleep(POLL_SECONDS)

if __name__ == '__main__':
    asyncio.run(main())
