from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass,field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

LOG = logging.getLogger(__name__)


def _open_with_retry(request: Request, timeout: int = 35) -> Any:
    """Retry transient Bot API failures with bounded exponential backoff."""
    last_error: Exception | None=None
    for attempt in range(5):
        try:
            return urlopen(request,timeout=timeout)
        except HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise
            last_error=exc
            retry_after=exc.headers.get('Retry-After') if exc.headers else None
            try: delay=float(retry_after) if retry_after else min(.5 * (2 ** attempt),8)
            except ValueError: delay=min(.5 * (2 ** attempt),8)
        except (URLError, TimeoutError, OSError) as exc:
            last_error=exc
            delay=min(.5 * (2 ** attempt),8)
        if attempt == 4: break
        time.sleep(delay)
    assert last_error is not None
    raise last_error


class Transport(Protocol):
    platform: str
    def updates(self, offset: int | str | None, timeout: int) -> list[dict[str,Any]]: ...
    def send(self, user_id:str, text:str, *, keyboard:list[list[str]]|None=None, remove_keyboard:bool=False, inline:list[list[dict[str,str]]]|None=None) -> None: ...
    def send_broadcast(self,user_id:str,payload:dict[str,Any],buttons:list[list[dict[str,str]]]) -> None: ...
    def answer_callback(self,callback_id:str,text:str='',*,inline:list[list[dict[str,str]]]|None=None) -> None: ...
    def edit(self,user_id:str,message_id:str,text:str,inline:list[list[dict[str,str]]]) -> None: ...
    def delete(self,user_id:str,message_id:str) -> None: ...


