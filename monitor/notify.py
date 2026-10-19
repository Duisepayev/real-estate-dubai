"""Формат уведомления и доставка (Telegram / e-mail)."""
import html
import os
import smtplib
from email.mime.text import MIMEText

from .http import request

VERDICT = {
    "target": "🔥 Кандидат: дисконт к сделкам DLD",
    "review": "🔎 Кандидат на проверку",
    "suspect": "⚠️ Подозрительно низкая цена — вероятна ошибка или неполная стоимость",
}


def m(x):
    """Деньги в AED с пробелами."""
    return f"{x:,.0f}".replace(",", " ") if x is not None else "—"


def pct(x):
    return f"{x * 100:.0f}%" if x is not None else "—"


def format_listing(n, r, cost, history, change=None):
    e = html.escape
    lines = [f"<b>{VERDICT[r['verdict']]}</b>"]
    if change:
        lines.append(f"<i>{e(change)}</i>")
    lines.append(f"🔗 <a href=\"{e(n['url'])}\">{e(n['title'][:90] or 'Объявление')}</a> — Property Finder")
    beds = "студия" if n.get("bedrooms") == 0 else f"{n.get('bedrooms')} сп."
    lines.append(f"📍 {e(n.get('location_full') or '')}")
    lines.append(f"🏠 {e(n.get('property_type') or '')}, {beds}, {n.get('size_sqm') or '?'} м², "
                 f"{'готовый' if n.get('completion') == 'ready' else 'off-plan'}"
                 + (f", мебель: {e(str(n.get('furnished')))}" if n.get("furnished") not in (None, "NO") else ""))
    pps = r.get("ppsqm")
    lines.append(f"💰 Цена: <b>{m(n['price_aed'])} AED</b> ({m(pps)} AED/м²)")

    c = r.get("comps")
    if c:
        lo, mid, hi = r["fair"]
        lines.append(f"📊 Обоснованный диапазон: {m(lo)} – {m(hi)} AED (медиана {m(mid)})")
        lines.append(f"   Дисконт к медиане: <b>{pct(r['discount'])}</b>. Основа: {c['n']} сделок DLD "
                     + (f"в «{e(r['project'])}»" if r.get("basis") != "area" else "района без названия проекта (старые дома; проект объявления в реестре не найден, сравнение грубое)")
                     + f", {c['first']} … {c['last']}, та же спальность, площадь ±25%; "
                     f"медиана {m(c['median'])} AED/м² (25–75%: {m(c['p25'])}–{m(c['p75'])}).")
        for x in r["comp_list"][:3]:
            lines.append(f"   • {x['date']}: {m(x['value_aed'])} AED, {x['size_sqm']:.0f} м², {m(x['ppsqm'])} AED/м²"
                         + (" (off-plan)" if x["offplan"] else ""))
        if c["n"] < 3:
            lines.append("   Сделок мало — оценка ненадёжна.")
    else:
        why = ("сделки Абу-Даби в реестре DLD не публикуются" if (cost or {}).get("emirate") == "abu_dhabi"
               else "проект не сопоставлен с реестром DLD") if not r.get("project") else "нет сделок с похожей площадью и спальностью за 12 мес."
        lines.append(f"📊 Рыночный диапазон по сделкам не рассчитан: {why}.")
    p = r.get("listing_peers")
    if p:
        lines.append(f"   Для ориентира (не сделки): медиана {p['n']} активных объявлений рядом — {m(p['median'])} AED/м², минимум {m(p['min'])}.")

    if r["keywords"]:
        lines.append(f"⏱ Срочность в тексте: {e(', '.join(r['keywords']))} (сами слова скидку не доказывают)")
    else:
        lines.append("⏱ Причина срочности: не указана")
    flags = n.get("pf_flags") or {}
    badges = [t for k, t in (("is_price_drop", "Price drop"), ("is_great_value", "Great value"),
                             ("is_under_offer_by_competitor", "Under offer у другого агентства")) if flags.get(k)]
    if badges:
        lines.append(f"🏷 Метки PF: {', '.join(badges)}")

    if n.get("completion") == "off_plan":
        ho = r.get("handover")
        lines.append(f"🏗 Сдача: {'до ' + ho[:7] + ' (по тексту объявления)' if ho else 'срок в объявлении не указан — уточнить'}")
        lines.append("🏦 Off-plan: ипотека обычно недоступна до готовности; нужен график оставшихся платежей застройщику.")
    else:
        lines.append(f"🏦 Ипотека возможна для готового объекта (для вашего профиля self-employed — не гарантирована). "
                     f"Взнос {pct(cost['dp_pct'])}: {m(cost['down'])}, кредит {m(cost['loan'])}, платёж "
                     + ", ".join(f"{m(p)}/мес при {r_ * 100:.1f}%" for r_, p in cost["payments"]) + ".")
    lines.append(f"💵 Собственные средства (предв.): ~{m(cost['own_funds'])} AED = взнос + сборы {m(cost['fees'])} "
                 f"({'ADREC' if cost.get('emirate') == 'abu_dhabi' else 'DLD'} {cost.get('reg_pct', 0.04):.0%} {m(cost['dld_fee'])}, агент 2%+VAT {m(cost['agency'])}, банк, оценка, траст, регистрация ипотеки). "
                 f"Покупка за наличные: ~{m(cost['cash_total'])}.")

    unknowns = list(r["risks"])
    if not n.get("size_sqm"):
        unknowns.append("площадь не указана")
    if r.get("project") and r.get("match_score", 1) < 0.8:
        unknowns.append(f"сопоставление с проектом DLD «{r['project']}» неточное — проверить")
    unknowns += ["этаж и вид (в DLD их нет)", "состояние, арендатор, сервисные сборы"]
    lines.append("❓ Неизвестно/риски: " + e("; ".join(unknowns)))
    lines.append("➡️ Следующий шаг: запросить у агента номер юнита, этаж, Title Deed/Oqood, сервисный сбор и статус арендатора; "
                 "сверить сделки по этому зданию в DXB Interact/Dubai REST.")
    seen = (n.get("first_seen") or "")[:16].replace("T", " ")
    hist = " → ".join(m(p) for _, p in history) if len(history) > 1 else "без изменений"
    lines.append(f"🕒 Обнаружено: {seen} UTC; на PF с {(n.get('listed_at') or '')[:10]}. Цена: {hist}.")
    return "\n".join(lines)


