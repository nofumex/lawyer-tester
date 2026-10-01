from __future__ import annotations

import logging
import signal
import threading
import time
from html import escape

from background import latency
from chat import message_key, message_content
from dispatch import UpdateDispatcher

from admin import Admin
from amocrm import AmoClient
from config import Config, load_dotenv
from engine import SurveyEngine
from seed import seed_default_test
from storage import Storage
from transports import MaxTransport, TelegramTransport, Transport
from mailings import MailingService
from referrals import AgentProgram


def _finish_broadcast(admin:Admin, source_platform:str, user_id:str, transports:dict[str,Transport], reply_transport:Transport) -> None:
    try:
        result=admin.deliver(source_platform,user_id,transports)
    except Exception:
        logging.exception('Broadcast failed')
        result='Рассылка завершилась с ошибкой. Подробности записаны в лог.'
    try:
        reply_transport.send(user_id,result)
    except Exception:
        logging.exception('Cannot send broadcast completion message')


def answer_callback_best_effort(transport:Transport, callback_id:str, text:str='', *, inline:list[list[dict[str,str]]]|None=None) -> None:
    try:
        transport.answer_callback(callback_id,text,inline=inline)
    except Exception:
        logging.warning('Callback acknowledgement failed (%s); continuing update processing',transport.platform,exc_info=True)


def handle(transport:Transport, update:dict, engine:SurveyEngine, admin:Admin, config:Config, transports:dict[str,Transport]|None=None) -> None:
    message=update.get('message') or update.get('callback_query',{}).get('message') or {}
    sender=(update.get('message') or update.get('callback_query',{}).get('from') or {}).get('from') or update.get('callback_query',{}).get('from') or {}
    user_id=str(sender.get('id') or message.get('chat',{}).get('id') or '')
    if not user_id: return
    name=' '.join(filter(None,[sender.get('first_name'),sender.get('last_name')])) or sender.get('username')
    engine.store.touch_user(transport.platform,user_id,name,sender.get('username'))
    callback=(update.get('callback_query') or {}).get('data')
    callback_query=update.get('callback_query') or {}
    text=(update.get('message') or {}).get('text','').strip()
    is_admin=user_id in config.admin_ids
    agent=getattr(engine,'agent_program',None)
    mailings=getattr(engine,'mailing_service',None)
    chat=getattr(agent,'chat',None)
    if chat:
        if callback and callback.startswith('chat:'):
            if chat.callback(transport,transport.platform,user_id,callback):
                if callback_query.get('id'): answer_callback_best_effort(transport,str(callback_query['id']))
                return
        if text in {'/endchat','Завершить чат'}:
            chat.callback(transport,transport.platform,user_id,'chat:end'); return
        if text in {'/tutor','/manager'}:
            chat.callback(transport,transport.platform,user_id,'chat:start'); return
        chat_text,attachments=message_content(update.get('message') or {})
        if chat.relay(transport,transport.platform,user_id,chat_text,message_key(transport.platform,update,user_id),attachments):
            return
    if callback=='user:test':
        if callback_query.get('id'): answer_callback_best_effort(transport,str(callback_query['id']))
        greeting,prompt=engine.begin(transport.platform,user_id,name)
        if prompt: transport.send(user_id,f"<b>{escape(greeting)}</b>\n\n{prompt.text}",keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard,inline=prompt.inline)
        else: transport.send(user_id,greeting)
        return
    if callback and agent and (callback.startswith('mail:') or callback.startswith('agent:') or callback.startswith('profile:') or callback.startswith('chat:') or callback=='user:main'):
        handled=agent.handle_callback(transport,transport.platform,user_id,callback)
        if handled and callback_query.get('id'): answer_callback_best_effort(transport,str(callback_query['id']))
        if handled:return
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
                if hasattr(transport,'defer_broadcast'):
                    transport.defer_broadcast(update); return
                threading.Thread(target=_finish_broadcast,args=(admin,transport.platform,user_id,transports or {transport.platform:transport},transport),name='broadcast-worker',daemon=True).start()
                return
            reply,keyboard=admin.callback(transport.platform,user_id,callback); transport.send(user_id,reply,inline=keyboard)
        return
    if text.startswith('/test_search'):
        if hasattr(transport,'defer_search'):
            transport.defer_search(update); return
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
    if text.startswith('/mailings'):
        if not is_admin: transport.send(user_id,'Недостаточно прав.');return
        transport.send(user_id,mailings.admin_command(transport.platform,user_id,text) if mailings else 'Сервис рассылок недоступен.')
        return
    if is_admin and agent and (text.startswith('/bonus ') or text.startswith('/bonus_paid ')):
        result=agent.admin_command(transport,transport.platform,user_id,text)
        if result:transport.send(user_id,result)
        return
    if text.startswith('/start'):
        if is_admin: admin.s.clear_draft(transport.platform,user_id)
        greeting,prompt=engine.begin(transport.platform,user_id,name)
        if agent: agent.start_payload(transport.platform,user_id,text)
        if prompt:
            first_attempt = greeting != 'Продолжаем незавершённое тестирование.'
            message = f"<b>{escape(greeting)}</b>\n\n{prompt.text}" if first_attempt else f"Продолжаем незавершённое тестирование.\n\n{prompt.text}"
            transport.send(user_id,message,keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard,inline=prompt.inline)
        else: transport.send(user_id,greeting)
        return
    if is_admin and (result:=admin.text(transport.platform,user_id,text)):
        reply,keyboard=result; transport.send(user_id,reply,inline=keyboard); return
    if agent and agent.handle_text(transport,transport.platform,user_id,text):
        return
    reply,prompt=engine.receive(transport.platform,user_id,text)
    if prompt:
        transport.send(user_id,prompt.text,keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard,inline=prompt.inline)
    else:
        transport.send(user_id,reply,remove_keyboard=True)


