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
        return data.get('updates',[])
    def send(self,user_id:str,text:str,*,keyboard=None,remove_keyboard=False,inline=None) -> None:
        # MAX supports inline keyboards; the payload is platform-native, unlike Telegram reply_markup.
        body={'user_id':int(user_id),'text':text}
        if inline: body['attachments']=[{'type':'inline_keyboard','payload':{'buttons':inline}}]
        self._call('/messages',body)
