"""Telegram'dan bilgisayardaki Claude Code'a görev (bkz. TASARIM_TELEFONDAN_GOREV.md).

Beyin, Kullanıcı yazılı onay verdikten sonra ⟦GÖREV {"proje":..,"gorev":..}⟧ yazar. Servis projenin
klasöründe `claude --bg --permission-mode auto` ile gerçek, kalıcı bir arka plan oturumu açar ve `claude agents --json`
ile izler. Oturum bitince cevabının sonundaki "ÖZET:" (ya da takıldıysa "SORU:") satırı Telegram sohbetine
yazılır.
Kullanıcının soruya cevabı ⟦GÖREV_CEVAP {"id":..,"cevap":..}⟧ ile aynı oturuma `--resume` edilerek iletilir.
Oturumlar kapatılmaz, yalnızca durdurulur: Kullanıcı bilgisayarda açıp devam edebilir."""
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import limit
from .ayarlar import KAYITLAR, KOK, ayar, log

YONETICI = None  # servis açılınca Gorevler örneği (sohbet ⟦GÖREV⟧ işaretlerini buna verir)
CLAUDE = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
# Verilmezse oturum varsayılan modelle (Opus) açılır, Pro planda haftalık limiti hızlı tüketir.
MODEL = ayar("GOREV_MODEL", "sonnet")
GIZLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DURUM = os.path.join(KAYITLAR, "gorevler.json")
# Sohbetten görev alabilecek projeler (ad -> klasör). Bilinmeyen proje reddedilir. Sırrı olan projeleri listeye
# koyma. Liste `projeler.json` dosyasından okunur (bkz. projeler.example.json); dosya yoksa yalnızca bu depo.
VARSAYILAN_ADI = "telegram-assistant"
VARSAYILAN_TAKMA_AD = {"asistan": VARSAYILAN_ADI, "telegram": VARSAYILAN_ADI, "kendin": VARSAYILAN_ADI,
                       "telegrambotu": VARSAYILAN_ADI, "claudeasistan": VARSAYILAN_ADI}


def _projeleri_yukle():
    """projeler.json: {"projeler": {ad: klasör}, "takma_ad": {söyleniş: ad}, "disarida": [kelime, ...]}.
    Dosya yoksa ya da bozuksa yalnızca bu deponun kendisi görev alabilir."""
    dosya = ayar("GOREV_PROJELER_DOSYASI", os.path.join(KOK, "projeler.json"))
    try:
        with open(dosya, encoding="utf-8") as f:
            veri = json.load(f)
        projeler = {str(a): os.path.expanduser(str(k)) for a, k in (veri.get("projeler") or {}).items()}
        takma = {str(t): str(a) for t, a in (veri.get("takma_ad") or {}).items() if str(a) in projeler}
        disarida = tuple(str(k).lower() for k in (veri.get("disarida") or ()))
        if projeler:
            return projeler, takma, disarida
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        log(f"gorevler: {dosya} okunamadı ({e}); varsayılan proje listesi kullanılıyor")
    return {VARSAYILAN_ADI: KOK}, dict(VARSAYILAN_TAKMA_AD), ()


# TAKMA_AD: söyleniş -> proje (küçük harf, Türkçe harfler sadeleşmiş, boşluksuz)
PROJELER, TAKMA_AD, _DISARIDA_DOSYA = _projeleri_yukle()
GUNLUK_SINIR = 5
# Görev oturumları şifre/anahtar dosyalarına dokunmasın (.env: Notion, Telegram, okul şifresi; her projede alt
# klasörler dahil). Kalıplar oturumun çalışma klasörüne göre (gitignore biçimi).
_GIZLI = ["**/.env", "**/.env.*", "**/*.jeton", "**/secrets.h", "**/.dev.vars", "**/*cerez*", "**/*.pfx"]
# Hem çalışma klasörüne göre hem ev klasörüne göre: bir görev başka bir projenin .env'ini de okuyamasın.
# (Write kuralı dosya izinlerinde eşleşmez; Edit kuralı tüm düzenleme araçlarını kapsar.)
YASAK = ([f"{arac}({k})" for k in _GIZLI for arac in ("Read", "Edit")]
         + [f"{arac}(~/{k})" for k in _GIZLI for arac in ("Read", "Edit")]
         # kabuk yolları (cat ./.env, python -c "open('.env')", Get-Content ...): komutta gizli dosya adı geçemez
         + [f"{kabuk}(*{ad}*)" for kabuk in ("Bash", "PowerShell")
            for ad in (".env", "jeton", "secrets.h", ".dev.vars", "cerez", "NOTION_KEY", "TELEGRAM_SOHBET_JETONU")])
