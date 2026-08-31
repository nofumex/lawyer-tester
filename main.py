from __future__ import annotations

import logging
import signal
import threading
import time
from html import escape

from admin import Admin
from amocrm import AmoClient
from config import Config, load_dotenv
from engine import SurveyEngine
from seed import seed_default_test
from storage import Storage
from transports import MaxTransport, TelegramTransport, Transport


def answer_callback_best_effort(transport:Transport, callback_id:str, text:str='', *, inline:list[list[dict[str,str]]]|None=None) -> None:
    try:
        transport.answer_callback(callback_id,text,inline=inline)
    except Exception:
        logging.warning('Callback acknowledgement failed (%s); continuing update processing',transport.platform,exc_info=True)


def handle(transport:Transport, update:dict, engine:SurveyEngine, admin:Admin, config:Config) -> None:
    message=update.get('message') or update.get('callback_query',{}).get('message') or {}
    sender=(update.get('message') or update.get('callback_query',{}).get('from') or {}).get('from') or update.get('callback_query',{}).get('from') or {}
    user_id=str(sender.get('id') or message.get('chat',{}).get('id') or '')
    if not user_id: return
    name=' '.join(filter(None,[sender.get('first_name'),sender.get('last_name')])) or sender.get('username')
    callback=(update.get('callback_query') or {}).get('data')
    callback_query=update.get('callback_query') or {}
    text=(update.get('message') or {}).get('text','').strip()
    is_admin=user_id in config.admin_ids
    if callback and (callback.startswith('survey:') or callback.startswith('review:')):
        reply,prompt,edit=engine.receive_callback(transport.platform,user_id,callback)
        max_callback_edit=transport.platform=='max' and prompt is not None and edit and prompt.inline is not None
        if callback_query.get('id'):
            if max_callback_edit:
                answer_callback_best_effort(transport,str(callback_query['id']),prompt.text,inline=prompt.inline)
            else:
                answer_callback_best_effort(transport,str(callback_query['id']),'' if prompt or transport.platform=='max' else reply)
        if prompt:
            if max_callback_edit:
                pass
            elif transport.platform!='max' and edit and callback_query.get('message',{}).get('message_id'):
                transport.edit(user_id,str(callback_query['message']['message_id']),prompt.text,prompt.inline or [])
            else: transport.send(user_id,prompt.text,keyboard=prompt.keyboard if transport.platform=='max' else None,remove_keyboard=prompt.remove_keyboard,inline=prompt.inline)
        elif reply: transport.send(user_id,reply,remove_keyboard=True)
        return
    if callback and callback.startswith('admin:'):
        if is_admin:
            reply,keyboard=admin.callback(callback); transport.send(user_id,reply,inline=keyboard)
        return
    if callback and callback.startswith('a:'):
        if is_admin:
            if callback=='a:castsend':
                transport.send(user_id,admin.deliver(transport.platform,user_id,transport)); return
            reply,keyboard=admin.callback(transport.platform,user_id,callback); transport.send(user_id,reply,inline=keyboard)
        return
    if text.startswith('/test_search'):
        if not is_admin: transport.send(user_id,'Недостаточно прав.');return
        query=text.partition(' ')[2].strip()
        if not query:transport.send(user_id,'Использование: /test_search Фамилия Имя Отчество');return
        try:
            lead=engine.crm.find_lead(query,'') if engine.crm else None
            transport.send(user_id,(f'Найдена сделка: <a href="{config.amo_base_url}/leads/detail/{lead}">#{lead}</a>' if lead else 'Однозначное совпадение не найдено.'))
        except Exception:logging.exception('test_search failed');transport.send(user_id,'Ошибка поиска amoCRM. Подробности записаны в лог.')
        return
    if text=='/admin':
        if is_admin:
            admin.s.clear_draft(transport.platform,user_id)
            reply,keyboard=admin.menu(); transport.send(user_id,reply,inline=keyboard)
        else: transport.send(user_id,'Недостаточно прав.')
        return
    if text=='/start':
        if is_admin: admin.s.clear_draft(transport.platform,user_id)
        greeting,prompt=engine.begin(transport.platform,user_id,name)
        if prompt:
            first_attempt = greeting != 'Продолжаем незавершённое тестирование.'
            message = f"<b>{escape(greeting)}</b>\n\n{prompt.text}" if first_attempt else f"Продолжаем незавершённое тестирование.\n\n{prompt.text}"
            transport.send(user_id,message,keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard,inline=prompt.inline)
        else: transport.send(user_id,greeting)
        return
    if is_admin and (result:=admin.text(transport.platform,user_id,text)):
        reply,keyboard=result; transport.send(user_id,reply,inline=keyboard); return
    reply,prompt=engine.receive(transport.platform,user_id,text)
    if prompt:
        transport.send(user_id,prompt.text,keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard,inline=prompt.inline)
    else:
        transport.send(user_id,reply,remove_keyboard=True)


