"""Sistem gözcüsü (LLM yok): 5 dakikada bir yerel sistemleri yoklar. Bir kontrol üst üste iki kez
başarısız olunca bildirir, düzelince haber verir. Önem eşikleri aşağıda."""
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from ..ayarlar import KAYITLAR, ayar, log, sessiz_mi
from .. import eylemler, telegram

ARALIK = 300
DURUM = os.path.join(KAYITLAR, "sistem.durum.json")  # hata sayaçları, bildirilenler, ilk hata zamanı (yeniden başlatmada korunur)
# İsteğe bağlı kontroller .env'den açılır; boş bırakılan kontrol listeye hiç girmez.
PANEL_SERVISI = ayar("PANEL_SERVISI", "")            # izlenecek Windows servisinin adı (sc query)
PANEL_URL = ayar("PANEL_URL", "")                    # cevap vermesi beklenen yerel web arayüzü
TAILSCALE_KONTROL = ayar("TAILSCALE_KONTROL", "0") == "1"
SYNCTHING_PORT = int(ayar("SYNCTHING_PORT", "0") or 0)
AG_CIHAZI_IP = ayar("AG_CIHAZI_IP", "")              # ping'le izlenecek ağ cihazı (ör. bir mikrodenetleyici)
AG_CIHAZI_AD = ayar("AG_CIHAZI_AD", "Ağ cihazı")
DISKLER = [h.strip().upper() for h in ayar("DISKLER", "C").split(",") if h.strip()]
# Komut zaman aşımı çoğunlukla bilgisayarın o an yoğun olmasıdır (19 Eki akşamı sc/tailscale/claude aynı turlarda
# takıldı): kontrolün kendi eşiği düşük olsa da en az bu kadar ardışık turdan sonra bildirilir.
ZAMAN_ASIMI_ESIK = 3
# Pahalı kontroller her turda değil: `claude auth status` her seferinde ~4,5 sn'lik bir Claude CLI süreci açıyor
# (5 dk'da bir günde 288 kez); giriş düşmesi için 30 dk yeterince erken.
SEYREK = {"Claude girişi": 6}
GIZLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# "HH:MM"den sonra gelen ayrılma eki, son okunan sayıya göre: 14:40 "kırk"tan, 23:05 "beş"ten, 22:00 "iki"den
_BIRLER = {"0": "'dan", "1": "'den", "2": "'den", "3": "'ten", "4": "'ten", "5": "'ten", "6": "'dan", "7": "'den",
           "8": "'den", "9": "'dan"}
_ONLAR = {"1": "'dan", "2": "'den", "3": "'dan", "4": "'tan", "5": "'den"}


def ayrilma_eki(saat):
    """'14:40' -> "'tan" ("14:40'tan"). Dakika 00 ise saat okunur ("22:00'den", "10:00'dan")."""
    sayi = saat[-2:] if saat[-2:] != "00" else saat[-5:-3]
    if sayi[1] != "0":
        return _BIRLER[sayi[1]]
    return _ONLAR.get(sayi[0], "'dan")  # "00" -> sıfır'dan


def bulunma_eki(saat):
    """'20:10' -> "'da" ("20:10'da"), '21:35' -> "'te": ayrılma ekinin bulunma hâli ('dan -> 'da, 'ten -> 'te).
    `saat` HH:MM ile bitmeli ('yarın 21:35' de olur)."""
    return ayrilma_eki(saat)[:-1]


def beri_metni(ts, simdi=None):
    """HH:MM'den beri (bugünse) ya da '16 Eki Cum 20:00'den beri' (başka gün, eylemler.tarih_metni; ek okunuşa
    göre); zaman İstanbul saatiyle."""
    simdi = dt.datetime.fromtimestamp(simdi or time.time(), eylemler.TZ)
    z = dt.datetime.fromtimestamp(ts, eylemler.TZ)
    metin = z.strftime("%H:%M") if z.date() == simdi.date() else eylemler.tarih_metni(z, simdi=simdi)
    return metin + ayrilma_eki(metin) + " beri"


def _komut(*args, sure=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=sure, creationflags=GIZLI)


def panel_servisi():
    r = _komut("sc", "query", PANEL_SERVISI)
    return "RUNNING" in r.stdout, "servis çalışmıyor" if "STATE" in r.stdout else "servis bulunamadı"


def panel_http():
    try:
        urllib.request.urlopen(PANEL_URL, timeout=5)
        return True, ""
    except urllib.error.HTTPError:
        return True, ""  # cevap veriyor (giriş sayfası vb.)
    except Exception as e:
        return False, f"cevap vermiyor ({type(e).__name__})"


def tailscale():
    try:
        r = _komut("tailscale", "status", "--json")
        return bool(json.loads(r.stdout).get("Self", {}).get("Online")), "çevrimdışı"
    except subprocess.TimeoutExpired:
        raise  # zaman aşımı ayrı sayılır (_kontrol_et)
    except Exception as e:
        return False, f"durum alınamadı ({type(e).__name__})"


