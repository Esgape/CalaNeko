import base64

b64 = open(r'D:\aaa GPT local\GameBoost\assets\favicon_b64.txt').read().strip()
src_path = r'D:\aaa GPT local\GameBoost\src\game_boost_web.py'
src = open(src_path, encoding='utf-8').read()

title_tag = '<title>CalaNeko - 游戏进程优化工具</title>'
assert title_tag in src, 'title 未找到'
link = title_tag + '\n<link rel="icon" type="image/png" href="data:image/png;base64,' + b64 + '">'
src = src.replace(title_tag, link, 1)

assert 'v0.2.5' in src, 'v0.2.5 未找到'
src = src.replace('v0.2.5', 'v0.2.6', 1)

open(src_path, 'w', encoding='utf-8').write(src)
print('✅ favicon 已嵌入 + 版本号升 v0.2.6')
print('   link 存在:', 'data:image/png;base64' in src)
