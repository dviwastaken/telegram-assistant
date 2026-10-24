"""Gece planlayıcısı: her gece 23:30'da ertesi günü gözden geçirir, brifing/hatırlatma mesajlarını planlar.

Model (haiku) yalnızca öneri JSON'u üretir; kurallar kodda da zorlanır (saat aralığı, günlük üst sınır,
mevcut hatırlatmalarla çakışma). Brifingin metnini zamanı gelince sohbet beyni güncel bağlamla yazar. Plan dosyası kayitlar/plan-YYYY-MM-DD.json olarak saklanır.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import limit
from . import eylemler
from .ayarlar import KAYITLAR, KOK, ayar, log

CLAUDE = shutil.which("claude")
PLAN_SAATI = ayar("PLAN_SAATI", "23:30")
EN_ERKEN, EN_GEC = dt.time(8, 30), dt.time(22, 30)
GUNLUK_UST_SINIR = 3

ISTEM = """kullanıcının kişisel asistanısın. Yarının ({yarin}, {gun}) Telegram brifing ve hatırlatma mesajlarını
planlıyorsun. Kullanıcı üniversitede okuyan bir öğrenci. Mesajlar onu gereksiz yere rahatsız etmemeli: değeri
olmayan mesaj planlama.

Bağlam (takvim, kurulu hatırlatmalar, okunmamış notlar):
{baglam}

Kurallar:
- Yarın dersi, yaklaşan (3 gün içinde) bir teslimi ya da sınavı, ya da önemli okunmamış notu varsa bir brifing
  planla. Brifingin saati {brifing} (kullanıcının kuralı: ders yoksa 13:00, ders varsa 13:00'ten sonraki ilk uzun
  boşluk); başka saat seçme. Önemli bir şey yoksa brifing planlama.
- Takvimdeki randevu ve etkinlikler için, zaten kurulu bir hatırlatma yoksa 30-60 dk önce hatırlatma planla.
- Kurulu hatırlatmalarla aynı konuyu tekrar etme. Mesajlar 08:30 ile 22:30 arasında olsun, en fazla 3 tane planla.
- Hatırlatmalarda "konu", kullanıcıya gidecek mesajın kendisi olsun: tek başına anlaşılır, kısa bir cümle.
- Brifingde "konu", brifingde değinilecek başlıkların kısa listesi olsun (metni zamanı gelince ayrıca yazılacak).

Yalnızca şu JSON'u döndür:
{{"aramalar": [{{"zaman": "YYYY-MM-DDTHH:MM", "tur": "brifing|hatirlatma", "konu": "..."}}], "not": "plan hakkında tek cümle"}}"""


class LimitDolu(RuntimeError):
    """Claude limiti dolu; args[0] sıfırlanma zamanı (datetime ya da None)."""


BRIFING_EN_ERKEN = dt.time(13, 0)
UZUN_BOSLUK = dt.timedelta(minutes=60)


def gunun_bloklari(gun):
    """O günün ders/etkinlik aralıkları [(başlangıç, bitiş)]. Haftalık derslerin bitişi Not'taki 'SS:DD-SS:DD'den."""
    t = eylemler.oku(eylemler.TAKVIM, {"tekrarlayan": [], "olaylar": []})
    bloklar = []
    for r in t.get("tekrarlayan", []):
        if r.get("gun") != gun.weekday():
            continue
        m = re.search(r"(\d{2}:\d{2})-(\d{2}:\d{2})", r.get("not") or "")
        bas = dt.datetime.combine(gun, dt.time.fromisoformat(m.group(1) if m else r["saat"]))
        bit = dt.datetime.combine(gun, dt.time.fromisoformat(m.group(2))) if m else bas + dt.timedelta(minutes=90)
        bloklar.append((bas, bit))
    for o in t.get("olaylar", []):
        if o.get("tur") in ("Ders", "Sınav", "Randevu", "Etkinlik") and (o.get("zaman") or "")[:10] == gun.isoformat():
            bas = dt.datetime.fromisoformat(o["zaman"])
            bloklar.append((bas, bas + dt.timedelta(minutes=60)))
    return sorted(bloklar)


