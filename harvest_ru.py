#!/usr/bin/env python3
"""
harvest_ru.py — собирает .ru домены из публичных рейтингов, резолвит их
и сохраняет в формате amnezia_sites.json (hostname / ip / ips).

Источники доменов (каждый можно отключить, если не отвечает):
  * Tranco top-1M        https://tranco-list.eu
  * Majestic Million     https://majestic.com/reports/majestic-million
  * Cisco Umbrella 1M    (содержит и поддомены вида mc.yandex.ru)
  * свой файл со списком (--extra domains.txt, по одному домену в строке)

Фильтр «для прямого подключения»: оставляются только IP, входящие в
российские диапазоны (список RU CIDR качается с ipdeny.com / ipverse).
Отключается флагом --keep-foreign.

Установка:   pip install dnspython
Запуск:      python harvest_ru.py
Запускать БЕЗ включённого VPN/прокси.
"""
import argparse
import bisect
import csv
import io
import ipaddress
import json
import sys
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover
    sys.exit("Нужен dnspython:  pip install dnspython")

UA = {"User-Agent": "Mozilla/5.0 (compatible; ru-domain-harvester/1.0)"}

TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
MAJESTIC_URL = "https://downloads.majestic.com/majestic_million.csv"
UMBRELLA_URL = "https://s3-us-west-1.amazonaws.com/umbrella-static/top-1m.csv.zip"
RU_CIDR_URLS = [
    "https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone",
    "https://raw.githubusercontent.com/ipverse/rir-ip/master/country/ru/ipv4-aggregated.txt",
]
DEFAULT_RESOLVERS = ["77.88.8.8", "77.88.8.1", "8.8.8.8", "1.1.1.1"]


# ───────────────────────── загрузка ─────────────────────────
def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def log(msg):
    print(msg, flush=True)


def norm(d):
    d = d.strip().lower().rstrip(".")
    if d.startswith("*."):
        d = d[2:]
    return d


def tld_ok(domain, tlds):
    return any(domain == t or domain.endswith("." + t) for t in tlds)


def from_tranco(tlds):
    raw = fetch(TRANCO_URL)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        data = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    out = []
    for row in csv.reader(io.StringIO(data)):
        if len(row) >= 2 and tld_ok(norm(row[1]), tlds):
            out.append(norm(row[1]))
    return out


def from_majestic(tlds):
    raw = fetch(MAJESTIC_URL, timeout=300).decode("utf-8", "ignore")
    out = []
    for row in csv.DictReader(io.StringIO(raw)):
        d = norm(row.get("Domain", ""))
        if d and tld_ok(d, tlds):
            out.append(d)
    return out


def from_umbrella(tlds):
    raw = fetch(UMBRELLA_URL)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        data = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    out = []
    for row in csv.reader(io.StringIO(data)):
        if len(row) >= 2 and tld_ok(norm(row[1]), tlds):
            out.append(norm(row[1]))
    return out


def from_file(path, tlds):
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            d = norm(line.split("#")[0].split(",")[0])
            if d and tld_ok(d, tlds):
                out.append(d)
    return out


def collect_domains(args, tlds):
    sources = []
    if not args.no_tranco:
        sources.append(("Tranco", from_tranco))
    if not args.no_majestic:
        sources.append(("Majestic", from_majestic))
    if not args.no_umbrella:
        sources.append(("Umbrella", from_umbrella))

    seen, ordered = set(), []

    def add(items):
        n = 0
        for d in items:
            if d not in seen:
                seen.add(d)
                ordered.append(d)
                n += 1
        return n

    for name, fn in sources:
        try:
            log(f"[+] Скачиваю {name} ...")
            n = add(fn(tlds))
            log(f"    {name}: +{n} новых доменов (всего {len(ordered)})")
        except Exception as e:  # источник недоступен — идём дальше
            log(f"    {name}: пропущен ({type(e).__name__}: {e})")
    for path in args.extra or []:
        n = add(from_file(path, tlds))
        log(f"[+] {path}: +{n} новых доменов (всего {len(ordered)})")
    return ordered


# ───────────────────────── RU диапазоны ─────────────────────────
class RangeSet:
    """Быстрая проверка «IP входит в один из CIDR» через bisect."""

    def __init__(self, cidrs):
        nets = sorted(
            (ipaddress.ip_network(c, strict=False) for c in cidrs),
            key=lambda n: int(n.network_address),
        )
        merged = []
        for n in nets:
            s, e = int(n.network_address), int(n.broadcast_address)
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        self.starts = [m[0] for m in merged]
        self.ends = [m[1] for m in merged]

    def __contains__(self, ip):
        x = int(ipaddress.ip_address(ip))
        i = bisect.bisect_right(self.starts, x) - 1
        return i >= 0 and x <= self.ends[i]


def load_ru_ranges(path=None):
    if path:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    else:
        text, last = None, None
        for url in RU_CIDR_URLS:
            try:
                text = fetch(url, timeout=60).decode("utf-8", "ignore")
                break
            except Exception as e:
                last = e
        if text is None:
            raise RuntimeError(f"не удалось скачать список RU CIDR: {last}")
    cidrs = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line and "/" in line:
            cidrs.append(line)
    if not cidrs:
        raise RuntimeError("список RU CIDR пуст")
    return RangeSet(cidrs), len(cidrs)


