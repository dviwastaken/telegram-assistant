"""Tekrarlayan hatırlatmalar: sıradaki zamanı hesaplar.

Tekrarlayan bir hatırlatma Notion'da tek kayıttır. Gönderilince yeni kayıt açılmaz, aynı kaydın zamanı sıradakine
ilerler ve durumu "Bekliyor" kalır. Seriyi durdurmak için kaydı iptal etmek yeter. Bulut hatırlatıcısı
(`bulut/src/index.js`) aynı kuralı uygular; iki taraf aynı test örnekleriyle sınanır (`test/tekrar_ornekleri.json`).
"""
import calendar
import datetime as dt

# yerel kod -> Notion "Tekrar" seçeneği
TR = {"gunluk": "Günlük", "haftaici": "Hafta içi", "haftalik": "Haftalık", "aylik": "Aylık"}
YEREL = {v: k for k, v in TR.items()}
ADLAR = {"gunluk": "her gün", "haftaici": "hafta içi her gün", "haftalik": "her hafta", "aylik": "her ay"}


def kod(deger):
    """'Haftalık' / 'haftalik' / 'Yok' / None -> 'haftalik' ya da None. Tanınmayan değer ValueError."""
    if deger in (None, "", "Yok", "yok"):
        return None
    if deger in TR:
        return deger
    if deger in YEREL:
        return YEREL[deger]
    raise ValueError(f"tanınmayan tekrar: {deger} (gunluk, haftaici, haftalik ya da aylik olmalı)")


def _ay_ekle(z, gun):
    """Bir sonraki ay, aynı gün (ay kısaysa ayın son günü)."""
    yil, ay = (z.year + 1, 1) if z.month == 12 else (z.year, z.month + 1)
    return z.replace(year=yil, month=ay, day=min(gun, calendar.monthrange(yil, ay)[1]))


def _bir_adim(z, tekrar, gun):
    if tekrar == "gunluk":
        return z + dt.timedelta(days=1)
    if tekrar == "haftalik":
        return z + dt.timedelta(days=7)
    if tekrar == "haftaici":
        z += dt.timedelta(days=1)
        while z.weekday() >= 5:
            z += dt.timedelta(days=1)
        return z
    if tekrar == "aylik":
        return _ay_ekle(z, gun)
    raise ValueError(tekrar)


def sonraki(zaman, tekrar, simdi):
    """`zaman`dan sonra gelen ve `simdi`den sonraki ilk tekrar. Kaçırılanlar atlanır (bilgisayar günlerce kapalı
    kaldıysa birikmiş kopyalar gelmesin). Ayın 31'i gibi günler kısa aylarda ayın son gününe düşer; kayıtta yalnızca
    zaman tutulduğu için seri oradan devam eder (31 Ekim -> 30 Kasım -> 30 Aralık)."""
    z = zaman
    for _ in range(4000):  # 10 yıldan uzun günlük seri yok; sonsuz döngüye karşı sınır
        z = _bir_adim(z, tekrar, zaman.day)
        if z > simdi:
            return z
    raise ValueError("sıradaki tekrar bulunamadı")
