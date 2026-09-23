"""GPU 显存探测/等待/驱逐工具（核心服务，供插件直接 import，同
core/subprocess_service.py 的定位——不是插件，是所有需要管理真实 GPU
显存占用的插件共用的领域相关工具层）。

和 core/resource_arbiter.py 的关系：那个模块是完全不懂"GPU"是什么的通用
具名资源租约原语（申请/抢占/释放），管的是"名额"；这个模块管的是"真实显存
数字"——探测空闲显存、等空闲显存达标、请求另一方主动让出显存。两者配合
使用：插件先用 resource_arbiter.acquire() 拿到"gpu:0"这个名额（决定"轮到
谁用"），真正加载模型前再用这个模块的 wait_for_vram() 确认物理显存真的够
（决定"现在能不能装得下"）。

移植自旧项目 obsidian-rag/gpu_arbiter.py 验证过的策略（问题41：同一时刻
只让一方真正驻留显存，8GB 卡上大模型之间无法共存）——**fail-open 铁律**
和旧项目完全一致：显存探测失败（无 torch 也无 nvidia-smi）一律返回
None，所有调用方必须视作"无法判断→不阻塞不抢占"，绝不让仲裁本身卡死
正常路径（架构红线5）。

**为什么这份文件只被 in_process 插件直接 import，不被 subprocess_service
插件的 server.py 直接 import**：official-embedder-bge-m3/official-reranker
运行在核心进程里，能直接访问 core.*（同 core/subprocess_service.py 已经
被 official-visual-wemm/official-ocr-mineru-local 的 plugin.py 直接
import 的先例）。但 official-visual-wemm/official-ocr-mineru-local 的
server.py 运行在完全隔离的独立子进程/独立 venv 里，import 不到 core
包——这两个 server.py 各自内嵌了一份自包含的（stdlib-only）等价小函数，
不是遗漏，是插件隔离原则在"跨隔离边界的小工具函数"这个具体场景下的正确
处理方式（同 core/subprocess_service.py::resolve_plugin_python 之前就
已经在多个测试文件间重复过的 _process_is_gone() helper 是同一类先例）。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

# GPU 驻留变更总锁（对齐旧项目问题59的并发调度考虑）。
#
# 语义：核心进程内一切"改变显存里住着谁"的操作——bge-m3/reranker 加载/
# 释放、驱逐子进程模型前的判定——都建议先拿这把锁，纯查询（已加载模型的
# 编码调用）不需要拿锁，天然可并发。用 RLock（同线程可重入）。
# 持有期纪律：只包"判定+快速变更"，绝不包子进程启动这种长等待；拿不到锁
# 的请求走降级路径，不得无限等。
GPU_LOCK = threading.RLock()

# 请求路径拿锁的最长等待（秒）：超时视为"此刻不适合抢显存"，走降级路径。
GPU_LOCK_ACQUIRE_TIMEOUT_S = 15.0

_cache: tuple[float | None, float] = (None, 0.0)


def vram_free_gb(max_age: float = 5.0) -> float | None:
    """当前空闲显存（GB）。探测失败返回 None（fail-open）。

    torch 优先（插件所在环境如果装了 torch 就顺手复用，不用额外依赖）；
    nvidia-smi 作双保险（torch 不可用但驱动在的场景）。max_age 秒内的
    探测结果会被缓存复用——探测本身有开销，短窗口内没必要重复探测。
    """
    global _cache
    now = time.time()
    if _cache[1] and now - _cache[1] < max_age:
        return _cache[0]
    val: float | None = None
    try:
        import torch  # noqa: PLC0415 - 故意懒导入，这个模块本身不该强制依赖 torch

        if torch.cuda.is_available():
            val = torch.cuda.mem_get_info()[0] / 2**30
    except Exception:  # noqa: BLE001 - 探测失败折叠成 None，不传播异常
        val = None
    if val is None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            nums = [float(x) for x in out.stdout.decode("utf-8", "replace").split()]
            if nums:
                val = nums[0] / 1024.0
        except Exception:  # noqa: BLE001
            _cache = (None, now)
            return None
    _cache = (val, now)
    return val


def wait_for_vram(min_free_gb: float, timeout_s: float = 900.0, poll_s: float = 10.0, log=None) -> bool:
    """阻塞等待空闲显存 ≥ min_free_gb。等到返回 True；超时返回 False。

    用于大模型加载前：另一个模型在线占着显存时，等它的空闲自动卸载守护
    线程让路，或等主动驱逐（request_evict）生效。探测不到显存信息时
    fail-open 直接放行（不能因为"判断不了"就永远卡住加载）。
    """
    deadline = time.time() + timeout_s
    announced = False
    while True:
        free = vram_free_gb(max_age=0.0)
        if free is None:
            return True
        if free >= min_free_gb:
            return True
        if time.time() >= deadline:
            return False
        if log is not None and not announced:
            announced = True
            log(f"空闲显存 {free:.1f}GB < 需求 {min_free_gb:.1f}GB，等待其他模型让路（最多 {timeout_s:.0f}s）…")
        time.sleep(poll_s)


def request_evict(url: str, timeout: float = 15.0) -> bool:
    """请求一个 subprocess_service 插件的子进程立即软驱逐（卸载模型、
    释放显存、但保留子进程本身继续监听）——子进程一侧如果正在处理中的
    请求会等它做完再卸（同 wemm_server.py 的"当前一条编完再卸"语义，由
    子进程自己实现，这里只负责发请求）。

    子进程不在/请求失败一律返回 False（fail-open：绝不能因为驱逐请求
    失败就阻塞调用方的加载流程——调用方应该改用 wait_for_vram 兜底）。
    """
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/evict", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception:  # noqa: BLE001
        return False
