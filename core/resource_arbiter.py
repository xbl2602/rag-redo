"""通用具名资源租约仲裁器（核心服务，不是插件——见 AGENTS.md 架构红线5）。

不懂任何 RAG 领域知识，不知道"GPU"或"BGE-M3"是什么，只知道"谁在占用一个
具名资源、优先级更高的能不能抢占"。这是把旧 obsidian-rag 项目 gpu_arbiter.py
验证过的策略（同一时刻只让一方持有、优先级更高的可以抢占、探测失败 fail-open）
抽象成不专属 GPU 的通用原语。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class _Holder:
    holder_id: str
    priority: int
    on_preempt: Callable[[], None] | None


@dataclass
class ResourceArbiter:
    _holders: dict[str, _Holder] = field(default_factory=dict)

    def acquire(
        self,
        resource_id: str,
        holder_id: str,
        *,
        priority: int = 0,
        on_preempt: Callable[[], None] | None = None,
        preempt_equal: bool = False,
    ) -> bool:
        """申请一个资源锁。

        - 当前无人占用 → 直接拿到
        - 已经是自己占用 → 幂等返回 True
        - 别人占用、且新请求优先级更高 → 抢占（调用被抢占方的 on_preempt 回调后接管）
        - 别人占用、且优先级相同、且 preempt_equal=True → 同样抢占（"同一层级里
          谁刚需要谁拿"，不是严格数值更高才行——对应旧项目 obsidian-rag
          gpu_arbiter.py 里 WEMM 和 MinerU 互相抢占显存的真实行为：两者是同一
          优先级层级的"按需占用"资源消费者，不分谁天生更重要，纯粹看谁现在要用；
          默认 False 保持原有"严格更高优先级才能抢占"语义不变，不影响既有调用方）
        - 别人占用、且优先级不够 → 拿不到，返回 False
        """
        current = self._holders.get(resource_id)
        if current is None:
            self._holders[resource_id] = _Holder(holder_id, priority, on_preempt)
            return True
        if current.holder_id == holder_id:
            return True
        if priority > current.priority or (preempt_equal and priority == current.priority):
            if current.on_preempt is not None:
                current.on_preempt()
            self._holders[resource_id] = _Holder(holder_id, priority, on_preempt)
            return True
        return False

    def release(self, resource_id: str, holder_id: str) -> None:
        current = self._holders.get(resource_id)
        if current is not None and current.holder_id == holder_id:
            del self._holders[resource_id]

    def holder_of(self, resource_id: str) -> str | None:
        current = self._holders.get(resource_id)
        return current.holder_id if current else None

    @staticmethod
    def probe(check: Callable[[], bool]) -> bool:
        """执行一次"资源是否可用"探测。

        探测函数自身抛异常时 fail-open——默认判定为"可用/放行"，绝不让探测失败
        变成阻塞。这条焊死在这一个函数里，所有插件复用，不用各自重新踩一遍旧项目
        问题41 踩过的坑（架构红线5）。
        """
        try:
            return check()
        except Exception:
            return True