def brifing_saati(gun, bloklar=None):
    """Kullanıcının kuralı (2026-10-09): ders yoksa 13:00; varsa 13:00'ten sonraki ilk en az 60 dk'lık boşluğun başı."""
    bloklar = gunun_bloklari(gun) if bloklar is None else bloklar
    t = dt.datetime.combine(gun, BRIFING_EN_ERKEN)
    for bas, bit in bloklar:
        if bit <= t:
            continue
        if bas - t >= UZUN_BOSLUK:
            break
        t = max(t, bit) + dt.timedelta(minutes=5)
    return min(t, dt.datetime.combine(gun, EN_GEC)).time()


def planla(gun=None, uygula=True):
    """gun (date) için plan yap. uygula=False ise yalnızca öneriyi döndürür (kuru çalıştırma)."""
    gun = gun or (dt.date.today() + dt.timedelta(days=1))
    brifing = brifing_saati(gun)
    istem = ISTEM.format(yarin=gun.isoformat(), gun=eylemler.GUNLER[gun.weekday()], baglam=eylemler.baglam_ozeti(gun=4),
                         brifing=f"{gun.isoformat()}T{brifing:%H:%M}")
    args = [CLAUDE, "-p", "--model", "haiku", "--output-format", "json", "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--system-prompt", "Yalnızca istenen JSON'u üret."]
    p = subprocess.run(args, input=istem, capture_output=True, text=True, encoding="utf-8", cwd=KOK, timeout=240,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    sonuc = str(json.loads(p.stdout).get("result", ""))
    if limit.kontrol(sonuc, "gece planlayıcısı"):
        raise LimitDolu(limit.bitis())
    m = re.search(r"\{.*\}", sonuc, re.S)
    if not m:
        raise RuntimeError(f"plan JSON'u yok: {sonuc[:200]}")
    oneri = json.loads(m.group(0))

    mevcut = [a for a in eylemler.oku(eylemler.ARAMALAR, []) if a.get("durum") == "bekliyor"]
    kabul, red = [], []
    for a in oneri.get("aramalar", []):
        try:
            z = dt.datetime.fromisoformat(a["zaman"])
        except (KeyError, ValueError, TypeError):
            red.append((a, "zaman okunamadı"))
            continue
        if a.get("tur") == "brifing" and z.date() == gun:  # saati model değil kural belirler
            z = dt.datetime.combine(gun, brifing)
            a["zaman"] = z.isoformat(timespec="minutes")
        if z.date() != gun:
            red.append((a, "başka bir gün"))
        elif not (EN_ERKEN <= z.time() <= EN_GEC):
            red.append((a, "saat aralığı dışında"))
        elif z <= dt.datetime.now() + dt.timedelta(minutes=10):
            red.append((a, "geçmiş"))
        elif any(abs(dt.datetime.fromisoformat(b["zaman"]) - z) <= dt.timedelta(minutes=30) for b in mevcut):
            red.append((a, "kurulu bir hatırlatmayla 30 dk içinde çakışıyor"))
        elif len(kabul) >= GUNLUK_UST_SINIR:
            red.append((a, "günlük üst sınır"))
        else:
            kabul.append(a)
    if uygula:
        for a in kabul:
            eylemler.uygula({"tip": "hatirlat", "zaman": a["zaman"], "konu": a.get("konu", "")[:300],
                             "tur": a.get("tur", "hatirlatma")}, kaynak="planlayıcı")
    kayit = {"gun": gun.isoformat(), "olusturuldu": dt.datetime.now().isoformat(timespec="seconds"),
             "uygulandi": uygula, "not": oneri.get("not"), "kabul": kabul,
             "red": [{"arama": a, "neden": n} for a, n in red]}
    with open(os.path.join(KAYITLAR, f"plan-{gun.isoformat()}{'' if uygula else '-kuru'}.json"), "w",
              encoding="utf-8") as f:
        json.dump(kayit, f, ensure_ascii=False, indent=2)
    log(f"planlayıcı: {gun} için {len(kabul)} mesaj {'kuruldu' if uygula else 'önerildi (kuru)'}, "
        f"{len(red)} öneri reddedildi — {oneri.get('not', '')}")
    return kayit


def durum_satiri(simdi=None):
    """/durum için: en son hangi gün için, ne zaman plan yapıldı; bu geceki plan eksikse uyar."""
    simdi = simdi or dt.datetime.now()
    planlar = sorted(f for f in os.listdir(KAYITLAR) if re.fullmatch(r"plan-\d{4}-\d\d-\d\d\.json", f))
    if not planlar:
        return "Gece planı: henüz yapılmadı."
    try:
        with open(os.path.join(KAYITLAR, planlar[-1]), encoding="utf-8") as f:
            k = json.load(f)
        gun = dt.date.fromisoformat(k["gun"])
        olusturuldu = dt.datetime.fromisoformat(k["olusturuldu"])
    except (OSError, ValueError, KeyError) as e:
        return f"⚠️ sorun: gece planı okunamadı ({type(e).__name__})."
    sat = (f"Gece planı: {eylemler.tarih_metni(k['gun'], saat=False, simdi=simdi)} için yapıldı "
           f"({eylemler.tarih_metni(olusturuldu, simdi=simdi)}), {len(k.get('kabul', []))} mesaj kuruldu.")
    saat, dakika = map(int, PLAN_SAATI.split(":"))
    beklenen = simdi.date() + dt.timedelta(days=1 if simdi.time() >= dt.time(saat, dakika) else 0)
    if gun < beklenen and simdi - dt.datetime.combine(beklenen - dt.timedelta(days=1), dt.time(saat, dakika)) \
            > dt.timedelta(minutes=60):  # planlayıcı 3 kez 10 dk arayla yeniden dener
        gun_metni = eylemler.tarih_metni(beklenen.isoformat(), saat=False, simdi=simdi)
        sat = "⚠️ sorun: " + sat + f" {gun_metni[0].upper() + gun_metni[1:]} için plan yok."
    return sat


class GecePlanlayicisi:
    def __init__(self):
        threading.Thread(target=self._dongu, daemon=True, name="planlayici").start()

    def _dongu(self):
        while True:
            simdi = dt.datetime.now()
            saat, dakika = map(int, PLAN_SAATI.split(":"))
            hedef = simdi.replace(hour=saat, minute=dakika, second=0, microsecond=0)
            if hedef <= simdi:
                hedef += dt.timedelta(days=1)
            time.sleep(max(30, (hedef - simdi).total_seconds()))
            yarin = dt.date.today() + dt.timedelta(days=1)
            if os.path.exists(os.path.join(KAYITLAR, f"plan-{yarin.isoformat()}.json")):
                continue  # bu gün için zaten plan yapılmış (ör. servis yeniden başladı)
            for deneme in range(3):
                try:
                    planla(yarin)
                    break
                except LimitDolu as e:
                    # 10 dk sonra yeniden denemek boşa: sıfırlanmayı bekle, sabaha (brifingden önce) yetişmiyorsa bırak
                    bitis = e.args[0]
                    if not bitis or bitis >= dt.datetime.combine(yarin, EN_ERKEN):
                        log(f"planlayıcı: Claude limiti dolu, {yarin} için plan yapılamadı (sıfırlanma {bitis})")
                        break
                    log(f"planlayıcı: Claude limiti dolu, {bitis:%H:%M}'de yeniden denenecek")
                    time.sleep(max(60, (bitis - dt.datetime.now()).total_seconds() + 120))
                except Exception as e:
                    log(f"planlayıcı hatası ({deneme + 1}/3):", repr(e))
                    time.sleep(600)
