#!/usr/bin/env python3
"""
make_lists.py — делает из полного amnezia_sites.json урезанные списки topN.

Берутся первые N доменов (в файле они идут по популярности), а IP-адреса,
которые уже встречались у более популярных сайтов, отбрасываются: клиент
добавляет маршрут на каждый IP, повторы только тормозят подключение.

Пример:
    python make_lists.py data/amnezia_sites.json -n 300 -n 1000 -o lists
"""
import argparse
import json
import os
import sys

DEFAULT_SIZES = [300, 1000]


def trim(entries, n):
    """Первые n записей, без IP-адресов, уже встречавшихся раньше."""
    seen, out = set(), []
    for e in entries[:n]:
        ips = [ip for ip in e["ips"] if ip not in seen]
        if not ips:
            continue
        seen.update(ips)
        out.append({"hostname": e["hostname"], "ip": ips[0], "ips": ips})
    return out


def write_lists(entries, sizes, out_dir):
    """Пишет lists/amnezia_top<N>.json для каждого N, возвращает сводку."""
    os.makedirs(out_dir, exist_ok=True)
    summary = []
    for n in sizes:
        part = trim(entries, n)
        path = os.path.join(out_dir, f"amnezia_top{n}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(part, f, indent=4, ensure_ascii=False)
        summary.append((n, path, len(part), sum(len(e["ips"]) for e in part)))
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description="Урезанные списки topN из полного amnezia_sites.json")
    p.add_argument("src", help="полный amnezia_sites.json (результат harvest_ru.py)")
    p.add_argument("-n", "--size", type=int, action="append",
                   help="сколько доменов брать (можно несколько раз; по умолчанию 300 и 1000)")
    p.add_argument("-o", "--out-dir", default="lists")
    args = p.parse_args(argv)

    with open(args.src, encoding="utf-8") as f:
        entries = json.load(f)
    for n, path, count, ips in write_lists(entries, args.size or DEFAULT_SIZES, args.out_dir):
        print(f"top{n}: {count} записей, {ips} IP -> {path}")


if __name__ == "__main__":
    sys.exit(main())
