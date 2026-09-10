# -*- coding: utf-8 -*-
"""
GameBoost Pro - 复合版游戏进程优化工具
双引擎: Python原生(优先级/亲和性/IO/内存) + ProcGovernor(开源完整功能)
统一图形界面管理
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import psutil
import json
import os
import sys
import subprocess
import threading
import time
from datetime import datetime

# 导入 Windows API 封装
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import win_process_api as winapi

# 优先级映射
PRIORITY_MAP = {
    "实时": psutil.REALTIME_PRIORITY_CLASS,
    "高": psutil.HIGH_PRIORITY_CLASS,
    "高于正常": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
    "正常": psutil.NORMAL_PRIORITY_CLASS,
    "低于正常": psutil.BELOW_NORMAL_PRIORITY_CLASS,
    "低": psutil.IDLE_PRIORITY_CLASS,
}
PRIORITY_NAME = {v: k for k, v in PRIORITY_MAP.items()}

# 内存优先级映射
MEM_PRIORITY_MAP = {
    "Very Low": 0,
    "Low": 1,
    "Medium": 2,
    "Below Normal": 3,
    "Normal": 5,
}

# IO 优先级映射
IO_PRIORITY_MAP = {
    "Very Low": 0,
    "Low": 1,
    "Normal": 2,
    "High": 3,
}


class GameBoostPro:
    def __init__(self, root):
        self.root = root
        self.root.title("GameBoost Pro - 复合版游戏优化工具")
        self.root.geometry("1000x700")
        self.root.minsize(900, 600)

        # 路径（适配项目目录结构）
        self.src_dir = os.path.dirname(os.path.abspath(__file__))
        self.project_dir = os.path.dirname(self.src_dir)
        self.config_path = os.path.join(self.project_dir, "config", "config.json")
        self.log_dir = os.path.join(self.project_dir, "logs")
        self.pg_dir = os.path.join(self.project_dir, "engine", "ProcGovernor")
        self.pg_exe = os.path.join(self.pg_dir, "proc_governor.exe")
        self.pg_config = os.path.join(self.pg_dir, "config_game_v2.ini")

        # 确保日志目录存在
        os.makedirs(self.log_dir, exist_ok=True)

        # 配置
        self.config = self.load_config()

        # 状态
        self.engine = "python"  # python 或 procgovernor
        self.monitoring = False
        self.monitor_thread = None
        self.pg_process = None
        self.active_games = {}
        self.lowered_processes = {}
        self.update_interval = int(self.config.get("监控间隔秒", 3)) * 1000

        # 构建界面
        self.build_ui()
        self.refresh_process_list()
        self.log("GameBoost Pro 启动")
        self.log(f"Python引擎: 优先级/亲和性/IO/内存 完整支持")
        if os.path.exists(self.pg_exe):
            self.log(f"ProcGovernor引擎: 已检测到 ({os.path.getsize(self.pg_exe)//1024}KB)")
        else:
            self.log("ProcGovernor引擎: 未检测到")

    def load_config(self):
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {
                "游戏进程列表": ["steam", "wegame", "Calabiyau"],
                "游戏优先级": "高",
                "游戏IO优先级": "Normal",
                "游戏内存优先级": "Normal",
                "降低后台进程优先级": True,
                "后台进程优先级": "低于正常",
                "后台IO优先级": "Low",
                "后台内存优先级": "Low",
                "排除进程": ["explorer", "svchost", "system"],
                "监控间隔秒": 3,
            }

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            messagebox.showerror("错误", f"保存配置失败: {e}")
            return False

    def build_ui(self):
        # 顶部工具栏
        toolbar = ttk.Frame(self.root, padding="5")
        toolbar.pack(fill=tk.X)

        # 引擎选择
        ttk.Label(toolbar, text="引擎:").pack(side=tk.LEFT, padx=(5, 2))
        self.engine_var = tk.StringVar(value="python")
        engine_combo = ttk.Combobox(toolbar, textvariable=self.engine_var, values=["python", "procgovernor"], width=15, state="readonly")
        engine_combo.pack(side=tk.LEFT, padx=2)
        engine_combo.bind("<<ComboboxSelected>>", self.on_engine_change)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)

        self.start_btn = ttk.Button(toolbar, text="▶ 开始监控", command=self.toggle_monitor)
        self.start_btn.pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="🔄 刷新", command=self.refresh_process_list).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="⚙ 配置", command=self.edit_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="↩ 恢复全部", command=self.restore_all).pack(side=tk.LEFT, padx=2)

        self.status_label = ttk.Label(toolbar, text="状态: 未监控 | 引擎: Python", foreground="gray")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # 主内容区
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：进程列表
        left_frame = ttk.LabelFrame(main_paned, text="进程列表（右键操作）", padding="5")
        main_paned.add(left_frame, weight=3)

        columns = ("pid", "name", "priority", "io", "mem", "cpu", "memory")
        self.process_tree = ttk.Treeview(left_frame, columns=columns, show="headings", height=25)
        self.process_tree.heading("pid", text="PID")
        self.process_tree.heading("name", text="进程名")
        self.process_tree.heading("priority", text="CPU优先级")
        self.process_tree.heading("io", text="IO")
        self.process_tree.heading("mem", text="内存")
        self.process_tree.heading("cpu", text="CPU%")
        self.process_tree.heading("memory", text="内存MB")
        self.process_tree.column("pid", width=55, anchor=tk.CENTER)
        self.process_tree.column("name", width=140)
        self.process_tree.column("priority", width=70, anchor=tk.CENTER)
        self.process_tree.column("io", width=55, anchor=tk.CENTER)
        self.process_tree.column("mem", width=55, anchor=tk.CENTER)
        self.process_tree.column("cpu", width=50, anchor=tk.CENTER)
        self.process_tree.column("memory", width=65, anchor=tk.CENTER)

        proc_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=proc_scroll.set)
        self.process_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        proc_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 右键菜单
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="设为高优先级", command=lambda: self.set_selected_priority("高"))
        self.context_menu.add_command(label="设为高于正常", command=lambda: self.set_selected_priority("高于正常"))
        self.context_menu.add_command(label="设为正常", command=lambda: self.set_selected_priority("正常"))
        self.context_menu.add_command(label="设为低于正常", command=lambda: self.set_selected_priority("低于正常"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="IO→Very Low", command=lambda: self.set_selected_io("Very Low"))
        self.context_menu.add_command(label="IO→Low", command=lambda: self.set_selected_io("Low"))
        self.context_menu.add_command(label="IO→Normal", command=lambda: self.set_selected_io("Normal"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="内存→Low", command=lambda: self.set_selected_mem("Low"))
        self.context_menu.add_command(label="内存→Normal", command=lambda: self.set_selected_mem("Normal"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="添加到游戏列表", command=self.add_to_game_list)
        self.process_tree.bind("<Button-3>", self.show_context_menu)

        # 右侧：信息面板
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=2)

        # 活跃游戏
        game_frame = ttk.LabelFrame(right_frame, text="🎮 活跃游戏", padding="5")
        game_frame.pack(fill=tk.X, pady=(0, 5))
        self.game_listbox = tk.Listbox(game_frame, height=5, bg="#f0f8ff")
        self.game_listbox.pack(fill=tk.X)

        # 引擎状态
        engine_frame = ttk.LabelFrame(right_frame, text="⚙ 引擎状态", padding="5")
        engine_frame.pack(fill=tk.X, pady=(0, 5))
        self.engine_text = scrolledtext.ScrolledText(engine_frame, height=6, font=("Consolas", 9))
        self.engine_text.pack(fill=tk.X)
        self.update_engine_display()

        # 日志
        log_frame = ttk.LabelFrame(right_frame, text="📋 日志", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
        self.log_text.see(tk.END)

    def update_engine_display(self):
        self.engine_text.delete(1.0, tk.END)
        cfg = self.config
        display = f"当前引擎: {'Python原生' if self.engine == 'python' else 'ProcGovernor'}\n"
        display += f"游戏优先级: {cfg.get('游戏优先级', '高')}\n"
        display += f"游戏IO优先级: {cfg.get('游戏IO优先级', 'Normal')}\n"
        display += f"游戏内存优先级: {cfg.get('游戏内存优先级', 'Normal')}\n"
        display += f"后台降权: {'开启' if cfg.get('降低后台进程优先级', False) else '关闭'}\n"
        display += f"游戏进程数: {len(cfg.get('游戏进程列表', []))}\n"
        if self.engine == "procgovernor":
            display += f"PG状态: {'运行中' if self.pg_process and self.pg_process.poll() is None else '未运行'}\n"
        self.engine_text.insert(1.0, display)

    def on_engine_change(self, event):
        new_engine = self.engine_var.get()
        if new_engine == self.engine:
            return
        # 切换引擎前停止当前监控
        if self.monitoring:
            self.stop_monitor()
        self.engine = new_engine
        self.status_label.config(text=f"状态: 未监控 | 引擎: {'Python原生' if self.engine == 'python' else 'ProcGovernor'}")
        self.update_engine_display()
        self.log(f"引擎切换为: {'Python原生' if self.engine == 'python' else 'ProcGovernor'}")
        if self.engine == "procgovernor" and not os.path.exists(self.pg_exe):
            messagebox.showwarning("警告", "ProcGovernor 未找到，请先下载")

    def refresh_process_list(self):
        for item in self.process_tree.get_children():
            self.process_tree.delete(item)
        try:
            for proc in psutil.process_iter(['pid', 'name', 'nice', 'cpu_percent', 'memory_info']):
                try:
                    pid = proc.info['pid']
                    name = proc.info['name'] or "?"
                    nice = proc.info['nice']
                    priority = PRIORITY_NAME.get(nice, "?") if nice else "正常"
                    cpu = proc.info['cpu_percent'] or 0
                    mem = (proc.info['memory_info'].rss / 1024 / 1024) if proc.info['memory_info'] else 0
                    tags = ()
                    if self.is_game_process(name):
                        tags = ("game",)
                    if pid in self.active_games:
                        tags = ("active",)
                    self.process_tree.insert("", tk.END, values=(
                        pid, name, priority, "-", "-", f"{cpu:.1f}", f"{mem:.0f}"
                    ), tags=tags)
                except:
                    continue
        except Exception as e:
            self.log(f"刷新失败: {e}")
        self.process_tree.tag_configure("game", background="#fff3cd")
        self.process_tree.tag_configure("active", background="#d4edda")

    def is_game_process(self, name):
        if not name:
            return False
        name_lower = name.lower()
        if name_lower.endswith('.exe'):
            name_lower = name_lower[:-4]
        game_list = [g.lower() for g in self.config.get("游戏进程列表", [])]
        for game in game_list:
            game_clean = game[:-4] if game.endswith('.exe') else game
            if not game_clean or len(game_clean) < 2:
                continue
            if name_lower == game_clean:
                return True
            if len(game_clean) >= 4 and name_lower.startswith(game_clean):
                return True
        return False

    def toggle_monitor(self):
        if self.monitoring:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self):
        if self.engine == "procgovernor":
            self.start_pg_engine()
        else:
            self.start_python_engine()

    def start_python_engine(self):
        self.monitoring = True
        self.start_btn.config(text="⏹ 停止监控")
        self.status_label.config(text="状态: 监控中 | 引擎: Python原生", foreground="green")
        self.log("Python引擎: 开始监控")
        self.monitor_loop()

    def start_pg_engine(self):
        if not os.path.exists(self.pg_exe):
            messagebox.showerror("错误", "ProcGovernor 未找到")
            return
        if not os.path.exists(self.pg_config):
            messagebox.showerror("错误", "PG配置文件未找到")
            return
        try:
            # 生成 PG 配置（从 Python 配置同步）
            self.sync_pg_config()
            # 启动 PG
            self.pg_process = subprocess.Popen(
                [self.pg_exe, "-config", self.pg_config, "-console",
                 "-interval", "2000", "-noUAC"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            self.monitoring = True
            self.start_btn.config(text="⏹ 停止监控")
            self.status_label.config(text="状态: 监控中 | 引擎: ProcGovernor", foreground="green")
            self.log("ProcGovernor引擎: 已启动")
            # 启动日志读取线程
            self.pg_log_thread = threading.Thread(target=self.read_pg_log, daemon=True)
            self.pg_log_thread.start()
        except Exception as e:
            self.log(f"PG启动失败: {e}")
            messagebox.showerror("错误", f"ProcGovernor 启动失败: {e}")

    def read_pg_log(self):
        if not self.pg_process:
            return
        for line in self.pg_process.stdout:
            try:
                decoded = line.decode('utf-8', errors='replace').strip()
                if decoded:
                    self.log(f"[PG] {decoded}")
            except:
                pass

    def sync_pg_config(self):
        """从 Python 配置同步生成 PG 配置"""
        lines = []
        game_prio = self.config.get("游戏优先级", "高")
        pg_prio = {"高": "high", "高于正常": "above normal", "正常": "normal",
                    "低于正常": "below normal", "低": "idle"}.get(game_prio, "high")
        for game in self.config.get("游戏进程列表", []):
            if not game.endswith('.exe'):
                game = game + '.exe'
            lines.append(f"{game}:{pg_prio}:0:all")
        # 后台进程
        if self.config.get("降低后台进程优先级", False):
            bg_prio = self.config.get("后台进程优先级", "低于正常")
            pg_bg = {"低于正常": "below normal", "低": "idle"}.get(bg_prio, "below normal")
            for bg in ["chrome.exe", "msedge.exe", "firefox.exe", "WeChat.exe", "QQ.exe"]:
                lines.append(f"{bg}:{pg_bg}:0:all")
        content = "\n".join(lines) + "\n"
        with open(self.pg_config, 'w', encoding='ascii') as f:
            f.write(content)
        self.log(f"PG配置已同步: {len(lines)} 条规则")

    def stop_monitor(self):
        self.monitoring = False
        if self.engine == "procgovernor":
            if self.pg_process and self.pg_process.poll() is None:
                self.pg_process.terminate()
                self.pg_process.wait(timeout=3)
                self.log("ProcGovernor引擎: 已停止")
            self.pg_process = None
        else:
            self.restore_all()
            self.log("Python引擎: 已停止")
        self.start_btn.config(text="▶ 开始监控")
        self.status_label.config(text=f"状态: 未监控 | 引擎: {'Python原生' if self.engine == 'python' else 'ProcGovernor'}", foreground="gray")
        self.update_engine_display()

    def monitor_loop(self):
        if not self.monitoring or self.engine != "python":
            return
        self.check_games()
        self.refresh_process_list()
        self.root.after(self.update_interval, self.monitor_loop)

    def check_games(self):
        for pid in list(self.active_games.keys()):
            if not psutil.pid_exists(pid):
                info = self.active_games.pop(pid)
                self.log(f"游戏退出: {info['name']} (PID={pid})")
                self.update_game_listbox()
                if not self.active_games and self.lowered_processes:
                    self.restore_background()
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']
                if pid in self.active_games:
                    continue
                if self.is_game_process(name):
                    self.boost_game(proc)
            except:
                continue

    def boost_game(self, proc):
        try:
            pid = proc.pid
            name = proc.name()
            original_priority = proc.nice()
            self.active_games[pid] = {
                "name": name,
                "original_priority": original_priority,
                "start_time": datetime.now().isoformat(),
            }
            # 设置 CPU 优先级
            priority_name = self.config.get("游戏优先级", "高")
            proc.nice(PRIORITY_MAP.get(priority_name, psutil.HIGH_PRIORITY_CLASS))
            # 设置 IO 优先级
            io_prio = self.config.get("游戏IO优先级", "Normal")
            try:
                winapi.set_io_priority(pid, IO_PRIORITY_MAP.get(io_prio, 2))
            except Exception as e:
                self.log(f"  IO优先级设置失败(可能需要管理员): {e}")
            # 设置内存优先级
            mem_prio = self.config.get("游戏内存优先级", "Normal")
            try:
                winapi.set_memory_priority(pid, MEM_PRIORITY_MAP.get(mem_prio, 5))
            except Exception as e:
                self.log(f"  内存优先级设置失败: {e}")
            self.log(f"游戏启动: {name} (PID={pid}) | CPU:{priority_name} IO:{io_prio} 内存:{mem_prio}")
            self.update_game_listbox()
            if self.config.get("降低后台进程优先级", False):
                self.lower_background()
        except Exception as e:
            self.log(f"优化游戏失败: {e}")

    def lower_background(self):
        bg_priority_name = self.config.get("后台进程优先级", "低于正常")
        bg_priority_class = PRIORITY_MAP.get(bg_priority_name, psutil.BELOW_NORMAL_PRIORITY_CLASS)
        bg_io = self.config.get("后台IO优先级", "Low")
        bg_mem = self.config.get("后台内存优先级", "Low")
        count = 0
        excluded = [e.lower() for e in self.config.get("排除进程", [])]
        for proc in psutil.process_iter(['pid', 'name', 'nice']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']
                if pid in self.active_games:
                    continue
                if self.is_game_process(name):
                    continue
                name_lower = name.lower() if name else ""
                if any(e in name_lower for e in excluded):
                    continue
                current = proc.info['nice']
                if current is not None and current > bg_priority_class:
                    if pid not in self.lowered_processes:
                        self.lowered_processes[pid] = current
                    proc.nice(bg_priority_class)
                    try:
                        winapi.set_io_priority(pid, IO_PRIORITY_MAP.get(bg_io, 1))
                    except:
                        pass
                    try:
                        winapi.set_memory_priority(pid, MEM_PRIORITY_MAP.get(bg_mem, 1))
                    except:
                        pass
                    count += 1
            except:
                continue
        if count > 0:
            self.log(f"后台降权: {count} 个进程 | CPU:{bg_priority_name} IO:{bg_io} 内存:{bg_mem}")

    def restore_background(self):
        for pid, original in list(self.lowered_processes.items()):
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).nice(original)
                    try:
                        winapi.set_io_priority(pid, 2)
                        winapi.set_memory_priority(pid, 5)
                    except:
                        pass
            except:
                pass
        self.lowered_processes.clear()
        self.log("后台进程已恢复")

    def restore_all(self):
        for pid, info in list(self.active_games.items()):
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).nice(info["original_priority"])
                    try:
                        winapi.set_io_priority(pid, 2)
                        winapi.set_memory_priority(pid, 5)
                    except:
                        pass
            except:
                pass
        self.active_games.clear()
        self.restore_background()
        self.update_game_listbox()
        self.log("所有进程已恢复")

    def update_game_listbox(self):
        self.game_listbox.delete(0, tk.END)
        if not self.active_games:
            self.game_listbox.insert(tk.END, "（无活跃游戏）")
        else:
            for pid, info in self.active_games.items():
                self.game_listbox.insert(tk.END, f"🎮 {info['name']} (PID={pid})")

    def show_context_menu(self, event):
        item = self.process_tree.identify_row(event.y)
        if item:
            self.process_tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def set_selected_priority(self, priority_name):
        selected = self.process_tree.selection()
        if not selected:
            return
        for item in selected:
            pid = int(self.process_tree.item(item, "values")[0])
            try:
                psutil.Process(pid).nice(PRIORITY_MAP[priority_name])
                self.log(f"PID {pid} CPU优先级→{priority_name}")
            except Exception as e:
                self.log(f"设置失败 PID {pid}: {e}")
        self.refresh_process_list()

    def set_selected_io(self, io_name):
        selected = self.process_tree.selection()
        if not selected:
            return
        for item in selected:
            pid = int(self.process_tree.item(item, "values")[0])
            try:
                winapi.set_io_priority(pid, IO_PRIORITY_MAP.get(io_name, 2))
                self.log(f"PID {pid} IO优先级→{io_name}")
            except Exception as e:
                self.log(f"IO设置失败 PID {pid}: {e}")

    def set_selected_mem(self, mem_name):
        selected = self.process_tree.selection()
        if not selected:
            return
        for item in selected:
            pid = int(self.process_tree.item(item, "values")[0])
            try:
                winapi.set_memory_priority(pid, MEM_PRIORITY_MAP.get(mem_name, 5))
                self.log(f"PID {pid} 内存优先级→{mem_name}")
            except Exception as e:
                self.log(f"内存设置失败 PID {pid}: {e}")

    def add_to_game_list(self):
        selected = self.process_tree.selection()
        if not selected:
            return
        item = selected[0]
        name = self.process_tree.item(item, "values")[1]
        game_list = self.config.get("游戏进程列表", [])
        if name not in game_list:
            game_list.append(name)
            self.config["游戏进程列表"] = game_list
            self.save_config()
            self.update_engine_display()
            self.log(f"已添加到游戏列表: {name}")
        else:
            self.log(f"{name} 已在游戏列表中")

    def edit_config(self):
        editor = tk.Toplevel(self.root)
        editor.title("编辑配置")
        editor.geometry("500x600")

        # 游戏优先级
        ttk.Label(editor, text="游戏CPU优先级:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        game_prio_var = tk.StringVar(value=self.config.get("游戏优先级", "高"))
        ttk.Combobox(editor, textvariable=game_prio_var, values=list(PRIORITY_MAP.keys()), state="readonly", width=15).pack(anchor=tk.W, padx=10)

        # 游戏IO优先级
        ttk.Label(editor, text="游戏IO优先级:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        game_io_var = tk.StringVar(value=self.config.get("游戏IO优先级", "Normal"))
        ttk.Combobox(editor, textvariable=game_io_var, values=list(IO_PRIORITY_MAP.keys()), state="readonly", width=15).pack(anchor=tk.W, padx=10)

        # 游戏内存优先级
        ttk.Label(editor, text="游戏内存优先级:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        game_mem_var = tk.StringVar(value=self.config.get("游戏内存优先级", "Normal"))
        ttk.Combobox(editor, textvariable=game_mem_var, values=list(MEM_PRIORITY_MAP.keys()), state="readonly", width=15).pack(anchor=tk.W, padx=10)

        # 后台降权开关
        bg_var = tk.BooleanVar(value=self.config.get("降低后台进程优先级", False))
        ttk.Checkbutton(editor, text="降低后台进程优先级", variable=bg_var).pack(anchor=tk.W, padx=10, pady=(10, 2))

        # 后台优先级
        ttk.Label(editor, text="后台CPU优先级:").pack(anchor=tk.W, padx=10, pady=(5, 2))
        bg_prio_var = tk.StringVar(value=self.config.get("后台进程优先级", "低于正常"))
        ttk.Combobox(editor, textvariable=bg_prio_var, values=list(PRIORITY_MAP.keys()), state="readonly", width=15).pack(anchor=tk.W, padx=10)

        # 游戏列表
        ttk.Label(editor, text="游戏进程列表（每行一个）:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        text = scrolledtext.ScrolledText(editor, height=10, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        for g in self.config.get("游戏进程列表", []):
            text.insert(tk.END, g + "\n")

        def save():
            self.config["游戏优先级"] = game_prio_var.get()
            self.config["游戏IO优先级"] = game_io_var.get()
            self.config["游戏内存优先级"] = game_mem_var.get()
            self.config["降低后台进程优先级"] = bg_var.get()
            self.config["后台进程优先级"] = bg_prio_var.get()
            lines = [l.strip() for l in text.get(1.0, tk.END).split("\n") if l.strip()]
            self.config["游戏进程列表"] = lines
            if self.save_config():
                self.update_engine_display()
                self.log("配置已保存")
                editor.destroy()

        btn_frame = ttk.Frame(editor)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="保存", command=save).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="取消", command=editor.destroy).pack(side=tk.RIGHT, padx=5)


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except:
        pass
    app = GameBoostPro(root)
    root.mainloop()


if __name__ == "__main__":
    main()
