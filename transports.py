from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen
from urllib.parse import urlencode


class Transport(Protocol):
    platform: str
    def updates(self, offset: int, timeout: int) -> list[dict[str,Any]]: ...
    def send(self, user_id:str, text:str, *, keyboard:list[list[str]]|None=None, remove_keyboard:bool=False, inline:list[list[dict[str,str]]]|None=None) -> None: ...
    def send_broadcast(self,user_id:str,payload:dict[str,Any],buttons:list[list[dict[str,str]]]) -> None: ...


@dataclass
class TelegramTransport:
    token: str
    platform: str = 'telegram'
    def _call(self, method:str, body:dict[str,Any]) -> Any:
        request=Request(f'https://api.telegram.org/bot{self.token}/{method}',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
        with urlopen(request,timeout=35) as result:
            data=json.loads(result.read())
        if not data.get('ok'): raise RuntimeError(str(data))
        return data['result']
    def updates(self,offset:int,timeout:int) -> list[dict[str,Any]]: return self._call('getUpdates',{'offset':offset,'timeout':timeout,'allowed_updates':['message','callback_query']})
    def send(self,user_id:str,text:str,*,keyboard=None,remove_keyboard=False,inline=None) -> None:
        markup=None
        if keyboard is not None: markup={'keyboard':keyboard,'resize_keyboard':True,'one_time_keyboard':False}
        if remove_keyboard: markup={'remove_keyboard':True}
        if inline: markup={'inline_keyboard':inline}
        body={'chat_id':user_id,'text':text}
        if markup: body['reply_markup']=markup
        self._call('sendMessage',body)
    def send_broadcast(self,user_id:str,payload:dict[str,Any],buttons:list[list[dict[str,str]]]) -> None:
        markup={'inline_keyboard':buttons} if buttons else None
        kind=payload.get('kind','text'); text=payload.get('text',''); body={'chat_id':user_id,'caption' if kind!='text' else 'text':text}
        if markup: body['reply_markup']=markup
        if payload.get('entities') and kind=='text': body['entities']=payload['entities']
        if kind=='text': self._call('sendMessage',body); return
        method={'photo':'sendPhoto','video':'sendVideo','document':'sendDocument','audio':'sendAudio','animation':'sendAnimation'}.get(kind,'sendDocument')
        body[kind if kind in {'photo','video','document','audio','animation'} else 'document']=payload['file_id']
        self._call(method,body)


@dataclass
class MaxTransport:
    """Official MAX Bot API long-polling adapter (platform-api2.max.ru)."""
    token:str
    base_url:str
    platform:str='max'
    marker: int | None = None
    def _call(self,path:str,body:dict[str,Any]|None=None) -> Any:
        request=Request(self.base_url+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Authorization':f'Bearer {self.token}','Content-Type':'application/json'} if body is not None else {'Authorization':f'Bearer {self.token}'},method='POST' if body is not None else 'GET')
        with urlopen(request,timeout=35) as response: return json.loads(response.read())
    def updates(self,offset:int,timeout:int) -> list[dict[str,Any]]:
        params={'timeout':timeout,'limit':100,'types':['message_created','message_callback','bot_started']}
        if self.marker is not None: params['marker']=self.marker
        data=self._call('/updates?'+urlencode(params,doseq=True)); self.marker=data.get('marker',self.marker)
        return [self.normalize_update(x) for x in data.get('updates',[])]
    def send(self,user_id:str,text:str,*,keyboard=None,remove_keyboard=False,inline=None) -> None:
        # MAX supports inline keyboards; the payload is platform-native, unlike Telegram reply_markup.
        body={'user_id':int(user_id),'text':text}
        if inline: body['attachments']=[{'type':'inline_keyboard','payload':{'buttons':inline}}]
        self._call('/messages',body)
    def send_broadcast(self,user_id:str,payload:dict[str,Any],buttons:list[list[dict[str,str]]]) -> None:
        body={'user_id':int(user_id),'text':payload.get('text','')}
        # MAX accepts uploaded media tokens in attachments; upload itself is deliberately
        # delegated to its official multipart /uploads flow by the deployment adapter.
        if payload.get('media_token'): body['attachments']=[{'type':payload.get('kind','file'),'payload':{'token':payload['media_token']}}]
        if buttons: body.setdefault('attachments',[]).append({'type':'inline_keyboard','payload':{'buttons':buttons}})
        self._call('/messages',body)
    @staticmethod
    def normalize_update(update:dict[str,Any]) -> dict[str,Any]:
        typ=update.get('update_type'); user=update.get('user') or {}; uid=str(user.get('user_id') or user.get('id') or '')
        if typ=='bot_started': return {'update_id':update.get('marker',update.get('timestamp',0)),'message':{'from':{'id':uid,'first_name':user.get('name','')},'chat':{'id':update.get('chat_id',uid)},'text':'/start'}}
        if typ=='message_created':
            msg=update.get('message') or {}; sender=msg.get('sender') or user
            return {'update_id':update.get('marker',update.get('timestamp',0)),'message':{'from':{'id':str(sender.get('user_id') or sender.get('id') or uid),'first_name':sender.get('name','')},'chat':{'id':msg.get('chat_id',update.get('chat_id',uid))},'text':msg.get('body',{}).get('text',msg.get('text',''))}}
        if typ=='message_callback':
            cb=update.get('callback') or {}; return {'update_id':update.get('marker',update.get('timestamp',0)),'callback_query':{'from':{'id':uid,'first_name':user.get('name','')},'data':cb.get('payload') or cb.get('data',''),'message':{'chat':{'id':update.get('chat_id',uid)}}}}
        return {'update_id':update.get('marker',update.get('timestamp',0))}
