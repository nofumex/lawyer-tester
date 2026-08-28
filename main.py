from __future__ import annotations

import logging
import time

from admin import Admin
from amocrm import AmoClient
from config import Config, load_dotenv
from engine import SurveyEngine
from seed import seed_default_test
from storage import Storage
from transports import MaxTransport, TelegramTransport, Transport


def handle(transport:Transport, update:dict, engine:SurveyEngine, admin:Admin, config:Config) -> None:
    message=update.get('message') or update.get('callback_query',{}).get('message') or {}
    sender=(update.get('message') or update.get('callback_query',{}).get('from') or {}).get('from') or update.get('callback_query',{}).get('from') or {}
    user_id=str(sender.get('id') or message.get('chat',{}).get('id') or '')
    if not user_id: return
    name=' '.join(filter(None,[sender.get('first_name'),sender.get('last_name')])) or sender.get('username')
    callback=(update.get('callback_query') or {}).get('data')
    text=(update.get('message') or {}).get('text','').strip()
    is_admin=user_id in config.admin_ids
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
    if text=='/admin':
        if is_admin:
            reply,keyboard=admin.menu(); transport.send(user_id,reply,inline=keyboard)
        else: transport.send(user_id,'Недостаточно прав.')
        return
    if is_admin and (result:=admin.text(transport.platform,user_id,text)):
        reply,keyboard=result; transport.send(user_id,reply,inline=keyboard); return
    if text=='/start':
        greeting,prompt=engine.begin(transport.platform,user_id,name)
        if prompt: transport.send(user_id,f"<b>{greeting}</b>\n\n{prompt.text}",keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard)
        else: transport.send(user_id,greeting)
        return
    reply,prompt=engine.receive(transport.platform,user_id,text)
    if prompt:
        transport.send(user_id,prompt.text,keyboard=prompt.keyboard,remove_keyboard=prompt.remove_keyboard)
    else:
        transport.send(user_id,reply,remove_keyboard=True)


def run_transport(transport:Transport, engine:SurveyEngine, admin:Admin, config:Config) -> None:
    offset=0; last_snapshot=0
    while True:
        for update in transport.updates(offset,config.poll_timeout):
            offset=max(offset,int(update.get('update_id',0))+1)
            try: handle(transport,update,engine,admin,config)
            except Exception: logging.exception('Update processing failed (%s)',transport.platform)
        if time.time()-last_snapshot>60:
            engine.send_snapshots(int(time.time())-config.inactivity_seconds); last_snapshot=time.time()


def main() -> int:
    load_dotenv(); config=Config.from_env()
    logging.basicConfig(level=getattr(logging, __import__('os').getenv('LOG_LEVEL','INFO').upper(),logging.INFO),format='%(asctime)s %(levelname)s %(message)s')
    store=Storage(config.database_path); seed_default_test(store)
    crm=AmoClient(config.amo_base_url,config.amo_token) if config.amo_base_url and config.amo_token else None
    engine=SurveyEngine(store,crm,config.target_pipeline,config.target_status); admin=Admin(store)
    transports:list[Transport]=[]
    if config.telegram_token: transports.append(TelegramTransport(config.telegram_token))
    if config.max_token and config.max_api_base_url: transports.append(MaxTransport(config.max_token,config.max_api_base_url))
    if not transports: raise SystemExit('Configure TELEGRAM_BOT_TOKEN or MAX_BOT_TOKEN + MAX_API_BASE_URL')
    if len(transports)>1: raise SystemExit('Run one transport per process; both share DATABASE_PATH safely.')
    run_transport(transports[0],engine,admin,config); return 0

if __name__=='__main__': raise SystemExit(main())
