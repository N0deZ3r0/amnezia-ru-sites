#!/usr/bin/env python3
"""
update.py — одной командой обновляет списки lists/amnezia_top300.json и
lists/amnezia_top1000.json:

  1. harvest_ru.py собирает свежие .ru домены из рейтингов и резолвит их;
  2. make_lists.py режет результат на списки нужного размера.

Запуск (без включённого VPN):
    pip install dnspython
    python update.py

Всё, что скрипт не знает, передаётся в harvest_ru.py, например:
    python update.py --keep-foreign --dns 77.88.8.8,8.8.8.8
"""
import argparse
import json
import os
import sys

import harvest_ru
import make_lists


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Обновляет lists/amnezia_top300.json и lists/amnezia_top1000.json",
        epilog="Остальные параметры передаются в harvest_ru.py (см. python harvest_ru.py -h).",
    )
    p.add_argument("--sizes", default=",".join(map(str, make_lists.DEFAULT_SIZES)),
                   help="размеры списков через запятую (по умолчанию 300,1000)")
    p.add_argument("--data-dir", default="data", help="куда класть полный список (в git не попадает)")
    p.add_argument("--lists-dir", default="lists", help="куда писать готовые списки")
    p.add_argument("--skip-harvest", action="store_true",
                   help="не собирать заново, взять уже готовый data/amnezia_sites.json")
    p.add_argument("--min-ratio", type=float, default=0.0,
                   help="не перезаписывать списки, если новый получился меньше старого "
                        "более чем в это число раз (например, 0.9; по умолчанию проверка выключена)")
    args, rest = p.parse_known_args(argv)

    sizes = sorted({int(s) for s in args.sizes.split(",") if s.strip()})
    os.makedirs(args.data_dir, exist_ok=True)
    full = os.path.join(args.data_dir, "amnezia_sites.json")
    domains = os.path.join(args.data_dir, "ru_domains.txt")

    if not args.skip_harvest:
        # Для топ-N не нужен весь рейтинг: берём с запасом, часть доменов
        # не резолвится или уходит под фильтр российских IP.
        if "--limit" not in rest:
            rest += ["--limit", str(max(sizes) * 3)]
        harvest_ru.main(["-o", full, "--domains-out", domains, *rest])

    if not os.path.exists(full):
        sys.exit(f"Нет файла {full}: запустите без --skip-harvest")
    with open(full, encoding="utf-8") as f:
        entries = json.load(f)
    if len(entries) < max(sizes):
        print(f"[!] В полном списке только {len(entries)} записей, меньше {max(sizes)}")

    if args.min_ratio > 0:
        for n in sizes:
            old_path = os.path.join(args.lists_dir, f"amnezia_top{n}.json")
            if not os.path.exists(old_path):
                continue
            with open(old_path, encoding="utf-8") as f:
                old = len(json.load(f))
            new = len(make_lists.trim(entries, n))
            if new < old * args.min_ratio:
                sys.exit(f"Новый top{n} подозрительно мал: {new} записей против {old}. "
                         "Списки не перезаписаны (проверьте источники и DNS).")

    print()
    for n, path, count, ips in make_lists.write_lists(entries, sizes, args.lists_dir):
        print(f"top{n}: {count} записей, {ips} IP -> {path}")


if __name__ == "__main__":
    sys.exit(main())
