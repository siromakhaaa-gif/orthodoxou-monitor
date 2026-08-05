"""Моніторинг наявності місць на порон Orthodoxou / CertusBook."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://orthodoxou-booking.certusonline.com/"
STATE_FILE = Path(__file__).parent / "state.json"

# колір кружечка -> (код, підпис)
LEVELS = {
    "text-success": ("ok", "є місця"),
    "text-warning": ("few", "мало місць"),
    "text-danger": ("none", "немає"),
}
CATEGORIES = {"fa-user": "pax", "fa-car": "vehicle", "fa-bed": "cabin"}
CAT_LABELS = {"pax": "Пасажир", "vehicle": "Авто", "cabin": "Каюта"}
PORTS = {"ATH": "Афіни/Пірей", "PIR": "Пірей", "CYP": "Кіпр", "LMS": "Лімасол"}


def notify(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        print("[!] Telegram не налаштовано, повідомлення лише в консоль:\n" + text)
        return
    for chat in chats:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except Exception as e:
            # один недоступний одержувач не має зривати сповіщення решті
            print(f"[!] Не вдалося надіслати в чат {chat}: {e}")


def _set_select(page, sel, val):
    page.evaluate("([s,v])=>{jQuery(document.querySelector(s)).val(v).trigger('change');}", [sel, val])


def _open_form(page):
    page.goto(BASE, wait_until="networkidle", timeout=60000)


def sailing_days(page, frm, to, months=2):
    """Дні, коли взагалі є рейс (календар сам вимикає порожні дати)."""
    _open_form(page)
    _set_select(page, "#fromPort_0", frm)
    page.wait_for_timeout(3000)
    _set_select(page, "#toPort_0", to)
    page.wait_for_timeout(1500)
    page.click("#toDate_0")
    page.wait_for_timeout(1500)
    days = []
    for _ in range(months):
        days += page.eval_on_selector_all(
            ".bootstrap-datetimepicker-widget .datepicker-days td.day:not(.disabled):not(.old):not(.new)",
            "els=>els.map(e=>e.getAttribute('data-day'))",
        )
        nxt = page.locator(".bootstrap-datetimepicker-widget th.next").locator("visible=true")
        if not nxt.count():
            break
        nxt.first.click()
        page.wait_for_timeout(700)
    return days


def check_date(page, frm, to, day):
    """day = date. Повертає список рейсів зі статусами."""
    _open_form(page)
    _set_select(page, "#fromPort_0", frm)
    page.wait_for_timeout(3000)
    _set_select(page, "#toPort_0", to)
    page.wait_for_timeout(1500)

    page.click("#toDate_0")
    page.wait_for_timeout(1500)
    cell = page.locator(f'td[data-day="{day.strftime("%d/%m/%Y")}"]').locator("visible=true")
    if not cell.count():
        return {"error": "дата поза календарем"}
    if "disabled" in (cell.first.get_attribute("class") or ""):
        return {"trips": [], "note": "рейсів немає"}
    cell.first.click()
    page.wait_for_timeout(600)
    ok = page.locator(".bootstrap-datetimepicker-widget a[data-action='close']").locator("visible=true")
    if ok.count():
        ok.first.click()
    page.wait_for_timeout(600)

    page.locator("button:has-text('ΑΝΑΖΗΤΗΣΗ')").locator("visible=true").first.click()
    try:
        page.wait_for_selector("section[id^=radiotripLine_], #notrip_0", timeout=45000)
    except Exception:
        return {"error": "сторінка результатів не завантажилась"}
    page.wait_for_timeout(2500)

    trips = page.evaluate(
        """() => [...document.querySelectorAll('section[id^=radiotripLine_]')].map(sec => {
            const radio = sec.querySelector('input.radiocheck');
            const parts = (radio?.getAttribute('data') || '').split('|');
            const av = {};
            sec.querySelectorAll('a[data-original-title] .fa-stack').forEach(st => {
                const icon = [...st.querySelectorAll('i')].map(i => i.className).join(' ');
                const cat = ['fa-user','fa-car','fa-bed'].find(c => icon.includes(c));
                if (!cat) return;
                const lvl = ['text-success','text-warning','text-danger'].find(c => icon.includes(c));
                av[cat] = lvl;
            });
            return {
                vessel: parts[7] || sec.innerText.trim().split('\\n')[0],
                dep: parts[9] || '', arr: parts[11] || '',
                price: (sec.querySelector('.select-totals')?.innerText || '').trim(),
                av,
            };
        })"""
    )
    out = []
    for t in trips:
        status = {}
        for icon, cat in CATEGORIES.items():
            lvl = t["av"].get(icon)
            status[cat] = LEVELS.get(lvl, ("unknown", "?"))[0]
        out.append({k: t[k] for k in ("vessel", "dep", "arr", "price")} | {"status": status})
    return {"trips": out}


def fmt_trip(t):
    s = " · ".join(
        f"{CAT_LABELS[c]}: {LEVELS_BY_CODE[t['status'][c]]}" for c in ("pax", "cabin", "vehicle")
    )
    return f"🚢 {t['vessel']} {t['dep']}→{t['arr']} {t['price']}\n   {s}"


LEVELS_BY_CODE = {c: lbl for c, lbl in LEVELS.values()}
LEVELS_BY_CODE["unknown"] = "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default=os.environ.get("ROUTE_FROM", "ATH"))
    ap.add_argument("--to", dest="to", default=os.environ.get("ROUTE_TO", "CYP"))
    ap.add_argument("--dates", default=os.environ.get("DATES", "2026-08-24,2026-08-25,2026-08-26,2026-08-27"))
    ap.add_argument("--watch", default=os.environ.get("WATCH", "pax"),
                    help="категорії через кому: pax,cabin,vehicle")
    ap.add_argument("--list-days", action="store_true", help="показати дні з рейсами і вийти")
    ap.add_argument("--always-notify", action="store_true", help="слати звіт щоразу, не лише при зміні")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    watch = [c.strip() for c in args.watch.split(",") if c.strip()]
    dates = [datetime.strptime(d.strip(), "%Y-%m-%d").date() for d in args.dates.split(",") if d.strip()]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(locale="el-GR")
        page.set_default_timeout(45000)

        if args.list_days:
            print(f"{args.frm}→{args.to}:", sailing_days(page, args.frm, args.to))
            browser.close()
            return

        results = {}
        for d in dates:
            key = f"{args.frm}>{args.to}:{d}"
            try:
                results[key] = check_date(page, args.frm, args.to, d)
            except Exception as e:
                results[key] = {"error": str(e)}
            print(key, json.dumps(results[key], ensure_ascii=False))
        browser.close()

    old = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    alerts, report = [], []

    for key, res in results.items():
        route, ds = key.split(":")
        frm, to = route.split(">")
        head = f"<b>{PORTS.get(frm, frm)} → {PORTS.get(to, to)}, {ds}</b>"
        if res.get("error"):
            report.append(f"{head}\n   ⚠️ {res['error']}")
            continue
        if not res.get("trips"):
            report.append(f"{head}\n   — рейсів немає")
            continue
        report.append(head + "\n" + "\n".join(fmt_trip(t) for t in res["trips"]))

        for i, t in enumerate(res["trips"]):
            prev = ((old.get(key) or {}).get("trips") or [])
            prev_status = prev[i]["status"] if i < len(prev) else {}
            for cat in watch:
                now, was = t["status"].get(cat), prev_status.get(cat)
                if now in ("ok", "few") and was != now:
                    alerts.append(
                        f"🎉 <b>З'явилися місця!</b>\n{PORTS.get(frm, frm)} → {PORTS.get(to, to)}, {ds}\n"
                        f"{t['vessel']} {t['dep']}→{t['arr']} · {t['price']}\n"
                        f"{CAT_LABELS[cat]}: <b>{LEVELS_BY_CODE[now]}</b>\n{BASE}"
                    )

    STATE_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1))

    if alerts:
        notify("\n\n".join(alerts))
    elif args.always_notify:
        notify("Статус без змін:\n\n" + "\n\n".join(report))
    else:
        print("Змін немає.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