def format_thread_post(post, info):
    e = html.escape
    lines = ["<b>🧵 Threads: упоминание срочной продажи — кандидат на проверку</b>",
             f"🔗 <a href=\"{e(post['url'])}\">@{e(post.get('username') or '?')}</a> — Threads, {e((post.get('timestamp') or '')[:16])}",
             f"«{e(post['text'][:500])}»"]
    if info.get("prices"):
        lines.append("💰 Цены в тексте: " + ", ".join(m(p) + " AED" for p in info["prices"]))
    if info.get("places"):
        lines.append("📍 Места: " + e(", ".join(info["places"])))
    lines.append("❓ Пост в соцсети не подтверждает ни цену, ни наличие объекта. Следующий шаг: найти объект на Property Finder/Bayut и сверить со сделками DLD.")
    return "\n".join(lines)


# ---------- доставка ----------

START_CODE = "/start monitor"  # ссылка t.me/<бот>?start=monitor присылает именно это
_chat = {"id": None}


def resolve_telegram_chat(store, log=print):
    """chat_id берётся из секрета TELEGRAM_CHAT_ID, из сохранённого состояния или из
    сообщения «/start monitor», которое владелец отправил боту (getUpdates хранит их ~24 ч)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or (store.get_kv("telegram_chat_id") if store else None)
    if token and not chat:
        import json
        try:
            upd = json.loads(request(f"https://api.telegram.org/bot{token}/getUpdates?limit=100"))
        except Exception as e:  # noqa: BLE001
            log(f"[notify] Telegram getUpdates: ошибка {e}")
            upd = {}
        for u in reversed(upd.get("result", [])):
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("type") == "private" and (msg.get("text") or "").strip() == START_CODE:
                chat = str(msg["chat"]["id"])
                if store:
                    store.set_kv("telegram_chat_id", chat)
                log("[notify] чат Telegram найден по сообщению /start monitor")
                break
        else:
            log("[notify] чат Telegram не найден: откройте бота по ссылке ?start=monitor и нажмите Start")
    _chat["id"] = chat
    return chat


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID") or _chat["id"]
    if not (token and chat):
        return False
    request(f"https://api.telegram.org/bot{token}/sendMessage", data={
        "chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
    return True


def send_email(subject, html_text):
    host, user, pwd, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"))
    if not (host and user and pwd and to):
        return False
    msg = MIMEText(html_text.replace("\n", "<br>"), "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(user, pwd)
        s.send_message(msg)
    return True


def deliver(subject, text, dry_run=False, log=print):
    if dry_run:
        log("----- УВЕДОМЛЕНИЕ (dry-run) -----\n" + text + "\n-------------------------------")
        return True
    sent = []
    if send_telegram(text):
        sent.append("telegram")
    if send_email(subject, text):
        sent.append("email")
    if not sent:
        raise RuntimeError("Не настроен ни один канал: нужны TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID или SMTP_*")
    log(f"[notify] отправлено: {', '.join(sent)}")
    return True
