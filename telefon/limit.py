"""Claude abonelik limiti: arka plandaki `claude -p` çıktılarında limit mesajı görülünce bir kez Telegram'a yazar ve
sıfırlanana kadar acil olmayan işleri (ders notu, ödev değerlendirmesi, görev, mail değerlendirmesi) bekletir.
Limit kullanıcının kendi Claude Code kullanımıyla ortak; 2026-10-09'da %93'e çıkmıştı."""
import datetime as dt
import re
import threading

from .ayarlar import log, sessiz_mi

LIMIT = re.compile(r"(session|usage|weekly|5-hour|rate) limit|hit your .{0,20}limit|limit reached|out of (extra )?usage",
                   re.I)
# "resets 12am", "resets at 3:30pm", haftalık limitte "resets Oct 22, 11pm" (tarih de var: o gün sayılır)
SAAT = re.compile(r"resets?\s+(?:at\s+)?(?:([A-Za-z]{3,9})\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)
AYLAR = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_durum = {"bitis": None}
_kilit = threading.Lock()


def _sifirlanma(metin, simdi):
    """(sıfırlanma zamanı, kullanıcıya yazılacak metin). Saat yazmıyorsa yarım saat sonra yeniden denenir."""
    m = SAAT.search(metin or "")
    if not m:
        return simdi + dt.timedelta(minutes=30), None
    ay, gun, saat, dakika, ogle = m.groups()
    s = int(saat) % 12 + (12 if ogle.lower() == "pm" else 0)
    z = simdi.replace(hour=s, minute=int(dakika or 0), second=0, microsecond=0)
    if ay:
        try:
            z = z.replace(month=AYLAR.index(ay[:3].lower()) + 1, day=int(gun))
            if z <= simdi - dt.timedelta(days=180):  # "Jan 2" aralıkta okunduysa gelecek yılın
                z = z.replace(year=z.year + 1)
        except ValueError:  # tanınmayan ay adı ya da geçersiz gün
            return simdi + dt.timedelta(minutes=30), None
        if z <= simdi:  # geçmiş bir tarih: limit sürüyorsa yarım saatte bir yeniden dene
            return simdi + dt.timedelta(minutes=30), None
    elif z <= simdi:
        z += dt.timedelta(days=1)
    from .eylemler import tarih_metni
    return z, tarih_metni(z, simdi=simdi)


def bitis():
    with _kilit:
        return _durum["bitis"]


def kontrol(metin, kaynak, simdi=None):
    """Çıktıda limit mesajı varsa durumu işaretle (ilk seferde Telegram). Limit varsa True."""
    if not metin or not LIMIT.search(metin[-2000:]):
        return False
    simdi = simdi or dt.datetime.now()
    bitis, saat = _sifirlanma(metin, simdi)
    with _kilit:
        yeni = _durum["bitis"] is None or _durum["bitis"] <= simdi
        _durum["bitis"] = bitis
    log(f"limit: {kaynak} çıktısında kullanım limiti görüldü, sıfırlanma {saat or 'bilinmiyor'}")
    if yeni:
        from .telegram import gonder
        gonder(f"Claude kullanım limiti doldu ({kaynak}).\n"
               + (f"{saat[0].upper() + saat[1:]} civarı sıfırlanacak.\n" if saat else "Sıfırlanma saati yazmıyor.\n")
               + "O zamana kadar sohbette cevap veremeyebilirim; ders notu, ödev değerlendirmesi, görevler ve mail "
                 "değerlendirmesi bekliyor.\n"
                 "Hatırlatmalar ve İEU Learn ödev bildirimleri sürüyor.", sessiz=sessiz_mi())
    return True


def dolu_mu(simdi=None):
    with _kilit:
        return _durum["bitis"] is not None and (simdi or dt.datetime.now()) < _durum["bitis"]


def bekle_mesaji():
    with _kilit:
        b = _durum["bitis"]
    if not b:
        return "Claude kullanım limiti dolu"
    from .eylemler import tarih_metni
    return f"Claude kullanım limiti dolu, {tarih_metni(b)} civarı sıfırlanacak"
