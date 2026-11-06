#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae 每日签到脚本（GitHub Actions 版）— 支持自动续期

凭证链路（推荐 TRAE_PASSPORT，全自动换发）：
  TRAE_PASSPORT (sid_guard 等 Passport Cookie, ~60 天)
      → POST /cloudide/api/v3/trae/Login        换发全新 X-Cloudide-Session (14 天)
      → POST /cloudide/api/v3/common/GetUserToken  换取 JWT (8 小时)
      → POST /trae/api/v2/ug/checkin_credits/claim  执行签到

兼容旧部署：仅有 TRAE_SESSION（X-Cloudide-Session，~14 天）时按旧链路直连。

依赖：仅标准库，无第三方依赖。

环境变量：
  TRAE_PASSPORT        账号 1 的 Passport Cookie（推荐，如 "sid_guard=xxx; passport_csrf_token=xxx"）
  TRAE_SESSION         账号 1 的 X-Cloudide-Session Cookie（旧方式；PASSPORT 存在时优先 PASSPORT）
  TRAE_DEVICE_ID       账号 1 的 x-device-id，16 位数字（选填，缺省随机）
  TRAE_PASSPORT_N      第 N(N≥2) 个账号的 Passport Cookie；与 TRAE_SESSION_N 至少其一
  TRAE_SESSION_N       第 N 个账号的 X-Cloudide-Session
  TRAE_DEVICE_ID_N     第 N 个账号的 x-device-id（选填）
  FEISHU_WEBHOOK       全部账号共享（选填，签到后推送一条汇总）

用法：
  python checkin.py
