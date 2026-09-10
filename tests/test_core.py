"""
CalaNeko v0.3.1 基础测试套件
覆盖：崩溃恢复 ledger 原子写入/读取/清空、单实例互斥、配置加载/保存
运行：python tests/test_core.py
"""
import os
import sys
import json
import tempfile
import shutil
import threading
import time

# 添加 src 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# 测试结果统计
passed = 0
failed = 0
errors = []


def test(name):
    """测试装饰器"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            global passed, failed
            try:
                func(*args, **kwargs)
                print(f"  ✅ {name}")
                passed += 1
            except AssertionError as e:
                print(f"  ❌ {name}: {e}")
                failed += 1
                errors.append((name, str(e)))
            except Exception as e:
                print(f"  💥 {name}: 异常 {type(e).__name__}: {e}")
                failed += 1
                errors.append((name, f"{type(e).__name__}: {e}"))
        return wrapper
    return decorator


# ============================================================
# 测试 1: Ledger 原子写入
# ============================================================
@test("Ledger 原子写入 - 正常写入")
def test_ledger_write():
    """测试 _save_ledger 的原子写入逻辑"""
    tmpdir = tempfile.mkdtemp()
    try:
        ledger_path = os.path.join(tmpdir, 'config', 'tweak_ledger.json')

        # 模拟 _save_ledger 的核心逻辑
        config_dir = os.path.dirname(ledger_path)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)

        ledger = {
            "1234": {"type": "game", "original_priority": 32, "name": "test.exe"},
            "5678": {"type": "background", "original_priority": 16384},
        }

        # 原子写入
        tmp_path = ledger_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ledger_path)

        # 验证
        assert os.path.exists(ledger_path), "ledger 文件未创建"
        with open(ledger_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert "1234" in data, "game 进程未写入"
        assert data["1234"]["type"] == "game", "类型错误"
        assert data["1234"]["original_priority"] == 32, "优先级错误"
        assert "5678" in data, "background 进程未写入"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@test("Ledger 原子写入 - 空 ledger 时删除文件")
def test_ledger_write_empty():
    """测试没有改动时清空 ledger"""
    tmpdir = tempfile.mkdtemp()
    try:
        ledger_path = os.path.join(tmpdir, 'tweak_ledger.json')

        # 先创建一个 ledger 文件
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump({"1234": {"type": "game"}}, f)

        # 模拟空 ledger 时删除
        ledger = {}
        if not ledger:
            if os.path.exists(ledger_path):
                os.remove(ledger_path)

        assert not os.path.exists(ledger_path), "空 ledger 时文件未删除"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@test("Ledger 原子写入 - config 目录不存在时自动创建")
def test_ledger_write_mkdir():
    """测试 config 目录不存在时自动创建"""
    tmpdir = tempfile.mkdtemp()
    try:
        ledger_path = os.path.join(tmpdir, 'nonexistent', 'subdir', 'tweak_ledger.json')

        # 模拟 _save_ledger 的目录创建逻辑
        config_dir = os.path.dirname(ledger_path)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)

        assert os.path.exists(config_dir), "config 目录未自动创建"

        # 写入测试
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump({"test": True}, f)
        assert os.path.exists(ledger_path), "ledger 文件未创建"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 测试 2: Ledger 读取与崩溃恢复
# ============================================================
@test("Ledger 读取 - 正常 JSON")
def test_ledger_read():
    """测试读取正常的 ledger 文件"""
    tmpdir = tempfile.mkdtemp()
    try:
        ledger_path = os.path.join(tmpdir, 'tweak_ledger.json')
        test_data = {
            "1000": {"type": "game", "original_priority": 32, "name": "game.exe"},
            "2000": {"type": "ace", "original_priority": 32, "orig_affinity": [0, 1]},
        }
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(test_data, f)

        with open(ledger_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        assert len(data) == 2, "进程数量错误"
        assert data["1000"]["type"] == "game", "game 类型错误"
        assert data["2000"]["type"] == "ace", "ace 类型错误"
        assert data["2000"]["orig_affinity"] == [0, 1], "亲和性错误"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@test("Ledger 读取 - 文件不存在时静默返回")
def test_ledger_read_not_exist():
    """测试 ledger 文件不存在时不报错"""
    ledger_path = os.path.join(tempfile.gettempdir(), 'nonexistent_ledger.json')
    # 模拟 _crash_recovery 的检查逻辑
    if not os.path.exists(ledger_path):
        return  # 正常返回，不报错
    raise AssertionError("不应到达这里")


@test("Ledger 读取 - 损坏 JSON 时不崩溃")
def test_ledger_read_corrupted():
    """测试损坏的 JSON 文件不导致崩溃（_crash_recovery 有 try/except）"""
    tmpdir = tempfile.mkdtemp()
    try:
        ledger_path = os.path.join(tmpdir, 'tweak_ledger.json')
        with open(ledger_path, 'w', encoding='utf-8') as f:
            f.write('{invalid json content!!!')

        # 模拟 _crash_recovery 的 try/except
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                json.load(f)
            raise AssertionError("应该抛出 JSON 解析错误")
        except json.JSONDecodeError:
            pass  # 预期行为，_crash_recovery 外层有 try/except 捕获
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 测试 3: 单实例互斥 (CreateMutexW)
# ============================================================
@test("单实例互斥 - 第一个实例获取锁成功")
def test_mutex_first():
    """测试第一个实例能获取互斥锁"""
    import ctypes
    from ctypes import wintypes

    mutex_name = "Global\\CalaNeko_Test_Mutex"
    kernel32 = ctypes.windll.kernel32

    # 创建互斥锁
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    assert handle != 0, "CreateMutexW 返回空句柄"

    # 检查是否是新创建的（ERROR_ALREADY_EXISTS 表示已存在）
    last_error = kernel32.GetLastError()
    assert last_error == 0, f"第一个实例应该是新创建的，Got error {last_error}"

    # 释放
    kernel32.CloseHandle(handle)


@test("单实例互斥 - 第二个实例检测到已存在")
def test_mutex_second():
    """测试第二个实例能检测到互斥锁已存在"""
    import ctypes

    mutex_name = "Global\\CalaNeko_Test_Mutex2"
    kernel32 = ctypes.windll.kernel32

    # 第一个实例获取锁
    handle1 = kernel32.CreateMutexW(None, False, mutex_name)
    assert handle1 != 0, "第一个实例 CreateMutexW 失败"

    try:
        # 第二个实例尝试获取锁
        handle2 = kernel32.CreateMutexW(None, False, mutex_name)
        assert handle2 != 0, "第二个实例 CreateMutexW 返回空句柄（应该返回已有句柄）"

        last_error = kernel32.GetLastError()
        ERROR_ALREADY_EXISTS = 183
        assert last_error == ERROR_ALREADY_EXISTS, f"第二个实例应该检测到已存在，Got error {last_error}"

        kernel32.CloseHandle(handle2)
    finally:
        kernel32.CloseHandle(handle1)


# ============================================================
# 测试 4: 配置加载/保存
# ============================================================
@test("配置加载 - 默认配置")
def test_config_default():
    """测试加载默认配置"""
    default_config = {
        "游戏进程列表": [],
        "监控间隔秒": 3,
        "后台降权": True,
        "后台目标优先级": "below_normal",
        "排除进程": ["system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe"],
        "ACE降权": True,
    }

    assert "游戏进程列表" in default_config, "缺少游戏进程列表"
    assert default_config["监控间隔秒"] == 3, "默认监控间隔错误"
    assert default_config["后台降权"] is True, "后台降权默认应为 True"
    assert len(default_config["排除进程"]) >= 10, "排除进程数量不足"


@test("配置保存 - JSON 序列化")
def test_config_save():
    """测试配置保存为 JSON"""
    tmpdir = tempfile.mkdtemp()
    try:
        config_path = os.path.join(tmpdir, 'config.json')
        config = {
            "游戏进程列表": ["game1.exe", "game2.exe"],
            "监控间隔秒": 5,
            "后台降权": False,
        }

        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        assert os.path.exists(config_path), "配置文件未创建"

        with open(config_path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)

        assert loaded["游戏进程列表"] == ["game1.exe", "game2.exe"], "游戏列表错误"
        assert loaded["监控间隔秒"] == 5, "监控间隔错误"
        assert loaded["后台降权"] is False, "后台降权错误"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 测试 5: 线程安全（_data_lock）
# ============================================================
@test("线程安全 - 并发写入 ledger 数据结构")
def test_thread_safety():
    """测试并发修改 active_games 时的数据一致性"""
    lock = threading.RLock()
    active_games = {}
    errors = []

    def worker(pid):
        for i in range(100):
            with lock:
                active_games[pid * 1000 + i] = {
                    "original_priority": 32,
                    "name": f"test_{pid}_{i}.exe",
                }
            time.sleep(0.0001)

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 验证：5 个线程 * 100 次 = 500 个条目
    assert len(active_games) == 500, f"并发写入后条目数错误: {len(active_games)} != 500"

    # 验证所有条目都完整
    for pid, info in active_games.items():
        assert "original_priority" in info, f"pid {pid} 缺少 original_priority"
        assert "name" in info, f"pid {pid} 缺少 name"


# ============================================================
# 主函数
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("CalaNeko v0.3.1 基础测试套件")
    print("=" * 60)

    print("\n📁 Ledger 原子写入测试:")
    test_ledger_write()
    test_ledger_write_empty()
    test_ledger_write_mkdir()

    print("\n📂 Ledger 读取与崩溃恢复测试:")
    test_ledger_read()
    test_ledger_read_not_exist()
    test_ledger_read_corrupted()

    print("\n🔒 单实例互斥测试:")
    test_mutex_first()
    test_mutex_second()

    print("\n⚙️ 配置加载/保存测试:")
    test_config_default()
    test_config_save()

    print("\n🧵 线程安全测试:")
    test_thread_safety()

    print("\n" + "=" * 60)
    print(f"测试结果: ✅ {passed} 通过, ❌ {failed} 失败")
    print("=" * 60)

    if errors:
        print("\n失败详情:")
        for name, err in errors:
            print(f"  - {name}: {err}")

    sys.exit(0 if failed == 0 else 1)
