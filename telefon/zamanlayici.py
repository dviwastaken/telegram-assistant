"""Zamanı gelen hatırlatmaları Telegram'a yazar (Tamam / ertele düğmeleriyle); brifingleri sohbete yazdırır.

Kaynak `<takvim klasörü>/aramalar.json` (Notion "Hatırlatma" veritabanının yerel kopyası). Gönderilen kaydın
durumu "arandi" olur (Notion'da "Gönderildi"). Tekrarlayan kayıt ise kapanmaz, sıradaki zamanına ilerler
(`tekrar.py`). Bilgisayar kapalıyken kaçırılanlar açılınca yazılır: 12 saatten yenileri tek tek ("geç kaldım"
notuyla), daha eskileri tek mesajda toplu.
"""
import datetime as dt
import json
import threading
import time

from . import bulut, eylemler, notion, tekrar, telegram
from .gozculer.sistem import bulunma_eki
from .notion import sayfa_id
from .ayarlar import log

GEC_DK = 5            # bundan geç gönderilen hatırlatmaya "geç kaldım" notu düşülür
GEC_UST_DK = 60       # gün değişmiş olsa bile bu kadar dakikaya kadar geç kalan "N dk geç" yazar (gece yarısı kuralı)
COK_ESKI_SAAT = 12    # bundan eski kaçırılmışlar tek mesajda toplu bildirilir
BULUT_BEKLE = dt.timedelta(minutes=10)  # bulut hatırlatıcısına ulaşılamazsa bu kadar bekleyip yine de gönder
GONDERIM_DENEME = 3                     # sahiplenilmiş hatırlatma Telegram'a gidemezse en fazla bu kadar tur
ILK_CEKIS_BEKLE = 300                   # sn: bulut açıkken ilk Notion çekişini en fazla bu kadar bekle


def hatirlatma_dugmeleri(konu, not_="", arama_id=None, tekrar_kodu=None):
    k = telegram.dugme_verisi({"tur": "hatirlatma", "konu": konu, "not": not_, "id": arama_id})
    satirlar = [[("Tamam", f"h:ok:{k}"), ("10 dk ertele", f"h:e:10:{k}"), ("1 saat ertele", f"h:e:60:{k}"),
                 ("Yarına ertele", f"h:e:y:{k}")]]
    if tekrar_kodu and arama_id:
        satirlar.append([("Bu tekrarı durdur", f"h:dur:{k}")])
    return satirlar


