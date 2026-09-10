# -*- coding: utf-8 -*-
"""
GameBoost Web - Web 界面版游戏进程优化工具
用 Python 自带 http.server，无需 tkinter，浏览器访问

改进点：
- 添加类型注解
- 改进错误处理（更具体的异常类型）
- 添加输入验证
- 改进日志记录
- 线程安全优化
"""

__version__ = "v0.4.0"  # 版本号唯一来源，HTML/日志导出共用，避免漏改
# v0.4.0: 新增功能合集（配置导入导出/更新检测/开机自启/桌面通知/全局快捷键/进程图标显示）
# v0.3.3: 修复单实例互斥（Global命名空间需管理员权限导致第二个实例检测失败，改用Local命名空间+立即保存错误码）
# v0.3.2: 修复预设丢失（打包时同步presets目录）+ 单实例已运行时自动打开WebUI
# v0.3.1: 修复DeepSeek审查发现的问题（预设取消初始值兜底 + 非管理员提示条严格类型检查 + Ledger原子写入+config目录确保存在）
# v0.3.0: 崩溃恢复ledger + 可选管理员模式 + 启动参数(--minimized/--autostart) + WebUI非管理员提示条

import json
import os
import re
import sys
import time
import threading
import webbrowser
import logging
import ctypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from typing import Dict, List, Optional, Any

import psutil

# ===== v0.2.8 新增：内置保护进程名单 =====
# 前缀匹配（同游戏匹配规则，长度>=4），覆盖显卡/外设/硬件调控软件，永不降权。
# 保护比漏保护安全：误保护最多少降权一个进程，漏保护可能压坏鼠标宏/灯光/风扇策略。
PROTECTED_PROCESSES: List[str] = [
    # 系统核心
    "explorer", "svchost", "services", "lsass", "csrss", "smss",
    "wininit", "winlogon", "dwm", "fontdrvhost", "searchindexer",
    "searchhost", "startmenuexperiencehost", "shellexperiencehost",
    "runtimebroker", "backgroundtaskhost", "system", "registry",
    # 显卡调控（NVIDIA / AMD / Intel）
    "nvcontainer", "nvbackend", "nvidia", "nvdisplay",
    "amdrsserv", "atieclxx", "radeonsoftware", "amd",
    "igfx", "igfxcuiservice", "intel",
    # 外设 / 硬件调控大厂（前缀命中全家桶）
    "lghub", "logi", "razer", "icue", "corsair", "steelseries",
    "awcc", "alienfx", "occontrol", "armourycrate", "asus",
    "msi", "dragoncenter", "ghelper",
]
# ACE 反作弊进程（腾讯系：卡丘/三角洲/瓦国服/无畏契约国服）
DEFAULT_ACE_PROCESSES: List[str] = [
    "ace", "anticheatexpert", "sguard", "sguard64",
    "aceguard", "ace-base", "ace-logic",
]


def _full_affinity_mask() -> int:
    """恢复 ACE 进程时使用的全核亲和性掩码"""
    n = psutil.cpu_count() or 1
    return (1 << n) - 1

# 导入 Windows API
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import win_process_api as winapi

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger("GameBoost")

# 优先级映射
PRIORITY_MAP: Dict[str, int] = {
    "realtime": psutil.REALTIME_PRIORITY_CLASS,
    "high": psutil.HIGH_PRIORITY_CLASS,
    "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
    "normal": psutil.NORMAL_PRIORITY_CLASS,
    "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
    "idle": psutil.IDLE_PRIORITY_CLASS,
}
PRIORITY_NAME: Dict[int, str] = {v: k for k, v in PRIORITY_MAP.items()}
PRIORITY_LABELS: Dict[str, str] = {
    "realtime": "实时", "high": "高", "above_normal": "高于正常",
    "normal": "正常", "below_normal": "低于正常", "idle": "低",
}
# 优先级顺序映射（值越小优先级越高）
# 注意：Windows 优先级类常量值不是线性的，不能直接用 > 比较
PRIORITY_RANK: Dict[int, int] = {
    psutil.REALTIME_PRIORITY_CLASS: 1,
    psutil.HIGH_PRIORITY_CLASS: 2,
    psutil.ABOVE_NORMAL_PRIORITY_CLASS: 3,
    psutil.NORMAL_PRIORITY_CLASS: 4,
    psutil.BELOW_NORMAL_PRIORITY_CLASS: 5,
    psutil.IDLE_PRIORITY_CLASS: 6,
}

MEM_PRIORITY_MAP: Dict[str, int] = {"very_low": 1, "low": 1, "medium": 2, "below_normal": 3, "normal": 5}  # very_low=0在Win10/11无效，映射到low(1)
IO_PRIORITY_MAP: Dict[str, int] = {"very_low": 0, "low": 1, "normal": 2, "high": 3}

# 有效优先级值集合（用于输入验证）
VALID_CPU_PRIORITIES = set(PRIORITY_MAP.keys())
VALID_IO_PRIORITIES = set(IO_PRIORITY_MAP.keys())
VALID_MEM_PRIORITIES = set(MEM_PRIORITY_MAP.keys())