@dataclass
class TelegramTransport:
    token: str
    platform: str = 'telegram'
    _last_message:dict[str,int]=field(default_factory=dict,init=False,repr=False)
    def _call(self, method:str, body:dict[str,Any]) -> Any:
        request=Request(f'https://api.telegram.org/bot{self.token}/{method}',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
        with _open_with_retry(request) as result:
            data=json.loads(result.read())
        if not data.get('ok'): raise RuntimeError(str(data))
        return data['result']
    def updates(self,offset:int | str | None,timeout:int) -> list[dict[str,Any]]:
        body={'timeout':timeout,'allowed_updates':['message','callback_query']}
        if offset is not None: body['offset']=int(offset)
        return self._call('getUpdates',body)
    def send(self,user_id:str,text:str,*,keyboard=None,remove_keyboard=False,inline=None) -> None:
        markup=None
        if keyboard is not None: markup={'keyboard':keyboard,'resize_keyboard':True,'one_time_keyboard':False}
        if remove_keyboard: markup={'remove_keyboard':True}
        if inline: markup={'inline_keyboard':inline}
        body={'chat_id':user_id,'text':text,'parse_mode':'HTML'}
        if markup: body['reply_markup']=markup
        result=self._call('sendMessage',body)
        previous=self._last_message.get(str(user_id));current=int(result['message_id'])
        self._last_message[str(user_id)]=current
        if previous and previous!=current:
            try:self._call('deleteMessage',{'chat_id':user_id,'message_id':previous})
            except Exception:pass
    def send_broadcast(self,user_id:str,payload:dict[str,Any],buttons:list[list[dict[str,str]]]) -> None:
        markup={'inline_keyboard':buttons} if buttons else None
        kind=payload.get('kind','text'); text=payload.get('text',''); body={'chat_id':user_id,'caption' if kind!='text' else 'text':text}
        if markup: body['reply_markup']=markup
        if payload.get('entities') and kind=='text': body['entities']=payload['entities']
        if kind=='text': self._call('sendMessage',body); return
        method={'photo':'sendPhoto','video':'sendVideo','document':'sendDocument','audio':'sendAudio','animation':'sendAnimation'}.get(kind,'sendDocument')
        body[kind if kind in {'photo','video','document','audio','animation'} else 'document']=payload['file_id']
        self._call(method,body)
    def answer_callback(self,callback_id:str,text:str='',*,inline:list[list[dict[str,str]]]|None=None) -> None: self._call('answerCallbackQuery',{'callback_query_id':callback_id,'text':text[:200]})
    def edit(self,user_id:str,message_id:str,text:str,inline:list[list[dict[str,str]]]) -> None:
        self._call('editMessageText',{'chat_id':user_id,'message_id':message_id,'text':text,'parse_mode':'HTML','reply_markup':{'inline_keyboard':inline}})
        self._last_message[str(user_id)]=int(message_id)
    def delete(self,user_id:str,message_id:str)->None:self._call('deleteMessage',{'chat_id':user_id,'message_id':message_id})


@dataclass
class MaxTransport:
    """Official MAX Bot API long-polling adapter (platform-api2.max.ru)."""
    token:str
    base_url:str
    platform:str='max'
    marker: str | int | None = None
    def _call(self,path:str,body:dict[str,Any]|None=None,method:str|None=None) -> Any:
        # MAX expects the access token itself in Authorization, not an HTTP Bearer
        # credential.  JSON is sent only for methods with a request body.
        headers={'Authorization':self.token}
        data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        if data is not None: headers['Content-Type']='application/json'
        request=Request(self.base_url+path,data=data,headers=headers,method=method or ('POST' if body is not None else 'GET'))
        with _open_with_retry(request) as response:
            result=json.loads(response.read())
        if isinstance(result,dict) and result.get('success') is False:
            raise RuntimeError(result.get('message','MAX API request failed'))
        return result
    def updates(self,offset:int | str | None,timeout:int) -> list[dict[str,Any]]:
        params={'timeout':timeout,'limit':100,'types':'message_created,message_callback,bot_started'}
        marker=offset if offset is not None else self.marker
        if marker is not None: params['marker']=str(marker)
        data=self._call('/updates?'+urlencode(params,doseq=True)); self.marker=data.get('marker')
        return [self.normalize_update(x) for x in data.get('updates',[])]
    def send(self,user_id:str,text:str,*,keyboard=None,remove_keyboard=False,inline=None) -> None:
        body={'text':text,'format':'html'}
        attachments=[]
        if keyboard is not None:
            # MAX has no Telegram-style reply keyboard.  A `message` button sends
            # its label as a regular message, preserving engine.receive semantics.
            attachments.append({'type':'inline_keyboard','payload':{'buttons':[
                [{'type':'message','text':label} for label in row] for row in keyboard
            ]}})
        if inline:
            attachments.append({'type':'inline_keyboard','payload':{'buttons':self._inline_buttons(inline)}})
        if attachments: body['attachments']=attachments
        self._call('/messages?'+urlencode({'user_id':int(user_id)}),body)
    def send_broadcast(self,user_id:str,payload:dict[str,Any],buttons:list[list[dict[str,str]]]) -> None:
        body={'text':payload.get('text',''),'format':'html'}
        # MAX accepts uploaded media tokens in attachments; upload itself is deliberately
        # delegated to its official multipart /uploads flow by the deployment adapter.
        if payload.get('media_token'): body['attachments']=[{'type':payload.get('kind','file'),'payload':{'token':payload['media_token']}}]
        if buttons: body.setdefault('attachments',[]).append({'type':'inline_keyboard','payload':{'buttons':self._inline_buttons(buttons)}})
        self._call('/messages?'+urlencode({'user_id':int(user_id)}),body)
    @staticmethod
    def _inline_buttons(buttons:list[list[dict[str,str]]]) -> list[list[dict[str,str]]]:
        return [[({'type':'link','text':button['text'],'url':button['url']} if button.get('url') else {'type':'callback','text':button['text'],'payload':button['callback_data']}) for button in row] for row in buttons]
    def _message_body(self,text:str,inline:list[list[dict[str,str]]]|None=None) -> dict[str,Any]:
        body={'text':text,'format':'html'}
        if inline is not None:
            body['attachments']=[{'type':'inline_keyboard','payload':{'buttons':self._inline_buttons(inline)}}] if inline else []
        return body
    def answer_callback(self,callback_id:str,text:str='',*,inline:list[list[dict[str,str]]]|None=None) -> None:
        path='/answers?'+urlencode({'callback_id':callback_id})
        try:
            message=self._message_body(text,inline) if text or inline is not None else None
            self._call(path,{'message':message})
        except HTTPError as exc:
            if exc.code != 400:
                raise
            body=exc.read().decode('utf-8','replace') if exc.fp else ''
            LOG.warning('MAX callback answer failed: status=%s body=%s',exc.code,body)
    def edit(self,user_id:str,message_id:str,text:str,inline:list[list[dict[str,str]]]) -> None:
        body=self._message_body(text,inline)
        try:
            self._call('/messages?'+urlencode({'message_id':message_id}),body,method='PUT')
        except HTTPError as exc:
            if exc.code != 400:
                raise
            response_body=exc.read().decode('utf-8','replace') if exc.fp else ''
            LOG.warning('MAX message edit failed: status=%s body=%s',exc.code,response_body)
    def delete(self,user_id:str,message_id:str)->None:
        self._call('/messages?'+urlencode({'message_id':message_id}),method='DELETE')
    @staticmethod
    def normalize_update(update:dict[str,Any]) -> dict[str,Any]:
        typ=update.get('update_type'); user=update.get('user') or {}; uid=str(user.get('user_id') or user.get('id') or '')
        msg=update.get('message') or {}; cb=update.get('callback') or {}
        event_id=str(update.get('update_id') or update.get('id') or cb.get('callback_id') or msg.get('body',{}).get('mid') or msg.get('message_id') or f'{typ}:{update.get("timestamp", "")}:{uid}')
        if typ=='bot_started': return {'update_id':update.get('marker',update.get('timestamp',0)),'_event_id':event_id,'message':{'from':{'id':uid,'first_name':user.get('name','')},'chat':{'id':update.get('chat_id',uid)},'text':'/start'}}
        if typ=='message_created':
            msg=update.get('message') or {}; sender=msg.get('sender') or user
            return {'update_id':update.get('marker',update.get('timestamp',0)),'_event_id':event_id,'message':{'message_id':msg.get('body',{}).get('mid',msg.get('message_id','')),'from':{'id':str(sender.get('user_id') or sender.get('id') or uid),'first_name':sender.get('name','')},'chat':{'id':msg.get('chat_id',update.get('chat_id',uid))},'text':msg.get('body',{}).get('text',msg.get('text',''))}}
        if typ=='message_callback':
            cb=update.get('callback') or {}; msg=update.get('message') or {}; sender=cb.get('user') or user
            return {'update_id':update.get('marker',update.get('timestamp',0)),'_event_id':event_id,'callback_query':{'id':cb.get('callback_id',''),'from':{'id':str(sender.get('user_id') or sender.get('id') or uid),'first_name':sender.get('name','')},'data':cb.get('payload',''),'message':{'message_id':msg.get('body',{}).get('mid',msg.get('message_id','')),'chat':{'id':msg.get('recipient',{}).get('chat_id',update.get('chat_id',uid))}}}}
        return {'update_id':update.get('marker',update.get('timestamp',0))}
