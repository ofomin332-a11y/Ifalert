import asyncio, logging, os
from datetime import datetime
import aiohttp
from telegram import Bot
from telegram.error import TelegramError

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('if-alert-monitor')
BOT_TOKEN=os.environ['TELEGRAM_BOT_TOKEN']; CHAT_ID=os.environ['TELEGRAM_CHAT_ID']; API_TOKEN=os.environ['ALERTS_API_TOKEN']
POLL_SECONDS=max(10,int(os.getenv('POLL_SECONDS','10')))
API_URL='https://api.alerts.in.ua/v1/alerts/active.json'; IF_UID='13'

def fmt(v):
    if not v: return 'не вказано'
    try: return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone().strftime('%d.%m.%Y %H:%M:%S')
    except (ValueError,TypeError): return str(v)
def relevant(a):
    return str(a.get('location_oblast_uid',''))==IF_UID or (str(a.get('location_uid',''))==IF_UID and a.get('location_type')=='oblast')
def key(a): return str(a.get('id') or '|'.join(str(a.get(k,'')) for k in ('location_uid','alert_type','started_at')))
def place(a): return a.get('location_title') or a.get('location_raion') or a.get('location_oblast') or 'Івано-Франківщина'
def threats(a):
    items=a.get('threats')
    if not isinstance(items,list) or not items: return 'ℹ️ Тип загрози в цьому записі не уточнено.'
    return '🛰️ <b>Дані про загрозу:</b>\n'+'\n'.join('• '+str(x.get('source_message') or x.get('threat_type') or 'повітряна загроза') for x in items if isinstance(x,dict))
def start_text(a):
    notes=str(a.get('notes') or '').strip(); extra=f'\n📝 Примітка джерела: {notes}' if notes else ''
    return f"🔴 <b>НОВЕ ОФІЦІЙНЕ СПОВІЩЕННЯ</b>\n📍 Локація: <b>{place(a)}</b>\n⚠️ Тип: {a.get('alert_type','загроза')}\n🕒 Початок за джерелом: {fmt(a.get('started_at'))}\n{threats(a)}\n🔗 <a href=\"https://devs.alerts.in.ua/\">Джерело: Alerts.in.ua API</a>{extra}\n\nПід час тривоги прямуйте в укриття. Бот не визначає самостійно траєкторії."
def update_text(a): return f"🔄 <b>ОНОВЛЕННЯ ЗАГРОЗИ</b>\n📍 Локація: <b>{place(a)}</b>\n🕒 Оновлено в джерелі: {fmt(a.get('updated_at'))}\n{threats(a)}\n🔗 <a href=\"https://devs.alerts.in.ua/\">Джерело: Alerts.in.ua API</a>"
def end_text(a): return f"🟢 <b>ОНОВЛЕННЯ СТАТУСУ</b>\n📍 Локація: <b>{place(a)}</b>\nℹ️ Запис більше не присутній у списку активних загроз API.\n🕒 Перевірено: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M:%S')}\nПеревірте офіційне сповіщення про відбій."
async def fetch(session,etag):
    headers={'Authorization':f'Bearer {API_TOKEN}'}
    if etag: headers['If-None-Match']=etag
    async with session.get(API_URL,headers=headers,timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status==304: return None,etag
        r.raise_for_status(); new_etag=r.headers.get('ETag',etag); data=await r.json(content_type=None)
    alerts=data.get('alerts') if isinstance(data,dict) else None
    if not isinstance(alerts,list): raise ValueError('Unexpected API response; preserving previous state')
    return [a for a in alerts if isinstance(a,dict) and relevant(a)],new_etag
async def send(bot,text): await bot.send_message(chat_id=CHAT_ID,text=text,parse_mode='HTML',disable_web_page_preview=True)
async def main():
    bot=Bot(token=BOT_TOKEN); previous=None; fingerprints={}; etag=None
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                alerts,etag=await fetch(session,etag)
                if alerts is None:
                    await asyncio.sleep(POLL_SECONDS); continue
                current={key(a):a for a in alerts}
                fp={k:repr((a.get('alert_type'),a.get('alert_level'),a.get('threats'),a.get('notes'))) for k,a in current.items()}
                if previous is None:
                    previous=current; fingerprints=fp; log.info('Baseline established: %d active Ivano-Frankivsk alert(s)',len(current))
                else:
                    for k,a in current.items():
                        try:
                            if k not in previous: await send(bot,start_text(a))
                            elif fp.get(k)!=fingerprints.get(k): await send(bot,update_text(a))
                        except TelegramError: log.exception('Telegram send failed')
                    for k,a in previous.items():
                        if k not in current:
                            try: await send(bot,end_text(a))
                            except TelegramError: log.exception('Telegram status update failed')
                    previous,fingerprints=current,fp
            except asyncio.CancelledError: raise
            except Exception: log.exception('Polling failed; preserving previous known state')
            await asyncio.sleep(POLL_SECONDS)
if __name__=='__main__': asyncio.run(main())