def _is_admin() -> bool:
    """v0.2.8: 检测当前进程是否管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _detect_gpu() -> str:
    """v0.2.8: 探测显卡型号（注册表，兼容 N/A/I 卡，跳过虚拟适配器）"""
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        skip = ("parsec", "virtual", "remote", "mirror", "basic display", "间接", "远程")
        for i in range(16):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{i:04d}")
                desc = winreg.QueryValueEx(key, "DriverDesc")[0]
                winreg.CloseKey(key)
                if desc:
                    d = str(desc).lower()
                    if any(s in d for s in skip):
                        continue
                    return str(desc)
            except OSError:
                continue
    except Exception:
        pass
    return "未知"


def _check_for_update() -> Dict[str, Any]:
    """v0.4.0: 检查 GitHub Release 是否有新版本"""
    try:
        import urllib.request
        repo = "Esgape/GameBoost-Pro"
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        req = urllib.request.Request(url, headers={
            "User-Agent": "CalaNeko-Updater",
            "Accept": "application/vnd.github.v3+json",
        })
        # 支持配置 GitHub token（私密仓库需要）
        gh_token = os.environ.get("CALANEKO_GITHUB_TOKEN", "")
        if gh_token:
            req.add_header("Authorization", f"token {gh_token}")
        with urllib.request.urlopen(req, timeout=8) as resp:
            release = json.loads(resp.read().decode("utf-8"))
            latest_tag = release.get("tag_name", "").lstrip("vV")
            current = __version__.lstrip("vV")
            # 简单版本比较（支持 x.y.z 格式）
            def _ver_tuple(v):
                parts = []
                for p in v.split("."):
                    try:
                        parts.append(int(p))
                    except ValueError:
                        parts.append(0)
                return tuple(parts)
            has_update = _ver_tuple(latest_tag) > _ver_tuple(current)
            return {
                "has_update": has_update,
                "current_version": __version__,
                "latest_version": release.get("tag_name", ""),
                "release_name": release.get("name", ""),
                "release_url": release.get("html_url", ""),
                "published_at": release.get("published_at", ""),
                "body": release.get("body", "")[:500] if release.get("body") else "",
            }
    except Exception as e:
        return {
            "has_update": False,
            "current_version": __version__,
            "error": str(e),
            "message": "检查更新失败（可能是网络问题或仓库无Release）",
        }


def _get_exe_path() -> str:
    """v0.4.0: 获取当前 exe 路径（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def _get_autostart_status() -> Dict[str, Any]:
    """v0.4.0: 检查开机自启状态（HKCU 注册表）"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
        try:
            value, _ = winreg.QueryValueEx(key, "CalaNeko")
            winreg.CloseKey(key)
            return {"enabled": True, "path": value, "exe_path": _get_exe_path()}
        except FileNotFoundError:
            winreg.CloseKey(key)
            return {"enabled": False, "exe_path": _get_exe_path()}
    except Exception as e:
        return {"enabled": False, "error": str(e), "exe_path": _get_exe_path()}


def _set_autostart(enable: bool) -> Dict[str, Any]:
    """v0.4.0: 设置开机自启（写 HKCU 注册表，不需要管理员权限）"""
    try:
        import winreg
        exe_path = _get_exe_path()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, "CalaNeko", 0, winreg.REG_SZ, f'"{exe_path}" --autostart')
            winreg.CloseKey(key)
            return {"success": True, "enabled": True, "path": exe_path, "message": "开机自启已启用"}
        else:
            try:
                winreg.DeleteValue(key, "CalaNeko")
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
            return {"success": True, "enabled": False, "message": "开机自启已禁用"}
    except Exception as e:
        return {"success": False, "error": str(e), "message": "设置开机自启失败"}


def _send_desktop_notification(title: str, message: str, icon_type: str = "info") -> bool:
    """v0.4.0: 发送 Windows 桌面通知（使用 PowerShell NotifyIcon，无需额外依赖）"""
    try:
        import subprocess
        icon_map = {"info": "Information", "warning": "Warning", "error": "Error"}
        icon = icon_map.get(icon_type, "Information")
        # 转义 PowerShell 字符串中的特殊字符
        safe_title = title.replace("'", "''").replace('"', '`"')
        safe_message = message.replace("'", "''").replace('"', '`"')
        ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::{icon}
$notify.Visible = $true
$notify.ShowBalloonTip(5000, "{safe_title}", "{safe_message}", [System.Windows.Forms.ToolTipIcon]::{icon})
Start-Sleep -Milliseconds 5500
$notify.Dispose()
'''
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        return True
    except Exception:
        return False


class GlobalHotkeyManager:
    """v0.4.0: 全局快捷键管理器（使用 Windows API RegisterHotKey）"""
    VK = {chr(i): i for i in range(0x41, 0x5B)}
    VK.update({str(i): 0x30 + i for i in range(10)})
    VK.update({f'F{i}': 0x6F + i for i in range(1, 13)})
    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
    WM_HOTKEY = 0x0312

    def __init__(self, callback):
        self.callback = callback
        self._thread = None
        self._running = False
        self._hotkey_id = 1
        self._hwnd = None
        self._wnd_proc_ref = None

    def _create_message_window(self):
        import ctypes
        from ctypes import wintypes
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_HOTKEY and self.callback:
                try: self.callback()
                except Exception: pass
            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        self._wnd_proc_ref = WNDPROC(wnd_proc)
        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HCURSOR), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        wc = WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc_ref
        wc.lpszClassName = "CalaNekoHotkeyWnd"
        wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        ctypes.windll.user32.RegisterClassW(ctypes.byref(wc))
        self._hwnd = ctypes.windll.user32.CreateWindowExW(0, "CalaNekoHotkeyWnd", "CalaNekoHotkey", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)

    def register(self, modifiers, key):
        import ctypes
        if not self._hwnd: self._create_message_window()
        mod = 0
        if 'ctrl' in modifiers: mod |= self.MOD_CONTROL
        if 'alt' in modifiers: mod |= self.MOD_ALT
        if 'shift' in modifiers: mod |= self.MOD_SHIFT
        if 'win' in modifiers: mod |= self.MOD_WIN
        vk = self.VK.get(key.upper(), 0)
        if vk == 0: return False
        return bool(ctypes.windll.user32.RegisterHotKey(self._hwnd, self._hotkey_id, mod, vk))

    def unregister(self):
        import ctypes
        if self._hwnd: ctypes.windll.user32.UnregisterHotKey(self._hwnd, self._hotkey_id)

    def _message_loop(self):
        import ctypes
        from ctypes import wintypes
        msg = wintypes.MSG()
        while self._running:
            result = ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == -1 or result == 0: break
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    def start(self, modifiers=('ctrl', 'alt'), key='G'):
        if self._running: return
        if not self.register(modifiers, key): return False
        self._running = True
        self._thread = threading.Thread(target=self._message_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self.unregister()
        if self._hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)
            self._hwnd = None


# v0.4.0: 进程图标缓存（避免重复提取）
_icon_cache: Dict[str, str] = {}


def _extract_process_icon(exe_path: str) -> str:
    """v0.4.0: 提取进程 exe 的图标，返回 base64 PNG 字符串（带缓存）"""
    if not exe_path or not os.path.exists(exe_path):
        return ""
    if exe_path in _icon_cache:
        return _icon_cache[exe_path]
    try:
        import win32api
        import win32con
        import win32gui
        from PIL import Image
        import io
        import base64

        # 使用 SHGetFileInfo 提取图标
        SHGFI_ICON = 0x000000100
        SHGFI_LARGEICON = 0x000000000
        SHGFI_SMALLICON = 0x000000001

        class SHFILEINFO(ctypes.Structure):
            _fields_ = [
                ("hIcon", ctypes.c_void_p),
                ("iIcon", ctypes.c_int),
                ("dwAttributes", ctypes.c_ulong),
                ("szDisplayName", ctypes.c_wchar * 260),
                ("szTypeName", ctypes.c_wchar * 80),
            ]

        shfi = SHFILEINFO()
        ctypes.windll.shell32.SHGetFileInfoW(
            exe_path, 0, ctypes.byref(shfi), ctypes.sizeof(shfi),
            SHGFI_ICON | SHGFI_SMALLICON
        )
        if not shfi.hIcon:
            _icon_cache[exe_path] = ""
            return ""

        # 转换 HICON 为 PIL Image
        hdc = win32gui.CreateCompatibleDC(0)
        bmp = win32gui.CreateCompatibleBitmap(win32gui.GetDC(0), 16, 16)
        win32gui.SelectObject(hdc, bmp)
        win32gui.DrawIconEx(hdc, 0, 0, shfi.hIcon, 16, 16, 0, 0, 3)
        win32gui.DestroyIcon(shfi.hIcon)

        # 获取位图数据
        bmp_info = win32gui.GetObject(bmp)
        bmp_str = win32api.GetBitmapBits(bmp, bmp_info.bmWidthBytes * bmp_info.bmHeight)
        img = Image.frombuffer('RGBA', (bmp_info.bmWidth, bmp_info.bmHeight), bmp_str, 'raw', 'BGRA', 0, 1)

        # 转换为 base64 PNG
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        icon_data = base64.b64encode(buf.getvalue()).decode('utf-8')

        win32gui.DeleteObject(bmp)
        win32gui.DeleteDC(hdc)

        _icon_cache[exe_path] = icon_data
        return icon_data
    except Exception:
        _icon_cache[exe_path] = ""
        return ""


class GameBoostEngine:
    """游戏优化引擎 - 线程安全的进程管理核心"""

    def __init__(self, config_path: str):
        self.config_path: str = config_path
        self.config: Dict[str, Any] = self.load_config()
        self.monitoring: bool = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.active_games: Dict[int, Dict[str, Any]] = {}
        self.lowered_processes: Dict[int, int] = {}
        self.ace_lowered: Dict[int, Dict[str, Any]] = {}  # v0.2.8: ACE 降权进程（含原始亲和性）
        self.logs: List[str] = []
        self.log_lock: threading.Lock = threading.Lock()
        self._state_lock: threading.Lock = threading.Lock()
        self._data_lock: threading.RLock = threading.RLock()
        # 游戏列表缓存（避免每次 is_game_process 都重新构建）
        self._game_list_cache: Optional[List[str]] = None
        self._game_list_mtime: float = 0.0
        # v0.3.0: 崩溃恢复 ledger（改前写磁盘，异常退出后下次启动自动恢复）
        self.ledger_path: str = os.path.join(os.path.dirname(config_path), "tweak_ledger.json")
        self._crash_recovery()
        # v0.4.0: 全局快捷键管理器
        self.hotkey_manager = None
        self._init_hotkey()

    def load_config(self) -> Dict[str, Any]:
        """加载配置文件，失败时返回默认配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8-sig') as f:
                config = json.load(f)
                logger.info(f"配置文件加载成功: {self.config_path}")
                return config
        except FileNotFoundError:
            logger.warning(f"配置文件不存在，使用默认配置: {self.config_path}")
        except json.JSONDecodeError as e:
            logger.error(f"配置文件 JSON 解析失败: {e}")
        except Exception as e:
            logger.error(f"配置文件加载失败: {e}")
        return self._default_config()

    def _default_config(self) -> Dict[str, Any]:
        """返回默认配置"""
        return {
            "游戏进程列表": ["steam", "wegame", "Calabiyau"],
            "游戏优先级": "high",
            "游戏IO优先级": "normal",
            "游戏内存优先级": "normal",
            "降低后台进程优先级": True,
            "后台进程优先级": "below_normal",
            "后台IO优先级": "low",
            "后台内存优先级": "low",
            "排除进程": ["explorer", "svchost", "system"],
            "监控间隔秒": 3,
            # v0.2.8 新增
            "ACE降权开关": True,
            "ACE进程列表": list(DEFAULT_ACE_PROCESSES),
            "ACE降权优先级": "idle",
            "保护进程": list(PROTECTED_PROCESSES),
            "黑名单进程": [],
            "记录降权明细": False,
        }

    # ===== v0.3.0: 崩溃恢复 ledger =====
    def _crash_recovery(self) -> None:
        """启动时检查上次是否异常退出，如有未恢复的改动则自动恢复。
        参考 quick-fps-optimizer 的 durable ledger 设计：改前写磁盘，强制杀进程后下次启动恢复。"""
        try:
            if not os.path.exists(self.ledger_path):
                return
            with open(self.ledger_path, 'r', encoding='utf-8-sig') as f:
                ledger = json.load(f)
            if not ledger:
                os.remove(self.ledger_path)
                return
            self.log(f"⚠️ 检测到上次异常退出，正在自动恢复 {len(ledger)} 个进程的优先级...")
            recovered = 0
            for pid_str, info in ledger.items():
                try:
                    pid = int(pid_str)
                    if not psutil.pid_exists(pid):
                        continue
                    proc = psutil.Process(pid)
                    # 恢复 CPU 优先级
                    orig = info.get("original_priority")
                    if orig is not None:
                        try:
                            proc.nice(orig)
                            recovered += 1
                        except (psutil.AccessDenied, psutil.NoSuchProcess):
                            pass
                    # 恢复 IO/内存优先级为 Normal
                    try:
                        winapi.set_io_priority(pid, 2)
                        winapi.set_memory_priority(pid, 5)
                    except Exception:
                        pass
                    # 恢复 ACE 进程的亲和性
                    if info.get("type") == "ace" and info.get("orig_affinity"):
                        try:
                            proc.cpu_affinity(info["orig_affinity"])
                        except Exception:
                            try:
                                winapi.set_affinity(pid, _full_affinity_mask())
                            except Exception:
                                pass
                except (ValueError, psutil.NoSuchProcess):
                    pass
                except Exception:
                    pass
            # 恢复后清空 ledger
            try:
                os.remove(self.ledger_path)
            except Exception:
                pass
            self.log(f"✅ 崩溃恢复完成，已恢复 {recovered} 个进程")
        except Exception as e:
            self.log(f"崩溃恢复检查失败: {e}")

    def _init_hotkey(self) -> None:
        """v0.4.0: 初始化全局快捷键管理器（默认 Ctrl+Alt+G 切换监控）"""
        try:
            def _toggle():
                if self.monitoring:
                    self.stop_monitor()
                else:
                    self.start_monitor()
            self.hotkey_manager = GlobalHotkeyManager(_toggle)
        except Exception as e:
            self.log(f"全局快捷键初始化失败: {e}")
            self.hotkey_manager = None

    def _save_ledger(self) -> None:
        """将当前所有改动写入磁盘 ledger（每次修改后调用，崩溃后可恢复）
        v0.3.1: 确保config目录存在 + 临时文件原子重命名（避免写入过程中崩溃导致文件损坏）"""
        try:
            # 确保 config 目录存在
            config_dir = os.path.dirname(self.ledger_path)
            if config_dir and not os.path.exists(config_dir):
                os.makedirs(config_dir, exist_ok=True)

            ledger: Dict[str, Any] = {}
            with self._data_lock:
                for pid, info in self.active_games.items():
                    ledger[str(pid)] = {
                        "type": "game",
                        "original_priority": info.get("original_priority"),
                        "name": info.get("name"),
                    }
                for pid, orig in self.lowered_processes.items():
                    if str(pid) not in ledger:
                        ledger[str(pid)] = {
                            "type": "background",
                            "original_priority": orig,
                        }
                for pid, info in self.ace_lowered.items():
                    ledger[str(pid)] = {
                        "type": "ace",
                        "original_priority": info.get("orig_prio"),
                        "orig_affinity": info.get("orig_affinity"),
                    }
            if ledger:
                # 临时文件 + 原子重命名（避免写入过程中崩溃导致文件损坏）
                tmp_path = self.ledger_path + '.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(ledger, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.ledger_path)  # 原子重命名
            else:
                # 没有改动时清空 ledger
                if os.path.exists(self.ledger_path):
                    try:
                        os.remove(self.ledger_path)
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"ledger 写入失败: {e}")

    def _clear_ledger(self) -> None:
        """恢复所有改动后清空 ledger 文件"""
        try:
            if os.path.exists(self.ledger_path):
                os.remove(self.ledger_path)
        except Exception:
            pass

    def save_config(self) -> bool:
        """保存配置文件，返回是否成功"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.info("配置文件保存成功")
            return True
        except OSError as e:
            logger.error(f"配置文件保存失败: {e}")
            return False

    def log(self, msg: str) -> None:
        """添加日志条目（线程安全，仅写入前端日志列表，不重复输出到控制台）"""
        timestamp = time.strftime("%H:%M:%S")
        with self.log_lock:
            self.logs.append(f"[{timestamp}] {msg}")
            if len(self.logs) > 200:
                self.logs = self.logs[-200:]

    def _get_game_list_lower(self) -> List[str]:
        """获取小写化的游戏列表（带缓存，config 修改后自动失效，线程安全）"""
        with self._data_lock:
            game_list = self.config.get("游戏进程列表", [])
            # 用内容做缓存 key（避免同长度增删时缓存不失效）
            cache_key = "|".join(game_list)
            if self._game_list_cache is not None and self._game_list_mtime == cache_key:
                return self._game_list_cache
            self._game_list_cache = [g.lower() for g in game_list]
            self._game_list_mtime = cache_key
            return self._game_list_cache

    def _invalidate_game_cache(self) -> None:
        """使游戏列表缓存失效（config 修改后调用）"""
        self._game_list_cache = None
        self._game_list_mtime = 0.0

    def is_game_process(self, name: Optional[str]) -> bool:
        """判断进程名是否匹配游戏列表（精确匹配优先，前缀匹配需长度>=4）"""
        if not name:
            return False
        name_lower = name.lower()
        if name_lower.endswith('.exe'):
            name_lower = name_lower[:-4]

        game_list = self._get_game_list_lower()
        for game in game_list:
            game_clean = game[:-4] if game.endswith('.exe') else game
            if not game_clean or len(game_clean) < 2:
                continue
            # 精确匹配
            if name_lower == game_clean:
                return True
            # 前缀匹配（游戏名长度>=4，且进程名以游戏名+分隔符开头，避免 steam 匹配 steamwebhelper）
            if len(game_clean) >= 4 and name_lower.startswith(game_clean):
                suffix = name_lower[len(game_clean):]
                # 允许的后缀分隔符：空、数字、下划线、连字符、点
                if suffix == '' or suffix[0] in '0123456789_-.':
                    return True
        return False

    def get_process_list(self) -> List[Dict[str, Any]]:
        """获取进程列表（按 CPU 降序排列，线程安全）"""
        processes: List[Dict[str, Any]] = []
        with self._data_lock:
            active_pids = set(self.active_games.keys())
        for proc in psutil.process_iter(['pid', 'name', 'nice', 'cpu_percent', 'memory_info']):
            try:
                pid = proc.info['pid']
                name = proc.info['name'] or "?"
                nice = proc.info['nice']
                priority = PRIORITY_NAME.get(nice, "normal") if nice else "normal"
                cpu = proc.info['cpu_percent'] or 0
                mem = (proc.info['memory_info'].rss / 1024 / 1024) if proc.info['memory_info'] else 0
                is_game = self.is_game_process(name)
                is_active = pid in active_pids
                # v0.4.0: 进程图标（只对活跃游戏提取，避免性能问题）
                icon = ""
                if is_active and self.config.get("显示进程图标", True):
                    try:
                        exe_path = proc.exe()
                        if exe_path:
                            icon = _extract_process_icon(exe_path)
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
                processes.append({
                    "pid": pid, "name": name, "priority": priority,
                    "priority_label": PRIORITY_LABELS.get(priority, priority),
                    "cpu": round(cpu, 1), "memory": round(mem, 0),
                    "is_game": is_game, "is_active": is_active,
                    "icon": icon,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        processes.sort(key=lambda x: x['cpu'], reverse=True)
        return processes

    def set_priority(self, pid: int, priority: str) -> bool:
        """设置进程 CPU 优先级"""
        if priority not in VALID_CPU_PRIORITIES:
            self.log(f"无效的优先级值: {priority}")
            return False
        try:
            proc = psutil.Process(pid)
            proc.nice(PRIORITY_MAP[priority])
            self.log(f"PID {pid} ({proc.name()}) CPU优先级→{PRIORITY_LABELS.get(priority, priority)}")
            return True
        except psutil.NoSuchProcess:
            self.log(f"进程不存在: PID {pid}")
        except psutil.AccessDenied:
            self.log(f"权限不足，无法设置 PID {pid} 的优先级")
        except Exception as e:
            self.log(f"设置优先级失败 PID {pid}: {e}")
        return False
    def set_io_priority(self, pid: int, io_priority: str) -> bool:
        """设置进程 IO 优先级"""
        if io_priority not in VALID_IO_PRIORITIES:
            self.log(f"无效的 IO 优先级值: {io_priority}")
            return False
        try:
            winapi.set_io_priority(pid, IO_PRIORITY_MAP[io_priority])
            self.log(f"PID {pid} IO优先级→{io_priority}")
            return True
        except psutil.NoSuchProcess:
            self.log(f"进程不存在: PID {pid}")
        except psutil.AccessDenied:
            self.log(f"权限不足，无法设置 PID {pid} 的 IO 优先级")
        except Exception as e:
            self.log(f"设置IO优先级失败 PID {pid}: {e}")
        return False

    def set_memory_priority(self, pid: int, mem_priority: str) -> bool:
        """设置进程内存优先级"""
        if mem_priority not in VALID_MEM_PRIORITIES:
            self.log(f"无效的内存优先级值: {mem_priority}")
            return False
        try:
            winapi.set_memory_priority(pid, MEM_PRIORITY_MAP[mem_priority])
            self.log(f"PID {pid} 内存优先级→{mem_priority}")
            return True
        except psutil.NoSuchProcess:
            self.log(f"进程不存在: PID {pid}")
        except psutil.AccessDenied:
            self.log(f"权限不足，无法设置 PID {pid} 的内存优先级")
        except Exception as e:
            self.log(f"设置内存优先级失败 PID {pid}: {e}")
        return False

    def add_to_game_list(self, name: str) -> bool:
        """添加进程到游戏列表（带输入验证）"""
        if not name:
            return False
        name = name.strip()
        # 长度验证
        if len(name) < 1 or len(name) > 100:
            self.log(f"添加游戏失败: 名称长度无效 ({len(name)} 字符)")
            return False
        # 字符验证：允许字母、数字、下划线、连字符、点、中文、空格、括号、加号
        import re
        if not re.match(r'^[\w\-.\u4e00-\u9fff\s()+\[\]{}]+$', name):
            self.log(f"添加游戏失败: 名称包含非法字符")
            return False
        with self._data_lock:
            game_list = self.config.get("游戏进程列表", [])
            if name not in game_list:
                game_list.append(name)
                self.config["游戏进程列表"] = game_list
                self._invalidate_game_cache()
                self.save_config()
                self.log(f"已添加到游戏列表: {name}")
                return True
        return False

    def start_monitor(self) -> None:
        """开始监控游戏进程"""
        with self._state_lock:
            if self.monitoring:
                return
            self.monitoring = True
        self.log("开始监控游戏进程")
        # v0.4.0: 注册全局快捷键（如果配置启用）
        if self.config.get("全局快捷键", True) and self.hotkey_manager:
            if not self.hotkey_manager.start(('ctrl', 'alt'), 'G'):
                self.log("警告: 全局快捷键注册失败（可能被其他程序占用）")
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop_monitor(self) -> None:
        """停止监控并恢复所有进程"""
        with self._state_lock:
            if not self.monitoring:
                return
            self.monitoring = False
        # v0.4.0: 注销全局快捷键
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        self.restore_all()
        self.log("停止监控")

    def _monitor_loop(self) -> None:
        """监控循环（在独立线程中运行）"""
        while self.monitoring:
            try:
                self._check_games()
            except Exception as e:
                self.log(f"监控错误: {e}")
            interval = self.config.get("监控间隔秒", 3)
            if not isinstance(interval, (int, float)) or interval < 1:
                interval = 3
            time.sleep(interval)

    def _check_games(self) -> None:
        """检查游戏进程状态（检测退出和新启动）"""
        # 检查已激活游戏是否退出
        with self._data_lock:
            active_pids = list(self.active_games.keys())
        for pid in active_pids:
            if not psutil.pid_exists(pid):
                with self._data_lock:
                    info = self.active_games.pop(pid, None)
                    has_active = bool(self.active_games)
                    has_lowered = bool(self.lowered_processes)
                if info:
                    self.log(f"游戏退出: {info['name']} (PID={pid})")
                    # v0.4.0 桌面通知
                    if self.config.get("桌面通知", True):
                        _send_desktop_notification("🐱 CalaNeko", f"游戏已退出: {info['name']}\n进程优先级已恢复", "info")
                if not has_active and has_lowered:
                    self._restore_background()

        # 检测新游戏
        with self._data_lock:
            active_pids = set(self.active_games.keys())
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']
                if pid in active_pids:
                    continue
                if self.is_game_process(name):
                    self._boost_game(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue

    def _boost_game(self, proc: psutil.Process) -> None:
        """优化游戏进程（设置 CPU/IO/内存优先级）"""
        try:
            pid = proc.pid
            name = proc.name()
            original_priority = proc.nice()
            with self._data_lock:
                self.active_games[pid] = {
                    "name": name, "original_priority": original_priority,
                    "start_time": time.time(),
                }
            self.log(f"游戏启动: {name} (PID={pid})")
            # v0.4.0 桌面通知（可配置开关）
            if self.config.get("桌面通知", True):
                _send_desktop_notification("🎮 CalaNeko", f"游戏已启动: {name}\n正在优化进程优先级...", "info")
            # CPU 优先级
            game_prio = self.config.get("游戏优先级", "high")
            cpu_ok = False
            if game_prio in VALID_CPU_PRIORITIES:
                target_class = PRIORITY_MAP.get(game_prio, psutil.HIGH_PRIORITY_CLASS)
                proc.nice(target_class)
                # 验证是否设置成功
                try:
                    actual = proc.nice()
                    cpu_ok = (actual == target_class)
                    if not cpu_ok:
                        self.log(f"  警告: {name} CPU优先级未生效 (目标={target_class}, 实际={actual})")
                except Exception as e:
                    self.log(f"  警告: {name} CPU优先级验证失败: {e}")
            # IO 优先级
            io_ok = False
            try:
                game_io = self.config.get("游戏IO优先级", "normal")
                if game_io in VALID_IO_PRIORITIES:
                    winapi.set_io_priority(pid, IO_PRIORITY_MAP.get(game_io, 2))
                    io_ok = True
            except Exception as e:
                self.log(f"  警告: {name} IO优先级设置失败: {e}")
            # 内存优先级
            mem_ok = False
            try:
                game_mem = self.config.get("游戏内存优先级", "normal")
                if game_mem in VALID_MEM_PRIORITIES:
                    winapi.set_memory_priority(pid, MEM_PRIORITY_MAP.get(game_mem, 5))
                    mem_ok = True
            except Exception as e:
                self.log(f"  警告: {name} 内存优先级设置失败: {e}")
            self.log(f"游戏启动: {name} (PID={pid}) | CPU:{'OK' if cpu_ok else 'FAIL'} | IO:{'OK' if io_ok else 'FAIL'} | 内存:{'OK' if mem_ok else 'FAIL'}")
            # v0.3.0: 优化后保存 ledger（崩溃恢复）
            self._save_ledger()
            # 降低后台
            if self.config.get("降低后台进程优先级", False):
                self._lower_background()
        except psutil.NoSuchProcess:
            self.log(f"游戏进程在优化前已退出: PID {proc.pid}")
        except psutil.AccessDenied:
            try:
                pname = proc.name()
            except Exception:
                pname = "?"
            self.log(f"权限不足，无法优化游戏进程: {pname} (PID={proc.pid})")
        except Exception as e:
            self.log(f"优化游戏失败: {e}")

    def _matches_name(self, name_lower: str, patterns: List[str]) -> bool:
        """前缀/精确匹配（同游戏匹配规则：去.exe，长度>=4前缀，后缀需分隔符）"""
        if not name_lower:
            return False
        name_clean = name_lower[:-4] if name_lower.endswith('.exe') else name_lower
        for p in patterns:
            p_clean = (p[:-4] if p.endswith('.exe') else p).lower()
            if not p_clean or len(p_clean) < 2:
                continue
            if name_clean == p_clean:
                return True
            if len(p_clean) >= 4 and name_clean.startswith(p_clean):
                suffix = name_clean[len(p_clean):]
                if suffix == '' or suffix[0] in '0123456789_-.':
                    return True
        return False

    def _lower_background(self) -> None:
        """降低后台进程优先级
        v0.2.8: 排除进程 + 内置保护名单合并跳过；黑名单进程强制压 idle；ACE 降权独立处理
        """
        bg_prio = self.config.get("后台进程优先级", "below_normal")
        bg_class = PRIORITY_MAP.get(bg_prio, psutil.BELOW_NORMAL_PRIORITY_CLASS)
        bg_io = self.config.get("后台IO优先级", "low")
        bg_mem = self.config.get("后台内存优先级", "low")
        count = 0
        io_fail = 0
        mem_fail = 0
        ace_count = 0
        black_count = 0
        detail_logs: List[str] = []
        excluded = [e.lower() for e in self.config.get("排除进程", [])]
        protected = [e.lower() for e in self.config.get("保护进程", PROTECTED_PROCESSES)]
        blacklist = [e.lower() for e in self.config.get("黑名单进程", [])]
        ace_enabled = bool(self.config.get("ACE降权开关", True))
        ace_patterns = [e.lower() for e in self.config.get("ACE进程列表", DEFAULT_ACE_PROCESSES)]
        ace_prio = self.config.get("ACE降权优先级", "idle")
        ace_class = PRIORITY_MAP.get(ace_prio, psutil.IDLE_PRIORITY_CLASS)
        last_core_mask = 1 << ((psutil.cpu_count() or 1) - 1)  # 锁最后一个核
        has_last_core = (psutil.cpu_count() or 1) >= 2  # 单核机器不锁核（避免 set_affinity mask=0）
        full_mask = _full_affinity_mask()
        verbose = bool(self.config.get("记录降权明细", False))
        for proc in psutil.process_iter(['pid', 'name', 'nice']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']
                if pid in self.active_games or self.is_game_process(name):
                    continue
                name_lower = name.lower() if name else ""
                # 黑名单：强制压 idle（用户指定，永不跳过）
                if blacklist and self._matches_name(name_lower, blacklist):
                    try:
                        with self._data_lock:
                            if pid not in self.lowered_processes:
                                self.lowered_processes[pid] = proc.info['nice']
                        proc.nice(psutil.IDLE_PRIORITY_CLASS)
                        winapi.set_io_priority(pid, 0)
                        winapi.set_memory_priority(pid, 1)
                        black_count += 1
                        if verbose:
                            detail_logs.append(f"    黑名单: {name} (PID={pid})")
                    except Exception:
                        pass
                    continue
                # 排除进程 + 内置保护名单（永不降权）
                if self._matches_name(name_lower, excluded) or self._matches_name(name_lower, protected):
                    continue
                # ACE 反作弊：独立降权（idle + 锁最后核 + IO/内存 low）
                if ace_enabled and self._matches_name(name_lower, ace_patterns):
                    try:
                        with self._data_lock:
                            if pid not in self.ace_lowered:
                                self.ace_lowered[pid] = {
                                    "orig_prio": proc.info['nice'],
                                    "orig_affinity": None,  # 尽力读，失败则恢复全核
                                }
                                try:
                                    self.ace_lowered[pid]["orig_affinity"] = proc.cpu_affinity()
                                except Exception:
                                    pass
                        proc.nice(ace_class)
                        winapi.set_io_priority(pid, 1)   # low
                        winapi.set_memory_priority(pid, 1)  # low
                        if has_last_core:
                            try:
                                proc.cpu_affinity([psutil.cpu_count() - 1])
                            except Exception:
                                winapi.set_affinity(pid, last_core_mask)
                        ace_count += 1
                        if verbose:
                            detail_logs.append(f"    ACE降权: {name} (PID={pid})")
                    except Exception:
                        pass
                    continue
                current = proc.info['nice']
                # 使用优先级顺序比较（Windows 常量值非线性，不能直接 > 比较）
                current_rank = PRIORITY_RANK.get(current, 99)
                bg_rank = PRIORITY_RANK.get(bg_class, 99)
                if current is not None and current_rank < bg_rank:
                    with self._data_lock:
                        if pid not in self.lowered_processes:
                            self.lowered_processes[pid] = current
                    proc.nice(bg_class)
                    try:
                        if bg_io in VALID_IO_PRIORITIES:
                            winapi.set_io_priority(pid, IO_PRIORITY_MAP.get(bg_io, 1))
                        else:
                            io_fail += 1
                    except Exception:
                        io_fail += 1
                    try:
                        if bg_mem in VALID_MEM_PRIORITIES:
                            winapi.set_memory_priority(pid, MEM_PRIORITY_MAP.get(bg_mem, 1))
                        else:
                            mem_fail += 1
                    except Exception:
                        mem_fail += 1
                    count += 1
                    if verbose:
                        detail_logs.append(f"    后台降权: {name} (PID={pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        parts = []
        if count > 0:
            parts.append(f"后台 {count}")
        if ace_count > 0:
            parts.append(f"ACE {ace_count}")
        if black_count > 0:
            parts.append(f"黑名单 {black_count}")
        if parts:
            warn = ""
            if io_fail > 0:
                warn += f" IO失败:{io_fail}"
            if mem_fail > 0:
                warn += f" 内存失败:{mem_fail}"
            self.log(f"降权完成: {' '.join(parts)} 个进程{warn}")
            if verbose and detail_logs:
                for d in detail_logs[:50]:
                    self.log(d)
        # v0.3.0: 降权后保存 ledger（崩溃恢复）
        self._save_ledger()

    def _restore_background(self) -> None:
        """恢复后台进程原始优先级（已知限制：仅CPU还原原始值，IO/内存重置为Normal，设计取舍）"""
        with self._data_lock:
            items = list(self.lowered_processes.items())
            self.lowered_processes.clear()
            ace_items = list(self.ace_lowered.items())
            self.ace_lowered.clear()
        for pid, original in items:
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).nice(original)
                    try:
                        # 已知限制：IO/内存优先级未保存原始值，重置为Normal
                        winapi.set_io_priority(pid, 2)
                        winapi.set_memory_priority(pid, 5)
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception:
                pass
        # v0.2.8: 恢复 ACE 降权进程（优先级 + 亲和性 + IO/内存）
        for pid, info in ace_items:
            try:
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    proc.nice(info.get("orig_prio") if info.get("orig_prio") is not None else psutil.NORMAL_PRIORITY_CLASS)
                    orig_aff = info.get("orig_affinity")
                    try:
                        if orig_aff:
                            proc.cpu_affinity(orig_aff)
                        else:
                            proc.cpu_affinity(list(range(psutil.cpu_count() or 1)))
                    except Exception:
                        winapi.set_affinity(pid, _full_affinity_mask())
                    try:
                        winapi.set_io_priority(pid, 2)
                        winapi.set_memory_priority(pid, 5)
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception:
                pass
        if ace_items:
            self.log(f"ACE进程已恢复: {len(ace_items)} 个")
        self.log("后台进程已恢复")
        # v0.3.0: 恢复后清空 ledger
        self._clear_ledger()

    def restore_all(self) -> None:
        """恢复所有被修改的进程"""
        with self._data_lock:
            items = list(self.active_games.items())
            self.active_games.clear()
        for pid, info in items:
            try:
                if psutil.pid_exists(pid):
                    orig = info.get("original_priority")
                    # 防御：original_priority 可能为 None（权限不足/平台差异），回退到 Normal
                    psutil.Process(pid).nice(orig if orig is not None else psutil.NORMAL_PRIORITY_CLASS)
                    try:
                        winapi.set_io_priority(pid, 2)
                        winapi.set_memory_priority(pid, 5)
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception:
                pass
        self._restore_background()

    def get_status(self) -> Dict[str, Any]:
        """获取当前监控状态（线程安全快照）"""
        # 实时检测 AWCC 进程
        awcc_names = {'awcc.exe', 'awccoverlay.exe', 'alienfxsubagent.exe',
                       'awcc.scsubagent.exe', 'awcc.ucsubagent.exe',
                       'awperformance.scsubagent.exe', 'awperformance.ucsubagent.exe',
                       'occontrol.service.exe'}
        awcc_detected = []
        try:
            for p in psutil.process_iter(['name']):
                if p.info['name'] and p.info['name'].lower() in awcc_names:
                    awcc_detected.append(p.info['name'])
        except Exception:
            pass
        with self._data_lock:
            return {
                "monitoring": self.monitoring,
                "active_games": list(self.active_games.values()),
                "active_count": len(self.active_games),
                "lowered_count": len(self.lowered_processes),
                "ace_lowered_count": len(self.ace_lowered),  # v0.2.8
                "ace_enabled": bool(self.config.get("ACE降权开关", True)),  # v0.2.8
                "preset": self.config.get("active_preset", "default"),  # v0.2.8
                "is_admin": _is_admin(),  # v0.2.8
                "config": dict(self.config),
                "awcc_running": len(awcc_detected) > 0,
                "awcc_processes": list(set(awcc_detected)),
            }

    def get_effect_report(self) -> Dict[str, Any]:
        """v0.2.8: 优化生效报告——验证配置是否真正生效，而不是测帧数"""
        report: Dict[str, Any] = {
            "monitoring": self.monitoring,
            "preset": self.config.get("active_preset", "default"),
            "is_admin": _is_admin(),
            "ace_enabled": bool(self.config.get("ACE降权开关", True)),
        }
        # 活跃游戏的实际优先级验证
        games = []
        with self._data_lock:
            active = list(self.active_games.items())
            lowered = len(self.lowered_processes)
            ace_lowered = len(self.ace_lowered)
        target = self.config.get("游戏优先级", "high")
        for pid, info in active:
            try:
                proc = psutil.Process(pid)
                actual = PRIORITY_NAME.get(proc.nice(), "?")
                games.append({
                    "name": info.get("name", "?"),
                    "pid": pid,
                    "target": target,
                    "actual": actual,
                    "ok": actual == target,
                })
            except Exception:
                games.append({"name": info.get("name", "?"), "pid": pid, "target": target, "actual": "?", "ok": False})
        report["active_games"] = games
        report["lowered_count"] = lowered
        report["ace_lowered_count"] = ace_lowered
        # 保护名单当前命中（运行中的保护进程数）
        protected_running = 0
        protected_patterns = [e.lower() for e in self.config.get("保护进程", PROTECTED_PROCESSES)]
        try:
            for p in psutil.process_iter(['name']):
                if p.info['name'] and self._matches_name(p.info['name'].lower(), protected_patterns):
                    protected_running += 1
        except Exception:
            pass
        report["protected_running"] = protected_running
        report["summary"] = (
            f"{'监控中' if self.monitoring else '未监控'} · 预设「{report['preset']}」 · "
            f"活跃游戏 {len(games)} · 后台降权 {lowered} · ACE降权 {ace_lowered} · 保护进程 {protected_running}"
        )
        return report

    def get_logs(self) -> List[str]:
        """获取日志列表（线程安全）"""
        with self.log_lock:
            return list(self.logs)

    def _get_presets_dir(self) -> str:
        """获取预设目录路径"""
        return os.path.join(os.path.dirname(self.config_path), "presets")

    def list_presets(self) -> List[Dict[str, Any]]:
        """列出所有可用预设"""
        presets_dir = self._get_presets_dir()
        presets: List[Dict[str, Any]] = []
        if not os.path.exists(presets_dir):
            return presets
        try:
            for filename in os.listdir(presets_dir):
                if filename.endswith('.json'):
                    preset_path = os.path.join(presets_dir, filename)
                    try:
                        with open(preset_path, 'r', encoding='utf-8-sig') as f:
                            data = json.load(f)
                            presets.append({
                                "id": filename[:-5],
                                "name": data.get("name", filename[:-5]),
                                "description": data.get("description", ""),
                            })
                    except (json.JSONDecodeError, OSError):
                        continue
        except OSError:
            pass
        presets.sort(key=lambda x: x["name"])
        return presets

    def load_preset(self, preset_id: str) -> bool:
        """加载指定预设到当前配置"""
        preset_path = os.path.join(self._get_presets_dir(), f"{preset_id}.json")
        if not os.path.exists(preset_path):
            self.log(f"预设不存在: {preset_id}")
            return False
        try:
            with open(preset_path, 'r', encoding='utf-8-sig') as f:
                preset = json.load(f)
            # 只提取配置字段（排除元数据）
            config_keys = [
                "游戏进程列表", "游戏优先级", "游戏IO优先级", "游戏内存优先级",
                "降低后台进程优先级", "后台进程优先级", "后台IO优先级",
                "后台内存优先级", "排除进程", "监控间隔秒",
                # v0.2.8
                "ACE降权开关", "ACE进程列表", "ACE降权优先级",
                "保护进程", "黑名单进程", "记录降权明细",
            ]
            with self._data_lock:
                for key in config_keys:
                    if key in preset:
                        self.config[key] = preset[key]
                self.config["active_preset"] = preset_id
                self._invalidate_game_cache()
                self.save_config()
            self.log(f"已加载预设: {preset.get('name', preset_id)}")
            return True
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"加载预设失败: {e}")
            return False

    def save_preset(self, preset_id: str, name: str = "", description: str = "") -> bool:
        """将当前配置保存为预设"""
        if not preset_id or not re.match(r'^[\w\-]+$', preset_id):
            self.log(f"保存预设失败: ID 无效")
            return False
        presets_dir = self._get_presets_dir()
        os.makedirs(presets_dir, exist_ok=True)
        preset_path = os.path.join(presets_dir, f"{preset_id}.json")
        try:
            with self._data_lock:
                preset = {
                    "name": name or preset_id,
                    "description": description,
                }
                config_keys = [
                    "游戏进程列表", "游戏优先级", "游戏IO优先级", "游戏内存优先级",
                    "降低后台进程优先级", "后台进程优先级", "后台IO优先级",
                    "后台内存优先级", "排除进程", "监控间隔秒",
                    # v0.2.8
                    "ACE降权开关", "ACE进程列表", "ACE降权优先级",
                    "保护进程", "黑名单进程", "记录降权明细",
                ]
                for key in config_keys:
                    if key in self.config:
                        preset[key] = self.config[key]
            with open(preset_path, 'w', encoding='utf-8') as f:
                json.dump(preset, f, ensure_ascii=False, indent=2)
            self.log(f"已保存预设: {name or preset_id}")
            return True
        except OSError as e:
            self.log(f"保存预设失败: {e}")
            return False

    def delete_preset(self, preset_id: str) -> bool:
        """删除指定预设"""
        preset_path = os.path.join(self._get_presets_dir(), f"{preset_id}.json")
        if not os.path.exists(preset_path):
            return False
        try:
            os.remove(preset_path)
            self.log(f"已删除预设: {preset_id}")
            return True
        except OSError as e:
            self.log(f"删除预设失败: {e}")
            return False


# 全局引擎实例
engine: Optional[GameBoostEngine] = None


class WebHandler(BaseHTTPRequestHandler):
    """Web 请求处理器 - REST API + 静态页面"""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默 HTTP 访问日志

    def _send_json(self, data: Any, status: int = 200) -> None:
        """发送 JSON 响应"""
        try:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass  # 客户端已关闭连接（浏览器关闭/刷新），静默忽略

    def _send_html(self, html: str) -> None:
        """发送 HTML 响应"""
        try:
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass  # 客户端已关闭连接，静默忽略

    def _read_body(self) -> Dict[str, Any]:
        """读取并解析请求体 JSON"""
        content_length_raw = self.headers.get('Content-Length', '0')
        try:
            content_length = int(content_length_raw)
        except (ValueError, TypeError):
            return {}
        if content_length <= 0:
            return {}
        try:
            body = self.rfile.read(content_length).decode('utf-8')
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        """处理 GET 请求"""
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == '/' or path == '/index.html':
                self._send_html(INDEX_HTML)
            elif path == '/api/processes':
                self._send_json({"processes": engine.get_process_list() if engine else []})
            elif path == '/api/status':
                self._send_json(engine.get_status() if engine else {"monitoring": False})
            elif path == '/api/logs':
                self._send_json({"logs": engine.get_logs() if engine else []})
            elif path == '/api/config':
                self._send_json(engine.config if engine else {})
            elif path == '/api/presets':
                self._send_json({"presets": engine.list_presets() if engine else [], "active": engine.config.get("active_preset", "default") if engine else "default"})
            elif path == '/api/effect_report':  # v0.2.8
                self._send_json(engine.get_effect_report() if engine else {"error": "no engine"}, 200)
            elif path == '/api/export_logs':
                # 导出日志为 txt 文件（供朋友测试反馈）
                logs = engine.get_logs() if engine else []
                import platform
                import time
                txt_content = f"CalaNeko 日志导出\n"
                txt_content += f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                txt_content += f"系统: {platform.platform()}\n"
                txt_content += f"显卡: {_detect_gpu()}\n"  # v0.2.8
                txt_content += f"Python: {platform.python_version()}\n"
                txt_content += f"版本: {__version__}\n"
                txt_content += f"制作人: mmr（UID才不是10482803喵）\n"
                txt_content += f"预设: {engine.config.get('active_preset', 'default') if engine else '?'}\n"  # v0.2.8
                txt_content += f"管理员: {'是' if _is_admin() else '否'}\n"  # v0.2.8
                txt_content += f"日志条数: {len(logs)}\n"
                txt_content += f"{'='*60}\n\n"
                for log in logs:
                    txt_content += f"{log}\n"
                body = txt_content.encode('utf-8')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Content-Disposition', f'attachment; filename="calaneko_logs_{time.strftime("%Y%m%d_%H%M%S")}.txt"')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionAbortedError, BrokenPipeError, OSError):
                    pass
            elif path == '/api/export_config':
                # v0.4.0 导出配置为 JSON 文件（分享给朋友）
                import time
                export_data = {
                    "version": __version__,
                    "export_time": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "config": engine.config if engine else {},
                    "active_preset": engine.config.get("active_preset", "default") if engine else "default",
                }
                body = json.dumps(export_data, ensure_ascii=False, indent=2).encode('utf-8')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Disposition', f'attachment; filename="calaneko_config_{time.strftime("%Y%m%d_%H%M%S")}.json"')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionAbortedError, BrokenPipeError, OSError):
                    pass
            elif path == '/api/check_update':
                # v0.4.0 检查更新（GitHub Release API）
                self._send_json(_check_for_update())
            elif path == '/api/autostart/status':
                # v0.4.0 检查开机自启状态
                self._send_json(_get_autostart_status())
            else:
                self._send_json({"error": "Not found"}, 404)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass  # 客户端已关闭连接，静默忽略

    def do_POST(self) -> None:
        """处理 POST 请求"""
        if engine is None:
            self._send_json({"error": "Engine not initialized"}, 500)
            return

        parsed = urlparse(self.path)
        path = parsed.path
        data = self._read_body()

        def _parse_pid(val: Any) -> Optional[int]:
            """兼容 int 和字符串数字的 pid 解析"""
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.strip().isdigit():
                return int(val.strip())
            return None

        if path == '/api/set_priority':
            pid = _parse_pid(data.get('pid'))
            priority = data.get('priority')
            if pid is not None and isinstance(priority, str):
                result = engine.set_priority(pid, priority)
                self._send_json({"success": result})
            else:
                self._send_json({"success": False, "error": "Invalid parameters"}, 400)
        elif path == '/api/set_io':
            pid = _parse_pid(data.get('pid'))
            io_priority = data.get('io_priority')
            if pid is not None and isinstance(io_priority, str):
                result = engine.set_io_priority(pid, io_priority)
                self._send_json({"success": result})
            else:
                self._send_json({"success": False, "error": "Invalid parameters"}, 400)
        elif path == '/api/set_memory':
            pid = _parse_pid(data.get('pid'))
            mem_priority = data.get('mem_priority')
            if pid is not None and isinstance(mem_priority, str):
                result = engine.set_memory_priority(pid, mem_priority)
                self._send_json({"success": result})
            else:
                self._send_json({"success": False, "error": "Invalid parameters"}, 400)
        elif path == '/api/add_game':
            name = data.get('name')
            if isinstance(name, str) and name.strip():
                result = engine.add_to_game_list(name)
                self._send_json({"success": result})
            else:
                self._send_json({"success": False, "error": "Invalid name"}, 400)
        elif path == '/api/start_monitor':
            engine.start_monitor()
            self._send_json({"success": True})
        elif path == '/api/stop_monitor':
            engine.stop_monitor()
            self._send_json({"success": True})
        elif path == '/api/restore_all':
            engine.restore_all()
            self._send_json({"success": True})
        elif path == '/api/shutdown':
            # 安全校验：仅允许本机页面触发（防本地恶意网页 CSRF 关停）
            origin = self.headers.get('Origin') or self.headers.get('Referer') or ''
            if not origin.startswith(('http://127.0.0.1', 'http://localhost')):
                logger.warning("拒绝来自 %s 的退出请求（非本机来源）", origin[:80] or "(无来源)")
                self._send_json({"ok": False, "message": "拒绝非本机来源的退出请求"}, 403)
                return
            # 防重入：ThreadingHTTPServer 下并发重复退出会并发执行 restore_all（集合竞态）
            if getattr(engine, '_shutting_down', False):
                self._send_json({"ok": True, "message": "正在退出"})
                return
            engine._shutting_down = True
            # 优雅退出：先恢复所有被修改的进程优先级，再停止服务
            try:
                engine.stop_monitor()
                engine.restore_all()
                engine.log("收到退出请求，已停止监控并恢复所有进程优先级")
            except Exception as e:
                engine.log(f"退出前清理异常: {e}")
            self._send_json({"ok": True, "message": "正在退出，请关闭此页面"})

            def _do_exit():
                time.sleep(0.8)  # 等响应发送完
                # server_close() 立即关闭监听 socket（shutdown() 有等待语义，
                # 可能被轮询请求拖住；os._exit 前 flush 防止缓冲日志丢失）
                try:
                    self.server.server_close()
                except Exception:
                    pass
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    logging.shutdown()  # 刷掉 logging 缓冲
                except Exception:
                    pass
                os._exit(0)

            threading.Thread(target=_do_exit, daemon=True).start()
        elif path == '/api/save_config':
            if isinstance(data, dict):
                # 类型校验：防止错误类型导致监控异常（游戏列表/排除进程/黑名单/保护/ACE列表必须是列表，间隔必须是数字）
                for key in ("游戏进程列表", "排除进程", "黑名单进程", "保护进程", "ACE进程列表"):
                    if key in data and not isinstance(data[key], list):
                        self._send_json({"success": False, "error": f"{key} 必须是列表"}, 400)
                        return
                if "监控间隔秒" in data and not isinstance(data["监控间隔秒"], (int, float)):
                    self._send_json({"success": False, "error": "监控间隔秒 必须是数字"}, 400)
                    return
                with engine._data_lock:
                    engine.config.update(data)
                    engine._invalidate_game_cache()
                    engine.save_config()
                self._send_json({"success": True, "config": engine.config})
            else:
                self._send_json({"success": False, "error": "Invalid config"}, 400)
        elif path == '/api/load_preset':
            preset_id = data.get('preset_id')
            if isinstance(preset_id, str) and preset_id:
                result = engine.load_preset(preset_id)
                self._send_json({"success": result, "config": engine.config})
            else:
                self._send_json({"success": False, "error": "Invalid preset_id"}, 400)
        elif path == '/api/save_preset':
            preset_id = data.get('preset_id')
            name = data.get('name', '')
            description = data.get('description', '')
            if isinstance(preset_id, str) and preset_id:
                result = engine.save_preset(preset_id, name, description)
                self._send_json({"success": result})
            else:
                self._send_json({"success": False, "error": "Invalid preset_id"}, 400)
        elif path == '/api/delete_preset':
            preset_id = data.get('preset_id')
            if isinstance(preset_id, str) and preset_id:
                result = engine.delete_preset(preset_id)
                self._send_json({"success": result})
            else:
                self._send_json({"success": False, "error": "Invalid preset_id"}, 400)
        elif path == '/api/import_config':
            # v0.4.0 导入配置 JSON（从朋友分享的配置文件导入）
            import_config = data.get('config')
            if not isinstance(import_config, dict):
                self._send_json({"success": False, "error": "配置格式错误，需要 config 对象"}, 400)
                return
            # 类型校验
            for key in ("游戏进程列表", "排除进程", "黑名单进程", "保护进程", "ACE进程列表"):
                if key in import_config and not isinstance(import_config[key], list):
                    self._send_json({"success": False, "error": f"{key} 必须是列表"}, 400)
                    return
            if "监控间隔秒" in import_config and not isinstance(import_config["监控间隔秒"], (int, float)):
                self._send_json({"success": False, "error": "监控间隔秒 必须是数字"}, 400)
                return
            with engine._data_lock:
                engine.config.update(import_config)
                engine._invalidate_game_cache()
                engine.save_config()
            engine.log(f"配置已从外部文件导入（来源版本: {data.get('version', '未知')}）")
            self._send_json({"success": True, "config": engine.config, "message": "配置导入成功"})
        elif path == '/api/autostart/enable':
            # v0.4.0 启用开机自启（写 HKCU 注册表，不需要管理员权限）
            result = _set_autostart(True)
            self._send_json(result)
        elif path == '/api/autostart/disable':
            # v0.4.0 禁用开机自启
            result = _set_autostart(False)
            self._send_json(result)
        else:
            self._send_json({"error": "Not found"}, 404)


INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CalaNeko - 游戏进程优化工具</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAALeUlEQVR4nG2XaZBc1XXHf2/p13tP9/T07JtmNDPSaLRiBmShzQgvLLJxQhKVsUt8iD/EJjEmJiQ4/hA7iQsnmKRMVYCynbLAYCzAEkYKASQEGFGWFM0gaYRGo9m3nunp6X19S+rdbplSKu/Le3Xvq3vO+Z///5xzpezQhFU8PoycyiOrCmZZx7IsJEnCfixAcaji2yyVobp+/bH/VdTKvqHriN3/888Nj71lWpgeJ+rtG5Dif/NLyyHL6KUSxVQWV10QRXNg6ro4SEYitxQXjngbwxiGYVsVh9jGVU0jm0phGSa+UE1lS68EUTEoVfyxKsFwPTjDwpAsVFVVycxHmTXTeAZ7yf7XeZprG3D5PML41fFraHv7ySfSREbGCDU3gSIhOR04NI2l8WmWp+dQXQ5hoamnE18whOJUhVM2apZpgSyhKAqSKgtHDNlANi3UxdkZko1O3P2b6P/aXZzL5UiHQiRjcRRTQupYz8B3vkLZNLj8w0OUNJX01XnWSg0kVpMsTc7Q1NctoM0lU8x9PIbs0HBoDpSQH0tTQFWQLFDtdJVNXKqGx+/H4XEhXfyTH1iRf/yayL8v4CWbTKP6vCgOBV8wgAmUkllkRUb2ubGzfeX5N6h9Z4LZqWlqGuqQFQXTNAUXJEUWXEhNzrNildj8wwdxBfwCCaNYIL+aIj8bpTQ6iyOeRRo/8LilfX0fLXu2ohdKqC6N3//Ds2x4YD/J2SgXDr1C1+f24mupZ/yVE5Rml3CulnBkdBwuJ8GmegG1JMtYZR2zUMJSZDybuyi3B8nEUvTcuRe9UKwEoSrgUNBLZTILy6i2wfGjJzE1mdZbNjL89CvMHnmPbQ9/lfzwKGvu3MuaOz6NqqmEN3Yxd/oSV7/xJO3revFH6kR+y4UcermIXOfHs2M93tvW4+lvQ1UUPnryV5SSGWSPhmlzQjexCkVBzEBzPWqhXKLtrt2EezsEc7vv2SnIVUykCW/qYeLkadILMUKdTZx74gUcZ8fp2bhRRDw7MkoukaDji3up++w23AOdOEPe6/oUzilOZwUhySafrSxZfNucKWfyNi8sSqkcDp+7omXDoBhLiAPcNT7CHS04gz6uHDqO+t+XqGtuQmkNk5XKjLx4GEMx6f/FowS39FYM24yv6l03dPRcCcWrYRk6kqwImWLZzLLToaI2trVx8d+PEOzvpL6vk+TEAuO/fINNf/mnTL1zFt0oMvbySbw1fjY89SAEPDgjNShOB40H9yF73QTaGqo8kITcLMMQxhKjM2iyA4fXTTmTQy8VURwaskPBKBniX9XMFfBu7ibc2y4Oad+1lcxjBwUS5mqG2TdP039gP437ttxY0SyL4LrOTwqcIldgt40rCuV8kUs/OUzfXXdg2OQU4JhEL40Ram/CVx+klM0hz8/NErx3O4okYxoGH/3naywceRfN4yL6wUdoBZnGXVsw8qUKvDaG4mUJ6f3BH/vbrnqKwuq1GYZ+9Dy17TYnAqCbOJ1O3KEALZt7WbpwlcS1OVzBAKosCCGTWkngD9cQffYYwcH14lDdhFsf+zpmLIMUcQvIRJpNC1mWWbk6jZ7N07ilT5Ayuxxn6rXfUZxZpfv2XYI7F198ndD6DpyKE0lV8TaEqVvbxtiRtxn/jY7a1NHJ0OMvEdu7gZsevh/L56b7wftIxVaJbOjF2VqPXcYsR7XBWFDM5Vk6f4XEyDhtu7eJ5dGXT5IanqRx4zoiX7kNs1wWTGzYuo7g9l4U08LQDWZPDXHpBz/lU//yVyTH51EVn4uumggrqQJlQwfFYvbY++hFnbW7byE3t8jEb0/Q8xf3onndAnq3z8Pymct03b2T2t42Lj13HGm5yKavfgnLZn8uLxxVXBpOt4dCNEHj1h6uHj7B8CM/prAcx10fpP3ztyIbxRI1Hc1o00lGvvUUgYJC+sMr9H3+MwQ2dXH5Z0d485FvkRidrsgUi4vPHaO2r0MY//jwCdQs9N53B6VcjnI2X2nHkoSpGyLvsYvjnPrmjzh130OYy1kUy+DtO79NPp5EFe1RVehq7cAs6YxvdrL+0YMopomVNfC3N9Nx0xfwdTYLBxRJYu3dO3EF/SQm5tAnV+j98mcpxBOCB9cnARsJuwE5PG4mf3qUxVPv49VqKJVyeLu76Pv7B4hNLyClv/uStboQZTEbR11IIu8cYPMjBzFjq8gOB8gyZp0bWZNvUKFhWVz5+TFaNwygBtxYunGDYbvnlwpFKJSYu3CRUj7HuYe/T+3grQw++xitm3pYmZhDTcwssNCo0f/Et5l45QRWtkxmfB5fSwRyeSwFJJuAdgWzXzZiILqdx+XH3RCiuJoSdeB6V7RRtTujnsmRX0nSODhA7U09pBZjtHxpjzCenFsm9vEkcrCpAXlskfxijI0H99O2fwfnn3kJvVjCsuu3S63mtBp6Ve8zx06Tnl0kt5wQZdyeokrFonjbqNmMd4dqWPrwI3LRFYHK9n/6Ji6Pm+mzI8SnF3H4PcjJ+CpyYw0OpybOt6Ofe+9D5s9dQPK4wK1+klfTFIUmvRgjPxcj+Ok+YrOTzJweIhNdZXVsjrn3h0RRsyFT7KZWLOJqt6UMmizTNNCNJxTAW+un9ZYB5KXZGWr++Db8Hc3k4ynqtq6jqaGZlVffx3IqoCmiRAvjdmSGzsThd1iz41bRNTvu3UU5oHLm6V9gBRTyZpaZM8OoDo1SMk37l3cSGeiqVFBJQlEV6rpbRd/RHA7ktQP95A+9y6l7HuHqoeO4Ax48nc3ExmcYO/k7UfHs/NrGU3NRLv7HUerX9uBurCMfTxOfnGfsiedY+PXrjP3sVQb+/IuEtveyMHJV8EVWqgOp3Seqk6n4rnZN1XI4WFPXTG52icZ9N4vFYq2Tzc88TDmR5sLPj+KuDVBO5TGTRdq2bsYTCYoD8mejXPjeU6ycGcLlrGXi14d564DKvhe+TyGWZPyd39N1/2eq/LGn40oyJXuhmldZsiRGx0dp+Lv7qN/QJRY9LfXU2rovlJl56wzmSpGW7j7W7LyFsj3NILF6+Rrn//pfiZ35H9zOMMViEl9LL+E9W4nNRKnb0kvg5i4CbfWVLmlBdGyWQq54g5zlXDSG67Z+OnZ/itj4XIVshiHGqFB/J1u+cz/uSC3OcA04ZLLzUYrZLOOvvk147zbqbh4kU1yk5qYB9hz/N/oeuIfVsRk0j5PufYOC/aIqlnVbHKhqtZ5U7w2q6nVhLqUZ/vEL6PkidX97kFIqKyCytaw6VHLpNJmFFXyNtdT2drBy8SqbvnGAtFQQbP7ge09x8+MPUdMQJnZlstqyKxcXmzvCkNNBpKu1qiZ7XRIzguwM+PFMrxL9yW+o3VCd76eiaAEf8+8NMfzE87gj9nSbIJ/O4grWiKlm5coUC2+fQ/V72PX0dzHSOaY/GKaQyhJZv6ZC3v/nGkfVuC3PsSdfRtXTWVZ8sOW3/0xkfRfZ1TS5yUWRhjX7d9J89w4Wj3yIOxQkfm2K9h3bUL1eYmOX6fvCHuZfP4uzPYS/uwVvpAZ3OEiwIVytnDc6IBySIDk5T/SFdwm3tKJqbjfOhIkS8BEfnWbimdfwSg4KpSKxK1NMvfQm4fY1tG7fRuatZWIjE4S6m/G11KHVeOjcPUhycoHM+SnsqSwuL+G/c1Do/YbogUIqw/LQGPE3hli7cxDyBaTkoy9a+USS5VScQraA1+kC+/pl5JCKJlYsSetDB8Qhq1emCPX3YBgl3HUhjGxOcER2ahilMsmFJQKbO6jr7/zklmxZlMsG0csTgrzW0SF8jQ2UfBohvw8p9d5lyzg5glY0MCVIZ9IU84XKxdHpJLYcpRT2oWXKGG0Rev/odlILy2QW43jra9EzWcr5PMlYjDV/tpdQW+Mfqp4gIhKzw6P4G8JiCM2+NUKkqYlVpUh4y1r+F2SpU+ZgDDEwAAAAAElFTkSuQmCC">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: #eee; min-height: 100vh; }
.header { background: linear-gradient(135deg, #16213e, #0f3460); padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 10px rgba(0,0,0,0.3); }
.header h1 { font-size: 20px; color: #e94560; }
.header .status { display: flex; gap: 12px; align-items: center; }
.status-badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold; }
.status-on { background: #00b894; color: #fff; }
.status-off { background: #636e72; color: #fff; }
.btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s; }
.btn-primary { background: #e94560; color: #fff; }
.btn-primary:hover { background: #d63850; }
.btn-secondary { background: #0f3460; color: #fff; }
.btn-secondary:hover { background: #1a4a7a; }
.btn-success { background: #00b894; color: #fff; }
.btn-success:hover { background: #00a381; }
.btn-danger { background: #d63031; color: #fff; }
.btn-danger:hover { background: #b33939; }
.container { display: grid; grid-template-columns: 1fr 350px; gap: 16px; padding: 16px 24px; }
.panel { background: #16213e; border-radius: 10px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
.panel h2 { font-size: 15px; color: #e94560; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #0f3460; }
.process-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.process-table th { background: #0f3460; padding: 8px 6px; text-align: left; color: #74b9ff; position: sticky; top: 0; }
.process-table td { padding: 6px; border-bottom: 1px solid #1a1a2e; }
.process-table tr:hover { background: #1a2744; }
.process-table tr.game { background: rgba(255, 193, 7, 0.1); }
.process-table tr.active { background: rgba(0, 184, 148, 0.15); }
.pid { color: #74b9ff; font-family: monospace; }
.priority-high { color: #e94560; font-weight: bold; }
.priority-normal { color: #dfe6e9; }
.priority-low { color: #fdcb6e; }
.cpu-bar { height: 6px; background: #1a1a2e; border-radius: 3px; overflow: hidden; }
.cpu-fill { height: 100%; background: linear-gradient(90deg, #00b894, #fdcb6e, #e94560); transition: width 0.3s; }
.right-panel { display: flex; flex-direction: column; gap: 16px; }
.info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 13px; }
.info-item { background: #1a1a2e; padding: 8px 10px; border-radius: 6px; }
.info-item .label { color: #636e72; font-size: 11px; }
.info-item .value { color: #dfe6e9; font-weight: bold; font-size: 14px; }
.game-list { max-height: 120px; overflow-y: auto; }
.game-item { padding: 4px 8px; background: #1a1a2e; border-radius: 4px; margin-bottom: 4px; font-size: 12px; display: flex; justify-content: space-between; }
.log-panel { flex: 1; min-height: 200px; }
.log-content { background: #0d1117; border-radius: 6px; padding: 10px; font-family: 'Consolas', monospace; font-size: 11px; height: 250px; overflow-y: auto; line-height: 1.6; }
.log-content .log-info { color: #74b9ff; }
.log-content .log-warn { color: #fdcb6e; }
.log-content .log-error { color: #e94560; }
.context-menu { position: fixed; background: #16213e; border: 1px solid #0f3460; border-radius: 8px; padding: 4px; display: none; z-index: 1000; min-width: 180px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }
.context-menu .menu-item { padding: 8px 12px; cursor: pointer; border-radius: 4px; font-size: 13px; }
.context-menu .menu-item:hover { background: #0f3460; }
.context-menu .menu-sep { height: 1px; background: #0f3460; margin: 4px 0; }
.modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); display: none; justify-content: center; align-items: center; z-index: 2000; }
.modal-content { background: #16213e; border-radius: 12px; padding: 24px; width: 500px; max-height: 80vh; overflow-y: auto; }
.modal-content h3 { color: #e94560; margin-bottom: 16px; }
.form-group { margin-bottom: 12px; }
.form-group label { display: block; color: #74b9ff; font-size: 12px; margin-bottom: 4px; }
.form-group input, .form-group select, .form-group textarea { width: 100%; padding: 8px; background: #1a1a2e; border: 1px solid #0f3460; border-radius: 6px; color: #eee; font-size: 13px; }
.form-group textarea { height: 120px; font-family: monospace; }
.modal-buttons { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.table-container { max-height: 600px; overflow-y: auto; border-radius: 8px; }
</style>
</head>
<body>

<div class="header">
    <h1>🐱 CalaNeko</h1>
    <div class="status">
        <span style="font-size:12px;color:#74b9ff;">预设:</span>
        <select id="presetSelect" onchange="loadPreset(this.value)" style="background:#1a1a2e;color:#eee;border:1px solid #0f3460;border-radius:4px;padding:4px 8px;font-size:12px;max-width:220px;">
            <option value="">加载中...</option>
        </select>
        <button class="btn btn-secondary" onclick="openConfig()">⚙ 配置</button>
        <button class="btn btn-secondary" onclick="saveCurrentAsPreset()" title="保存当前配置为新预设">💾 存为预设</button>
        <button class="btn btn-secondary" onclick="restoreAll()">↩ 恢复全部</button>
        <button class="btn btn-secondary" onclick="exportLogs()" title="导出日志为txt文件，发给开发者反馈">📥 导出日志</button>
        <button class="btn btn-secondary" onclick="exportConfig()" title="导出配置为JSON文件，分享给朋友">📤 导出配置</button>
        <button class="btn btn-secondary" onclick="document.getElementById('importConfigFile').click()" title="从JSON文件导入配置">📂 导入配置</button>
        <input type="file" id="importConfigFile" accept=".json" style="display:none" onchange="importConfig(event)">
        <button class="btn btn-primary" id="monitorBtn" onclick="toggleMonitor()">🎮 点击准备</button>
        <button class="btn btn-danger" onclick="shutdownApp()" title="退出 CalaNeko（自动恢复所有被优化的进程优先级）">⏻ 退出</button>
    </div>
</div>

<div class="container">
    <div class="panel">
        <h2>进程列表（右键操作）</h2>
        <div class="table-container">
            <table class="process-table">
                <thead>
                    <tr><th>PID</th><th>进程名</th><th>优先级</th><th>CPU%</th><th>内存MB</th></tr>
                </thead>
                <tbody id="processBody"></tbody>
            </table>
        </div>
    </div>

    <div class="right-panel">
        <div class="panel">
            <h2>📊 状态</h2>
            <div id="nekoHint" style="display:none;background:#fff5f5;border-left:4px solid #fd79a8;padding:10px 15px;margin-bottom:15px;border-radius:4px;font-size:14px;color:#e84393;cursor:pointer;" onclick="document.getElementById('monitorBtn').click()">
                🐱没有点击准备是笨蛋猫娘喵～点击即可开始优化把分还给糖猫喵！
            </div>
            <!-- v0.3.0: 非管理员提示条（仅WebUI提示，不弹系统窗） -->
            <div id="adminHint" style="display:none;background:#fff8e1;border-left:4px solid #fdcb6e;padding:10px 15px;margin-bottom:15px;border-radius:4px;font-size:13px;color:#e17055;">
                ⚠️ 当前未获得管理员权限，部分系统进程可能无法优化。建议关闭后右键「以管理员身份运行」以获得完整优化能力。当前模式下仍可优化当前用户会话的进程。
            </div>
            <div class="info-grid">
                <div class="info-item"><div class="label">监控状态</div><div class="value" id="statusMonitor">未监控</div></div>
                <div class="info-item"><div class="label">活跃游戏</div><div class="value" id="statusActive">0</div></div>
                <div class="info-item"><div class="label">后台降权</div><div class="value" id="statusLowered">0</div></div>
                <div class="info-item"><div class="label">ACE降权</div><div class="value" id="statusAce">0</div></div>
                <div class="info-item"><div class="label">游戏数量</div><div class="value" id="statusGames">0</div></div>
                <div class="info-item"><div class="label">管理员</div><div class="value" id="statusAdmin">-</div></div>
                <div class="info-item" id="awccItem" style="display:none;"><div class="label">AWCC 状态</div><div class="value" id="statusAwcc">未运行</div></div>
            </div>
            <div id="effectSummary" style="margin-top:10px;background:#1a1a2e;border:1px solid #0f3460;border-radius:6px;padding:8px 10px;font-size:12px;color:#74b9ff;"></div>
        </div>

        <div class="panel">
            <h2>🎮 活跃游戏</h2>
            <div class="game-list" id="activeGames"><div style="color:#636e72;font-size:12px;">无活跃游戏</div></div>
        </div>

        <div class="panel log-panel">
            <h2>📋 日志</h2>
            <div class="log-content" id="logContent"></div>
        </div>
    </div>
</div>

<div class="context-menu" id="contextMenu">
    <div class="menu-item" onclick="setPriority('high')">🔴 CPU → 高</div>
    <div class="menu-item" onclick="setPriority('above_normal')">🟠 CPU → 高于正常</div>
    <div class="menu-item" onclick="setPriority('normal')">⚪ CPU → 正常</div>
    <div class="menu-item" onclick="setPriority('below_normal')">🟡 CPU → 低于正常</div>
    <div class="menu-sep"></div>
    <div class="menu-item" onclick="setIo('low')">💾 IO → Low</div>
    <div class="menu-item" onclick="setIo('normal')">💾 IO → Normal</div>
    <div class="menu-sep"></div>
    <div class="menu-item" onclick="setMemory('low')">🧠 内存 → Low</div>
    <div class="menu-item" onclick="setMemory('normal')">🧠 内存 → Normal</div>
    <div class="menu-sep"></div>
    <div class="menu-item" onclick="addGame()">⭐ 添加到游戏列表</div>
</div>

<div class="modal" id="configModal">
    <div class="modal-content">
        <h3>⚙ 配置</h3>
        <div class="form-group">
            <label>游戏 CPU 优先级</label>
            <select id="cfgGamePrio">
                <option value="high">高</option>
                <option value="above_normal">高于正常</option>
                <option value="normal">正常</option>
            </select>
        </div>
        <div class="form-group">
            <label>游戏 IO 优先级</label>
            <select id="cfgGameIo">
                <option value="normal">Normal</option>
                <option value="low">Low</option>
                <option value="high">High</option>
            </select>
        </div>
        <div class="form-group">
            <label>游戏内存优先级</label>
            <select id="cfgGameMem">
                <option value="normal">Normal</option>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
            </select>
        </div>
        <div class="form-group">
            <label><input type="checkbox" id="cfgLowerBg"> 降低后台进程优先级</label>
        </div>
        <div class="form-group">
            <label>后台进程优先级</label>
            <select id="cfgBgPrio">
                <option value="below_normal">低于正常</option>
                <option value="idle">低</option>
                <option value="normal">正常</option>
            </select>
        </div>
        <div class="form-group">
            <label>游戏进程列表（每行一个）</label>
            <textarea id="cfgGameList"></textarea>
        </div>
        <div class="form-group">
            <label><input type="checkbox" id="cfgAceSwitch"> ACE 反作弊降权（腾讯系：卡丘/三角洲/瓦国服）</label>
        </div>
        <div class="form-group">
            <label><input type="checkbox" id="cfgAutostart" onchange="toggleAutostart(this.checked)"> 开机自启（登录 Windows 后自动启动 CalaNeko）</label>
        </div>
        <div class="form-group">
            <label><input type="checkbox" id="cfgNotify"> 桌面通知（游戏启动/退出时弹气泡提示）</label>
        </div>
        <div class="form-group">
            <label><input type="checkbox" id="cfgHotkey"> 全局快捷键（Ctrl+Alt+G 快速切换监控状态）</label>
        </div>
        <div class="form-group">
            <label><input type="checkbox" id="cfgVerbose"> 记录降权明细（日志更详细）</label>
        </div>
        <div class="form-group">
            <label>黑名单进程（每行一个，强制压到低优先级，永不跳过）</label>
            <textarea id="cfgBlacklist" style="height:60px;" placeholder="例如：某下载器/后台软件进程名"></textarea>
        </div>
        <div class="form-group">
            <label>保护进程（每行一个，永不降权。内置显卡/外设/硬件调控名单，可追加）</label>
            <textarea id="cfgProtected" style="height:80px;"></textarea>
        </div>
        <div class="modal-buttons">
            <button class="btn btn-secondary" onclick="closeConfig()">取消</button>
            <button class="btn btn-primary" onclick="saveConfig()">保存</button>
        </div>
    </div>
</div>

<script>
let currentPid = null;
let currentName = null;

async function api(url, method='GET', data=null) {
    const opts = { method, headers: {'Content-Type': 'application/json'} };
    if (data) opts.body = JSON.stringify(data);
    const res = await fetch(url, opts);
    return res.json();
}

async function refreshProcesses() {
    const data = await api('/api/processes');
    const tbody = document.getElementById('processBody');
    // HTML 转义函数，防止 XSS
    const escapeHtml = (str) => {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    };
    tbody.innerHTML = data.processes.map(p => {
        const prioClass = p.priority === 'high' || p.priority === 'realtime' ? 'priority-high' :
                          p.priority === 'below_normal' || p.priority === 'idle' ? 'priority-low' : 'priority-normal';
        const rowClass = p.is_active ? 'active' : (p.is_game ? 'game' : '');
        // v0.4.0: 进程图标（活跃游戏显示图标）
        const iconHtml = p.icon ? `<img src="data:image/png;base64,${p.icon}" style="width:16px;height:16px;vertical-align:middle;margin-right:4px;">` : '';
        return `<tr class="${rowClass}" data-pid="${p.pid}" data-name="${escapeHtml(p.name)}">
            <td class="pid">${p.pid}</td>
            <td>${iconHtml}${escapeHtml(p.name)}</td>
            <td class="${prioClass}">${escapeHtml(p.priority_label)}</td>
            <td><div class="cpu-bar"><div class="cpu-fill" style="width:${Math.min(p.cpu, 100)}%"></div></div> ${p.cpu}</td>
            <td>${p.memory}</td>
        </tr>`;
    }).join('');
}

async function refreshStatus() {
    const data = await api('/api/status');
    document.getElementById('monitorBtn').textContent = data.monitoring ? '⏹ 停止优化' : '🎮 点击准备';
    document.getElementById('statusMonitor').textContent = data.monitoring ? '监控中' : '未监控';
    document.getElementById('statusActive').textContent = data.active_count;
    document.getElementById('statusLowered').textContent = data.lowered_count;
    document.getElementById('statusAce').textContent = data.ace_lowered_count ?? 0;
    document.getElementById('statusAdmin').textContent = data.is_admin === true ? '是' : '否⚠';
    // v0.3.0: 非管理员提示条（仅WebUI显示，不弹系统窗，严格类型检查）
    document.getElementById('adminHint').style.display = data.is_admin === true ? 'none' : 'block';
    document.getElementById('statusGames').textContent = data.config['游戏进程列表']?.length || 0;

    // v0.2.8: 生效报告摘要
    try {
        const rep = await api('/api/effect_report');
        document.getElementById('effectSummary').textContent = rep.summary || '';
    } catch (e) {}

    // AWCC 状态（仅检测到时显示）
    const awccItem = document.getElementById('awccItem');
    const awccEl = document.getElementById('statusAwcc');
    if (data.awcc_running) {
        awccItem.style.display = '';
        awccEl.textContent = '运行中（互补）';
        awccEl.style.color = '#0984e3';
        awccEl.title = 'AWCC 负责硬件层优化，CalaNeko 负责进程层优化，两者互补: ' + (data.awcc_processes || []).join(', ');
    } else {
        awccItem.style.display = 'none';
    }

    // 猫娘提醒：未开始监测时显示
    const nekoHint = document.getElementById('nekoHint');
    if (!data.monitoring) {
        nekoHint.style.display = '';
    } else {
        nekoHint.style.display = 'none';
    }

    const ag = document.getElementById('activeGames');
    if (data.active_games.length === 0) {
        ag.innerHTML = '<div style="color:#636e72;font-size:12px;">无活跃游戏</div>';
    } else {
        ag.innerHTML = data.active_games.map(g =>
            `<div class="game-item"><span>🎮 ${g.name}</span><span style="color:#00b894;">运行中</span></div>`
        ).join('');
    }
}

async function refreshLogs() {
    const data = await api('/api/logs');
    const logEl = document.getElementById('logContent');
    logEl.innerHTML = data.logs.slice(-50).map(l =>
        `<div class="log-info">${l}</div>`
    ).join('');
    logEl.scrollTop = logEl.scrollHeight;
}

function showContext(e, pid, name) {
    e.preventDefault();
    currentPid = pid;
    currentName = name;
    const menu = document.getElementById('contextMenu');
    menu.style.display = 'block';
    menu.style.left = e.clientX + 'px';
    menu.style.top = e.clientY + 'px';
}

// 右键菜单事件委托（避免在 HTML 内联拼接进程名导致 JS 注入）
document.addEventListener('contextmenu', (e) => {
    const tr = e.target.closest('tr[data-pid]');
    if (!tr) return;
    showContext(e, tr.dataset.pid, tr.dataset.name);
});

document.addEventListener('click', () => {
    document.getElementById('contextMenu').style.display = 'none';
});

async function setPriority(prio) {
    await api('/api/set_priority', 'POST', {pid: currentPid, priority: prio});
    refreshProcesses();
}

async function setIo(io) {
    await api('/api/set_io', 'POST', {pid: currentPid, io_priority: io});
}

async function setMemory(mem) {
    await api('/api/set_memory', 'POST', {pid: currentPid, mem_priority: mem});
}

async function addGame() {
    await api('/api/add_game', 'POST', {name: currentName});
    refreshStatus();
}

async function toggleMonitor() {
    const data = await api('/api/status');
    const nekoHint = document.getElementById('nekoHint');
    if (data.monitoring) {
        await api('/api/stop_monitor', 'POST');
    } else {
        // 点击准备：先显示"哈！"再消失
        nekoHint.innerHTML = '🐱哈！';
        nekoHint.style.display = '';
        await api('/api/start_monitor', 'POST');
        setTimeout(() => { nekoHint.style.display = 'none'; }, 600);
    }
    refreshStatus();
}

async function restoreAll() {
    await api('/api/restore_all', 'POST');
    refreshProcesses();
    refreshStatus();
}

function exportLogs() {
    // 导出日志为 txt 文件下载（供朋友测试反馈）
    window.open('/api/export_logs', '_blank');
}

function exportConfig() {
    // v0.4.0 导出配置为 JSON 文件下载（分享给朋友）
    window.open('/api/export_config', '_blank');
}

function importConfig(event) {
    // v0.4.0 从 JSON 文件导入配置
    const file = event.target.files[0];
    if (!file) return;
    if (!confirm('确定导入此配置文件？当前配置将被覆盖。')) {
        event.target.value = '';
        return;
    }
    const reader = new FileReader();
    reader.onload = function(e) {
        try {
            const data = JSON.parse(e.target.result);
            fetch('/api/import_config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            })
            .then(r => r.json())
            .then(result => {
                if (result.success) {
                    alert('配置导入成功！');
                    refreshStatus();
                    loadPresets();
                } else {
                    alert('配置导入失败: ' + (result.error || '未知错误'));
                }
            })
            .catch(err => alert('导入请求失败: ' + err));
        } catch (err) {
            alert('文件解析失败，请确认是有效的 JSON 配置文件: ' + err);
        }
    };
    reader.readAsText(file, 'utf-8');
    event.target.value = '';
}

let isShuttingDown = false;
function shutdownApp() {
    // 优雅退出：先恢复所有被优化的进程优先级
    if (isShuttingDown) return;  // 防重复提交（并发双请求会让后端并发 restore_all）
    isShuttingDown = true;
    if (!confirm('确定退出 CalaNeko？将自动恢复所有被优化的进程优先级。')) { isShuttingDown = false; return; }
    fetch('/api/shutdown', { method: 'POST', signal: AbortSignal.timeout(8000) })
        .then(r => {
            if (!r.ok) { alert('退出失败：' + r.status); return; }
            // 退出成功：显示退出页并尝试自动关闭标签页
            document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;background:#1a1a2e;color:#eee;font-size:16px;">✅ CalaNeko 似了喵（已退出），进程优先级已恢复<br><span style="color:#636e72;font-size:13px;">此页面 1 秒后自动关闭，也可手动关闭</span></div>';
            setTimeout(() => { try { window.close(); } catch (e) {} }, 1000);
        })
        .catch(() => { document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;background:#1a1a2e;color:#eee;">✅ CalaNeko 似了喵（已退出），此页面可以关闭</div>'; setTimeout(() => { try { window.close(); } catch (e) {} }, 1000); });
}

// window.close() 仅对 window.open() 打开的标签页有效；手动打开的标签页会静默忽略，退出页是最终兜底
function openConfig() {
    api('/api/config').then(cfg => {
        document.getElementById('cfgGamePrio').value = cfg['游戏优先级'] || 'high';
        document.getElementById('cfgGameIo').value = cfg['游戏IO优先级'] || 'normal';
        document.getElementById('cfgGameMem').value = cfg['游戏内存优先级'] || 'normal';
        document.getElementById('cfgLowerBg').checked = cfg['降低后台进程优先级'] || false;
        document.getElementById('cfgBgPrio').value = cfg['后台进程优先级'] || 'below_normal';
        document.getElementById('cfgGameList').value = (cfg['游戏进程列表'] || []).join('\n');
        document.getElementById('cfgAceSwitch').checked = cfg['ACE降权开关'] !== false;
        document.getElementById('cfgVerbose').checked = cfg['记录降权明细'] || false;
        document.getElementById('cfgNotify').checked = cfg['桌面通知'] !== false;
        document.getElementById('cfgHotkey').checked = cfg['全局快捷键'] !== false;
        document.getElementById('cfgBlacklist').value = (cfg['黑名单进程'] || []).join('\n');
        document.getElementById('cfgProtected').value = (cfg['保护进程'] || []).join('\n');
    });
    // v0.4.0 加载开机自启状态
    fetch('/api/autostart/status').then(r => r.json()).then(data => {
        document.getElementById('cfgAutostart').checked = data.enabled || false;
    }).catch(() => {});
    document.getElementById('configModal').style.display = 'flex';
}

function toggleAutostart(enabled) {
    // v0.4.0 切换开机自启
    const url = enabled ? '/api/autostart/enable' : '/api/autostart/disable';
    fetch(url, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (!data.success) {
                alert('设置开机自启失败: ' + (data.error || data.message || '未知错误'));
                document.getElementById('cfgAutostart').checked = !enabled;
            }
        })
        .catch(err => {
            alert('设置开机自启失败: ' + err);
            document.getElementById('cfgAutostart').checked = !enabled;
        });
}

function closeConfig() {
    document.getElementById('configModal').style.display = 'none';
}

async function saveConfig() {
    const gameList = document.getElementById('cfgGameList').value.split('\n').map(s => s.trim()).filter(s => s);
    const blacklist = document.getElementById('cfgBlacklist').value.split('\n').map(s => s.trim().toLowerCase()).filter(s => s);
    const protectedList = document.getElementById('cfgProtected').value.split('\n').map(s => s.trim().toLowerCase()).filter(s => s);
    await api('/api/save_config', 'POST', {
        '游戏优先级': document.getElementById('cfgGamePrio').value,
        '游戏IO优先级': document.getElementById('cfgGameIo').value,
        '游戏内存优先级': document.getElementById('cfgGameMem').value,
        '降低后台进程优先级': document.getElementById('cfgLowerBg').checked,
        '后台进程优先级': document.getElementById('cfgBgPrio').value,
        '游戏进程列表': gameList,
        'ACE降权开关': document.getElementById('cfgAceSwitch').checked,
        '记录降权明细': document.getElementById('cfgVerbose').checked,
        '桌面通知': document.getElementById('cfgNotify').checked,
        '全局快捷键': document.getElementById('cfgHotkey').checked,
        '黑名单进程': blacklist,
        '保护进程': protectedList,
    });
    closeConfig();
    refreshStatus();
    loadPresets();
}

async function loadPresets() {
    const data = await api('/api/presets');
    const select = document.getElementById('presetSelect');
    select.innerHTML = '';
    data.presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        // 顶部只显示短名；描述注释只在展开下拉时对其他选项可见（选中项收起时显示短名）
        opt.textContent = p.id === data.active
            ? p.name
            : (p.name + (p.description ? ' · ' + p.description : ''));
        opt.title = p.description || '';
        if (p.id === data.active) opt.selected = true;
        select.appendChild(opt);
    });
    window._currentPreset = data.active || 'default';  // v0.3.0: 记录当前活跃预设，供取消时同步恢复（默认值兜底）
}

async function loadPreset(presetId) {
    if (!presetId) return;
    if (!confirm(`确定加载预设「${presetId}」？当前配置将被覆盖。`)) {
        // v0.3.0: 同步恢复下拉框选中项（不依赖异步 loadPresets，避免取消后仍显示新预设）
        const select = document.getElementById('presetSelect');
        if (window._currentPreset) select.value = window._currentPreset;
        return;
    }
    await api('/api/load_preset', 'POST', {preset_id: presetId});
    loadPresets();  // 刷新选项文本（新选中项收起时显示短名）
    refreshStatus();
    refreshProcesses();
}

async function saveCurrentAsPreset() {
    const presetId = prompt('输入预设ID（英文/数字/下划线）:', 'my_preset');
    if (!presetId) return;
    const name = prompt('输入预设名称:', presetId);
    const description = prompt('输入预设描述（可选）:', '') || '';
    await api('/api/save_preset', 'POST', {preset_id: presetId, name: name, description: description});
    loadPresets();
}

setInterval(() => { refreshProcesses(); refreshStatus(); refreshLogs(); }, 2000);
refreshProcesses();
refreshStatus();
refreshLogs();
loadPresets();
checkUpdate();

function checkUpdate() {
    // v0.4.0 检查更新（静默检查，有新版本时提示）
    fetch('/api/check_update')
        .then(r => r.json())
        .then(data => {
            if (data.has_update) {
                if (confirm(`发现新版本 ${data.latest_version}！\n当前版本: ${data.current_version}\n\n是否前往下载页面？`)) {
                    window.open(data.release_url, '_blank');
                }
            }
        })
        .catch(() => {});  // 检查更新失败静默忽略
}
</script>
<footer style="text-align:center;padding:12px;color:#636e72;font-size:12px;border-top:1px solid #0f3460;margin-top:16px;">🐱 制作人是mmr，UID才不是10482803喵。</footer>
</body>
</html>
"""