def run_transport(transport:Transport, engine:SurveyEngine, admin:Admin, config:Config, processing_lock:threading.RLock, run_snapshots:bool=False, stop_event:threading.Event|None=None, transports:dict[str,Transport]|None=None) -> None:
    stop_event=stop_event or threading.Event()
    stored_cursor=engine.store.poll_cursor(transport.platform)
    offset=int(stored_cursor) if transport.platform=='telegram' and stored_cursor is not None else stored_cursor
    dispatcher=UpdateDispatcher(transport,engine,admin,config,transports,handle)
    snapshot_thread=None
    if run_snapshots:
        def snapshots():
            while not stop_event.is_set():
                try:
                    with latency('snapshots'):
                        engine.send_snapshots(int(time.time())-config.inactivity_seconds)
                except Exception:
                    logging.exception('Snapshots failed')
                stop_event.wait(60)
        snapshot_thread=threading.Thread(target=snapshots,name='snapshots',daemon=True)
        snapshot_thread.start()
    try:
        while not stop_event.is_set():
            try:
                updates=transport.updates(offset,config.poll_timeout)
                with latency('poll.persist',platform=transport.platform):
                    entries=[]
                    cursor=offset
                    for update in updates:
                        key=str(update.get('_event_id') or update.get('update_id', ''))
                        if not key: continue
                        entries.append((key,dispatcher.user_id(update),update))
                        if transport.platform=='telegram':
                            cursor=max(cursor or 0,int(update['update_id'])+1)
                    if transport.platform=='max': cursor=getattr(transport,'marker',None)
                    engine.store.enqueue_updates(transport.platform,entries,cursor)
                    offset=cursor
                dispatcher.wake.set()
            except Exception:
                if transport.platform=='max': transport.marker=offset
                logging.exception('Cannot receive/persist updates (%s)',transport.platform)
                stop_event.wait(3)
    finally:
        dispatcher.close()
        if snapshot_thread is not None:
            snapshot_thread.join()


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
    transports_by_platform={transport.platform:transport for transport in transports}
    mailings=MailingService(store,transports_by_platform,config.mailing_interval_seconds,crm)
    agent_program=AgentProgram(store,config,crm,mailings,transports_by_platform)
    engine.mailing_service=mailings
    engine.agent_program=agent_program
    processing_lock=threading.RLock(); stop_event=threading.Event()
    def request_stop(signum: int, frame: object) -> None:
        logging.info('Received signal %s; stopping polling',signum); stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig,request_stop)
    workers=[threading.Thread(
        target=run_transport,
        args=(transport,engine,admin,config,processing_lock,index == 0,stop_event,transports_by_platform),
        name=f'{transport.platform}-polling',
    ) for index,transport in enumerate(transports)]
    mailing_worker=threading.Thread(target=mailings.run,args=(stop_event,),name='automatic-mailings',daemon=True)
    referral_worker=threading.Thread(target=agent_program.run,args=(stop_event,),name='referral-retry',daemon=True)
    mailing_worker.start()
    referral_worker.start()
    for worker in workers: worker.start()
    try:
        for worker in workers: worker.join()
    finally:
        stop_event.set(); mailing_worker.join(); referral_worker.join(); agent_program.close(); engine.shutdown(); store.close()
    return 0

if __name__=='__main__': raise SystemExit(main())