def syncthing():
    try:
        socket.create_connection(("127.0.0.1", SYNCTHING_PORT), timeout=3).close()
        return True, ""
    except OSError:
        return False, "arayüzü cevap vermiyor (çalışmıyor olabilir)"


def ag_cihazi():
    r = _komut("ping", "-n", "2", "-w", "1500", AG_CIHAZI_IP)
    return "TTL=" in r.stdout.upper(), f"{AG_CIHAZI_IP} ping'e cevap vermiyor"


def disk(harf):
    def kontrol():
        try:
            k = shutil.disk_usage(f"{harf}:\\")
        except OSError:
            return True, ""  # sürücü takılı değil
        bos = k.free / k.total * 100
        return bos >= 10, f"{harf}: diskinde yalnızca %{bos:.0f} boş yer kaldı ({k.free / 2**30:.0f} GB)"
    return kontrol


CLAUDE = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")


def claude_girisi():
    """Sohbet beyni, gözcüler ve arka plan işleri `claude` girişine bağlı; düşerse hepsi sessizce durur.
    `claude auth status` sürenin ne zaman dolacağını söylemiyor, yalnızca düştüğünü; o an haber veririz."""
    r = _komut(CLAUDE, "auth", "status", "--json", sure=30)
    try:
        giris = bool(json.loads(r.stdout).get("loggedIn"))
    except (json.JSONDecodeError, ValueError):
        return False, f"giriş durumu okunamadı ({(r.stdout or r.stderr).strip()[:80]})"
    return giris, ("Claude girişi düşmüş: bilgisayarda bir claude terminalinde /login gerekiyor. O zamana kadar "
                   "Telegram'daki Claude, mail değerlendirme ve arka plan işleri çalışmıyor")