def main() -> None:
    """主入口函数 - 初始化引擎并启动 HTTP 服务"""
    global engine

    # ===== v0.3.0: 解析命令行参数 =====
    no_browser = "--no-browser" in sys.argv
    minimized = "--minimized" in sys.argv  # 启动最小化（不自动开浏览器）
    autostart = "--autostart" in sys.argv  # 注册开机自启后退出
    check_update = "--check-update" in sys.argv  # 检查更新后退出

    if autostart:
        # 注册开机自启（当前用户 HKCU\...\Run，不需要管理员）
        try:
            import winreg
            exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "CalaNeko", 0, winreg.REG_SZ, f'"{exe_path}" --minimized')
            winreg.CloseKey(key)
            print(f"✅ CalaNeko 已注册开机自启: {exe_path} --minimized")
        except Exception as e:
            print(f"❌ 注册开机自启失败: {e}")
        return

    if check_update:
        print(f"CalaNeko 当前版本: {__version__}")
        print("更新检查功能将在 V2 中实现（需 GitHub Releases API）")
        return

    # --minimized 等价于 --no-browser（启动时不自动开浏览器）
    if minimized:
        no_browser = True

    # ===== 单实例锁（防止重复启动）=====
    # v0.3.3: 改用端口检测（更可靠，不依赖互斥体权限）+ Mutex 双重保障
    PORT = 18765  # 端口常量（单实例检测和HTTP服务共用）
    already_running = False
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', PORT))
        sock.close()
        if result == 0:
            already_running = True
            print(f"端口 {PORT} 已被占用，检测到已有实例运行")
    except Exception:
        pass

    # Mutex 作为补充检测（端口检测失败时使用）
    if not already_running:
        try:
            mutex_name = "Local\\CalaNeko_Mutex_v1"
            kernel32_mutex = ctypes.WinDLL('kernel32', use_last_error=True)
            mutex = kernel32_mutex.CreateMutexW(None, False, mutex_name)  # noqa: F841
            mutex_error = ctypes.get_last_error()
            if mutex_error == 183:  # ERROR_ALREADY_EXISTS
                already_running = True
                print("Mutex 检测到已有实例运行")
        except Exception:
            pass

    if already_running:
        # v0.3.2: 检测到已有实例运行时，自动打开 Web UI，而不是只弹框提示
        web_ui_url = f"http://127.0.0.1:{PORT}/"
        try:
            import webbrowser
            webbrowser.open(web_ui_url)
            print(f"CalaNeko 已在运行，已打开 Web UI: {web_ui_url}")
        except Exception:
            msg = f"CalaNeko 已经在运行中！\n\nWeb UI: {web_ui_url}\n\n如需重启，请先在任务管理器结束 CalaNeko.exe 进程。"
            try:
                ctypes.windll.user32.MessageBoxW(0, msg, "CalaNeko", 0x30)
            except Exception:
                print(msg)
        sys.exit(1)

    # ===== AWCC 兼容性检测 =====
    AWCC_PROCESS_NAMES = {'awcc.exe', 'awccoverlay.exe', 'alienfxsubagent.exe',
                           'awcc.scsubagent.exe', 'awcc.ucsubagent.exe',
                           'awperformance.scsubagent.exe', 'awperformance.ucsubagent.exe',
                           'occontrol.service.exe'}
    awcc_running = []
    try:
        for proc in psutil.process_iter(['name', 'pid']):
            if proc.info['name'] and proc.info['name'].lower() in AWCC_PROCESS_NAMES:
                awcc_running.append(proc.info['name'])
    except Exception:
        pass

    # 路径（兼容 PyInstaller onefile 模式）
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后，用 exe 所在目录
        project_dir = os.path.dirname(sys.executable)
    else:
        # 开发模式，用源码目录
        src_dir = os.path.dirname(os.path.abspath(__file__))
        project_dir = os.path.dirname(src_dir)
    config_path = os.path.join(project_dir, "config", "config.json")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    # 初始化引擎
    engine = GameBoostEngine(config_path)
    engine.log("GameBoost Web 启动")
    engine.log(f"配置文件: {config_path}")
    # v0.2.8: 启动时记录预设名 + 管理员状态（排障关键信息）
    engine.log(f"预设: {engine.config.get('active_preset', 'default')} | 管理员权限: {'是' if _is_admin() else '否（部分进程可能无法优化）'}")

    # AWCC 兼容性检测日志
    if awcc_running:
        engine.log(f"检测到 AWCC（Alienware Command Center）正在运行: {', '.join(set(awcc_running))}")
        engine.log("   AWCC 负责硬件层优化（超频/风扇/电源），CalaNeko 负责进程层优化（优先级/降权），两者互补共存")
    else:
        engine.log("AWCC 未运行")

    # 启动 HTTP 服务（线程化+大连接队列：浏览器页面每2秒轮询多接口，单线程会卡死积压）
    port = PORT  # 使用上方定义的端口常量
    server = ThreadingHTTPServer(('127.0.0.1', port), WebHandler)
    server.request_queue_size = 32
    server.daemon_threads = True
    engine.log(f"HTTP 服务: http://127.0.0.1:{port}")

    # 自动打开浏览器（后台运行时用 --no-browser 禁用）
    if not no_browser:
        try:
            webbrowser.open(f'http://127.0.0.1:{port}')
        except Exception:
            pass

    print(f"GameBoost Pro 已启动: http://127.0.0.1:{port}")
    print("按 Ctrl+C 退出")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        engine.log("收到退出信号")
        engine.stop_monitor()
        server.shutdown()
        print("已退出")
    except Exception as e:
        engine.log(f"HTTP 服务异常退出: {e}")
        import traceback
        engine.log(traceback.format_exc())
        print(f"异常退出: {e}")
        input("按回车键退出...")


if __name__ == "__main__":
    main()