def run_transport(transport:Transport, engine:SurveyEngine, admin:Admin, config:Config, processing_lock:threading.RLock, run_snapshots:bool=False, stop_event:threading.Event|None=None) -> None:
    stop_event=stop_event or threading.Event()
    stored_cursor=engine.store.poll_cursor(transport.platform)
    offset=int(stored_cursor) if transport.platform=='telegram' and stored_cursor is not None else stored_cursor
    last_snapshot=0
    while not stop_event.is_set():
        try:
            updates=transport.updates(offset,config.poll_timeout)
        except Exception:
            logging.exception('Cannot receive updates (%s). Check that only one bot process uses this token.',transport.platform)
            stop_event.wait(3)
            continue
        batch_ok=True
        for update in updates:
            try:
                # Storage and the CRM client are shared by both polling workers.  The
                # lock makes each update an atomic unit for this process and avoids
                # concurrent use of the single SQLite connection.
                with processing_lock:
                    update_key=str(update.get('_event_id') or update.get('update_id') or '')
                    if not update_key or engine.store.update_processed(transport.platform,update_key):
                        continue
                    handle(transport,update,engine,admin,config)
                    incoming=update.get('message') or {}
                    if incoming.get('message_id'):
                        try:transport.delete(str(incoming.get('chat',{}).get('id') or incoming.get('from',{}).get('id')),str(incoming['message_id']))
                        except Exception:logging.debug('Cannot delete incoming message',exc_info=True)
                    if transport.platform=='telegram':
                        offset=max(offset or 0,int(update['update_id'])+1)
                        engine.store.complete_update(transport.platform,update_key,offset)
                    else:
                        engine.store.complete_update(transport.platform,update_key)
            except Exception:
                logging.exception('Update processing failed (%s)',transport.platform)
                batch_ok=False
        # A MAX marker acknowledges the whole fetched page.  Save it only after
        # every event has completed; per-event de-duplication makes replay safe.
        if transport.platform=='max' and batch_ok and getattr(transport,'marker',None) is not None:
            engine.store.set_poll_cursor(transport.platform,transport.marker)
            offset=transport.marker
        if run_snapshots and not stop_event.is_set() and time.time()-last_snapshot>60:
            with processing_lock:
                engine.send_snapshots(int(time.time())-config.inactivity_seconds)
            last_snapshot=time.time()


def main() -> int:
    load_dotenv(); config=Config.from_env()
    logging.basicConfig(level=getattr(logging, __import__('os').getenv('LOG_LEVEL','INFO').upper(),logging.INFO),format='%(asctime)s %(levelname)s %(message)s')
    store=Storage(config.database_path); seed_default_test(store)
    crm=AmoClient(config.amo_base_url,config.amo_token) if config.amo_base_url and config.amo_token else None
    engine=SurveyEngine(store,crm,config.target_pipeline,config.target_status); engine.resume_crm(); admin=Admin(store,config.amo_base_url)
    transports:list[Transport]=[]
    if config.telegram_token: transports.append(TelegramTransport(config.telegram_token))
    if config.max_token and config.max_api_base_url:
        transports.append(MaxTransport(config.max_token,config.max_api_base_url,marker=store.poll_cursor('max')))
    if not transports: raise SystemExit('Configure TELEGRAM_BOT_TOKEN or MAX_BOT_TOKEN + MAX_API_BASE_URL')
    processing_lock=threading.RLock(); stop_event=threading.Event()
    def request_stop(signum: int, frame: object) -> None:
        logging.info('Received signal %s; stopping polling',signum); stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig,request_stop)
    workers=[threading.Thread(
        target=run_transport,
        args=(transport,engine,admin,config,processing_lock,index == 0,stop_event),
        name=f'{transport.platform}-polling',
    ) for index,transport in enumerate(transports)]
    for worker in workers: worker.start()
    try:
        for worker in workers: worker.join()
    finally:
        stop_event.set(); engine.shutdown(); store.close()
    return 0

if __name__=='__main__': raise SystemExit(main())
