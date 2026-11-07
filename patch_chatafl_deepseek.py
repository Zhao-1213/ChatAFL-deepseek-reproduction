#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 ChatAFL 的 chat-llm.c 从 OpenAI 接口改成 DeepSeek 接口。
用法: python3 patch_chatafl_deepseek.py <chat-llm.c路径> [更多文件...]
改动内容:
  1. API 地址 api.openai.com -> api.deepseek.com (统一为 /chat/completions)
  2. 模型名 gpt-3.5-turbo(-instruct) -> deepseek-chat
  3. instruct 分支请求体: 旧版 "prompt" -> chat 版 "messages"
  4. instruct 分支响应解析: choices[0].text -> choices[0].message.content
"""
import re
import shutil
import sys


def patch(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        s = f.read()
    orig = s

    # 1) API 地址 -> DeepSeek
    s = s.replace('https://api.openai.com/v1/completions',
                  'https://api.deepseek.com/chat/completions')
    s = s.replace('https://api.openai.com/v1/chat/completions',
                  'https://api.deepseek.com/chat/completions')

    # 2) 模型名 -> deepseek-chat（先替换长名，再替换短名）
    s = s.replace('gpt-3.5-turbo-instruct', 'deepseek-chat')
    s = s.replace('gpt-3.5-turbo', 'deepseek-chat')

    # 3) instruct 分支请求体: "prompt" -> "messages"
    s = s.replace(r'\"prompt\": \"%s\"',
                  r'\"messages\": [{\"role\": \"user\", \"content\": \"%s\"}]')

    # 4) instruct 分支响应解析: text -> message.content
    s = re.sub(
        r'json_object \*jobj4 = json_object_object_get\(first_choice, "text"\);[ \t]*\r?\n[ \t]*data = json_object_get_string\(jobj4\);',
        'json_object *jobj4 = json_object_object_get(first_choice, "message");\n'
        '                    json_object *jobj5 = json_object_object_get(jobj4, "content");\n'
        '                    data = json_object_get_string(jobj5);',
        s, count=1,
    )

    if s == orig:
        print('[未改动] ' + path)
        return

    shutil.copyfile(path, path + '.orig')   # 备份原文件
    with open(path, 'w', encoding='utf-8') as f:
        f.write(s)
    print('[已补丁] ' + path + '  (备份: ' + path + '.orig)')

    leftover = re.findall(r'api\.openai\.com|gpt-3\.5', s)
    if leftover:
        print('  警告: 仍残留 openai/gpt 引用: ' + str(leftover))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python3 patch_chatafl_deepseek.py <chat-llm.c> [更多...]')
        sys.exit(1)
    for p in sys.argv[1:]:
        patch(p)
