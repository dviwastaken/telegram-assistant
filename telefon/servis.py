"""Ana servis: Telegram sohbeti, hatırlatmalar, gözcüler, Notion eşitlemesi, yerel kontrol arayüzü (127.0.0.1)."""
import datetime as dt
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bulut, dersler, eylemler, gorevler, hafiza, hava, limit, odevler, planlayici, telegram
from .ayarlar import KOK, KONTROL_PORT, log
from .gozculer.gmail import GmailGozcusu
from .gozculer.okul_posta import GmailImapGozcusu, OkulPostaGozcusu
from .gozculer.sistem import SistemGozcusu
from .gozculer.uyari import Uyari
from .notion import Esitleyici
from .planlayici import GecePlanlayicisi
from .sohbet import Sohbet
from .zamanlayici import Zamanlayici

JETON_YOLU = os.path.join(KOK, "kontrol.jeton")


def kontrol_jetonu():
    if not os.path.exists(JETON_YOLU):
        with open(JETON_YOLU, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(24))
    with open(JETON_YOLU, encoding="utf-8") as f:
        return f.read().strip()


class Servis:
    def __init__(self):
        log("servis başlıyor")
        self.baslangic = time.time()
        self.esitleyici = Esitleyici()
        eylemler.degisiklik_dinleyicisi = self.esitleyici.tetikle
        self.uyari = Uyari()
        self.sohbet = Sohbet(uyari=self.uyari, durum=self.durum_metni)  # gönderimler bundan sonra geçmişe işlenir
        self.dinleyici = telegram.Dinleyici(self.sohbet.mesaj_geldi, self.sohbet.dugme_geldi)
        self.zamanlayici = Zamanlayici(self.sohbet)
        self.planlayici = GecePlanlayicisi()
        self.hafiza = hafiza.HaftalikHafiza()  # pazar 21:00 haftalık sohbet özeti; eksik hafta açılışta yazılır
        # Gmail: uygulama şifresi varsa IMAP (LLM'siz), yoksa claude.ai bağlantısı (abonelik limitini tüketir)
        gmail = GmailImapGozcusu if GmailImapGozcusu.SIFRE else GmailGozcusu
        log(f"gmail gözcüsü: {'IMAP' if gmail is GmailImapGozcusu else 'claude.ai bağlantısı (IMAP şifresi yok)'}")
        self.gozculer = [OkulPostaGozcusu(self.uyari), gmail(self.uyari),
                         SistemGozcusu(self.uyari, self.dinleyici.saglikli)]
        self.uyari.posta = self.gozculer[0]  # gecikme kontrolü: posta okunmuyorsa onay görülmemiş olabilir
        self.gorevler = gorevler.Gorevler(self.sohbet.gorev_bildir)
        gorevler.YONETICI = self.gorevler
        dersler.YONETICI = dersler.Dersler(self.sohbet.ders_bildir)
        odevler.YONETICI = odevler.Odevler(self.sohbet.odev_bildir)
        hava.tazeleyici()
        threading.Thread(target=odevler.learn_metinlerini_hazirla, daemon=True, name="learn-metin").start()
        if bulut.nabiz_baslat():  # 5 dk'da bir nabız; temiz kapanışta /kapaniyor (Worker "sustu" uyarısı yazmasın)
            log("bulut: nabız açık")

    def durum_metni(self):
        sure = int(time.time() - self.baslangic)
        sat = [f"Servis çalışıyor, {sure // 3600} sa {sure % 3600 // 60} dk'dır açık."]
        d = self.dinleyici
        if not telegram.cift_yonlu():
            sat.append("Telegram: yalnızca tek yönlü (sohbet botu jetonu yok).")
        elif d.saglikli():
            sat.append(f"Telegram: @{d.bot_adi}, son yoklama {int(time.time() - d.son_basari)} sn önce.")
        else:
            sat.append(f"⚠️ sorun: Telegram yoklaması başarısız, {d.son_hata[:120]}")
        bekleyen = len(telegram._kuyruk_oku())
        if bekleyen:
            sat.append(f"⚠️ sorun: gönderilmeyi bekleyen {bekleyen} mesaj var.")
        okuma = getattr(self.esitleyici, "son_basari", 0)
        sat.append(f"Notion: son çekiş {int((time.time() - okuma) // 60)} dk önce." if okuma else "Notion: henüz çekilmedi.")
        if bulut.etkin():
            sat.append(bulut_satiri(bulut.durum()))
        try:
            a = sorted((x for x in eylemler.oku(eylemler.ARAMALAR, []) if x.get("durum") == "bekliyor"),
                       key=lambda x: x["zaman"])
            sat.append(f"Kurulu hatırlatma: {len(a)}" + (f" (sıradaki {eylemler.tarih_metni(a[0]['zaman'])})" if a else ""))
        except (json.JSONDecodeError, OSError):
            sat.append("⚠️ sorun: hatırlatma dosyası okunamadı.")
        sat.append(f"{limit.bekle_mesaji()}." if limit.dolu_mu() else "Claude: kullanılabilir.")
        b = self.sohbet.beyin
        sat.append(f"Sohbet beyni: açık, {b.tur} tur." if b is not None and b.canli else "Sohbet beyni: ilk mesajda açılacak.")
        for g in self.gozculer:  # okul postası, Gmail, sistem kontrolleri
            if hasattr(g, "durum_satiri"):
                try:
                    sat.append(g.durum_satiri())
                except Exception as e:
                    sat.append(f"⚠️ sorun: {type(g).__name__}: durum okunamadı ({type(e).__name__})")
        try:
            sat.append(planlayici.durum_satiri())
        except Exception as e:
            sat.append(f"⚠️ sorun: gece planı durumu okunamadı ({type(e).__name__})")
        acik = [g for g in self.gorevler.kayitlar if g["durum"] == "calisiyor"]
        if acik:
            sat.append(f"Süren görev: {len(acik)} ({', '.join(g['kisa'] for g in acik)})")
        return "\n".join(sat)


