# -*- coding: utf-8 -*-
"""
GameBoost - 游戏进程优先级自动设置工具
借鉴 Process Lasso / System Informer / GameShift 核心功能
功能：
  - 自动检测游戏进程启动
  - 设置游戏进程为高优先级
  - 可选设置 CPU 亲和性
  - 可选降低后台进程优先级
  - 游戏退出后自动恢复
  - 日志记录
用法：
  python game_boost.py              # 持续监控模式（默认）
  python game_boost.py --once       # 单次检测优化
  python game_boost.py --pid 1234   # 手动优化指定 PID
  python game_boost.py --status     # 查看当前游戏状态
"""

import psutil
import json
import time
import sys
import os
import logging
import argparse
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


class GameBoost:
    def __init__(self, config_path=None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_boost_config.json")
        self.config_path = config_path
        self.config = self.load_config()
        self.active_games = {}  # pid -> {name, original_priority, original_affinity}
        self.lowered_processes = {}  # pid -> original_priority
        self.setup_logging()

    def load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"配置文件加载失败: {e}，使用默认配置")
            return self.default_config()

    def default_config(self):
        return {
            "游戏进程列表": ["steam", "wegame"],
            "游戏优先级": "高",
            "游戏CPU亲和性": "all",
            "降低后台进程优先级": True,
            "后台进程优先级": "低于正常",
            "排除进程": ["explorer", "svchost", "system"],
            "监控间隔秒": 3,
            "启用日志": True,
            "日志路径": "game_boost.log",
            "游戏退出时恢复": True,
        }

    def setup_logging(self):
        """设置日志"""
        if not self.config.get("启用日志", True):
            return
        log_path = self.config.get("日志路径", "game_boost.log")
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.FileHandler(log_path, encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.log = logging.getLogger("GameBoost")

    def log_info(self, msg):
        if self.config.get("启用日志", True):
            self.log.info(msg)

    def is_game_process(self, proc):
        """判断是否为游戏进程"""
        try:
            name = proc.name()
            if not name:
                return False
            # 去掉 .exe 后缀，转小写
            name_lower = name.lower()
            if name_lower.endswith('.exe'):
                name_lower = name_lower[:-4]

            # 排除系统关键进程（名字特殊或为空）
            system_names = {'system', 'registry', 'memory compression', 'idle',
                           'smss.exe', 'csrss.exe', 'wininit.exe', 'winlogon.exe',
                           'services.exe', 'lsass.exe', 'dwm.exe', 'fontdrvhost.exe'}
            if name_lower in system_names or name.lower() in system_names:
                return False

            game_list = [g.lower() for g in self.config.get("游戏进程列表", [])]
            # 精确匹配（去掉.exe后）或游戏名是进程名的前缀（游戏名长度>=4避免误匹配）
            for game in game_list:
                game_clean = game[:-4] if game.endswith('.exe') else game
                if not game_clean or len(game_clean) < 2:
                    continue
                # 精确匹配
                if name_lower == game_clean:
                    return True
                # 前缀匹配（游戏名长度>=4，且进程名以游戏名开头）
                if len(game_clean) >= 4 and name_lower.startswith(game_clean):
                    return True
            return False
        except:
            return False

    def is_excluded_process(self, proc):
        """判断是否为排除进程（系统关键进程）"""
        try:
            name = proc.name().lower()
            excluded = [e.lower() for e in self.config.get("排除进程", [])]
            return any(e in name for e in excluded)
        except:
            return True

    def boost_game(self, proc):
        """优化游戏进程"""
        try:
            pid = proc.pid
            name = proc.name()

            # 保存原始状态
            original_priority = proc.nice()
            try:
                original_affinity = proc.cpu_affinity()
            except:
                original_affinity = None

            self.active_games[pid] = {
                "name": name,
                "original_priority": original_priority,
                "original_affinity": original_affinity,
                "start_time": datetime.now().isoformat(),
            }

            # 设置优先级
            priority_name = self.config.get("游戏优先级", "高")
            priority_class = PRIORITY_MAP.get(priority_name, psutil.HIGH_PRIORITY_CLASS)
            proc.nice(priority_class)
            self.log_info(f"游戏启动: {name} (PID={pid})，优先级设置为 {priority_name}")

            # 设置 CPU 亲和性
            affinity_config = self.config.get("游戏CPU亲和性", "all")
            if affinity_config != "all":
                try:
                    if isinstance(affinity_config, list):
                        proc.cpu_affinity(affinity_config)
                        self.log_info(f"CPU 亲和性设置为: {affinity_config}")
                except Exception as e:
                    self.log_info(f"CPU 亲和性设置失败: {e}")

            # 降低后台进程优先级
            if self.config.get("降低后台进程优先级", False):
                self.lower_background_processes()

            return True
        except Exception as e:
            self.log_info(f"优化游戏进程失败: {e}")
            return False

    def lower_background_processes(self):
        """降低后台进程优先级"""
        bg_priority_name = self.config.get("后台进程优先级", "低于正常")
        bg_priority_class = PRIORITY_MAP.get(bg_priority_name, psutil.BELOW_NORMAL_PRIORITY_CLASS)
        count = 0

        for proc in psutil.process_iter(['pid', 'name', 'nice']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']

                # 跳过游戏进程和排除进程
                if pid in self.active_games:
                    continue
                if self.is_excluded_process(proc):
                    continue
                if self.is_game_process(proc):
                    continue

                # 只降低当前优先级高于目标的进程
                current_priority = proc.info['nice']
                if current_priority is not None and current_priority > bg_priority_class:
                    if pid not in self.lowered_processes:
                        self.lowered_processes[pid] = current_priority
                    proc.nice(bg_priority_class)
                    count += 1
            except:
                continue

        if count > 0:
            self.log_info(f"已降低 {count} 个后台进程优先级为 {bg_priority_name}")

    def restore_process(self, pid, proc_info):
        """恢复单个进程状态"""
        try:
            proc = psutil.Process(pid)
            # 恢复优先级
            proc.nice(proc_info["original_priority"])
            # 恢复 CPU 亲和性
            if proc_info["original_affinity"]:
                proc.cpu_affinity(proc_info["original_affinity"])
            self.log_info(f"游戏退出: {proc_info['name']} (PID={pid})，已恢复原始设置")
        except:
            pass

    def restore_all(self):
        """恢复所有进程"""
        # 恢复游戏进程
        for pid, info in list(self.active_games.items()):
            if psutil.pid_exists(pid):
                self.restore_process(pid, info)
            del self.active_games[pid]

        # 恢复后台进程
        for pid, original_priority in list(self.lowered_processes.items()):
            try:
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    proc.nice(original_priority)
            except:
                pass
            del self.lowered_processes[pid]

        self.log_info("所有进程已恢复原始状态")

    def check_games(self):
        """检测游戏进程状态"""
        # 检查已激活的游戏是否还在运行
        for pid in list(self.active_games.keys()):
            if not psutil.pid_exists(pid):
                info = self.active_games.pop(pid)
                if self.config.get("游戏退出时恢复", True):
                    self.restore_process(pid, info)
                # 游戏退出后恢复所有后台进程
                if not self.active_games and self.lowered_processes:
                    self.restore_all()

        # 检测新的游戏进程
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pid = proc.info['pid']
                if pid in self.active_games:
                    continue
                if self.is_game_process(proc):
                    self.boost_game(proc)
            except:
                continue

    def run_monitor(self):
        """持续监控模式"""
        self.log_info("=" * 50)
        self.log_info("GameBoost 监控启动")
        self.log_info(f"游戏列表: {len(self.config.get('游戏进程列表', []))} 个")
        self.log_info(f"游戏优先级: {self.config.get('游戏优先级', '高')}")
        self.log_info(f"降低后台进程: {self.config.get('降低后台进程优先级', False)}")
        self.log_info(f"监控间隔: {self.config.get('监控间隔秒', 3)} 秒")
        self.log_info("=" * 50)

        try:
            while True:
                self.check_games()
                time.sleep(self.config.get("监控间隔秒", 3))
        except KeyboardInterrupt:
            self.log_info("收到中断信号，正在恢复...")
            self.restore_all()
            self.log_info("已退出")

    def run_once(self):
        """单次检测优化"""
        self.log_info("单次检测模式")
        self.check_games()
        if self.active_games:
            self.log_info(f"当前活跃游戏: {len(self.active_games)} 个")
            for pid, info in self.active_games.items():
                self.log_info(f"  - {info['name']} (PID={pid})")
        else:
            self.log_info("未检测到游戏进程")

    def boost_pid(self, pid):
        """手动优化指定 PID"""
        try:
            proc = psutil.Process(pid)
            self.log_info(f"手动优化: {proc.name()} (PID={pid})")
            self.active_games[pid] = {
                "name": proc.name(),
                "original_priority": proc.nice(),
                "original_affinity": proc.cpu_affinity() if hasattr(proc, 'cpu_affinity') else None,
                "start_time": datetime.now().isoformat(),
            }
            priority_name = self.config.get("游戏优先级", "高")
            proc.nice(PRIORITY_MAP.get(priority_name, psutil.HIGH_PRIORITY_CLASS))
            self.log_info(f"优先级已设置为 {priority_name}")
            if self.config.get("降低后台进程优先级", False):
                self.lower_background_processes()
        except Exception as e:
            self.log_info(f"手动优化失败: {e}")

    def show_status(self):
        """显示当前状态"""
        print("=" * 50)
        print("GameBoost 当前状态")
        print("=" * 50)
        print(f"活跃游戏: {len(self.active_games)} 个")
        for pid, info in self.active_games.items():
            try:
                proc = psutil.Process(pid)
                current_priority = PRIORITY_NAME.get(proc.nice(), "未知")
                print(f"  - {info['name']} (PID={pid}) 当前优先级: {current_priority}")
            except:
                print(f"  - {info['name']} (PID={pid}) 进程已退出")
        print(f"已降低后台进程: {len(self.lowered_processes)} 个")
        print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="GameBoost - 游戏进程优先级自动设置工具")
    parser.add_argument("--once", action="store_true", help="单次检测优化")
    parser.add_argument("--pid", type=int, help="手动优化指定 PID")
    parser.add_argument("--status", action="store_true", help="查看当前状态")
    parser.add_argument("--restore", action="store_true", help="恢复所有进程")
    parser.add_argument("--config", type=str, help="配置文件路径")
    args = parser.parse_args()

    gb = GameBoost(args.config)

    if args.status:
        # 先检测一次
        gb.check_games()
        gb.show_status()
    elif args.restore:
        gb.restore_all()
        print("所有进程已恢复")
    elif args.pid:
        gb.boost_pid(args.pid)
    elif args.once:
        gb.run_once()
    else:
        gb.run_monitor()


if __name__ == "__main__":
    main()
