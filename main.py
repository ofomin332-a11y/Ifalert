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
CHANNEL_POLL_SECONDS = max(10, int(os.getenv('CHANNEL_POLL_SECONDS', '10')))
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
    'truexafrankivsk',    # TrueX Івано-Франківськ
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
    """Forward only threat-related posts explicitly mentioning Ivano-Frankivsk region."""
    t = body.casefold()
    local_terms = (
        'івано-франків', 'івано франків', 'іванофранків', 'прикарпат', 'франківськ',
        'калуш', 'коломия', 'надвірна', 'долина', 'косів', 'верховина',
        'бурштин', 'галич', 'тлумач', 'рогатин', 'богородчан', 'яремче',
        'івано-франківська область', 'івано-франківський район', 'івано-франківщин',
        'івано-франківську область', 'на прикарпатті'
    )
    other_region = (
        'київщина', 'харківщина', 'одещина', 'сумщина', 'дніпропетровщина',
        'полтавщина', 'чернігівщина', 'херсонщина', 'запоріжжя', 'донеччина',
        'луганщина', 'вінниччина', 'львівщин', 'тернопільщин', 'закарпат',
        'чернівеччин', 'волин', 'рівненщин', 'хмельниччин'
    )
    threat_keywords = (
        'тривог', 'повітрян', 'ракета', 'баліст', 'шахед', 'дрон', 'бпла',
        'вибух', 'обстріл', 'відбій', 'укрит', 'ппо', 'загроз', 'зліт',
        'пуск', 'курс на', 'курсом на', 'рухається', 'рухаються', 'летить',
        'летять', 'напрям', 'ціль', 'цілі', 'повітряний простір', 'вибухи'
    )
    local = any(term in t for term in local_terms)
    threat = any(term in t for term in threat_keywords)
    # Avoid forwarding posts clearly about another region even if they mention a local term incidentally.
    if any(term in t for term in other_region) and not any(term in t for term in local_terms):
        return False
    return local and threat


async def poll_one_channel(session, bot, handle, seen):
    """Poll one public Telegram channel independently so sources run concurrently."""
    result = {
        'checked': 0, 'failed': 0, 'posts_found': 0, 'new': 0,
        'forwarded': 0, 'filtered': 0, 'baselines': 0,
    }
    url = f'https://t.me/s/{handle}'
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            r.raise_for_status()
            page = await r.text()
        posts = extract_posts(page, handle)
        result['checked'] = 1
        result['posts_found'] = len(posts)

        # First successful read establishes baseline to avoid dumping old posts.
        if handle not in seen:
            seen[handle] = {p[0] for p in posts}
            result['baselines'] = 1
            log.info('Channel baseline established: @%s (%d recent posts)', handle, len(posts))
            return result

        known = seen[handle]
        new_posts = [p for p in posts if p[0] not in known]
        result['new'] = len(new_posts)
        for post_id, when, body, link in new_posts:
            known.add(post_id)
            if should_forward(handle, body):
                message = (f'📡 Джерело: <b>@{html.escape(handle)}</b>\n\n'
                           f'{html.escape(body[:2600])}')
                try:
                    await send(bot, message)
                    result['forwarded'] += 1
                    log.info('Telegram forward OK: @%s post=%s', handle, post_id)
                except TelegramError:
                    log.exception('Telegram forward failed for @%s post=%s', handle, post_id)
            else:
                result['filtered'] += 1
                log.info('New post filtered: @%s post=%s (not matching local threat rules)', handle, post_id)

        # Limit memory; channel page only exposes recent posts.
        if len(known) > 500:
            seen[handle] = set(list(known)[-250:])
    except asyncio.CancelledError:
        raise
    except Exception:
        result['failed'] = 1
        log.exception('Public Telegram source poll failed: @%s', handle)
    return result


async def poll_channels(session, bot, seen):
    # Query every source concurrently; a slow channel won't delay the others.
    results = await asyncio.gather(*(
        poll_one_channel(session, bot, handle, seen) for handle in CHANNELS
    ))
    totals = {key: sum(item[key] for item in results) for key in results[0]} if results else {}
    log.info(
        'Channel poll complete: checked=%d/%d failed=%d posts_on_pages=%d new=%d forwarded=%d filtered=%d baselines=%d interval=%ss',
        totals.get('checked', 0), len(CHANNELS), totals.get('failed', 0),
        totals.get('posts_found', 0), totals.get('new', 0), totals.get('forwarded', 0),
        totals.get('filtered', 0), totals.get('baselines', 0), CHANNEL_POLL_SECONDS
    )

async def channel_monitor(session, bot, seen_channels):
    """Poll public channels on a 10-second cadence, independently of the alert API loop."""
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await poll_channels(session, bot, seen_channels)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Channel monitor cycle failed')
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(0.0, CHANNEL_POLL_SECONDS - elapsed))


async def main():
    bot = Bot(token=BOT_TOKEN)
    if not API_TOKEN:
        log.warning('ALERTS_API_TOKEN is not set: Alerts.in.ua API disabled; using public Telegram sources only. Coverage is not guaranteed.')
    log.info('Monitor started: channels=%d channel_interval=%ss local_threat_filter=enabled', len(CHANNELS), CHANNEL_POLL_SECONDS)
    previous = None
    fingerprints = {}
    etag = None
    seen_channels = {}
    async with aiohttp.ClientSession(headers={'User-Agent':'Mozilla/5.0 IF-Alert-Monitor/3.0'}) as session:
        channel_task = asyncio.create_task(channel_monitor(session, bot, seen_channels))
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

            await asyncio.sleep(POLL_SECONDS)

if __name__ == '__main__':
    asyncio.run(main())
