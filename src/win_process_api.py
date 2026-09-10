# -*- coding: utf-8 -*-
"""
Windows 进程高级 API 封装
支持: 进程优先级 / CPU亲和性 / IO优先级 / 内存优先级
通过 ctypes 调用 Windows 原生 API
"""

import ctypes
from ctypes import wintypes

# Windows API 常量
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 优先级类
IDLE_PRIORITY_CLASS = 0x0040
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
NORMAL_PRIORITY_CLASS = 0x0020
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
HIGH_PRIORITY_CLASS = 0x0080
REALTIME_PRIORITY_CLASS = 0x0100

# 后台模式（同时降低IO和CPU优先级）
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
PROCESS_MODE_BACKGROUND_END = 0x00200000

# ProcessInformationClass
# 注意：以下两个信息类常量是实测验证过的，切勿按某些 AI 审查建议改成 0x1D/0x1C。
# 实测证据（Windows 10/11 x64）：0x27 设置内存优先级返回 NTSTATUS=0（成功），
# 0x28 返回 STATUS_INVALID_PARAMETER（失败）。0x21 设置 IO 优先级同样实测可用。
# ProcessMemoryPriority = 0x27  # Windows 8+（注意：不是0x28）
ProcessMemoryPriority = 0x27  # Windows 8+；实测有效，见上方注释
ProcessIoPriority = 0x21       # 未文档化；实测可用（NtSetInformationProcess）

# 内存优先级值 (1-5，0无效)
MEMORY_PRIORITY_LOW = 1
MEMORY_PRIORITY_MEDIUM = 2
MEMORY_PRIORITY_BELOW_NORMAL = 3
MEMORY_PRIORITY_NORMAL = 5

# MEMORY_PRIORITY_INFORMATION 结构体（NtSetInformationProcess 用 ULONG 即可）

# IO 优先级值（通过 NtSetInformationProcess）
IO_PRIORITY_VERY_LOW = 0
IO_PRIORITY_LOW = 1
IO_PRIORITY_NORMAL = 2
IO_PRIORITY_HIGH = 3

# 加载 DLL
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll', use_last_error=True)

# 函数原型
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.SetPriorityClass.restype = wintypes.BOOL
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]

kernel32.GetPriorityClass.restype = wintypes.DWORD
kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]

kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]

# SetProcessInformation
kernel32.SetProcessInformation.restype = wintypes.BOOL
kernel32.SetProcessInformation.argtypes = [
    wintypes.HANDLE,  # ProcessHandle
    ctypes.c_int,      # ProcessInformationClass
    ctypes.c_void_p,   # ProcessInformation
    wintypes.DWORD,    # ProcessInformationSize
]

# NtSetInformationProcess (未文档化)
ntdll.NtSetInformationProcess.restype = ctypes.c_long
ntdll.NtSetInformationProcess.argtypes = [
    wintypes.HANDLE,  # ProcessHandle
    ctypes.c_int,      # ProcessInformationClass
    ctypes.c_void_p,   # ProcessInformation
    wintypes.ULONG,    # ProcessInformationLength
]


def open_process(pid, access=PROCESS_QUERY_INFORMATION | PROCESS_SET_INFORMATION):
    """打开进程句柄"""
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def close_handle(handle):
    """关闭句柄"""
    if handle:
        kernel32.CloseHandle(handle)


def set_priority(pid, priority_class):
    """设置进程优先级类"""
    handle = open_process(pid)
    try:
        result = kernel32.SetPriorityClass(handle, priority_class)
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def get_priority(pid):
    """获取进程优先级类（失败时抛异常，不返回0）"""
    handle = open_process(pid, PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION)
    try:
        result = kernel32.GetPriorityClass(handle)
        if result == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return result
    finally:
        close_handle(handle)


def set_affinity(pid, mask):
    """设置CPU亲和性掩码"""
    handle = open_process(pid)
    try:
        result = kernel32.SetProcessAffinityMask(handle, mask)
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def set_memory_priority(pid, priority):
    """
    设置进程内存优先级 (Windows 8+)
    priority: 1(low), 2(medium), 3(below_normal), 5(normal)
    注意：0(very_low) 在 Windows 10/11 上无效，会被拒绝
    使用 NtSetInformationProcess（和 IO 优先级同一 API）
    """
    handle = open_process(pid)
    try:
        if priority not in (1, 2, 3, 5):
            raise ValueError(f"内存优先级无效: {priority}（有效值 1/2/3/5）")
        value = ctypes.c_ulong(priority)
        result = ntdll.NtSetInformationProcess(
            handle,
            ProcessMemoryPriority,
            ctypes.byref(value),
            ctypes.sizeof(value)
        )
        if result != 0:
            raise OSError(f"NtSetInformationProcess(MemoryPriority) failed: NTSTATUS={result:#x}")
    finally:
        close_handle(handle)


def set_io_priority(pid, priority):
    """
    设置进程IO优先级（通过 NtSetInformationProcess，未文档化API）
    priority: 0(very low), 1(low), 2(normal), 3(high)
    注意：高IO优先级需要管理员权限
    """
    handle = open_process(pid)
    try:
        if priority not in (0, 1, 2, 3):
            raise ValueError(f"IO优先级无效: {priority}（有效值 0/1/2/3）")
        value = ctypes.c_ulong(priority)
        result = ntdll.NtSetInformationProcess(
            handle,
            ProcessIoPriority,
            ctypes.byref(value),
            ctypes.sizeof(value)
        )
        if result != 0:
            raise OSError(f"NtSetInformationProcess failed: NTSTATUS={result:#x}")
    finally:
        close_handle(handle)


def set_background_mode(pid, enable=True):
    """
    设置进程后台模式（同时降低CPU和IO优先级）
    enable=True: 进入后台模式（低IO+低CPU）
    enable=False: 退出后台模式
    注意：PROCESS_MODE_BACKGROUND_BEGIN/END 仅适用于当前进程句柄（GetCurrentProcess），
    对其他进程调用 SetPriorityClass 会失败（ERROR_ACCESS_DENIED）。
    """
    import os
    if pid != os.getpid():
        raise ValueError("后台模式(PROCESS_MODE_BACKGROUND_BEGIN/END)仅能用于当前进程，不能用于其他进程")
    handle = open_process(pid)
    try:
        flag = PROCESS_MODE_BACKGROUND_BEGIN if enable else PROCESS_MODE_BACKGROUND_END
        result = kernel32.SetPriorityClass(handle, flag)
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


# 优先级名称映射
PRIORITY_NAMES = {
    IDLE_PRIORITY_CLASS: "低 (Idle)",
    BELOW_NORMAL_PRIORITY_CLASS: "低于正常",
    NORMAL_PRIORITY_CLASS: "正常",
    ABOVE_NORMAL_PRIORITY_CLASS: "高于正常",
    HIGH_PRIORITY_CLASS: "高",
    REALTIME_PRIORITY_CLASS: "实时",
}

MEMORY_PRIORITY_NAMES = {
    0: "Very Low",
    1: "Low",
    2: "Medium",
    3: "Below Normal",
    5: "Normal",
}

IO_PRIORITY_NAMES = {
    0: "Very Low",
    1: "Low",
    2: "Normal",
    3: "High",
}


if __name__ == "__main__":
    # 测试
    import os
    print("Windows 进程高级 API 封装测试")
    print(f"当前 PID: {os.getpid()}")
    print(f"当前优先级: {PRIORITY_NAMES.get(get_priority(os.getpid()), '未知')}")
    print("API 加载成功")
