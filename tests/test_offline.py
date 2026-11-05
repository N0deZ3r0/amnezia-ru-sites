"""
Проверки без сети: DNS подменяется заглушкой, списки диапазонов берутся из файла.

Запуск:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import harvest_ru  # noqa: E402
import make_lists  # noqa: E402
import update  # noqa: E402

def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


FAKE_DNS = {
    "yandex.ru": ["5.255.255.77", "77.88.55.88"],
    "mc.yandex.ru": ["77.88.21.1"],
    "sber.ru": ["84.252.149.198"],
    "cf-site.ru": ["104.21.77.152", "84.252.149.1"],  # один зарубежный IP, один российский
    "foreign-only.ru": ["104.21.77.153"],
}
RU_CIDRS = "5.255.255.0/24\n77.88.0.0/16\n84.252.149.0/24\n# комментарий\n"


def fake_resolve(domain, servers, timeout, queries):
    return domain, list(FAKE_DNS.get(domain, []))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cidr = os.path.join(self.dir, "ru.cidr")
        with open(self.cidr, "w", encoding="utf-8") as f:
            f.write(RU_CIDRS)
        self.domains = os.path.join(self.dir, "domains.txt")
        with open(self.domains, "w", encoding="utf-8") as f:
            f.write("yandex.ru\nvk.com\nYANDEX.RU.\n# комментарий\nmc.yandex.ru\n"
                    "sber.ru\ncf-site.ru\nforeign-only.ru\ndead.ru\n")
        self._orig = harvest_ru.resolve_one
        harvest_ru.resolve_one = fake_resolve

    def tearDown(self):
        harvest_ru.resolve_one = self._orig
        self.tmp.cleanup()

    def harvest_args(self, out, domains_out):
        return ["--no-tranco", "--no-majestic", "--no-umbrella", "--extra", self.domains,
                "--ru-cidr-file", self.cidr, "--workers", "4", "-o", out, "--domains-out", domains_out]


class RangeSetTest(Base):
    def test_membership(self):
        rs, count = harvest_ru.load_ru_ranges(self.cidr)
        self.assertEqual(count, 3)
        self.assertIn("77.88.55.88", rs)
        self.assertIn("5.255.255.255", rs)
        self.assertNotIn("5.255.254.255", rs)
        self.assertNotIn("104.21.77.152", rs)

    def test_overlapping_ranges_are_merged(self):
        rs = harvest_ru.RangeSet(["10.0.0.0/24", "10.0.0.128/25", "10.0.1.0/24"])
        self.assertIn("10.0.1.200", rs)
        self.assertNotIn("10.0.2.1", rs)


class HarvestTest(Base):
    def test_pipeline_filters_and_normalizes(self):
        out = os.path.join(self.dir, "full.json")
        dom = os.path.join(self.dir, "full.txt")
        harvest_ru.main(self.harvest_args(out, dom))
        data = load(out)
        hosts = [e["hostname"] for e in data]
        # vk.com — не .ru; dead.ru не резолвится; foreign-only.ru — нет российских IP
        self.assertEqual(hosts, ["yandex.ru", "mc.yandex.ru", "sber.ru", "cf-site.ru"])
        cf = next(e for e in data if e["hostname"] == "cf-site.ru")
        self.assertEqual(cf["ips"], ["84.252.149.1"])
        for e in data:
            self.assertEqual(set(e), {"hostname", "ip", "ips"})
            self.assertEqual(e["ip"], e["ips"][0])
        self.assertEqual(read(dom).split(), hosts)

    def test_keep_foreign(self):
        out = os.path.join(self.dir, "full.json")
        harvest_ru.main(self.harvest_args(out, os.path.join(self.dir, "f.txt")) + ["--keep-foreign"])
        hosts = [e["hostname"] for e in load(out)]
        self.assertIn("foreign-only.ru", hosts)


class MakeListsTest(unittest.TestCase):
    ENTRIES = [
        {"hostname": "a.ru", "ip": "1.1.1.1", "ips": ["1.1.1.1", "1.1.1.2"]},
        {"hostname": "b.ru", "ip": "1.1.1.1", "ips": ["1.1.1.1"]},            # целиком повтор
        {"hostname": "c.ru", "ip": "1.1.1.2", "ips": ["1.1.1.2", "1.1.1.3"]},  # один повтор
        {"hostname": "d.ru", "ip": "1.1.1.4", "ips": ["1.1.1.4"]},
    ]

    def test_trim_drops_duplicate_ips_and_empty_entries(self):
        res = make_lists.trim(self.ENTRIES, 4)
        self.assertEqual([e["hostname"] for e in res], ["a.ru", "c.ru", "d.ru"])
        self.assertEqual(res[1]["ips"], ["1.1.1.3"])
        self.assertEqual(res[1]["ip"], "1.1.1.3")

    def test_trim_output_is_deterministic(self):
        """Порядок IP в ответе DNS случаен, но файл от него зависеть не должен."""
        a = [{"hostname": "x.ru", "ip": "9.9.9.9", "ips": ["9.9.9.9", "10.0.0.1", "2.2.2.2"]}]
        b = [{"hostname": "x.ru", "ip": "2.2.2.2", "ips": ["2.2.2.2", "9.9.9.9", "10.0.0.1"]}]
        self.assertEqual(make_lists.trim(a, 1), make_lists.trim(b, 1))
        self.assertEqual(make_lists.trim(a, 1)[0]["ips"], ["2.2.2.2", "9.9.9.9", "10.0.0.1"])
        self.assertEqual(make_lists.trim(a, 1)[0]["ip"], "2.2.2.2")

    def test_trim_respects_limit(self):
        self.assertEqual([e["hostname"] for e in make_lists.trim(self.ENTRIES, 1)], ["a.ru"])

    def test_write_lists(self):
        with tempfile.TemporaryDirectory() as d:
            summary = make_lists.write_lists(self.ENTRIES, [2, 4], d)
            self.assertEqual([s[0] for s in summary], [2, 4])
            self.assertTrue(os.path.exists(os.path.join(d, "amnezia_top2.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "amnezia_top4.json")))


class UpdateTest(Base):
    def test_update_end_to_end(self):
        data_dir = os.path.join(self.dir, "data")
        lists_dir = os.path.join(self.dir, "lists")
        update.main(["--sizes", "2,10", "--data-dir", data_dir, "--lists-dir", lists_dir,
                     "--no-tranco", "--no-majestic", "--no-umbrella", "--extra", self.domains,
                     "--ru-cidr-file", self.cidr, "--workers", "4"])
        top2 = load(os.path.join(lists_dir, "amnezia_top2.json"))
        top10 = load(os.path.join(lists_dir, "amnezia_top10.json"))
        self.assertEqual([e["hostname"] for e in top2], ["yandex.ru", "mc.yandex.ru"])
        self.assertEqual(len(top10), 4)

    def _run_update(self, lists_dir, *extra):
        update.main(["--sizes", "2", "--data-dir", os.path.join(self.dir, "data"),
                     "--lists-dir", lists_dir, "--no-tranco", "--no-majestic", "--no-umbrella",
                     "--extra", self.domains, "--ru-cidr-file", self.cidr, "--workers", "4", *extra])

    def test_min_ratio_protects_old_lists(self):
        lists_dir = os.path.join(self.dir, "lists")
        os.makedirs(lists_dir)
        old_path = os.path.join(lists_dir, "amnezia_top2.json")
        old = [{"hostname": f"old{i}.ru", "ip": f"1.1.1.{i}", "ips": [f"1.1.1.{i}"]} for i in range(1, 6)]
        with open(old_path, "w", encoding="utf-8") as f:
            json.dump(old, f)
        # старый top2 на 5 записей (так бывает только в тесте), новый получится из 2: 2 < 5 * 0.9
        with self.assertRaises(SystemExit):
            self._run_update(lists_dir, "--min-ratio", "0.9")
        self.assertEqual(load(old_path), old)  # файл не тронут

    def test_min_ratio_allows_normal_update(self):
        lists_dir = os.path.join(self.dir, "lists")
        self._run_update(lists_dir)                       # первая сборка
        self._run_update(lists_dir, "--min-ratio", "0.9")  # повторная: размер тот же
        self.assertEqual(len(load(os.path.join(lists_dir, "amnezia_top2.json"))), 2)

    def test_skip_harvest_needs_existing_file(self):
        with self.assertRaises(SystemExit):
            update.main(["--skip-harvest", "--data-dir", os.path.join(self.dir, "nope")])


class ShippedListsTest(unittest.TestCase):
    """Готовые списки в репозитории должны быть в формате Amnezia."""

    def test_shipped_lists(self):
        for name in ("amnezia_top300.json", "amnezia_top1000.json"):
            path = os.path.join(ROOT, "lists", name)
            data = load(path)
            self.assertGreater(len(data), 100, name)
            all_ips = []
            for e in data:
                self.assertEqual(set(e), {"hostname", "ip", "ips"}, name)
                self.assertEqual(e["ip"], e["ips"][0], name)
                all_ips += e["ips"]
            self.assertEqual(len(all_ips), len(set(all_ips)), f"{name}: повторяются IP")


if __name__ == "__main__":
    unittest.main()