"""

import datetime
import json
import os
import random
import re
import sys
import time
import urllib.request

BASE = "https://api.trae.cn"


def _post(path, headers, body=""):
    """POST 请求。非 2xx 不抛 HTTPError，而是以 (status, body) 正常返回，便于上层判断原因。"""
    import urllib.error
    req = urllib.request.Request(BASE + path, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def login_for_session(passport_cookie: str) -> str:
    """用 Passport Cookie（sid_guard 等）调 /cloudide/api/v3/trae/Login，
    换发全新 X-Cloudide-Session（每次运行换发，彻底规避 14 天过期）。"""
    import urllib.error
    req = urllib.request.Request(
        BASE + "/cloudide/api/v3/trae/Login",
        data=b"{}",
        headers={
            "Cookie": passport_cookie,
            "Referer": "https://www.trae.cn/",
            "Origin": "https://www.trae.cn",
            "Content-Type": "application/json",
            "User-Agent": "TraeCheckin/1.0",
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 401:
            raise RuntimeError(
                "Passport Cookie 已失效(HTTP 401)：sid_guard 约 60 天有效期可能已过。"
                "请重新登录 trae.cn 并更新 TRAE_PASSPORT。原始返回: " + body[:200])
        raise RuntimeError("Login 换发 Session 失败: HTTP %s %s" % (e.code, body[:200]))
    if status != 200:
        raise RuntimeError("Login 换发 Session 失败: HTTP %s %s" % (status, text[:200]))
    for c in set_cookies:
        m = re.search(r"X-Cloudide-Session=([^;]+)", c)
        if m:
            return m.group(1)
    raise RuntimeError("Login 响应未包含 X-Cloudide-Session: " + text[:200])


def get_token(session: str) -> str:
    """用 X-Cloudide-Session Cookie 换取全新 JWT。"""
    headers = {
        "Cookie": "X-Cloudide-Session=" + session,
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
        "User-Agent": "TraeCheckin/1.0",
        "Accept": "application/json, text/plain, */*",
    }
    status, text = _post("/cloudide/api/v3/common/GetUserToken", headers)
    data = json.loads(text)
    token = (data.get("Result") or {}).get("Token")
    if status == 401:
        raise RuntimeError(
            "账号会话已失效(HTTP 401)：X-Cloudide-Session 可能已过期(~14天有效期)。"
            "原始返回: " + text[:200])
    if status != 200 or not token:
        raise RuntimeError("GetUserToken 失败: HTTP %s %s" % (status, text[:200]))
    return token


def checkin(token: str, device_id: str) -> dict:
    """执行每日签到（claim）。"""
    headers = {
        "Authorization": "Cloud-IDE-JWT " + token,
        "X-User-Region": "cn",
        "x-device-id": device_id,
        "Content-Type": "application/json",
        "User-Agent": "TraeCheckin/1.0",
    }
    status, text = _post("/trae/api/v2/ug/checkin_credits/claim", headers, "{}")
    try:
        return {"http": status, "body": json.loads(text)}
    except json.JSONDecodeError:
        return {"http": status, "body": {"raw": text}}


def notify_feishu(webhook, text):
    """向飞书机器人推送一条文本消息；webhook 为空则跳过。返回 HTTP 状态码，失败返回 None。"""
    if not webhook:
        return None
    try:
        payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception:
        return None


def beijing_now_str():
    """返回北京时间字符串（GitHub Actions 运行在 UTC，需 +8 小时）。"""
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def iter_accounts():
    """按顺序产出 (账号序号, passport, session, device_id)。
    账号 1 读 TRAE_PASSPORT / TRAE_SESSION；之后依次读 TRAE_PASSPORT_N / TRAE_SESSION_N …
    直到某序号的两种凭证都缺空为止。"""
    n = 1
    while True:
        passport = os.environ.get("TRAE_PASSPORT" if n == 1 else "TRAE_PASSPORT_%d" % n, "").strip()
        session = os.environ.get("TRAE_SESSION" if n == 1 else "TRAE_SESSION_%d" % n, "").strip()
        device_id = os.environ.get("TRAE_DEVICE_ID" if n == 1 else "TRAE_DEVICE_ID_%d" % n, "").strip()
        if not passport and not session:
            break
        yield n, passport, session, device_id
        n += 1


def random_device_id():
    """随机生成 16 位数字风控设备号（仅在缺省时兜底）。"""
    return str(random.randint(10**15, 10**16 - 1))


def main():
    accounts = list(iter_accounts())
    if not accounts:
        print("错误：缺少环境变量 TRAE_PASSPORT 或 TRAE_SESSION")
        sys.exit(1)

    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    ok_names, fail_names = [], []
    all_ok = True

    for index, passport, session, device_id in accounts:
        # 多账号之间错开 3~6 秒随机间隔，降低请求密度，规避 9074「参与用户太多」风控
        if index > 1:
            time.sleep(random.uniform(3, 6))
        name = "账号 %d" % index
        device_id = device_id or random_device_id()
        print("[%s] device_id=%s" % (name, device_id))
        try:
            # 优先用 Passport Cookie 换发全新 Session（每次运行都换发，规避 14 天过期）
            if passport:
                session = login_for_session(passport)
                print("[%s] 已由 Passport Cookie 换发新 X-Cloudide-Session" % name)
            token = get_token(session)
            print("[%s] 已换取新 JWT，长度=%d" % (name, len(token)))
            result = checkin(token, device_id)
            body = result["body"]
            code = body.get("code", -1)
            # 9074「参与用户太多」= 设备号被风控标记；换全新设备号自动重试（最多 5 次）
            attempt = 1
            while code == 9074 and attempt < 5:
                device_id = random_device_id()
                attempt += 1
                print("[%s] 命中风控 9074，换新设备号重试（第 %d 次）" % (name, attempt))
                time.sleep(random.uniform(0.8, 1.5))
                result = checkin(token, device_id)
                body = result["body"]
                code = body.get("code", -1)
            checked = body.get("checked_in", False)
            ok = (result["http"] == 200) and (code == 0 or checked)
            credits = body.get("credits", 0)
            if ok:
                print("[%s] 签到成功，本次获得：%s 积分" % (name, credits))
                ok_names.append(name)
            else:
                reason = body.get("message") or ("HTTP %s" % result["http"])
                print("[%s] 签到失败：%s" % (name, reason))
                fail_names.append(name)
                all_ok = False
        except Exception as e:
            print("[%s] 签到异常: %s" % (name, e))
            fail_names.append(name)
            all_ok = False

    # 汇总一条飞书推送（无论成功/失败都汇总，webhook 为空则跳过）
    summary = ["Trae 多账号签到结果", "时间：%s" % beijing_now_str()]
    if ok_names:
        summary.append("成功：" + "、".join(ok_names))
    if fail_names:
        summary.append("失败：" + "、".join(fail_names))
    if webhook and (ok_names or fail_names):
        notify_feishu(webhook, "\n".join(summary))

    if not all_ok:
        sys.exit(1)
    print("全部账号签到完成")


if __name__ == "__main__":
    main()