# ───────────────────────── резолвинг ─────────────────────────
_tls = threading.local()


def get_resolver(servers, timeout):
    r = getattr(_tls, "r", None)
    if r is None:
        r = dns.resolver.Resolver(configure=False)
        r.nameservers = list(servers)
        r.timeout = timeout
        r.lifetime = timeout * 2
        _tls.r = r
    return r


def resolve_one(domain, servers, timeout, queries):
    ips = []
    for q in range(queries):
        r = get_resolver(servers, timeout)
        # разные запросы начинают с разных серверов -> больше разнообразия адресов
        r.nameservers = servers[q % len(servers):] + servers[: q % len(servers)]
        try:
            ans = r.resolve(domain, "A", raise_on_no_answer=False)
            for rr in ans:
                ip = rr.address
                if ip not in ips:
                    ips.append(ip)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            if q == 0:
                break  # домена нет — второй запрос не нужен
        except (dns.exception.Timeout, dns.resolver.YXDOMAIN, dns.exception.DNSException):
            continue
    return domain, ips


def resolve_all(domains, servers, workers, timeout, queries, progress_every=2000):
    results = {}
    t0 = time.time()
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(resolve_one, d, servers, timeout, queries) for d in domains]
            for f in as_completed(futs):
                d, ips = f.result()
                if ips:
                    results[d] = ips
                done += 1
                if done % progress_every == 0 or done == len(domains):
                    rate = done / max(time.time() - t0, 0.001)
                    log(f"    {done}/{len(domains)} обработано, резолвится {len(results)} ({rate:.0f}/с)")
    except KeyboardInterrupt:
        log("\n[!] Прервано (Ctrl+C) — сохраняю то, что успело собраться")
    return results


# ───────────────────────── main ─────────────────────────
def build(domains, resolved, ru_ranges):
    out, dropped = [], 0
    for d in domains:  # сохраняем порядок рейтинга
        ips = resolved.get(d)
        if not ips:
            continue
        if ru_ranges is not None:
            ips = [ip for ip in ips if ip in ru_ranges]
            if not ips:
                dropped += 1
                continue
        out.append({"hostname": d, "ip": ips[0], "ips": ips})
    return out, dropped


def main(argv=None):
    p = argparse.ArgumentParser(description="Сбор .ru доменов и их IP в формат amnezia_sites.json")
    p.add_argument("--tld", default="ru", help="зоны через запятую (по умолчанию ru; для РФ: ru,xn--p1ai)")
    p.add_argument("--limit", type=int, default=0, help="взять только первые N доменов (0 = все)")
    p.add_argument("--extra", action="append", help="файл со своим списком доменов (можно несколько раз)")
    p.add_argument("--no-tranco", action="store_true")
    p.add_argument("--no-majestic", action="store_true")
    p.add_argument("--no-umbrella", action="store_true")
    p.add_argument("--keep-foreign", action="store_true", help="не фильтровать по российским IP")
    p.add_argument("--ru-cidr-file", help="локальный файл со списком RU CIDR (вместо скачивания)")
    p.add_argument("--dns", default=",".join(DEFAULT_RESOLVERS), help="DNS-серверы через запятую")
    p.add_argument("--workers", type=int, default=200)
    p.add_argument("--timeout", type=float, default=2.5)
    p.add_argument("--queries", type=int, default=2, help="сколько раз опрашивать каждый домен")
    p.add_argument("-o", "--out", default="amnezia_sites.json")
    p.add_argument("--domains-out", default="ru_domains.txt")
    args = p.parse_args(argv)

    tlds = [t.strip().lstrip(".").lower() for t in args.tld.split(",") if t.strip()]
    servers = [s.strip() for s in args.dns.split(",") if s.strip()]

    domains = collect_domains(args, tlds)
    if args.limit:
        domains = domains[: args.limit]
    if not domains:
        sys.exit("Не удалось получить ни одного домена (проверь интернет / --extra файл)")

    ru_ranges = None
    if not args.keep_foreign:
        ru_ranges, n = load_ru_ranges(args.ru_cidr_file)
        log(f"[+] Загружено {n} российских диапазонов IP")

    log(f"[+] Резолвлю {len(domains)} доменов ({args.workers} потоков) ...")
    resolved = resolve_all(domains, servers, args.workers, args.timeout, args.queries)

    result, dropped = build(domains, resolved, ru_ranges)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)
    with open(args.domains_out, "w", encoding="utf-8") as f:
        f.write("\n".join(e["hostname"] for e in result) + "\n")

    log("")
    log(f"Всего доменов собрано:      {len(domains)}")
    log(f"Резолвятся:                 {len(resolved)}")
    if ru_ranges is not None:
        log(f"Отброшено (нет RU IP):      {dropped}")
    log(f"Записано в {args.out}: {len(result)} доменов, {sum(len(e['ips']) for e in result)} IP")
    log(f"Список доменов:             {args.domains_out}")


if __name__ == "__main__":
    main()
