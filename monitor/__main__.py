"""Точка входа: python -m monitor {run,sync-dld,test-notify,check}."""
import argparse
import datetime as dt
import json
import sys
import time
from collections import Counter

from . import analyze, dld, notify
from .config import data_dir, load_config, state_dir
from .sources import propertyfinder as pf
from .sources import threads
from .store import Store, now


def log(*a):
    print(*a, flush=True)


def should_notify(store, key, n, verdict, cfg):
    last = store.last_notification(key)
    if last is None:
        return True, None
    _, old_price, old_verdict = last
    if old_price and n["price_aed"]:
        ch = (n["price_aed"] - old_price) / old_price
        if abs(ch) >= cfg["alerts"]["renotify_price_change"]:
            return True, f"Цена изменилась с {notify.m(old_price)} до {notify.m(n['price_aed'])} AED ({ch * 100:+.1f}%) с прошлого уведомления"
    rank = {"review": 1, "suspect": 1, "target": 2}
    if rank.get(verdict, 0) > rank.get(old_verdict, 0):
        return True, "Оценка повысилась до «кандидат: дисконт к сделкам»"
    return False, None


def run(cfg, args):
    store = Store(state_dir() / "state.db")
    if not args.dry_run:
        notify.resolve_telegram_chat(store, log=log)
    ddb = dld.connect(data_dir() / "dld.db")
    if not args.skip_dld:
        try:
            dld.sync(ddb, months=cfg["alerts"]["comps_months"], log=log)
        except Exception as e:  # noqa: BLE001
            log(f"[dld] синхронизация не удалась, использую имеющиеся данные: {e}")
    projects = dld.Projects(ddb)

    sch = cfg["schedule"]
    last_full = store.get_kv("last_full_sweep")
    full = args.full or not last_full or \
        dt.datetime.fromisoformat(last_full) < dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=sch["full_sweep_every_hours"])
    log(f"[run] режим: {'полный обход' if full else 'быстрый'}; dry-run={args.dry_run}")

    pf_seen = store.ids("propertyfinder")
    first_run = not pf_seen
    known = store.searches()  # новые зоны тоже начинают со сводки, а не с пачки сообщений
    collected = []
    for s in cfg["searches"]:
        collected += pf.collect(s, cfg["filters"], sch["full_sweep_pages"] if full else sch["quick_pages"],
                                sch["request_delay_sec"], seen_ids=None if full else pf_seen, log=log)
        time.sleep(sch["request_delay_sec"])
    log(f"[pf] получено {len(collected)} объявлений после фильтра")

    sent = candidates = 0
    stats = {"target": 0, "review": 0, "suspect": 0, "skip": 0}
    matches, unmatched, digest, summary = Counter(), Counter(), [], []
    for n in collected:
        is_new, old_price, history = store.upsert(n)
    for n in collected:
        history = store.history(n["id"])
        r = analyze.evaluate(n, ddb, projects, store, cfg)
        if r["verdict"] != "skip" and n["completion"] == "off_plan" and not r["handover"] and not n.get("handover_fields"):
            # в выдаче нет срока сдачи — берём его со страницы объявления (кэш в state, только для кандидатов)
            cached = store.get_kv(f"handover:{n['id']}")
            if cached is None:
                try:
                    cached = json.dumps(pf.detail_handover(n["url"]))
                    time.sleep(sch["request_delay_sec"])
                except Exception as e:  # noqa: BLE001
                    log(f"[pf] срок сдачи {n['id']}: {e}")
                    cached = "[]"
                store.set_kv(f"handover:{n['id']}", cached)
            n["handover_fields"] = json.loads(cached)
            if args.verbose:
                log(f"[pf] срок сдачи {n['id']}: {n['handover_fields'][:3]}")
            if n["handover_fields"]:
                r = analyze.evaluate(n, ddb, projects, store, cfg)
        stats[r["verdict"]] += 1
        place = n.get("tower") or n.get("subcommunity")
        (matches.__setitem__((n["search"], place, r["project"]), matches[(n["search"], place, r["project"])] + 1)
         if r["project"] else unmatched.update([(n["search"], place)]))
        if args.verbose and r["verdict"] != "skip":
            c = r["comps"]
            summary.append(f"  {n['id']} {n['tower'] or n['subcommunity']} {n['bedrooms']}BR {n['size_sqm']}м² {notify.m(n['price_aed'])} "
                f"→ {r['project']} ({r['match_score']:.2f}) comps={c['n'] if c else 0} disc={notify.pct(r['discount'])} {r['verdict']} {n['completion']} {','.join(r['keywords'])}")
        if r["verdict"] == "skip":
            continue
        candidates += 1
        key = f"pf:{n['fingerprint']}"
        ok, change = should_notify(store, key, n, r["verdict"], cfg)
        if not ok and not args.summary_all:
            continue
        if len(history) > 1 and not change:
            change = "Цена снижена" if history[-1][1] < history[0][1] else "Цена изменилась"
        emirate = next((s_.get("emirate", "dubai") for s_ in cfg["searches"] if s_["name"] == n.get("search")), "dubai")
        cost = analyze.costs(n["price_aed"], cfg, n["completion"], emirate)
        text = notify.format_listing(n, r, cost, history, change)
        if args.summary_all or ((first_run or n["search"] not in known) and not args.dry_run and not args.notify_initial):
            # первый запуск: запоминаем базу и шлём одну сводку вместо десятков сообщений
            if not args.dry_run:
                store.record_notification(key, n["id"], n["price_aed"], r["verdict"])
            digest.append((r["discount"] or 0, n, r))
            continue
        try:
            if args.dry_run and candidates > args.show:
                sent += 1
                continue
            notify.deliver(f"[Dubai deals] {n['title'][:60]}", text, dry_run=args.dry_run, log=log)
            if not args.dry_run:
                store.record_notification(key, n["id"], n["price_aed"], r["verdict"])
            sent += 1
        except Exception as e:  # noqa: BLE001
            log(f"[notify] ошибка: {e}")
    if digest:
        digest.sort(key=lambda x: -x[0])
        uniq = {}
        for item in digest:  # один объект от разных агентов — одна строка
            uniq.setdefault(item[1]["fingerprint"], item)
        digest = list(uniq.values())
        zones = sorted({n["search"] for n in collected if n["search"] not in known})
        head = "Текущие кандидаты" if args.summary_all else "Мониторинг: стартовая база" if first_run else "Новые районы: стартовая база (" + ", ".join(zones) + ")"
        base = len(collected) if first_run or args.summary_all else sum(1 for n in collected if n["search"] not in known)
        lines = [f"<b>{head}</b>\n{'Проверено' if args.summary_all else 'Сохранено'} {base} объявлений, кандидатов {len(digest)}. "
                 "Дальше будут приходить только новые объекты и изменения цен. Все кандидаты по дисконту к сделкам DLD:"]
        for d, n, r in digest:
            lines.append(f"• {notify.VERDICT[r['verdict']].split(' ', 1)[0]} <a href=\"{n['url']}\">{n.get('tower') or n.get('subcommunity')}</a>, "
                         f"{n['bedrooms']} сп., {n['size_sqm']} м², {notify.m(n['price_aed'])} AED, дисконт {notify.pct(r['discount'])} "
                         f"({r['comps']['n'] if r['comps'] else 0} сделок)")
        try:
            notify.deliver("[Dubai deals] Сводка", "\n".join(lines), dry_run=args.dry_run, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"[notify] ошибка: {e}")
    if args.verbose:
        log("[candidates]\n" + "\n".join(summary))
        log("[match] сопоставления (поиск, здание → проект DLD, объявлений):")
        for (srch, place, proj), k in sorted(matches.items()):
            log(f"   {srch} | {place} → {proj} ({k})")
        areas = sorted({a for s_ in cfg["searches"] for a in s_.get("dld_areas", [])})
        log("[dld] профиль районов (район | мастер-проект | проект | сделок):")
        for row in dld.area_profile(ddb, areas):
            log("   " + " | ".join(map(str, row)))
        log("[dld] названия районов (район | сделок | без проекта): " + "; ".join(
            " | ".join(map(str, r)) for r in dld.area_names(ddb, ["MARSA", "JUMEIRA", "HARBOUR", "ISLAND", "DEIRA", "RANCH", "CREEK", "JEBEL", "RASHID", "GHAF", "SPRING", "MEADOW", "HEBIAH", "SAFA", "THANYAH", "MARSA"])))
        log("[pf] completion_status: " + json.dumps(Counter(str(n.get("completion_raw")) for n in collected)))
        hf = Counter(k for n in collected for k, _ in n.get("handover_fields") or [])
        log("[pf] поля срока сдачи: " + json.dumps(hf) + "; примеры: " + "; ".join(
            f"{n['id']} {v}" for n in collected[:400] for k, v in (n.get("handover_fields") or [])[:1])[:1500])
        log("[match] не сопоставлены: " + "; ".join(f"{srch} | {place} ({k})" for (srch, place), k in unmatched.most_common()))
    if full:
        gone = store.mark_inactive("propertyfinder", {n["id"] for n in collected}, [s["name"] for s in cfg["searches"]])
        log(f"[pf] снято с публикации/вне фильтра: {gone}")
        if not args.dry_run:
            store.set_kv("last_full_sweep", now())
    log(f"[run] оценки: {json.dumps(stats)}; кандидатов {candidates}; отправлено {sent}"
        + (" (первый запуск: база сохранена без рассылки)" if first_run and not args.dry_run and not args.notify_initial else ""))

    # Threads
    tcfg = cfg.get("threads") or {}
    last_t = float(store.get_kv("threads_last_run") or 0)
    if tcfg.get("enabled") and (args.full or time.time() - last_t >= tcfg.get("every_hours", 4) * 3600 - 600):
        since = float(store.get_kv("threads_since") or (time.time() - 2 * 86400))
        posts = threads.search(tcfg["keywords"], since, cfg=tcfg, log=log)
        tsent = 0
        for p in posts:
            info = threads.extract(p["text"])
            if not info["places"] or store.last_notification(p["id"]):
                continue
            if info["prices"] and min(info["prices"]) > cfg["filters"]["max_price_aed"]:
                continue
            try:
                notify.deliver("[Dubai deals] Threads", notify.format_thread_post(p, info), dry_run=args.dry_run, log=log)
                if not args.dry_run:
                    store.record_notification(p["id"], p["id"], None, "threads")
                tsent += 1
            except Exception as e:  # noqa: BLE001
                log(f"[notify] ошибка: {e}")
        log(f"[threads] постов {len(posts)}, уведомлений {tsent}")
        if not args.dry_run:
            store.set_kv("threads_since", str(time.time() - 600))
            store.set_kv("threads_last_run", str(time.time()))
    if not args.dry_run:
        store.set_kv("last_run", now())
    store.commit()
    return 0


