"""Agent 写权限门禁：核心服务，不是插件（见 ../AGENTS.md"两个核心组件"
节、docs/ARCHITECTURE.md 第2.2节——写权限的裁决必须有唯一入口，不能
每个插件自己决定"这算不算 AI 写"）。

模式沿用旧 obsidian-rag 项目验证过的"提案号+确认码+TTL+一次性"两段式
确认：AI 想做一次受保护的写入，先 propose()（拿到 proposal_id + 一次性
确认码），用户/上层确认后带着确认码调 confirm()，门禁校验码对、没过期、
没被用过，才放行，返回原始 payload 给调用方去真正执行写入——WriteGate
本身不知道 payload 是什么、不替调用方执行任何写操作，它只管"这次确认
合不合法"。人类在 GUI 里直接操作不走这个门禁（无条件生效）——这是门禁
要保护的场景（AI 触发的、影响范围较大或不可逆的写入），不是全部写入
路径都要走它。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class WriteGateError(Exception):
    """确认码错误/过期/已被使用——统一这一个异常类型。调用方折叠成用户
    能看懂的提示时，错误消息本身已经说清楚原因，不需要靠异常子类型分支。"""


@dataclass
class _Proposal:
    action_id: str
    payload: Any
    confirmation_code: str
    expires_at: float
    used: bool = False


@dataclass(frozen=True)
class ProposalTicket:
    proposal_id: str
    confirmation_code: str
    expires_at: float


class WriteGate:
    """核心服务：任何插件要接受 AI 触发的写操作都直接复用这一套，不用
    各自发明一遍确认码生成/校验逻辑（AGENTS.md"避免功能重复实现"这条
    约束最直接的落地点之一）。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._proposals: dict[str, _Proposal] = {}

    def propose(self, action_id: str, payload: Any, *, ttl_seconds: int = 600) -> ProposalTicket:
        """action_id 只是给日志/展示用的人类可读标签（比如"覆盖用户已写的
        库摘要"），不参与校验逻辑——校验只认 proposal_id+confirmation_code
        这一对。"""
        proposal_id = secrets.token_hex(8)
        confirmation_code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = self._clock() + ttl_seconds
        self._proposals[proposal_id] = _Proposal(
            action_id=action_id,
            payload=payload,
            confirmation_code=confirmation_code,
            expires_at=expires_at,
        )
        return ProposalTicket(proposal_id=proposal_id, confirmation_code=confirmation_code, expires_at=expires_at)

    def confirm(self, proposal_id: str, confirmation_code: str) -> Any:
        """校验通过则返回 propose() 时存的 payload，调用方拿它去执行真正
        的写入；WriteGate 自己绝不执行任何写操作。"""
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise WriteGateError("提案不存在或已被使用")
        if proposal.used:
            raise WriteGateError("提案已经被使用过，一次性有效")
        if self._clock() > proposal.expires_at:
            del self._proposals[proposal_id]
            raise WriteGateError("提案已过期")
        # 用 secrets.compare_digest 而不是 == ——确认码校验是这个门禁存在
        # 的唯一意义，不能因为用了会短路提前返回的字符串比较而留下时序
        # 侧信道（哪怕本地单机场景风险很低，这个成本几乎为零，没有理由
        # 不做对）。
        if not secrets.compare_digest(confirmation_code, proposal.confirmation_code):
            raise WriteGateError("确认码错误")
        proposal.used = True
        payload = proposal.payload
        del self._proposals[proposal_id]
        return payload

    def pending_count(self) -> int:
        return len(self._proposals)

    def sweep_expired(self) -> int:
        """清理过期未确认的提案，返回清理数量。不是自动定时任务——由核心
        在合适的时机（比如每次 propose() 之前）主动调用，保持 WriteGate
        本身不依赖任何后台线程/事件循环。"""
        now = self._clock()
        expired_ids = [pid for pid, p in self._proposals.items() if now > p.expires_at]
        for pid in expired_ids:
            del self._proposals[pid]
        return len(expired_ids)