def _gecikme_metni(fark):
    """Geç kalma süresi: '12 dk', '1 saat 5 dk', '3 saat'."""
    dk = max(1, int(fark.total_seconds() // 60))
    sa, dk = divmod(dk, 60)
    return f"{sa} saat" + (f" {dk} dk" if dk else "") if sa else f"{dk} dk"


def hatirlatma_metni(a, simdi):
    """Saat bir kez yazılır: zamanında '· bugün 20:00', geç kalınca '· bugün 20:00 içindi, 12 dk geç'. Gün değişmiş
    olsa bile gecikme 60 dk'yı aşmadıysa yine 'N dk geç' ('dün 23:58 içindi, 6 dk geç'); 60 dk'yı aşan ve başka güne
    kalan: '· 16 Eki Cum 20:00 içindi, bilgisayar kapalıydı'. Konu ve not Notion'dan gelir: biçim işaretleri atılır.
    Not satır satır italik (çok satırlı notta '*' satırlar arasında eşleşmeyip ham görünmesin; boş satırlar atılır).
    bulut/src/zaman.js hatirlatmaMetni ile aynı çıktı."""
    z = dt.datetime.fromisoformat(a["zaman"])
    simge = "🚨 " if a.get("tur") == "acil" else ""
    zaman = eylemler.tarih_metni(z, simdi=simdi)
    fark = simdi - z
    if fark > dt.timedelta(minutes=GEC_DK):
        zaman += " içindi, " + (f"{_gecikme_metni(fark)} geç"
                                if z.date() == simdi.date() or fark <= dt.timedelta(minutes=GEC_UST_DK)
                                else "bilgisayar kapalıydı")
    not_satirlari = [f"*{s.strip()}*" for s in telegram.duz_metin(a.get("not") or "").replace("\r\n", "\n").split("\n")
                     if s.strip()]
    seri = f"\n{tekrar.ADLAR.get(a['tekrar'], a['tekrar'])} tekrarlanıyor" if a.get("tekrar") else ""
    return (f"{simge}**Hatırlatma** · {zaman}\n{telegram.duz_metin(a['konu'])}"
            + "".join(f"\n{s}" for s in not_satirlari) + seri)


def ertele_zamani(secim, simdi=None):
    simdi = (simdi or dt.datetime.now()).replace(second=0, microsecond=0)
    if secim == "y":
        return simdi + dt.timedelta(days=1)
    return simdi + dt.timedelta(minutes=int(secim))


class Zamanlayici:
    def __init__(self, sohbet=None, baslat=True):
        self.sohbet = sohbet
        self.gonderilen = set()   # "id@zaman": durum yazılamazsa bile aynı hatırlatma ikinci kez gitmesin
        self.bulut_bekleyen = {}  # "id@zaman" -> buluta ilk kez sorulamadığı an
        self.gonderim_hatasi = {}  # "id@zaman" -> sahiplenilip Telegram'a gidemediği tur sayısı
        self.basladi = time.monotonic()
        if baslat:
            threading.Thread(target=self._dongu, daemon=True, name="zamanlayici").start()

    def _dongu(self):
        while True:
            try:
                self.kontrol()
            except Exception as e:
                log("zamanlayıcı hatası:", repr(e))
            time.sleep(20)

    def kontrol(self, simdi=None):
        simdi = simdi or dt.datetime.now()
        if (bulut.etkin() and not notion.ilk_cekis.is_set()
                and time.monotonic() - self.basladi < ILK_CEKIS_BEKLE):
            return  # açılışta önbellek bayat olabilir: bilgisayar kapalıyken bulutun gönderdikleri hâlâ "bekliyor"
        try:
            aramalar = eylemler.oku(eylemler.ARAMALAR, [])
        except json.JSONDecodeError:
            return
        vadesi = []
        for a in aramalar:
            if a.get("durum") != "bekliyor" or self._anahtar(a) in self.gonderilen:
                continue
            try:
                z = dt.datetime.fromisoformat(a["zaman"])
            except (KeyError, ValueError):
                continue
            if z <= simdi:
                vadesi.append((z, a))
        vadesi.sort(key=lambda x: x[0])
        eski = []
        for z, a in vadesi:
            if simdi - z <= dt.timedelta(hours=COK_ESKI_SAAT):
                continue
            karar = self._sahiplen(a, z, simdi)  # bulut göndermiş olabilir (12 saat dolmadan)
            if karar is False:
                self._isaretle(a, "arandi", simdi)
            elif karar:
                eski.append(a)
        if eski:
            telegram.gonder("Bilgisayar kapalıyken zamanı geçen hatırlatmalar:\n"
                            + "\n".join(f"• {eylemler.tarih_metni(a['zaman'], simdi=simdi)} · "
                                        f"{telegram.duz_metin(a['konu'])}" for a in eski))
            for a in eski:
                self._isaretle(a, "cevapsiz", simdi)
        for z, a in vadesi:
            if simdi - z > dt.timedelta(hours=COK_ESKI_SAAT):
                continue
            sahiplenildi = False
            if a.get("tur") != "brifing":  # brifingi bulut göndermez, sormaya gerek yok
                karar = self._sahiplen(a, z, simdi)
                if karar is None:
                    continue  # bulut sorulamadı: 20 sn sonra yeniden
                if karar is False:
                    log(f"hatırlatma buluttan gönderilmiş, burada atlandı: {a['zaman']} {a['konu'][:80]}")
                    self._isaretle(a, "arandi", simdi)
                    continue
                sahiplenildi = bool(a.get("notion") and bulut.etkin())
            if a.get("tur") == "brifing" and self.sohbet is not None:
                self.sohbet.brifing(a)
                log(f"brifing sohbete verildi: {a['konu'][:80]}")
            else:
                # Sahiplenilmiş kayıt hemen kuyruğa alınmaz: gidemezse sahiplenme bırakılır, 20 sn sonra yeniden
                # denenir, bilgisayar o arada kapanırsa bulut gönderir (kuyrukta kalsaydı bilgisayar yeniden açılana
                # kadar gitmezdi). Bu en fazla GONDERIM_DENEME tur sürer: zaman aşımında mesaj aslında gitmiş
                # olabilir, sonsuz döngü kopya çoğaltırdı. Son turda kuyruğa alınır.
                anahtar = self._anahtar(a)
                son_sans = self.gonderim_hatasi.get(anahtar, 0) >= GONDERIM_DENEME - 1
                mid = telegram.gonder(hatirlatma_metni(a, simdi), kuyruga=not sahiplenildi or son_sans,
                                      dugmeler=hatirlatma_dugmeleri(a["konu"], a.get("not", ""),
                                                                    a.get("notion") or a["id"], a.get("tekrar")))
                if sahiplenildi and mid is None and not son_sans:
                    self.gonderim_hatasi[anahtar] = self.gonderim_hatasi.get(anahtar, 0) + 1
                    bulut.birak(sayfa_id(a["notion"]), a["zaman"])
                    log(f"hatırlatma gönderilemedi, sahiplenme bırakıldı (20 sn sonra yeniden): {a['konu'][:60]}")
                    continue
                self.gonderim_hatasi.pop(anahtar, None)
                if sahiplenildi and mid is not None:
                    bulut.teslim(sayfa_id(a["notion"]), a["zaman"])
                log(f"hatırlatma gönderildi: {a['zaman']} {a['konu'][:80]}" + ("" if mid else " (kuyrukta)"))
            self._isaretle(a, "arandi", simdi)

    def _sahiplen(self, a, z, simdi):
        """Bilgisayar kapalıyken gönderen bulut hatırlatıcısıyla çift gönderim olmasın: Notion'daki kayıt için önce
        sahiplen. True: gönder, False: başka yerden gönderilmiş, None: buluta ulaşılamadı, biraz sonra yeniden sor.
        Bulut ilk denemeden beri BULUT_BEKLE kadar sorulamazsa (ağ yok ya da yapılandırma hatası) Notion'daki kayda
        bakılır: bulut o arada göndermişse kayıt kapanmış ya da ilerlemiştir; hâlâ aynı zamanda bekliyorsa gönderilir
        (hatırlatma kaybolmasın). Süre hatırlatmanın zamanından değil ilk denemeden sayılır: açılışta ağ gelmemişken
        bulutun saatler önce gönderdiği bir hatırlatma hemen ikinci kez gitmesin."""
        if not a.get("notion") or not bulut.etkin():
            return True
        karar = bulut.sahiplen(sayfa_id(a["notion"]), a["zaman"])
        if karar is not None:
            self.bulut_bekleyen.pop(self._anahtar(a), None)
            return karar
        ilk = self.bulut_bekleyen.setdefault(self._anahtar(a), simdi)
        if simdi - ilk < BULUT_BEKLE:
            return None
        kayit = notion.kayit_oku(a["notion"])
        if kayit and (kayit["durum"] != "bekliyor" or kayit["zaman"] != a["zaman"]):
            log(f"bulut sorulamadı ama Notion'da kayıt kapanmış/ilerlemiş, gönderilmedi: {a['konu'][:60]}")
            return False
        log(f"bulut {BULUT_BEKLE.seconds // 60} dk'dır sorulamıyor, hatırlatma yine de gönderiliyor: {a['konu'][:60]}")
        return True

    @staticmethod
    def _anahtar(a):
        return f"{a['id']}@{a.get('zaman')}"

    def _isaretle(self, a, durum, simdi=None):
        """Gönderildi/cevapsız: tek seferlik kayıt kapanır, tekrarlayan kayıt sıradaki zamanına ilerler (kontrolün
        `simdi`sine göre: gerçek saat değil, böylece sonuç ve testler kontrol anına bağlı)."""
        self.gonderilen.add(self._anahtar(a))
        try:
            if a.get("tekrar"):
                yeni = eylemler.hatirlatma_ilerlet(a["id"], simdi)
                log(f"tekrarlayan hatırlatma ilerledi: {a['konu'][:60]} → {yeni}")
            else:
                eylemler.arama_durumu(a["id"], durum)
        except Exception as e:
            log("hatırlatma durumu yazılamadı:", repr(e)[:150])

    @staticmethod
    def ertele(veri, secim, simdi=None):
        """Düğmeden erteleme: aynı konuyla yeni hatırlatma kur. Dönüş: kısa onay metni."""
        z = ertele_zamani(secim, simdi)
        eylemler.uygula({"tip": "hatirlat", "zaman": z.isoformat(timespec="minutes"), "konu": veri["konu"]},
                        kaynak="sohbet")
        zaman = eylemler.tarih_metni(z, simdi=simdi)
        return f"{zaman[0].upper() + zaman[1:]}{bulunma_eki(zaman)} yeniden hatırlatacağım."
