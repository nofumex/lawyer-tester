from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen


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
    """Adapter boundary. Configure a MAX gateway implementing /updates and /messages."""
    token:str
    base_url:str
    platform:str='max'
    def _call(self,path:str,body:dict[str,Any]) -> Any:
        request=Request(self.base_url+path,data=json.dumps(body,ensure_ascii=False).encode(),headers={'Authorization':f'Bearer {self.token}','Content-Type':'application/json'},method='POST')
        with urlopen(request,timeout=35) as response: return json.loads(response.read())
    def updates(self,offset:int,timeout:int) -> list[dict[str,Any]]: return self._call('/updates',{'offset':offset,'timeout':timeout}).get('updates',[])
    def send(self,user_id:str,text:str,*,keyboard=None,remove_keyboard=False,inline=None) -> None: self._call('/messages',{'user_id':user_id,'text':text,'keyboard':keyboard,'remove_keyboard':remove_keyboard,'inline_keyboard':inline})
