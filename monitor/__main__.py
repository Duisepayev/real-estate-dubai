"""Точка входа: python -m monitor {run,sync-dld,test-notify,check}."""
import argparse
import datetime as dt
import json
import sys
import time

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
    collected = []
    for s in cfg["searches"]:
        collected += pf.collect(s, cfg["filters"], sch["full_sweep_pages"] if full else sch["quick_pages"],
                                sch["request_delay_sec"], seen_ids=None if full else pf_seen, log=log)
        time.sleep(sch["request_delay_sec"])
    log(f"[pf] получено {len(collected)} объявлений после фильтра")

    sent = candidates = 0
    stats = {"target": 0, "review": 0, "suspect": 0, "skip": 0}
    for n in collected:
        is_new, old_price, history = store.upsert(n)
    for n in collected:
        history = store.history(n["id"])
        r = analyze.evaluate(n, ddb, projects, store, cfg)
        stats[r["verdict"]] += 1
        if args.verbose:
            c = r["comps"]
            log(f"  {n['id']} {n['tower'] or n['subcommunity']} {n['bedrooms']}BR {n['size_sqm']}м² {notify.m(n['price_aed'])} "
                f"→ {r['project']} ({r['match_score']:.2f}) comps={c['n'] if c else 0} disc={notify.pct(r['discount'])} {r['verdict']}")
        if r["verdict"] == "skip":
            continue
        candidates += 1
        key = f"pf:{n['fingerprint']}"
        ok, change = should_notify(store, key, n, r["verdict"], cfg)
        if not ok:
            continue
        if len(history) > 1 and not change:
            change = "Цена снижена" if history[-1][1] < history[0][1] else "Цена изменилась"
        cost = analyze.costs(n["price_aed"], cfg, n["completion"])
        text = notify.format_listing(n, r, cost, history, change)
        if first_run and not args.dry_run and not args.notify_initial:
            # первый запуск: запоминаем базу, но не заваливаем уведомлениями
            store.record_notification(key, n["id"], n["price_aed"], r["verdict"])
            continue
        try:
            notify.deliver(f"[Dubai deals] {n['title'][:60]}", text, dry_run=args.dry_run, log=log)
            if not args.dry_run:
                store.record_notification(key, n["id"], n["price_aed"], r["verdict"])
            sent += 1
        except Exception as e:  # noqa: BLE001
            log(f"[notify] ошибка: {e}")
    if full:
        gone = store.mark_inactive("propertyfinder", {n["id"] for n in collected}, [s["name"] for s in cfg["searches"]])
        log(f"[pf] снято с публикации/вне фильтра: {gone}")
        if not args.dry_run:
            store.set_kv("last_full_sweep", now())
    log(f"[run] оценки: {json.dumps(stats)}; кандидатов {candidates}; отправлено {sent}"
        + (" (первый запуск: база сохранена без рассылки)" if first_run and not args.dry_run and not args.notify_initial else ""))

    # Threads
    tcfg = cfg.get("threads") or {}
    if tcfg.get("enabled"):
        since = float(store.get_kv("threads_since") or (time.time() - 2 * 86400))
        posts = threads.search(tcfg["keywords"], since, log=log)
        tsent = 0
        for p in posts:
            info = threads.extract(p["text"])
            if not info["places"] or store.last_notification(p["id"]):
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
    if not args.dry_run:
        store.set_kv("last_run", now())
    store.commit()
    return 0


def test_notify(cfg, args):
    text = ("<b>✅ Тест мониторинга distress-сделок</b>\n"
            f"Канал доставки работает. Время: {now()} UTC.\n"
            f"Фильтр: {notify.m(cfg['filters']['min_price_aed'])}–{notify.m(cfg['filters']['max_price_aed'])} AED, "
            f"спальни {cfg['filters']['bedrooms']}, типы {', '.join(cfg['filters']['property_types'])}; "
            f"районы: {', '.join(s['name'] for s in cfg['searches'])}.")
    notify.deliver("[Dubai deals] Тестовое уведомление", text, dry_run=args.dry_run, log=log)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="monitor")
    ap.add_argument("command", choices=["run", "sync-dld", "test-notify"])
    ap.add_argument("--dry-run", action="store_true", help="не отправлять, печатать уведомления")
    ap.add_argument("--full", action="store_true", help="принудительно полный обход")
    ap.add_argument("--skip-dld", action="store_true")
    ap.add_argument("--notify-initial", action="store_true", help="рассылать и при первом запуске")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.command == "run":
        return run(cfg, args)
    if args.command == "sync-dld":
        dld.sync(dld.connect(data_dir() / "dld.db"), months=cfg["alerts"]["comps_months"], log=log)
        return 0
    return test_notify(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