def bulut_satiri(d, simdi=None):
    """Bulut hatırlatıcısının /durum özeti: son taraması ne zaman, son hatası ne. Sorun varsa satır
    '⚠️ sorun: ' ile başlar (durum her zaman yazıyla söylenir)."""
    t = (d or {}).get("son_tarama")
    if not t:
        return "⚠️ sorun: Bulut hatırlatıcısına ulaşılamadı (bilgisayar kapanırsa hatırlatmalar gitmez)."
    simdi = simdi or dt.datetime.now(dt.timezone.utc)
    once = int((simdi - dt.datetime.fromisoformat(t["zaman"].replace("Z", "+00:00"))).total_seconds() // 60)
    sorun = once > 5
    sat = f"Bulut hatırlatıcısı: son tarama {once} dk önce" + (" (her dakika olmalıydı)" if sorun else "") + "."
    if t.get("hatalar"):
        sorun = True
        sat += f" Son hata: {t['hatalar'][0][:100]}"
    n = (d or {}).get("nabiz") or {}
    if n.get("ms"):  # Worker'ın gördüğü son nabız (5 dk'da bir gelmeli)
        nabiz_dk = int((simdi.timestamp() * 1000 - n["ms"]) // 60000)
        sorun = sorun or nabiz_dk > 10
        sat += f" Son nabız {nabiz_dk} dk önce" + (" (5 dk'da bir gelmeli)" if nabiz_dk > 10 else "") + "."
    return ("⚠️ sorun: " if sorun else "") + sat


def kontrol_sunucusu(servis):
    """Yalnızca 127.0.0.1, jetonla: GET /durum, POST /gonder {"metin": "..."} (başka betikler kullanıcıya yazabilsin),
    POST /simule (canlı test: Telegram'dan gelmiş gibi mesaj/düğme)."""
    jeton = kontrol_jetonu()

    class Isleyici(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cevap(self, kod, veri):
            b = json.dumps(veri, ensure_ascii=False).encode()
            self.send_response(kod)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _yetkili(self):
            if not secrets.compare_digest(self.headers.get("X-Jeton", ""), jeton):
                self._cevap(401, {"hata": "jeton"})
                return False
            return True

        def do_GET(self):
            if self.path != "/durum":
                self._cevap(404, {"hata": "bulunamadı"})
            elif self._yetkili():
                self._cevap(200, {"durum": servis.durum_metni(), "telegram": servis.dinleyici.saglikli(),
                                  "zaman": dt.datetime.now().isoformat(timespec="seconds")})

        def do_POST(self):
            if self.path not in ("/gonder", "/simule"):
                self._cevap(404, {"hata": "bulunamadı"})
                return
            if not self._yetkili():
                return
            if self.path == "/simule":
                return self._simule()
            try:
                istek = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                metin = str(istek["metin"])[:3500]
            except (ValueError, KeyError):
                self._cevap(400, {"hata": "metin gerekli"})
                return
            mid = telegram.gonder(metin)
            self._cevap(200 if mid else 202, {"mesaj": mid, "durum": "gönderildi" if mid else "kuyrukta"})

        def _simule(self):
            """Yerel test kancası: Telegram'dan gelmiş gibi bir mesaj ya da düğme tıklaması. Gövde
            {"mesaj": "..."}, {"dugme": "h:e:10:<anahtar>", "mesaj_id": 12} ya da {"medya": {"voice": {...}}} (Telegram
            mesajının medya alanları; dosya kimliği botun kendi gönderdiği bir dosyadan alınır). Bu yoldan gelen hiçbir
            şey görev ya da silme onaylayamaz."""
            try:
                istek = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                self._cevap(400, {"hata": "JSON bozuk"})
                return
            if istek.get("mesaj"):
                servis.sohbet.mesaj_geldi({"message_id": 0, "text": str(istek["mesaj"])[:2000], "_simule": True})
            elif isinstance(istek.get("medya"), dict):
                izinli = {k: v for k, v in istek["medya"].items()
                          if k in ("voice", "audio", "document", "photo", "caption")}
                servis.sohbet.mesaj_geldi({"message_id": 0, **izinli, "_simule": True})
            elif istek.get("dugme"):
                servis.sohbet.dugme_geldi(str(istek["dugme"])[:64], "simule", {"message_id": istek.get("mesaj_id")})
            else:
                self._cevap(400, {"hata": "mesaj ya da dugme gerekli"})
                return
            self._cevap(202, {"durum": "işleniyor"})

    sunucu = ThreadingHTTPServer(("127.0.0.1", KONTROL_PORT), Isleyici)
    threading.Thread(target=sunucu.serve_forever, daemon=True, name="kontrol").start()
    log(f"kontrol arayüzü: http://127.0.0.1:{KONTROL_PORT}")


def main():
    s = Servis()
    kontrol_sunucusu(s)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
