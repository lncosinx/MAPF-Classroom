"""WS 消息（客户端 -> 服务端）。服务端下发的消息是普通 dict，结构见 core/round.py 和 core/session.py。

动作编号一律：0=wait、1=up、2=down、3=left、4=right（不变式 1）。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter

from ..core.settings import MessageKind, RoundSettings


class Join(BaseModel):
    type: Literal["join"]
    nickname: str = Field(default="", max_length=32)
    seat: str = Field(default="", max_length=16)
    token: Optional[str] = Field(default=None, max_length=64)


class Choose(BaseModel):
    type: Literal["choose"]
    t: int
    action: int = Field(ge=0, le=4)


class ViewRequest(BaseModel):
    """请求和 to 号队友交换视野（本步有效）。"""
    type: Literal["view_request"]
    t: int
    to: int


class ViewAnswer(BaseModel):
    """回应 frm 号队友的交换视野请求。"""
    type: Literal["view_answer"]
    t: int
    frm: int = Field(alias="from")
    accept: bool


class SendMessage(BaseModel):
    type: Literal["message"]
    t: int
    to: int
    kind: MessageKind


StudentMsg = TypeAdapter(Annotated[Union[Join, Choose, ViewRequest, ViewAnswer, SendMessage],
                                   Field(discriminator="type")])


class Configure(BaseModel):
    type: Literal["configure"]
    settings: RoundSettings


class Assign(BaseModel):
    type: Literal["assign"]
    player_id: int
    agent: Optional[int] = None


class Remove(BaseModel):
    """移除同学：player_id 为空时移除所有离线的同学。"""
    type: Literal["remove"]
    player_id: Optional[int] = None


class Rpc(BaseModel):
    """地图工坊、地图库、模板的请求-应答（见 server/teacher_rpc.py）。"""
    type: Literal["rpc"]
    id: int
    method: str = Field(max_length=40)
    params: dict = Field(default_factory=dict)


class Command(BaseModel):
    # step_now：立即结算本步（不等倒计时）
    type: Literal["start", "pause", "resume", "step_now", "end", "shuffle", "refresh"]


TeacherMsg = TypeAdapter(Annotated[Union[Configure, Assign, Remove, Rpc, Command], Field(discriminator="type")])
