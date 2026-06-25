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

GECICI HATA DAYANIKLILIGI: GitHub runner <-> Supabase Cloudflare-edge yolunda
tek seferlik blip'ler olur (000 baglanti hatasi, 402/429, 5xx, 520-524 CF
hatalari). Bu blip'ler PROJE basina ve RUN basina gezer; gercek bir pause/
restriction DEGILDIR. Tek bir gecici blip butun run'i fail edip "all jobs
failed" mailini tetiklememeli. Bu yuzden: gecici kodlar birden cok GECISTE
(pass) tekrar denenir; sadece tum gecislerden sonra hala basarisiz olan proje
GERCEK sorun sayilir (exit 1 -> mail). Boylece mail sadece kalici problemde
gelir, gurultu sifirlanir.
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

# Gecici (transient) kodlar: tekrar denemeye deger. Bir sonraki geciste
# muhtemelen duzelir. Bunlarin disindaki basarisizliklar (401 yanlis key,
# 403, 404-tukenmis sema) KALICI sayilir; tekrar denemek bosa zaman.
#   000     -> baglanti/DNS hatasi (runner gecici)
#   402     -> gateway "Payment Required" blip'i (kalici restriction degilse gecer)
#   408     -> request timeout
#   425/429 -> too early / rate limit
#   5xx     -> Supabase origin gecici hata
#   520-524 -> Cloudflare edge<->origin gecici hatalari (521 = origin down)
TRANSIENT_CODES = {
    "000", "402", "408", "425", "429",
    "500", "502", "503", "504",
    "520", "521", "522", "523", "524",
}

PASSES = 3          # toplam deneme gecisi sayisi
PASS_WAIT = 15      # gecisler arasi bekleme (sn) — global blip'in gecmesi icin


def count_query(url, key, table):
    """Gercek bir DB count sorgusu (tek istek). HTTP kodunu doner.
    200/206 = basarili. Baglanti kurulamazsa "000" doner.
    """
    r = subprocess.run(
        [
            "curl", "-sS", "--connect-timeout", "8", "-m", "15",
            "-o", "/dev/null", "-w", "%{http_code}",
            "-H", f"apikey: {key}",
            "-H", f"Authorization: Bearer {key}",
            "-H", "Prefer: count=exact",
            "-H", "Range: 0-0",
            f"{url}/rest/v1/{table}?select=*",
        ],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or "000"


def ping_once(t):
    """Bir hedefe gercek count sorgusu atar.
    (ok: bool, table: str|None, code: str) doner.
    Birincil tablo 404 verirse yedek tablolari sirayla dener (sema farki).
    200/206 disi, 404 disi bir kodda fail-fast (yedek tablo denemek bosa).
    """
    url = t["url"]
    key = t["key"]
    primary = t.get("table")
    tables = ([primary] if primary else []) + [c for c in FALLBACK_TABLES if c != primary]

    last = "000"
    for table in tables:
        code = count_query(url, key, table)
        if code in ("200", "206"):
            return True, table, code
        last = code
        if code != "404":
            # 000/402/5xx/401... -> baska tablo denemek anlamsiz
            break
    return False, None, last


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

    if not isinstance(targets, list) or not targets:
        print("HATA: KEEPALIVE_TARGETS bos liste / liste degil.")
        sys.exit(1)

    # KEEPALIVE_SKIP: gecici olarak atlanacak projeler (virgulle ayrilmis
    # ref/url parcasi). Kullanim: bir proje GERCEKTEN kalici sorunluysa
    # (or. egress-restricted 402) ve farkindaysak, gunluk yanlis-olmayan ama
    # tekrarlayan mail'i susturmak icin secret'a refini ekle. App ismi
    # sizdirmamak icin ref secret'ta tutulur (logda yalniz #N gorunur).
    # Duzeldikten sonra secret'tan cikar -> tekrar izlenir.
    skip_raw = os.environ.get("KEEPALIVE_SKIP", "").strip()
    skips = [s.strip() for s in skip_raw.split(",") if s.strip()]

    def is_skipped(t):
        url = str(t.get("url", ""))
        return any(s in url for s in skips)

    # NOT: Repo public, run loglari da public. App isimlerini SIZDIRMAMAK icin
    # logda sadece sira numarasi (#1..#N) basiyoruz; index->isim eslesmesi
    # KEEPALIVE_TARGETS secret'indaki sirada (gizli kalir).
    #
    # Cok-gecisli deneme: ilk geciste basarisiz olan AMA kodu gecici (transient)
    # olan hedefler bir sonraki geciste tekrar denenir. Kalici hata (401/403/
    # 404-tukenmis) ya da basari -> hedef "kesinlesir", bir daha denenmez.
    results = {}                              # idx -> (ok, table, code) | ("SKIP",)
    pending = []                              # [(idx, target), ...] (atlanmayanlar)
    skipped = 0
    for idx, t in enumerate(targets, 1):
        if is_skipped(t):
            results[idx] = ("SKIP", None, "-")
            skipped += 1
        else:
            pending.append((idx, t))

    for p in range(PASSES):
        if p:
            print(f"... {len(pending)} hedef gecici hata verdi, "
                  f"{PASS_WAIT}s sonra tekrar deneniyor (gecis {p + 1}/{PASSES})", flush=True)
            time.sleep(PASS_WAIT)
        retry = []
        for idx, t in pending:
            ok, table, code = ping_once(t)
            results[idx] = (ok, table, code)
            if not ok and code in TRANSIENT_CODES:
                retry.append((idx, t))        # gecici -> sonraki geciste tekrar dene
        pending = retry
        if not pending:
            break

    # Rapor — index sirasinda
    failures = []
    for idx in range(1, len(targets) + 1):
        ok, table, code = results[idx]
        label = f"#{idx}"
        if ok == "SKIP":
            print(f"SKIP  {label:4} (manuel atlandi: KEEPALIVE_SKIP)", flush=True)
        elif ok:
            print(f"OK    {label:4} {table:14} -> HTTP {code}", flush=True)
        else:
            print(f"FAIL  {label:4} (son HTTP {code})", flush=True)
            failures.append(label)

    print("")
    active = len(targets) - skipped
    if failures:
        # Buraya geldiyse: hedef TUM gecislerde basarisiz oldu = gecici degil,
        # GERCEK problem (paused/restricted/yanlis key). Mail HAK EDILMIS.
        print(f"{len(failures)}/{active} aktif proje {PASSES} gecisten sonra hala "
              f"pinglenemedi: {failures}")
        sys.exit(1)
    extra = f" ({skipped} proje atlandi)" if skipped else ""
    print(f"Tum {active} aktif proje canli tutuldu (gercek DB sorgusu).{extra}")


if __name__ == "__main__":
    main()