def test_notify(cfg, args):
    store = Store(state_dir() / "state.db")
    if not args.dry_run:
        notify.resolve_telegram_chat(store, log=log)
        store.commit()
    text = ("<b>✅ Тест мониторинга distress-сделок</b>\n"
            f"Канал доставки работает. Время: {now()} UTC.\n"
            f"Фильтр: {notify.m(cfg['filters']['min_price_aed'])}–{notify.m(cfg['filters']['max_price_aed'])} AED, "
            f"спальни {cfg['filters']['bedrooms']}, типы {', '.join(cfg['filters']['property_types'])}; "
            f"районы: {', '.join(s['name'] for s in cfg['searches'])}.")
    notify.deliver("[Dubai deals] Тестовое уведомление", text, dry_run=args.dry_run, log=log)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="monitor")
    ap.add_argument("command", choices=["run", "summary", "sync-dld", "test-notify"])
    ap.add_argument("--dry-run", action="store_true", help="не отправлять, печатать уведомления")
    ap.add_argument("--full", action="store_true", help="принудительно полный обход")
    ap.add_argument("--skip-dld", action="store_true")
    ap.add_argument("--notify-initial", action="store_true", help="рассылать и при первом запуске")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--show", type=int, default=10**6, help="dry-run: сколько уведомлений печатать целиком")
    args = ap.parse_args(argv)
    cfg = load_config()
    args.summary_all = args.command == "summary"
    if args.command in ("run", "summary"):
        if args.summary_all:
            args.full = True
        return run(cfg, args)
    if args.command == "sync-dld":
        dld.sync(dld.connect(data_dir() / "dld.db"), months=cfg["alerts"]["comps_months"], log=log)
        return 0
    return test_notify(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
