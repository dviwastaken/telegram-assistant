"""Okul postası (Zimbra, IMAP) gözcüsü. Kutu salt okunur açılır (EXAMINE + BODY.PEEK): hiçbir mail
okundu işaretlenmez, taşınmaz, silinmez. Yeni UID'ler dakikada bir yoklanır."""
import datetime as dt
import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import re
import socket
import ssl
import threading
import time

from .. import eylemler, limit
from ..ayarlar import KAYITLAR, ayar, log
from .sistem import beri_metni

SUNUCU = ayar("OKUL_IMAP_SUNUCU", "imap.example.edu")
KULLANICI = ayar("OKUL_EPOSTA_KULLANICI") or ayar("OKUL_EPOSTASI") or ayar("OKUL_POSTA")
SIFRE = ayar("OKUL_EPOSTA_SIFRE") or ayar("OKUL_POSTA_SIFRE")
DURUM = os.path.join(KAYITLAR, "okul_posta.durum.json")
ARALIK = 60
# Gmail boşta duran bağlantıyı günde birkaç kez kapatıyor ("command: EXAMINE => socket error: EOF"). Bu bir arıza
# değil: kısa beklemeyle sessizce yeniden bağlanılır, yalnızca üst üste KOPMA_ESIK kez olursa günlüğe yazılır.
KOPMA_BEKLEME = 5
KOPMA_ESIK = 3
ILK_GERIYE = 20   # ilk çalıştırmada son kaç maili değerlendirelim (geçmişi sel gibi taramayalım)
# "2026-1 BIL 283 Oyun Tasarımı ve Programlama 1 dersi için yeni ödev: "Oyun Mekanikleri" (Son teslim: 15.10.2026 20:05)"
YENI_ODEV = re.compile(r"\d{4}-\d\s+([A-ZÇĞİÖŞÜ]{2,4} \d{3})\s+(.+?)\s+dersi için yeni ödev:\s*[\"“](.+?)[\"”]\s*"
                       r"\(Son teslim:\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}:\d{2})\)")
# "Ders: 2026-1 BIL 207 Görsel Programlama II 2 (17408). Değerlendirme: Ders Uygulaması 1. Gönderim: Salı, ..."
# Ödev adı "Gönderim:"e (yoksa satır sonuna) kadar: adın içinde de ". " olabiliyor ("BIL 251 - 1. Ödev – Blockchain").
GONDERIM = re.compile(r"Ders:\s*\d{4}-\d\s+([A-ZÇĞİÖŞÜ]{2,4} \d{3}).*?Değerlendirme:\s*([^\n]+?)\.?\s*(?:Gönderim\s*:|$)",
                      re.S | re.M)


def teslim_zamani(gun, ay, yil, saat, simdi=None):
    """Learn'deki son teslim. Hoca yılı yanlış girebiliyor (2026-10-15: "21.10.0023"): yıl makul değilse
    gün/ayın en yakın gelecekteki hâli alınır."""
    simdi = simdi or dt.datetime.now()
    try:
        y = int(yil)
        if simdi.year - 1 <= y <= simdi.year + 2:
            dt.datetime(y, int(ay), int(gun))
            return f"{y:04d}-{ay}-{gun}T{saat}"
        z = dt.datetime(simdi.year, int(ay), int(gun), *map(int, saat.split(":")))
    except ValueError:
        return f"{yil}-{ay}-{gun}T{saat}"
    if z < simdi - dt.timedelta(days=60):  # ör. Aralık'ta gelen "Ocak" teslimi
        z = z.replace(year=simdi.year + 1)
    return z.isoformat(timespec="minutes")


REDDEDILDI = re.compile(r"AUTHENTICATIONFAILED|LOGIN failed|invalid credentials|authentication failed", re.I)


def hata_turu(e):
    """IMAP hatasını sınıflandır: "red" (sunucu girişi reddetti), "ag" (ağ, zaman aşımı, DNS: sunucuya
    ulaşılamadı) ya da "sunucu" (sunucuya ulaşıldı ama başka bir hata, ör. TLS ya da protokol)."""
    if isinstance(e, imaplib.IMAP4.error):
        return "red" if REDDEDILDI.search(str(e)) else "sunucu"
    if isinstance(e, ssl.SSLError):
        return "sunucu"
    if isinstance(e, OSError):  # gaierror (DNS), TimeoutError, ConnectionError, ağ bağdaştırıcısı yok...
        return "ag"
    return "sunucu"


