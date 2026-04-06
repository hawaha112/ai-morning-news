#!/usr/bin/env python3
"""
Twitter/X 登录工具 — 生成 twikit cookies 文件

用法:
    python3 twitter_login.py

首次运行会要求输入 Twitter 用户名、邮箱和密码，
登录成功后保存 cookies 到 twitter_cookies.json。
后续 fetch_news.py 会自动读取这个文件。

注意：
- 如果开启了两步验证(2FA)，会提示输入验证码
- cookies 有效期通常较长，但如果失效需重新运行此脚本
- 请勿将 twitter_cookies.json 提交到 git
"""

import asyncio
import getpass
import json
import os
import sys


async def main():
    try:
        from twikit import Client
    except ImportError:
        print("❌ twikit 未安装，请运行: pip install twikit")
        sys.exit(1)

    cookies_file = "twitter_cookies.json"

    if os.path.exists(cookies_file):
        print(f"⚠️ {cookies_file} 已存在")
        overwrite = input("是否覆盖? (y/N): ").strip().lower()
        if overwrite != 'y':
            print("已取消")
            return

    print("=== Twitter/X 登录 ===\n")
    username = input("用户名 (screen name, 不带@): ").strip()
    email = input("邮箱: ").strip()
    password = getpass.getpass("密码: ")

    client = Client('en-US')

    print("\n正在登录...")
    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )
    except Exception as e:
        if '2fa' in str(e).lower() or 'challenge' in str(e).lower():
            print("\n需要两步验证:")
            totp_code = input("请输入 2FA 验证码: ").strip()
            try:
                await client.login(
                    auth_info_1=username,
                    auth_info_2=email,
                    password=password,
                    totp_code=totp_code,
                )
            except Exception as e2:
                print(f"\n❌ 登录失败: {e2}")
                sys.exit(1)
        else:
            print(f"\n❌ 登录失败: {e}")
            sys.exit(1)

    client.save_cookies(cookies_file)
    print(f"\n✅ 登录成功! cookies 已保存到 {cookies_file}")
    print(f"   fetch_news.py 现在可以抓取 Twitter 源了")
    print(f"\n⚠️ 请确保 {cookies_file} 已在 .gitignore 中")


if __name__ == '__main__':
    asyncio.run(main())