IZLEME_SN = 15
EN_UZUN_DK = 60
KURALLAR = """Bu görev kullanıcıdan Telegram'dan geldi; telefondan yazıldığı için küçük yazım hataları olabilir.
Kullanıcı şu an bilgisayar başında değil ve bu oturumda sana cevap veremez. Kurallar:
- .env, *.jeton, secrets.h gibi şifre ve anahtar dosyalarını okuma ve değiştirme. Ayar gerekiyorsa özette söyle.
- Yalnızca bu projenin klasöründe çalış; başka projelerin ya da kullanıcının diğer klasörlerine dokunma.
- git commit ya da push yapma. Dosya silme. Zamanlanmış görev, açılış görevi gibi kalıcılık kurma. Dışarıya
  (mail, mesaj, web formu) bir şey gönderme. Hizmeti yeniden başlatman gerekiyorsa yapma, özette söyle.
- İş bitince cevabının en sonuna "ÖZET:" ile başlayan, Telegram'da gönderilecek 1-3 kısa cümle yaz.
- Devam etmek için kullanıcının bir kararına gerçekten ihtiyacın varsa (görev belirsiz, iki makul yol var, riskli bir
  adım gerekiyor) işi orada bırak ve cevabının en sonuna "SORU:" ile başlayan tek ve net bir soru yaz. O zaman
  ÖZET yazma. Kullanıcının cevabı bu oturuma iletilecek."""


def _sade(s):
    s = (s or "").replace("İ", "i").replace("I", "ı").lower()
    s = s.translate(str.maketrans("çğıöşü", "cgiosu"))
    return re.sub(r"[^a-z0-9]+", "", s.replace("projesi", "").replace("proje", "").replace("uygulamasi", ""))


# Bu kelimeler geçen söyleniş hiçbir projeye eşlenmez (sırrı olan ya da görev almaması gereken projeler).
# projeler.json'daki "disarida" listesi buna eklenir.
DISARIDA = _DISARIDA_DOSYA


def proje_bul(ad):
    """'asistan projesi', 'telegram botu' gibi söylenişleri projeye eşle.
    Belirsizse None: yanlış klasörde görev açılmasın (onay mesajında proje ve klasör de görünür)."""
    n = _sade(ad)
    if len(n) < 4 or any(k in n for k in DISARIDA):
        return None
    if n in TAKMA_AD:
        return TAKMA_AD[n]
    for k in PROJELER:
        kn = _sade(k)
        if kn in n or (len(n) >= 6 and n in kn):  # kısa parça ("panel") bir projenin içinde geçiyor diye eşlenmez
            return k
    return next((p for t, p in TAKMA_AD.items() if len(t) >= 5 and t in n), None)


