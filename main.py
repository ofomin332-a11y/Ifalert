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
API_TOKEN = os.getenv('ALERTS_API_TOKEN', '').strip()
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
    'zahidnimonitoring',  # Західний Моніторинг (неофіційне джерело)
    'totallzrada',        # Тотальна Зрада (неофіційний агрегатор)
    'ifalarm',            # ТРИВОГА ІФ (локальний канал тривог)
    'air_alert_ua',       # офіційний загальноукраїнський канал тривог
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
def extract_posts(page, handle):
    # Parse Telegram's public preview HTML robustly.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, 'html.parser')
    posts = []
    for node in soup.select('.tgme_widget_message_wrap'):
        msg = node.select_one('.tgme_widget_message')
        if not msg:
            continue
        post_id = msg.get('data-post')
        if not post_id:
            link = node.select_one('a.tgme_widget_message_date')
            href = link.get('href', '') if link else ''
            m = re.search(r't\.me/([^/]+/\d+)', href)
            if m:
                post_id = m.group(1)
        if not post_id:
            continue
        tm = node.select_one('time')
        when = tm.get('datetime', 'час не вказано') if tm else 'час не вказано'
        body_node = node.select_one('.tgme_widget_message_text')
        if not body_node:
            continue
        for br in body_node.select('br'):
            br.replace_with('\n')
        body = body_node.get_text('', strip=True)
        body = html.unescape(body).strip()
        if body:
            posts.append((post_id, when, body, f'https://t.me/{post_id}'))
    # Telegram page displays newest first; preserve only unique recent posts.
    unique = {}
    for post in posts:
        unique[post[0]] = post
    return list(unique.values())[-20:]

def should_forward(handle, body):
    """Forward local threat news, including western-route warnings that may affect the oblast.

    Telegram aggregators are unofficial. Keep the source label and original link on every post.
    """
    t = body.casefold()
    local_terms = (
        'івано-франків', 'івано франків', 'прикарпат', 'франківськ',
        'калуш', 'коломия', 'надвірна', 'долина', 'косів', 'верховина',
        'бурштин', 'галич', 'тлумач', 'рогатин', 'богородчан', 'яремче',
        'івано-франківська область', 'івано-франківський район'
    )
    western_terms = (
        'захід україни', 'західні області', 'західних област', 'західна україна',
        'львівщин', 'тернопільщин', 'закарпат', 'чернівеччин', 'волин', 'рівненщин',
        'карпат', 'прикарпат', 'хмельниччин'
    )
    other_region = (
        'київщина', 'харківщина', 'одещина', 'сумщина', 'дніпропетровщина',
        'полтавщина', 'чернігівщина', 'херсонщина', 'запоріжжя', 'донеччина',
        'луганщина', 'вінниччина'
    )
    threat_keywords = (
        'тривог', 'повітрян', 'ракета', 'баліст', 'шахед', 'дрон', 'бпла',
        'вибух', 'обстріл', 'відбій', 'укрит', 'ппо', 'загроз', 'зліт',
        'пуск', 'курс на', 'курсом на', 'рухається', 'рухаються', 'летить',
        'летять', 'напрям', 'ціль', 'цілі', 'повітряний простір'
    )
    local = any(term in t for term in local_terms)
    western = any(term in t for term in western_terms)
    threat = any(term in t for term in threat_keywords)
    route_context = any(term in t for term in (
        'курс', 'курсом', 'напрям', 'рухається', 'рухаються', 'летить', 'летять',
        'залітає', 'залітають', 'повз', 'через область', 'через захід'
    ))

    # Local official sources can send civic and threat updates, except posts clearly
    # about a different region with no local reference.
    official_local = {'martsinkiv_online', 'mrada_if_ua', 'onyshchuksvitlana'}
    if handle in official_local:
        if any(term in t for term in other_region) and not local:
            return False
        return True

    # Mandatory aggregators: include local threats and threat/flight-route updates
    # about western Ukraine that could be relevant to Ivano-Frankivsk. Other-region-only
    # posts without western/local/route context are excluded.
    aggregators = {'zahidnimonitoring', 'totallzrada'}
    if handle in aggregators:
        if not threat:
            return False
        if local:
            return True
        if western:
            return True
        if route_context and any(term in t for term in (
            'захід', 'західн', 'львів', 'терноп', 'закарпат', 'чернів',
            'волин', 'рівнен', 'карпат', 'хмельниц'
        )):
            return True
        return False

    # Other sources: require explicit local geography and threat-related content.
    if any(term in t for term in other_region) and not local:
        return False
    return local and threat


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
    if not API_TOKEN:
        log.warning('ALERTS_API_TOKEN is not set: Alerts.in.ua API disabled; using public Telegram sources only. Coverage is not guaranteed.')
    previous = None
    fingerprints = {}
    etag = None
    seen_channels = {}
    last_channel_poll = 0.0
    async with aiohttp.ClientSession(headers={'User-Agent':'Mozilla/5.0 IF-Alert-Monitor/3.0'}) as session:
        while True:
            try:
                if API_TOKEN:
                    alerts, etag = await fetch_alerts(session, etag)
                else:
                    alerts = None
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
