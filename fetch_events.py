#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Фид календаря групповых событий VRChat для мира 5ul.

Логинится бот-аккаунтом в VRChat API, забирает события календаря каждой
группы из groups.json (текущий + следующий месяц), обрезает лишнее и пишет
компактный docs/events.json для GitHub Pages. Мир читает его через
VRCStringDownloader (*.github.io — доверенный домен String Loading).

Секреты (env): VRC_USERNAME, VRC_PASSWORD, VRC_CONTACT, VRC_SESSION_KEY.
VRC_TOTP_SECRET необязателен; без него вход подтверждается VRC_EMAIL_CODE.
Сессия шифруется в state/cookies.enc и переиспользуется между
запусками — повторные логины съедают лимит одновременных сессий VRChat.

Правила игры с неофициальным API: внятный User-Agent с контактом,
темп ~1 запрос в несколько секунд, бэкофф на 429. Нарушение — риск бана.

v1.0.1
"""

import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pyotp
import requests

from session_state import load_cookies, save_cookies, session_cipher

API = "https://api.vrchat.cloud/api/1"
STATE_DIR = "state"
COOKIES_PATH = os.path.join(STATE_DIR, "cookies.enc")
OUT_PATH = os.path.join("docs", "events.json")
GROUPS_PATH = "groups.json"

USERNAME = os.environ.get("VRC_USERNAME", "")
PASSWORD = os.environ.get("VRC_PASSWORD", "")
TOTP_SECRET = os.environ.get("VRC_TOTP_SECRET", "")
EMAIL_CODE = os.environ.get("VRC_EMAIL_CODE", "").strip()
LOGIN_ACTION = os.environ.get("VRC_LOGIN_ACTION", "auto") or "auto"
CONTACT = os.environ.get("VRC_CONTACT", "no-contact-set@example.com")
SESSION_KEY = os.environ.get("VRC_SESSION_KEY", "")

USER_AGENT = "FiveUlCalendarFeed/1.0 " + CONTACT

# Горизонт: прошедшие события старше 3 часов выбрасываем, дальше 35 дней — тоже.
PAST_GRACE = timedelta(hours=3)
HORIZON = timedelta(days=35)
DESC_LIMIT = 280
HEARTBEAT = timedelta(hours=6)  # обновлять gen даже без изменений — маркер живости фида
# Мир парсит максимум 96 событий (MAX_EVENTS контроллера). Режем глобально по
# дате начала, иначе при переполнении клиент молча терял бы ПОСЛЕДНИЕ группы
# списка целиком, а не самые дальние по времени события.
TOTAL_CAP = 90

# Символы, запечённые в TMP-атлас мира (см. CalendarSetup.CAL_CHARS).
# Всё вне набора заменяем пробелом, чтобы в панели не было «квадратов».
ALLOWED_CHARS = set(
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789 .,:;!?()[]{}+-<>=/*%\"'#&_|\\—–«»→•№…\n"
)


def log(msg):
    print("[feed] " + msg, flush=True)


def die(msg):
    log("ОШИБКА: " + msg)
    sys.exit(1)


def clean_text(text, limit, keep_newlines):
    if not text:
        return ""
    out = []
    for ch in text:
        if ch == "\r":
            continue
        if ch == "\n" and not keep_newlines:
            ch = " "
        out.append(ch if ch in ALLOWED_CHARS else " ")
    result = "".join(out)
    while "  " in result:
        result = result.replace("  ", " ")
    result = result.strip()
    if len(result) > limit:
        result = result[: limit - 1].rstrip() + "…"
    return result


# ---------------------------------------------------------------- сессия --

def load_session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    cookies = load_cookies(COOKIES_PATH, SESSION_KEY, USERNAME)
    for name, value in cookies.items():
        s.cookies.set(name, value, domain="api.vrchat.cloud")
    if cookies:
        log("encrypted session restored")
    return s


def save_session(s):
    save_cookies(COOKIES_PATH, SESSION_KEY, USERNAME,
                 requests.utils.dict_from_cookiejar(s.cookies))


def session_alive(s):
    r = s.get(API + "/auth/user", timeout=30)
    if r.status_code == 401:
        return False
    r.raise_for_status()
    body = r.json()
    return (isinstance(body, dict) and "requiresTwoFactorAuth" not in body
            and str(body.get("id", "")).startswith("usr_"))


def login_failure_reason(response):
    # Classify known responses without putting arbitrary server text in logs.
    try:
        body = response.json()
    except ValueError:
        return "non-json-response"
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message.strip().casefold() == "invalid username/email or password":
        return "invalid-credentials"
    return "unclassified-auth-rejection"


def login(s, action="auto"):
    if not USERNAME or not PASSWORD:
        die("VRC_USERNAME/VRC_PASSWORD не заданы (secrets репозитория)")

    if action == "verify-email":
        # Continue the cached challenge without another password login/email.
        if not requests.utils.dict_from_cookiejar(s.cookies).get("auth"):
            die("Нет сохранённого входа. Сначала запусти request-email.")
        if len(EMAIL_CODE) != 6 or not EMAIL_CODE.isascii() or not EMAIL_CODE.isdigit():
            die("Добавь шестизначный код из письма в GitHub Secret VRC_EMAIL_CODE.")
        r = s.post(API + "/auth/twofactorauth/emailotp/verify",
                   json={"code": EMAIL_CODE}, timeout=30)
        if r.status_code != 200 or r.json().get("verified") is not True:
            die("Код из письма не принят (HTTP " + str(r.status_code) +
                "). Проверь последний код; если истёк, запусти request-email.")
        if not session_alive(s):
            die("Код принят, но сессия не подтверждена. Публикация остановлена.")
        save_session(s)
        log("email verification successful; encrypted session saved")
        return True

    if action != "request-email" and not TOTP_SECRET:
        die("Нужен вход по почте: Run workflow > request-email. "
            "Автоматическая отправка новых писем отключена.")
    if USERNAME != USERNAME.strip() or "\r" in USERNAME or "\n" in USERNAME:
        die("VRC_USERNAME содержит лишние пробелы или перенос строки. Исправь секрет, не отправляя его в чат.")
    if "\r" in PASSWORD or "\n" in PASSWORD:
        die("VRC_PASSWORD содержит перенос строки. Вставь пароль одной строкой без кавычек.")
    if action == "request-email":
        s.cookies.clear()
    log("логин по паролю (кука отсутствует или протухла)")
    creds = quote(USERNAME, safe="") + ":" + quote(PASSWORD, safe="")
    auth = base64.b64encode(creds.encode("utf-8")).decode("ascii")
    r = s.get(API + "/auth/user",
              headers={"Authorization": "Basic " + auth}, timeout=30)
    if r.status_code == 401:
        die("HTTP 401: " + login_failure_reason(r) +
            ". Проверь вход теми же данными на сайте VRChat.")
    r.raise_for_status()
    body = r.json()
    if "requiresTwoFactorAuth" in body:
        methods = body["requiresTwoFactorAuth"]
        if "emailOtp" in methods:
            if not requests.utils.dict_from_cookiejar(s.cookies).get("auth"):
                die("VRChat не вернул сессию для кода из письма; публикация остановлена.")
            save_session(s)
            message = ("VRChat запросил код из письма. Добавь его в Repository secret "
                       "VRC_EMAIL_CODE и запусти Run workflow > verify-email. "
                       "Код не вводить в чат, файлы или поля Run workflow.")
            log("EMAIL_VERIFICATION_REQUIRED: " + message)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as stream:
                    stream.write("## Подтверждение входа по почте\n\n" + message + "\n")
            return False
        if "totp" not in methods:
            die("Требуется другой способ подтверждения аккаунта; публикация остановлена.")
        if not TOTP_SECRET:
            die("нужен VRC_TOTP_SECRET (base32-секрет из настройки 2FA)")
        code = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
        r2 = s.post(API + "/auth/twofactorauth/totp/verify",
                    json={"code": code}, timeout=30)
        if r2.status_code != 200:
            die("TOTP не принят (HTTP " + str(r2.status_code) + ")")
        if not session_alive(s):
            die("после 2FA сессия так и не поднялась")
    save_session(s)
    log("логин успешен, кука сохранена")
    return True


def authenticate(s, action):
    if action not in ("auto", "request-email", "verify-email"):
        die("Неизвестный режим входа.")
    # Verify first: probing /auth/user must not replace the pending challenge.
    if action == "verify-email":
        return login(s, action)
    if session_alive(s):
        log("сессия жива, логин не нужен")
        save_session(s)
        return True
    return login(s, action)


def workflow_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(name + "=" + value + "\n")


# ------------------------------------------------------------- календарь --

def month_start(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def fetch_group_month(s, group_id, month_dt):
    """Одна страница календаря группы за месяц. None => 401 (нужен релогин)."""
    params = {
        "date": month_dt.strftime("%Y-%m-%dT00:00:00.000Z"),
        "n": 100,
    }
    for attempt in (1, 2):
        r = s.get(API + "/calendar/" + group_id, params=params, timeout=30)
        if r.status_code == 401:
            return None
        if r.status_code == 429:
            if attempt == 1:
                log("429 от API — пауза 65 с")
                time.sleep(65)
                continue
            die("повторный 429 — снизьте частоту cron")
        if r.status_code == 404:
            log("группа " + group_id + " не найдена (404) — проверьте id")
            return []
        r.raise_for_status()
        return r.json().get("results", [])
    return []


def parse_iso(ts):
    # '2026-07-22T04:00:00.000Z' -> aware datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def collect(s, cfg):
    now = datetime.now(timezone.utc)
    m1 = month_start(now)
    m2 = month_start(m1 + timedelta(days=32))
    out_groups = []
    relogged = False

    for grp in cfg["groups"]:
        gid = grp["id"].strip()
        seen = {}
        for month in (m1, m2):
            results = fetch_group_month(s, gid, month)
            if results is None:
                if relogged:
                    die("401 повторился после релогина")
                relogged = True
                if not login(s):
                    die("Сбор остановлен до подтверждения входа; старый фид сохранён.")
                results = fetch_group_month(s, gid, month)
                if results is None:
                    die("401 сразу после успешного логина")
            for ev in results:
                seen[ev.get("id", str(len(seen)))] = ev
            time.sleep(3)  # вежливый темп

        events = []
        for ev in seen.values():
            if ev.get("isDraft"):
                continue
            try:
                starts = parse_iso(ev["startsAt"])
                ends = parse_iso(ev["endsAt"])
            except (KeyError, ValueError):
                continue
            if ends < now - PAST_GRACE:
                continue
            if starts > now + HORIZON:
                continue
            platforms = ev.get("platforms") or []
            events.append({
                "t": clean_text(ev.get("title", ""), 80, False),
                "d": clean_text(ev.get("description", ""), DESC_LIMIT, True),
                "s": int(starts.timestamp()),
                "e": int(ends.timestamp()),
                "acc": ev.get("accessType", "public"),
                "cnt": int(ev.get("interestedUserCount") or 0),
                "q": 1 if "android" in platforms else 0,
            })
        events.sort(key=lambda e: e["s"])
        out_groups.append({
            "id": gid,
            "name": grp.get("name", gid),
            "events": events,
        })
        log(gid + ": событий в горизонте — " + str(len(events)))

    # Глобальный кап: оставляем TOTAL_CAP ближайших событий по всем группам
    total = sum(len(g["events"]) for g in out_groups)
    if total > TOTAL_CAP:
        flat = []
        for g in out_groups:
            for ev in g["events"]:
                flat.append((ev["s"], g["id"], ev))
        flat.sort(key=lambda t: t[0])
        keep = flat[:TOTAL_CAP]
        kept_by_group = {}
        for _, gid2, ev in keep:
            kept_by_group.setdefault(gid2, []).append(ev)
        for g in out_groups:
            g["events"] = kept_by_group.get(g["id"], [])
        log("глобальный кап: " + str(total) + " → " + str(TOTAL_CAP) +
            " (обрезаны самые дальние по дате)")

    return out_groups


# ------------------------------------------------------------------ main --

def content_hash(groups):
    return hashlib.sha256(
        json.dumps(groups, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def main():
    workflow_output("publish", "false")
    missing = [name for name in ("VRC_USERNAME", "VRC_PASSWORD", "VRC_CONTACT", "VRC_SESSION_KEY")
               if not os.environ.get(name, "").strip()]
    if missing:
        die("Missing GitHub Secrets: " + ", ".join(missing))
    session_cipher(SESSION_KEY)
    with open(GROUPS_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("groups"):
        die("groups.json пуст — добавьте хотя бы одну группу")

    s = load_session()
    if not authenticate(s, LOGIN_ACTION):
        return

    groups = collect(s, cfg)
    now_unix = int(time.time())
    payload = {"gen": now_unix, "groups": groups}

    # Не трогаем файл, если события не изменились и heartbeat свежий —
    # меньше пустых коммитов в репозитории.
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            same = content_hash(old.get("groups", [])) == content_hash(groups)
            fresh = now_unix - int(old.get("gen", 0)) < HEARTBEAT.total_seconds()
            if same and fresh:
                log("изменений нет, heartbeat свежий — файл не переписываем")
                workflow_output("publish", "true")
                return
        except Exception:  # noqa: BLE001
            pass

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    total = sum(len(g["events"]) for g in groups)
    log("записан " + OUT_PATH + ": групп " + str(len(groups)) +
        ", событий " + str(total))
    workflow_output("publish", "true")


if __name__ == "__main__":
    main()
