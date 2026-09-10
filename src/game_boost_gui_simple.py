# -*- coding: utf-8 -*-
"""
GameBoost GUI - 游戏进程优先级自动设置工具（图形界面版）
借鉴 Process Lasso / System Informer / GameShift 核心功能
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import psutil
import json
import time
import threading
import os
import sys
from datetime import datetime

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


class GameBoostGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GameBoost - 游戏进程优化工具")
        self.root.geometry("900x650")
        self.root.minsize(800, 550)

        # 配置
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_boost_config.json")
        self.config = self.load_config()

        # 状态
        self.monitoring = False
        self.monitor_thread = None
        self.active_games = {}
        self.lowered_processes = {}
        self.update_interval = int(self.config.get("监控间隔秒", 3)) * 1000

        # 构建界面
        self.build_ui()

        # 初始刷新进程列表
        self.refresh_process_list()

    def load_config(self):
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {
                "游戏进程列表": ["steam", "wegame"],
                "游戏优先级": "高",
                "降低后台进程优先级": True,
                "后台进程优先级": "低于正常",
                "排除进程": ["explorer", "svchost"],
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

        self.start_btn = ttk.Button(toolbar, text="▶ 开始监控", command=self.toggle_monitor)
        self.start_btn.pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="🔄 刷新进程", command=self.refresh_process_list).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="⚙ 配置游戏列表", command=self.edit_game_list).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="↩ 恢复所有进程", command=self.restore_all).pack(side=tk.LEFT, padx=2)

        # 状态标签
        self.status_label = ttk.Label(toolbar, text="状态: 未监控", foreground="gray")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # 主内容区（左右分栏）
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：进程列表
        left_frame = ttk.LabelFrame(main_paned, text="进程列表", padding="5")
        main_paned.add(left_frame, weight=3)

        # 进程列表表格
        columns = ("pid", "name", "priority", "cpu", "memory")
        self.process_tree = ttk.Treeview(left_frame, columns=columns, show="headings", height=20)
        self.process_tree.heading("pid", text="PID")
        self.process_tree.heading("name", text="进程名")
        self.process_tree.heading("priority", text="优先级")
        self.process_tree.heading("cpu", text="CPU%")
        self.process_tree.heading("memory", text="内存MB")
        self.process_tree.column("pid", width=60, anchor=tk.CENTER)
        self.process_tree.column("name", width=150)
        self.process_tree.column("priority", width=80, anchor=tk.CENTER)
        self.process_tree.column("cpu", width=60, anchor=tk.CENTER)
        self.process_tree.column("memory", width=80, anchor=tk.CENTER)

        # 滚动条
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
        self.context_menu.add_command(label="添加到游戏列表", command=self.add_to_game_list)
        self.process_tree.bind("<Button-3>", self.show_context_menu)

        # 右侧：信息面板
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=2)

        # 活跃游戏
        game_frame = ttk.LabelFrame(right_frame, text="🎮 活跃游戏", padding="5")
        game_frame.pack(fill=tk.X, pady=(0, 5))

        self.game_listbox = tk.Listbox(game_frame, height=6, bg="#f0f8ff")
        self.game_listbox.pack(fill=tk.X)

        # 配置信息
        config_frame = ttk.LabelFrame(right_frame, text="⚙ 当前配置", padding="5")
        config_frame.pack(fill=tk.X, pady=(0, 5))

        self.config_text = scrolledtext.ScrolledText(config_frame, height=8, font=("Consolas", 9))
        self.config_text.pack(fill=tk.X)
        self.update_config_display()

        # 日志
        log_frame = ttk.LabelFrame(right_frame, text="📋 日志", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log("GameBoost GUI 启动")
        self.log(f"游戏列表: {len(self.config.get('游戏进程列表', []))} 个进程")

    def log(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
        self.log_text.see(tk.END)

    def update_config_display(self):
        self.config_text.delete(1.0, tk.END)
        cfg = self.config
        display = f"游戏优先级: {cfg.get('游戏优先级', '高')}\n"
        display += f"降低后台进程: {cfg.get('降低后台进程优先级', False)}\n"
        display += f"后台优先级: {cfg.get('后台进程优先级', '低于正常')}\n"
        display += f"监控间隔: {cfg.get('监控间隔秒', 3)} 秒\n"
        display += f"游戏进程数: {len(cfg.get('游戏进程列表', []))}\n"
        display += "─" * 30 + "\n"
        display += "游戏列表:\n"
        for g in cfg.get("游戏进程列表", [])[:10]:
            display += f"  - {g}\n"
        if len(cfg.get("游戏进程列表", [])) > 10:
            display += f"  ... 还有 {len(cfg.get('游戏进程列表', [])) - 10} 个\n"
        self.config_text.insert(1.0, display)

    def refresh_process_list(self):
        """刷新进程列表"""
        for item in self.process_tree.get_children():
            self.process_tree.delete(item)

        try:
            for proc in psutil.process_iter(['pid', 'name', 'nice', 'cpu_percent', 'memory_info']):
                try:
                    pid = proc.info['pid']
                    name = proc.info['name'] or "?"
                    nice = proc.info['nice']
                    priority = PRIORITY_NAME.get(nice, "未知") if nice else "正常"
                    cpu = proc.info['cpu_percent'] or 0
                    mem = (proc.info['memory_info'].rss / 1024 / 1024) if proc.info['memory_info'] else 0

                    # 高亮游戏进程
                    tags = ()
                    if self.is_game_process(name):
                        tags = ("game",)
                    elif pid in self.active_games:
                        tags = ("active",)

                    self.process_tree.insert("", tk.END, values=(
                        pid, name, priority, f"{cpu:.1f}", f"{mem:.0f}"
                    ), tags=tags)
                except:
                    continue
        except Exception as e:
            self.log(f"刷新进程列表失败: {e}")

        # 设置颜色
        self.process_tree.tag_configure("game", background="#fff3cd")
        self.process_tree.tag_configure("active", background="#d4edda")

    def is_game_process(self, name):
        """判断是否为游戏进程"""
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
        self.monitoring = True
        self.start_btn.config(text="⏹ 停止监控")
        self.status_label.config(text="状态: 监控中", foreground="green")
        self.log("开始监控游戏进程")
        self.monitor_loop()

    def stop_monitor(self):
        self.monitoring = False
        self.start_btn.config(text="▶ 开始监控")
        self.status_label.config(text="状态: 未监控", foreground="gray")
        self.log("停止监控")
        self.restore_all()

    def monitor_loop(self):
        if not self.monitoring:
            return
        self.check_games()
        self.refresh_process_list()
        self.root.after(self.update_interval, self.monitor_loop)

    def check_games(self):
        """检测游戏进程"""
        # 检查已激活的游戏是否还在运行
        for pid in list(self.active_games.keys()):
            if not psutil.pid_exists(pid):
                info = self.active_games.pop(pid)
                self.log(f"游戏退出: {info['name']} (PID={pid})")
                self.update_game_listbox()
                # 恢复后台进程
                if not self.active_games and self.lowered_processes:
                    self.restore_background()

        # 检测新的游戏进程
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
        """优化游戏进程"""
        try:
            pid = proc.pid
            name = proc.name()
            original_priority = proc.nice()
            self.active_games[pid] = {
                "name": name,
                "original_priority": original_priority,
                "start_time": datetime.now().isoformat(),
            }
            priority_name = self.config.get("游戏优先级", "高")
            proc.nice(PRIORITY_MAP.get(priority_name, psutil.HIGH_PRIORITY_CLASS))
            self.log(f"游戏启动: {name} (PID={pid})，优先级→{priority_name}")
            self.update_game_listbox()

            # 降低后台进程
            if self.config.get("降低后台进程优先级", False):
                self.lower_background()
        except Exception as e:
            self.log(f"优化游戏失败: {e}")

    def lower_background(self):
        """降低后台进程优先级"""
        bg_priority_name = self.config.get("后台进程优先级", "低于正常")
        bg_priority_class = PRIORITY_MAP.get(bg_priority_name, psutil.BELOW_NORMAL_PRIORITY_CLASS)
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
                    count += 1
            except:
                continue
        if count > 0:
            self.log(f"已降低 {count} 个后台进程优先级→{bg_priority_name}")

    def restore_background(self):
        """恢复后台进程"""
        for pid, original in list(self.lowered_processes.items()):
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).nice(original)
            except:
                pass
        self.lowered_processes.clear()
        self.log("后台进程已恢复")

    def restore_all(self):
        """恢复所有进程"""
        for pid, info in list(self.active_games.items()):
            try:
                if psutil.pid_exists(pid):
                    psutil.Process(pid).nice(info["original_priority"])
            except:
                pass
        self.active_games.clear()
        self.restore_background()
        self.update_game_listbox()
        self.log("所有进程已恢复原始状态")

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
                proc = psutil.Process(pid)
                proc.nice(PRIORITY_MAP[priority_name])
                self.log(f"PID {pid} 优先级→{priority_name}")
            except Exception as e:
                self.log(f"设置失败 PID {pid}: {e}")
        self.refresh_process_list()

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
            self.update_config_display()
            self.log(f"已添加到游戏列表: {name}")
        else:
            self.log(f"{name} 已在游戏列表中")

    def edit_game_list(self):
        """编辑游戏列表"""
        editor = tk.Toplevel(self.root)
        editor.title("编辑游戏进程列表")
        editor.geometry("400x500")

        ttk.Label(editor, text="游戏进程列表（每行一个，支持 .exe 或不带后缀）:").pack(padx=10, pady=5, anchor=tk.W)

        text = scrolledtext.ScrolledText(editor, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        for g in self.config.get("游戏进程列表", []):
            text.insert(tk.END, g + "\n")

        def save():
            lines = [l.strip() for l in text.get(1.0, tk.END).split("\n") if l.strip()]
            self.config["游戏进程列表"] = lines
            if self.save_config():
                self.update_config_display()
                self.log(f"游戏列表已更新: {len(lines)} 个进程")
                editor.destroy()

        btn_frame = ttk.Frame(editor)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="保存", command=save).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="取消", command=editor.destroy).pack(side=tk.RIGHT, padx=5)


def main():
    root = tk.Tk()
    # 设置主题
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except:
        pass
    app = GameBoostGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
