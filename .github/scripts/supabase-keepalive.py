#!/usr/bin/env python3
"""
Supabase free-tier keep-alive — tum projeleri tek yerden canli tutar.

NEDEN: Free-tier proje 7 gun GERCEK DB sorgusu almazsa otomatik pause olur.
Cache'li API-gateway cevaplari (root /rest/v1/ gibi) SAYILMAZ. Bu yuzden her
proje icin gercek bir tablo count sorgusu atiyoruz (RLS bos dondurse bile
sorgu Postgres'te calisir = aktivite sayilir).

Hedefler KEEPALIVE_TARGETS secret'inda JSON olarak:
  [{"name":"app","url":"https://REF.supabase.co","key":"<public anon/publishable>","table":"profiles"}, ...]

Yeni proje eklemek: secret'taki JSON'a bir satir ekle (gh secret set KEEPALIVE_TARGETS).
"""
import json
import os
import subprocess
import sys
import time

# table alani 404 verirse denenecek yedek tablolar (sema-bagimsizlik icin)
FALLBACK_TABLES = [
    "profiles", "referrals", "push_tokens", "generations",
    "app_settings", "entitlements", "user_packages", "payments",
]


def count_query(url, key, table, attempts=3):
    """Gercek bir DB count sorgusu. HTTP kodunu doner. 200/206 = basarili.

    000 (baglanti/DNS hatasi) runner'da gecici olabilir; kisa beklemeyle
    birkac kez dener. Proje gercekten paused ise tum denemeler 000 doner.
    """
    code = "000"
    for n in range(attempts):
        if n:
            time.sleep(3)
        r = subprocess.run(
            [
                "curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{http_code}",
                "-H", f"apikey: {key}",
                "-H", f"Authorization: Bearer {key}",
                "-H", "Prefer: count=exact",
                "-H", "Range: 0-0",
                f"{url}/rest/v1/{table}?select=*",
            ],
            capture_output=True, text=True,
        )
        code = r.stdout.strip()
        if code != "000":
            break
    return code


def main():
    raw = os.environ.get("KEEPALIVE_TARGETS", "").strip()
    if not raw:
        print("HATA: KEEPALIVE_TARGETS secret bos/tanimsiz.")
        sys.exit(1)
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"HATA: KEEPALIVE_TARGETS gecerli JSON degil: {e}")
        sys.exit(1)

    # NOT: Repo public, run loglari da public. App isimlerini SIZDIRMAMAK icin
    # logda sadece sira numarasi (#1..#N) basiyoruz; index->isim eslesmesi
    # KEEPALIVE_TARGETS secret'indaki sirada (gizli kalir).
    failures = []
    for i, t in enumerate(targets, 1):
        label = f"#{i}"
        url = t["url"]
        key = t["key"]
        # once projeye ozel bilinen tablo, sonra yedekler
        primary = t.get("table")
        tables = ([primary] if primary else []) + [c for c in FALLBACK_TABLES if c != primary]

        hit = None
        last = None
        for table in tables:
            last = count_query(url, key, table)
            if last in ("200", "206"):
                hit = (table, last)
                break
            # 000 = host'a ulasilamiyor (paused/down); baska tablo denemek
            # bosa zaman, fail-fast. 404 ise sonraki aday tabloyu dene.
            if last == "000":
                break

        if hit:
            print(f"OK    {label:4} {hit[0]:14} -> HTTP {hit[1]}", flush=True)
        else:
            print(f"FAIL  {label:4} (son HTTP {last})", flush=True)
            failures.append(label)

    print("")
    if failures:
        print(f"{len(failures)}/{len(targets)} proje pinglenemedi: {failures}")
        sys.exit(1)
    print(f"Tum {len(targets)} proje canli tutuldu (gercek DB sorgusu).")


if __name__ == "__main__":
    main()