def internet_var(hedefler=(("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)), sure=3):
    """Genel internete ulaşılabiliyor mu? Okul sunucusuna ulaşılamadığında yanlış alarmı önler."""
    for adres in hedefler:
        try:
            socket.create_connection(adres, timeout=sure).close()
            return True
        except OSError:
            continue
    return False


class ErisimTakibi:
    """Okul postası "erişim kesildi" tespiti (MFA geçişi IMAP'i sessizce bozabilir). Hesap önceden okunuyorken
    girişler art arda başarısız olur ve ilk başarısızlıktan ESIK geçerse tek uyarı; aynı kesinti için en fazla
    TEKRAR'da bir yeniden uyarı; giriş düzelince (uyarı verildiyse) bir kez "yeniden okunuyor", sayaçlar sıfırlanır.
    İnternet de yokken ağ hatası sayılmaz. Durum `dosya`da (yeniden başlatmada korunur)."""
    ESIK = 30 * 60
    TEKRAR = 6 * 3600

    def __init__(self, dosya, bildir, once_basarili=False, internet=internet_var):
        self.dosya, self.bildir, self.internet = dosya, bildir, internet
        self.durum = {"once_basarili": False, "ilk_hata": None, "hata_sayisi": 0, "son_uyari": None,
                      "son_tur": None}
        try:
            with open(dosya, encoding="utf-8") as f:
                self.durum.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.durum["once_basarili"] = bool(self.durum["once_basarili"] or once_basarili)

    @property
    def once_basarili(self):
        return self.durum["once_basarili"]

    def _yaz(self):
        gecici = self.dosya + ".tmp"
        with open(gecici, "w", encoding="utf-8") as f:
            json.dump(self.durum, f)
        os.replace(gecici, self.dosya)

    def basari(self):
        d = self.durum
        degisti = not d["once_basarili"] or d["ilk_hata"] is not None or d["son_uyari"] is not None
        uyarilmisti = d["son_uyari"] is not None
        d.update(once_basarili=True, ilk_hata=None, hata_sayisi=0, son_uyari=None, son_tur=None)
        if degisti:
            self._yaz()
        if uyarilmisti:
            self.bildir("Okul postası yeniden okunuyor, giriş tekrar başarılı.")

    def hata(self, tur, simdi=None):
        """Başarısız giriş ya da yoklama (`tur`: hata_turu). Uyarı gönderildiyse True."""
        simdi = simdi or time.time()
        d = self.durum
        if not d["once_basarili"]:
            return False  # hiç okunmamış hesap: gözcünün "giriş reddedildi" mesajı yeter
        if tur == "ag" and not self.internet():
            return False  # internet de yok: okul postasına özgü değil, sayma
        if d["ilk_hata"] is None:
            d["ilk_hata"], d["hata_sayisi"] = simdi, 0
        d["hata_sayisi"] += 1
        d["son_tur"] = tur
        uyar = (d["hata_sayisi"] >= 2 and simdi - d["ilk_hata"] >= self.ESIK
                and (d["son_uyari"] is None or simdi - d["son_uyari"] >= self.TEKRAR))
        if uyar:
            d["son_uyari"] = simdi
        self._yaz()
        if uyar:
            self.bildir(self.mesaj(tur))
        return uyar

    def mesaj(self, tur):
        beri = beri_metni(self.durum["ilk_hata"])  # "20:00'den beri" ya da "16 Eki Cum 20:00'den beri"
        if tur == "red":
            neden = (f"Okul postası okunamıyor: {beri} sunucu girişi reddediyor (hesap önceden okunuyordu).\n"
                     "Olası neden: okulun MFA (çok faktörlü doğrulama) geçişi hesabınıza ulaşmış olabilir.")
        elif tur == "ag":
            neden = (f"Okul postası okunamıyor: {beri} okul posta sunucusuna ulaşılamıyor (internet var).\n"
                     "Sunucu bakımda olabilir ya da MFA geçişiyle IMAP erişimi kapatılmış olabilir.")
        else:
            neden = (f"Okul postası okunamıyor: {beri} posta sunucusu hata veriyor.\n"
                     "Olası neden: MFA geçişi ya da sunucu sorunu.")
        return (neden + "\n\nYapmanız gerekenler:\n"
                "1) Webmail'e tarayıcıdan girip girebildiğinize bakın.\n"
                "2) MFA etkinse IMAP için uygulama şifresi oluşturup .env'ye yazın, ya da BT biriminden "
                "yönlendirme/muafiyet isteyin.\n"
                "O zamana kadar okul maillerini göremiyorum.")


def _baslik(deger):
    parcalar = []
    for metin, kod in email.header.decode_header(deger or ""):
        parcalar.append(metin.decode(kod or "utf-8", "replace") if isinstance(metin, bytes) else metin)
    return re.sub(r"\s+", " ", "".join(parcalar)).strip()


def _govde(mesaj):
    duz, zengin = [], []
    for parca in mesaj.walk() if mesaj.is_multipart() else [mesaj]:
        if parca.get_content_maintype() == "multipart" or parca.get("Content-Disposition", "").startswith("attachment"):
            continue
        tur = parca.get_content_type()
        if tur not in ("text/plain", "text/html"):
            continue
        ham = parca.get_payload(decode=True) or b""
        metin = ham.decode(parca.get_content_charset() or "utf-8", "replace")
        (duz if tur == "text/plain" else zengin).append(metin)
    if duz:
        metin = html.unescape("\n".join(duz))  # Zimbra düz metne de &Ouml; gibi varlıklar koyabiliyor
    else:
        metin = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(zengin))
        metin = html.unescape(re.sub(r"<br\s*/?>|</p>|</div>", "\n", metin, flags=re.I))
        metin = re.sub(r"<[^>]+>", " ", metin)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", metin)).strip()


class OkulPostaGozcusu:
    """Salt okunur IMAP gözcüsü. Alt sınıflar (Gmail) yalnızca bu özellikleri değiştirir."""
    AD, KAYNAK = "okul postası", "okul"
    SUNUCU, KULLANICI, SIFRE, DURUM = SUNUCU, KULLANICI, SIFRE, DURUM
    FILTRE = ()  # yeni UID'ler aranırken eklenecek IMAP ölçütleri
    _bekleyen = ()  # işlenemeyen (limit vb.) UID'ler: aynıları her dakika yeniden indirilip yazılmasın
    ERISIM = os.path.join(KAYITLAR, "okul_posta.erisim.json")  # "erişim kesildi" takibi (None: kapalı)
    RED_MESAJI = ("Okul postasına giremiyorum, giriş reddedildi.\n"
                  "Şifre değişmiş ya da MFA fakültenize gelmiş olabilir (kademeli açılıyor). MFA ise Zimbra'da "
                  "uygulama şifresi gerekir.\n"
                  "O zamana kadar okul maillerini göremiyorum.")

    def __init__(self, uyari):
        self.uyari = uyari
        self.durum = self._oku()
        # /durum için: bağlantı hâli, son yoklama ve son yeni mail (time.time()), son hata
        self.bagli, self.son_yoklama, self.son_hata = False, 0.0, ""
        try:  # son yeni mail zamanı yeniden başlatmada korunur
            self.son_yeni = float(self.durum.get("son_yeni") or 0.0)
        except (TypeError, ValueError):
            self.son_yeni = 0.0
        self.kapali = not (self.KULLANICI and self.SIFRE)
        # Durum dosyasında UIDVALIDITY varsa hesap daha önce okunmuş demektir
        self.erisim = (ErisimTakibi(self.ERISIM, lambda m: self.uyari.bildir("onemli", m, self.AD),
                                    once_basarili=bool(self.durum.get("uidvalidity")))
                       if self.ERISIM and not self.kapali else None)
        if self.kapali:
            log(f"{self.AD}: kullanıcı adı / şifre .env'de yok, gözcü kapalı")
            return
        threading.Thread(target=self._dongu, daemon=True, name=self.KAYNAK + "-imap").start()

    def _ieu_learn(self, m, ilk_tur):
        """İEU Learn'ün kalıplı bildirimlerini LLM'siz işle (limit dolsa bile ödev kaçmasın). İşlendiyse True."""
        if ayar("OKUL_LEARN_GONDEREN", "learn@example.edu").lower() not in m["kimden"].lower():
            return False
        yeni = YENI_ODEV.search(m["metin"])
        if yeni:
            kod, ders, odev, gun, ay, yil, saat = yeni.groups()
            zaman = teslim_zamani(gun, ay, yil, saat)
            if zaman[:4] != yil:
                log(f"{self.AD}: '{odev}' ödevinin yılı Learn'de yanlış ({yil}), {zaman[:4]} sayıldı")
            eylemler.mail_kaydet(m, "onemli", f"{kod} {ders.strip()}: yeni ödev \"{odev}\", son teslim {gun}.{ay} {saat}")
            self.uyari.teslim_isle({"ders": kod, "baslik": odev, "zaman": zaman, "tur": "Teslim"}, "İEU Learn")
            if not ilk_tur:  # kullanıcının isteği: hemen yaz, "başladım" diyene kadar 2 saatte bir yaz (uyari.odev_takip)
                self.uyari.odev_takibe_al(kod, odev, zaman)
            return True
        gonderim = GONDERIM.search(m["metin"])
        if gonderim:
            eylemler.mail_kaydet(m, "bilgi", f"{gonderim.group(1)} \"{gonderim.group(2)}\" gönderildi (İEU Learn onayı)")
            self.uyari.gonderim_isle(gonderim.group(1), gonderim.group(2))
            return True
        return False  # başka bir İEU Learn bildirimi (mesaj, duyuru): LLM değerlendirsin

    def _oku(self):
        try:
            with open(self.DURUM, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _yaz(self):
        gecici = self.DURUM + ".tmp"  # yarıda kesilen yazım son_uid'i bozup eski mailleri yeniden işletmesin
        with open(gecici, "w", encoding="utf-8") as f:
            json.dump(self.durum, f)
        os.replace(gecici, self.DURUM)

    def _dongu(self):
        bekleme, reddedildi_bildirildi, kopma = 30, False, 0
        while True:
            try:
                imap = imaplib.IMAP4_SSL(self.SUNUCU, 993, timeout=30)
                imap.login(self.KULLANICI, self.SIFRE)
                if not 0 < kopma < KOPMA_ESIK:  # sessiz kopmadan dönüş günlüğe yazılmaz
                    log(f"{self.AD}: bağlandı")
                self.bagli, self.son_hata = True, ""
                bekleme, reddedildi_bildirildi = 30, False
                self._erisim(None)
                while True:
                    self._yokla(imap)
                    kopma = 0
                    time.sleep(ARALIK)
            except (imaplib.IMAP4.abort, ssl.SSLEOFError) as e:
                kopma += 1
                if kopma < KOPMA_ESIK:
                    time.sleep(KOPMA_BEKLEME)
                    continue
                self.bagli, self.son_hata = False, str(e)[:120]
                log(f"{self.AD}: bağlantı {kopma} kez üst üste koptu, yeniden denenecek:", str(e)[:150])
                self._erisim(e)
            except imaplib.IMAP4.error as e:
                kopma = 0
                self.bagli, self.son_hata = False, str(e)[:120]
                if REDDEDILDI.search(str(e)):
                    log(f"{self.AD}: giriş reddedildi (şifre değişmiş ya da MFA gelmiş olabilir):", str(e)[:150])
                    bekleme = max(bekleme, 1800)  # yanlış şifreyle hesabı kilitletmeyelim
                    # Önceden okunan okul hesabında uyarıyı ErisimTakibi verir (30 dk sonra, tek mesaj)
                    if not reddedildi_bildirildi and not (self.erisim and self.erisim.once_basarili):
                        reddedildi_bildirildi = True
                        self.uyari.bildir("onemli", self.RED_MESAJI, self.AD)
                else:
                    log(f"{self.AD}: IMAP bağlantı hatası, yeniden denenecek:", str(e)[:150])
                self._erisim(e)
            except Exception as e:
                kopma = 0
                self.bagli, self.son_hata = False, repr(e)[:120]
                log(f"{self.AD}: bağlantı hatası:", repr(e)[:150])
                self._erisim(e)
            time.sleep(bekleme)
            bekleme = min(bekleme * 2, 3600)

    def _erisim(self, hata):
        """Erişim takibine giriş sonucunu bildir (hata None: başarılı). Takip hatası gözcüyü durdurmasın."""
        if not self.erisim:
            return
        try:
            if hata is None:
                self.erisim.basari()
            else:
                self.erisim.hata(hata_turu(hata))
        except Exception as e:
            log(f"{self.AD}: erişim takibi hatası:", repr(e)[:150])

    def _yokla(self, imap):
        tip, veri = imap.select("INBOX", readonly=True)
        if tip != "OK":
            raise imaplib.IMAP4.error("INBOX açılamadı")
        _, gecerlilik = imap.response("UIDVALIDITY")
        gecerlilik = (gecerlilik[0] or b"").decode()
        ilk_tur = self.durum.get("uidvalidity") != gecerlilik
        if ilk_tur:  # ilk kez ya da kutu yeniden oluşturulmuş
            _, ids = imap.uid("SEARCH", None, "ALL")
            hepsi = [int(x) for x in (ids[0] or b"").split()]
            self.durum = {"uidvalidity": gecerlilik, "son_uid": (hepsi[-ILK_GERIYE - 1] if len(hepsi) > ILK_GERIYE else 0)}
            log(f"{self.AD}: kutuda {len(hepsi)} mail, son {min(len(hepsi), ILK_GERIYE)} tanesi değerlendirilecek")
        _, ids = imap.uid("SEARCH", None, "UID", f"{self.durum['son_uid'] + 1}:*", *self.FILTRE)
        yeni = [int(x) for x in (ids[0] or b"").split() if int(x) > self.durum["son_uid"]]
        self.son_yoklama = time.time()
        if not yeni:
            return
        parti = tuple(yeni[:30])
        if parti == self._bekleyen and limit.dolu_mu():
            return  # 21 Ekim: limit dolu kaldıkça aynı mail 11 saat boyunca dakikada bir indirilip yazıldı
        mailler = []
        for uid in parti:
            _, d = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            ham = next((p[1] for p in d if isinstance(p, tuple)), None)
            if not ham:
                continue
            m = email.message_from_bytes(ham)
            mailler.append({"id": f"{self.KAYNAK}-{uid}", "kaynak": self.KAYNAK, "kimden": _baslik(m.get("From")),
                            "konu": _baslik(m.get("Subject")), "tarih": m.get("Date", ""),
                            "metin": _govde(m)})
        if parti != self._bekleyen:
            log(f"{self.AD}: {len(mailler)} yeni mail")
        kalan = [m for m in mailler if not self._ieu_learn(m, ilk_tur)]
        if self.uyari.mailleri_isle(kalan, self.AD, ilk_tur):
            self._bekleyen = ()
            self.son_yeni = time.time()  # yalnızca işlenen mail sayılır (başarısızsa sonraki turda tekrar denenir)
            self.durum["son_uid"], self.durum["son_yeni"] = max(parti), self.son_yeni
            self._yaz()
        else:
            self._bekleyen = parti

    def durum_satiri(self, simdi=None):
        """/durum satırı: bağlı mı, en son ne zaman yokladı, en son ne zaman yeni mail geldi."""
        simdi = simdi or time.time()
        ad = self.AD[0].upper() + self.AD[1:]
        if self.kapali:
            return f"{ad}: kapalı (.env'de kullanıcı/şifre yok)."
        if not self.bagli:
            ilk_hata = (getattr(self, "erisim", None) and self.erisim.durum.get("ilk_hata")) or None
            return (f"⚠️ sorun: {ad} bağlı değil" + (f" ({self.son_hata})" if self.son_hata else " (bağlanıyor)")
                    + (f", {beri_metni(ilk_hata, simdi)}" if ilk_hata else "") + ".")
        once = int((simdi - self.son_yoklama) // 60) if self.son_yoklama else None
        sat = f"{ad}: bağlı, son yoklama " + (f"{once} dk önce" if once is not None else "henüz yok")
        if once is not None and once > 10:
            sat = "⚠️ sorun: " + sat + " (dakikada bir olmalıydı)"
        if self.son_yeni:
            sat += ", son yeni mail " + eylemler.tarih_metni(dt.datetime.fromtimestamp(self.son_yeni, eylemler.TZ),
                                                             simdi=dt.datetime.fromtimestamp(simdi, eylemler.TZ))
        return sat + "."


class GmailImapGozcusu(OkulPostaGozcusu):
    """Gmail'i IMAP + uygulama şifresiyle salt okunur yoklar: LLM'siz, abonelik limiti tüketmez (claude.ai bağlantısıyla
    çalışan eski gmail.py gözcüsü çağrı başına ~94 bin token harcıyordu). Uygulama şifresi için Google hesabında
    2 adımlı doğrulama açık olmalı. Tanıtım/sosyal/forum kategorileri Gmail'in kendi aramasıyla dışarıda bırakılır."""
    AD, KAYNAK = "gmail", "gmail"
    SUNUCU = "imap.gmail.com"
    KULLANICI = ayar("GMAIL_KULLANICI")
    SIFRE = (ayar("GMAIL_UYGULAMA_SIFRESI") or "").replace(" ", "")  # Google 4'lü gruplar hâlinde gösteriyor
    DURUM = os.path.join(KAYITLAR, "gmail-imap.durum.json")
    ERISIM = None  # MFA geçişi okula özgü; Gmail'de giriş reddi hemen bildirilir (RED_MESAJI)
    FILTRE = ("X-GM-RAW", '"-category:promotions -category:social -category:forums"')
    RED_MESAJI = ("Gmail'e giremiyorum, uygulama şifresi reddedildi.\n"
                  "Google'da şifre iptal edilmiş ya da 2 adımlı doğrulama kapatılmış olabilir.\n"
                  "Yeni bir uygulama şifresini .env'ye yazmanız gerekiyor.")