class SistemGozcusu:
    def __init__(self, uyari, telegram_saglikli=lambda: True):
        self.uyari = uyari
        # (ad, kontrol, kaç ardışık hata sonra, önem)
        self.kontroller = []
        if PANEL_SERVISI:
            self.kontroller.append(("Uzaktan kontrol paneli servisi", panel_servisi, 2, "onemli"))
        if PANEL_URL:
            self.kontroller.append(("Panel web arayüzü", panel_http, 2, "onemli"))
        if TAILSCALE_KONTROL:
            self.kontroller.append(("Tailscale", tailscale, 2, "onemli"))
        if SYNCTHING_PORT:
            self.kontroller.append(("Syncthing", syncthing, 3, "onemli"))
        if AG_CIHAZI_IP:
            self.kontroller.append((AG_CIHAZI_AD, ag_cihazi, 6, "onemli"))  # kararsız cihazlar için 30 dk tolerans
        for harf in DISKLER:
            self.kontroller.append((f"{harf} diski", disk(harf), 1, "acil_kritik" if harf == "C" else "onemli"))
        self.kontroller += [
            ("Telegram sohbet botu", lambda: (telegram_saglikli(), "mesajlar alınamıyor"), 3, "onemli"),
            ("Claude girişi", claude_girisi, 1, "onemli"),
        ]
        self.hata = {ad: 0 for ad, *_ in self.kontroller}
        self.neden = {}            # /durum için: sorunlu kontrolün son nedeni
        self.ilk_hata = {}         # /durum için: kontrolün şu anki hata dizisinin ilk zamanı (epoch)
        self.son_tur = 0.0         # son kontrol turu (time.time())
        self.bildirildi = set()
        self.zaman_asimi = set()   # son denemesi zaman aşımına uğrayan kontroller (eşikleri ZAMAN_ASIMI_ESIK)
        self.dosya = DURUM
        self._yukle()
        threading.Thread(target=self._dongu, daemon=True, name="sistem").start()

    def _yukle(self):
        """Önceki çalışmanın durumunu oku: yeniden başlatma bildirilmiş bir sorunu tekrar bildirmesin, düzelmeyi
        de kaçırmasın. Bozuk ya da eksik dosya yok sayılır."""
        try:
            with open(self.dosya, encoding="utf-8") as f:
                d = json.load(f)
            adlar = {ad for ad, *_ in self.kontroller}
            hata = {ad: max(0, int(n)) for ad, n in (d.get("hata") or {}).items() if ad in adlar}
            bildirildi = {ad for ad in (d.get("bildirildi") or []) if ad in adlar}
            ilk_hata = {ad: float(t) for ad, t in (d.get("ilk_hata") or {}).items() if ad in adlar}
            neden = {ad: str(n) for ad, n in (d.get("neden") or {}).items() if ad in adlar}
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError) as e:  # JSONDecodeError ValueError'dır
            log(f"sistem: {self.dosya} okunamadı ({e!r}), sıfırdan başlanıyor")
            return
        self.hata.update(hata)
        self.bildirildi, self.ilk_hata, self.neden = bildirildi, ilk_hata, neden

    def _durum(self):
        return {"hata": {a: n for a, n in self.hata.items() if n}, "bildirildi": sorted(self.bildirildi),
                "ilk_hata": dict(self.ilk_hata), "neden": dict(self.neden)}

    def _kaydet(self):
        """Durumu atomik yaz (tmp + os.replace). Yazılamazsa gözcü durmasın."""
        gecici = self.dosya + ".tmp"
        try:
            with open(gecici, "w", encoding="utf-8") as f:
                json.dump(self._durum(), f, ensure_ascii=False)
            os.replace(gecici, self.dosya)
        except OSError as e:
            log(f"sistem: durum yazılamadı ({e!r})")

    def _esik(self, ad, esik):
        return max(esik, ZAMAN_ASIMI_ESIK) if ad in getattr(self, "zaman_asimi", ()) else esik

    def _kontrol_et(self, ad, kontrol, esik, onem, simdi=None):
        simdi = simdi or time.time()
        zaman_asimi = self.__dict__.setdefault("zaman_asimi", set())
        try:
            tamam, neden = kontrol()
            zaman_asimi.discard(ad)
        except subprocess.TimeoutExpired:
            tamam, neden = False, "kontrol zaman aşımına uğradı (bilgisayar çok yoğun olabilir)"
            zaman_asimi.add(ad)
        except Exception as e:
            tamam, neden = False, f"kontrol yapılamadı ({type(e).__name__})"
            zaman_asimi.discard(ad)
        esik = self._esik(ad, esik)
        if tamam:
            if ad in self.bildirildi:
                self.bildirildi.discard(ad)
                self._kaydet()  # gönderimden önce: çökülürse yeniden başlatma düzelmeyi tekrar duyurmasın
                telegram.gonder(f"{ad} yeniden çalışıyor.", sessiz=sessiz_mi())
                log(f"sistem: {ad} düzeldi")
            self.hata[ad] = 0
            self.neden.pop(ad, None)
            self.ilk_hata.pop(ad, None)
            return
        if not self.hata[ad]:
            self.ilk_hata[ad] = simdi
        self.hata[ad] += 1
        self.neden[ad] = neden
        if self.hata[ad] >= esik and ad not in self.bildirildi:
            self.bildirildi.add(ad)
            self._kaydet()  # bildirimden önce: gönderim sırasında çökülürse yeniden başlatma aynısını tekrarlamasın
            if onem == "acil_kritik":  # disk: %5'in altı acil, %10'un altı önemli
                bos = float(neden.split("%")[1].split()[0]) if "%" in neden else 100
                onem = "acil" if bos < 5 else "onemli"
            self.uyari.bildir(onem, f"{ad}: {neden}", "sistem")

    def _dongu(self):
        time.sleep(60)
        tur = 0
        while True:
            onceki = self._durum()
            for ad, kontrol, esik, onem in self.kontroller:
                if tur % SEYREK.get(ad, 1) == 0 or self.hata.get(ad):  # sorunluysa her turda: düzelme kaçmasın
                    self._kontrol_et(ad, kontrol, esik, onem)
            tur += 1
            if self._durum() != onceki:
                self._kaydet()
            self.son_tur = time.time()
            time.sleep(ARALIK)

    def durum_satiri(self, simdi=None):
        simdi = simdi or time.time()
        if not self.son_tur:
            return "Sistem kontrolleri: ilk tur açılıştan 1 dk sonra."
        once = int((simdi - self.son_tur) // 60)
        # uyarı eşiğiyle tutarlı: eşiği geçen sorun ("⚠️ sorun:"), geçmeyen (ör. ağ cihazının tek kaçan pingi) yalnızca
        # "izleniyor"; durum her zaman yazıyla söylenir
        sorunlu = [ad for ad, _, esik, _ in self.kontroller if self.hata.get(ad, 0) >= self._esik(ad, esik)]
        izlenen = [ad for ad, _, esik, _ in self.kontroller if 0 < self.hata.get(ad, 0) < self._esik(ad, esik)]
        toplam = len(self.kontroller)
        ilk_hata = getattr(self, "ilk_hata", {})

        def beri(ad):
            return f" ({beri_metni(ilk_hata[ad], simdi)})" if ad in ilk_hata else ""
        if sorunlu:
            return (f"⚠️ sorun: sistem kontrollerinde {len(sorunlu)} sorun var (son tur {once} dk önce)\n"
                    + "\n".join(f"• {ad}: {self.neden.get(ad, '?')[:60]}{beri(ad)}" for ad in sorunlu))
        sat = f"Sistem kontrolleri: {toplam - len(izlenen)}/{toplam} tamam ({once} dk önce)"
        if once > 12:  # 5 dk'da bir dönmeliydi: iş parçacığı takılmış ya da ölmüş
            sat = f"⚠️ sorun: sistem kontrolleri durmuş görünüyor, son tur {once} dk önce ({toplam - len(izlenen)}/{toplam} tamam)"
        if izlenen:
            sat += "; izleniyor: " + ", ".join(f"{ad} ({self.hata[ad]} kez" + (f", {beri_metni(ilk_hata[ad], simdi)}" if ad in ilk_hata else "") + ")"
                                         for ad in izlenen)
        return sat + "."