def _calistir(*args, cwd=None, sure=60):
    return subprocess.run([CLAUDE, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=sure, creationflags=GIZLI)


def ajanlar():
    try:
        return json.loads(_calistir("agents", "--json", "--all", sure=30).stdout or "[]")
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as e:
        log("görev: claude agents okunamadı:", repr(e)[:150])
        return None


def son_cevap(oturum_id):
    """Oturum dökümündeki son asistan metni."""
    yollar = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", f"{oturum_id}.jsonl"))
    if not yollar:
        return ""
    son = ""
    with open(yollar[0], encoding="utf-8", errors="replace") as f:
        for satir in f:
            try:
                ev = json.loads(satir)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant":
                continue
            metin = "".join(b.get("text", "") for b in (ev.get("message") or {}).get("content", [])
                            if isinstance(b, dict) and b.get("type") == "text").strip()
            if metin:
                son = metin
    return son


def sonucu_ayir(metin):
    """-> ('soru' | 'ozet', metin). Ne SORU ne ÖZET varsa son 400 karakter özet sayılır."""
    soru = re.search(r"(?im)^\s*\**SORU:\**\s*(.+)$", metin or "")
    if soru:
        return "soru", soru.group(1).strip()
    ozet = re.search(r"(?is)\**ÖZET:\**\s*(.+)$", metin or "")
    if ozet:
        return "ozet", re.sub(r"\s+", " ", ozet.group(1)).strip()[:600]
    return "ozet", re.sub(r"\s+", " ", (metin or "").strip())[-400:]


class Gorevler:
    def __init__(self, bildir):
        """bildir(gorev_kaydi, tur: 'ozet'|'soru'|'hata', metin)"""
        self.bildir = bildir
        self.kilit = threading.Lock()
        self.kayitlar = self._oku()
        threading.Thread(target=self._izle, daemon=True, name="gorevler").start()

    def _oku(self):
        try:
            with open(DURUM, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _yaz(self):
        with open(DURUM, "w", encoding="utf-8") as f:
            json.dump(self.kayitlar[-200:], f, ensure_ascii=False, indent=1)

    def bul(self, kimlik):
        return next((g for g in reversed(self.kayitlar) if g["id"] == kimlik or g.get("kisa") == kimlik), None)

    # ------------------------------------------------------------ başlatma
    def baslat(self, proje, gorev, arayarak=False):
        """Dönüş: kullanıcıya söylenecek kısa sonuç. Geçersizse ValueError."""
        p = proje_bul(proje)
        if not p:
            raise ValueError(f"'{proje}' telefondan görev alabilen projeler arasında yok ({', '.join(PROJELER)})")
        gorev = str(gorev).strip()[:2000]
        if len(gorev) < 8:
            raise ValueError("görev metni çok kısa")
        if limit.dolu_mu():
            raise ValueError(limit.bekle_mesaji())
        bugun = dt.date.today().isoformat()
        with self.kilit:
            if sum(1 for g in self.kayitlar if g["baslangic"][:10] == bugun) >= GUNLUK_SINIR:
                raise ValueError(f"bugünkü görev sınırı ({GUNLUK_SINIR}) doldu")
        ad = f"Telegram {dt.datetime.now():%H%M}: {re.sub(r'\s+', ' ', gorev)[:40]}"
        r = _calistir("--bg", "-n", ad, "--model", MODEL, "--permission-mode", "auto", "--append-system-prompt", KURALLAR,
                      "--disallowedTools", *YASAK, "--", gorev,
                      cwd=PROJELER[p], sure=90)
        kisa = re.search(r"backgrounded\s*·\s*([0-9a-f]{6,})", r.stdout or "")
        if not kisa:
            raise RuntimeError(f"arka plan oturumu açılamadı: {(r.stdout + r.stderr).strip()[-200:]}")
        kayit = {"id": kisa.group(1), "kisa": kisa.group(1), "oturum": None, "ad": ad, "proje": p, "gorev": gorev,
                 "arayarak": bool(arayarak), "baslangic": dt.datetime.now().isoformat(timespec="seconds"),
                 "durum": "calisiyor", "mesgul_goruldu": False}
        with self.kilit:
            self.kayitlar.append(kayit)
            self._yaz()
        log(f"görev başladı [{kayit['kisa']}] {p}: {gorev[:120]}")
        return f"görev başladı ({p}, oturum {kayit['kisa']})"

    def cevap_bekleyen(self, kimlik):
        """Cevabın gideceği görev: kimlikle bulunan, yoksa soruya takılmış son görev. Yoksa ValueError."""
        g = self.bul(kimlik) or next((x for x in reversed(self.kayitlar) if x["durum"] == "soru"), None)
        if not g or not g.get("oturum"):
            raise ValueError("cevap bekleyen görev bulunamadı")
        return g

    def cevapla(self, kimlik, cevap):
        """SORU'ya takılmış görevin oturumuna kullanıcının cevabını ilet ve oturumu arka planda sürdür."""
        g = self.cevap_bekleyen(kimlik)
        r = _calistir("--bg", "--resume", g["oturum"], "--model", MODEL, "--permission-mode", "auto",
                      "--disallowedTools", *YASAK, "--",
                      f"Kullanıcının cevabı (Telegram'dan): {str(cevap).strip()[:1500]}", cwd=PROJELER[g["proje"]], sure=90)
        with self.kilit:
            g.update(durum="calisiyor", mesgul_goruldu=False, devam=dt.datetime.now().isoformat(timespec="seconds"))
            self._yaz()
        log(f"görev [{g['kisa']}] cevapla sürdürüldü: {(r.stdout or r.stderr).strip()[-120:]}")
        return f"cevabınız göreve iletildi (oturum {g['kisa']})"

    # ------------------------------------------------------------ izleme
    def _izle(self):
        while True:
            time.sleep(IZLEME_SN)
            try:
                self._tur()
            except Exception as e:
                log("görev izleme hatası:", repr(e)[:200])

    def _tur(self):
        acik = [g for g in self.kayitlar if g["durum"] == "calisiyor"]
        if not acik:
            return
        liste = ajanlar()
        if liste is None:
            return
        for g in acik:
            a = next((x for x in liste if x.get("name") == g["ad"]
                      or (x.get("sessionId") or "").startswith(g["kisa"])), None)
            if a and not g.get("oturum"):
                g["oturum"] = a.get("sessionId")
            durum = (a or {}).get("status")
            if durum == "busy":
                g["mesgul_goruldu"] = True
            basla = dt.datetime.fromisoformat(g.get("devam") or g["baslangic"])
            sure_doldu = dt.datetime.now() - basla > dt.timedelta(minutes=EN_UZUN_DK)
            metin = son_cevap(g["oturum"]) if g.get("oturum") else ""
            bitti = durum != "busy" and (g["mesgul_goruldu"] or a is None) and metin
            if not (bitti or sure_doldu):
                continue
            if sure_doldu and not bitti:
                tur, sonuc = "hata", f"{EN_UZUN_DK} dakikadır bitmedi, oturum açık bırakıldı"
            else:
                limit.kontrol(metin, "telefondan görev")
                tur, sonuc = sonucu_ayir(metin)
                _calistir("stop", g["kisa"], sure=30)  # konuşma korunur; --resume / attach ile açılır
            g.update(durum="soru" if tur == "soru" else "bitti" if tur == "ozet" else "hata",
                     sonuc=sonuc, bitis=dt.datetime.now().isoformat(timespec="seconds"))
            self._yaz()
            log(f"görev [{g['kisa']}] {g['durum']}: {sonuc[:200]}")
            try:
                self.bildir(g, tur, sonuc)
            except Exception as e:
                log("görev bildirimi hatası:", repr(e)[:150])
        with self.kilit:
            self._yaz()
