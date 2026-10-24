"""İndirilen Panopto derslerinin notları: telefonda okumak ve eksikse hazırlamak.

Panopto hattı ayrı bir projede (`PANOPTO_ARAC_KLASORU`): panopto-indir.ps1 kaydı indirir (`PANOPTO_KLASORU`/<ders>),
transkript.py --v2 Whisper dökümü yazar. Burada eksik adımlar sırayla çalıştırılır ve dökümlerden Sonnet ile
"<tarih> - Ders Notu.md" çıkarılır. Sohbette ⟦DERS_NOTU {"ders":"PSYC 114","tarih":"2026-10-01"}⟧:
- not hazırsa dosyası Telegram'a gönderilir, içeriği de beyne verilir (istenen kısmı anlatır),
- değilse hazırlık arka planda başlar; bitince özet ve dosya Telegram'a gelir.
"""
import datetime as dt
import glob
import os
import re
import shutil
import subprocess
import threading

from . import limit
from .ayarlar import KOK, ayar, log

PANOPTO_KOK = ayar("PANOPTO_KLASORU", os.path.join(KOK, "panopto"))
# panopto-bekle.ps1 / panopto-indir.ps1 betiklerinin ve cookies.txt dosyasının durduğu klasör
PANOPTO_ARAC = ayar("PANOPTO_ARAC_KLASORU", os.path.join(KOK, "panopto-arac"))
CLAUDE = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
GIZLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)
YONETICI = None  # servis açılınca Dersler örneği
NOT_ISTEMI = """Aşağıda bir üniversite dersinin ses kaydından çıkarılmış Whisper dökümleri var ({ders}, {tarih}).
Kayıt sınıftan uzak mikrofonla alınmış olabilir; döküm yer yer bozuk. Kullanıcı için Türkçe ders notu yaz:
- "# {ders} — {tarih}" başlığı; ardından kısa bir "Kaynak" satırı.
- Duyurular (sınav, ödev, devam) varsa en başta ayrı bölüm.
- Konular başlıklar altında, maddeler ve gerekirse tablolarla. Hocanın örneklerini koru.
- Emin olmadığın yerleri *(?)* ile işaretle; "Altyazı M.K.", "İzlediğiniz için teşekkürler" gibi Whisper
  uydurmalarını yok say. Uydurma bilgi ekleme.
- En sona "## Kısa özet" başlığıyla 4-6 cümlelik bir özet yaz.
Yalnızca notun kendisini yaz.

Dökümler:
{dokum}"""


def _kod(s):
    """'psyc114 sosyal' -> 'PSYC 114' (Panopto klasör adlarındaki biçim)."""
    from .eylemler import ders_kodu, okunur_kod
    return okunur_kod(ders_kodu(s)) or None


def ders_klasoru(ders):
    """'PSYC 114' / 'Sosyal Psikoloji' -> Panopto klasörü altındaki ders klasörü (yoksa None)."""
    kod, ad = _kod(ders), (ders or "").lower()
    for k in sorted(glob.glob(os.path.join(PANOPTO_KOK, "20*"))):
        taban = os.path.basename(k)
        if (kod and kod in taban.upper()) or (ad and len(ad) > 4 and ad in taban.lower()):
            return k
    return None


def gun_durumu(klasor, tarih):
    """-> (not_yolu|None, [mp4], [döküm]) o günün dosyaları."""
    not_ = os.path.join(klasor, f"{tarih} - Ders Notu.md")
    mp4 = sorted(glob.glob(os.path.join(klasor, f"{tarih} *.mp4")))
    dokum = []
    for v in mp4:
        k = v[:-4]
        dokum.append(next((y for y in (k + ".v2.txt", k + ".txt") if os.path.exists(y)), None))
    return (not_ if os.path.exists(not_) else None), mp4, dokum


_ozet_onbellek = {"zaman": 0.0, "sat": None}


def ozet():
    """Bağlam için: hangi derslerin hangi günleri indirilmiş, notu hazır mı (H diskini 5 dk'da bir tarar)."""
    import time
    if _ozet_onbellek["sat"] is not None and time.time() - _ozet_onbellek["zaman"] < 300:
        return _ozet_onbellek["sat"]
    _ozet_onbellek.update(zaman=time.time(), sat=_ozet_tara())
    return _ozet_onbellek["sat"]


def _ozet_tara():
    sat = []
    for k in sorted(glob.glob(os.path.join(PANOPTO_KOK, "20*"))):
        gunler = sorted({os.path.basename(f)[:10] for f in glob.glob(os.path.join(k, "20??-??-?? *.mp4"))})
        if not gunler:
            continue
        parca = []
        for g in gunler[-6:]:
            not_, mp4, dokum = gun_durumu(k, g)
            parca.append(f"{g} ({'not hazır' if not_ else 'döküm var' if all(dokum) else 'video var'})")
        sat.append(f"- {re.sub(r'^\d{4}-\d ', '', os.path.basename(k))}: " + ", ".join(parca))
    if not sat:
        return ["İndirilmiş ders kaydı yok."]
    return ["İndirilmiş ders kayıtları (⟦DERS_NOTU⟧ ile okunur; listede olmayan gün de istenebilir, indirilir):"] + sat


class Isleniyor(RuntimeError):
    """Kayıt indirilemedi: Panopto'da henüz işleniyor ya da yok."""


BEKLEME_SAAT = 24


class Dersler:
    def _isleme_bekle(self, ders, tarih):
        """panopto-bekle.ps1 ile kaydın izlenebilir olmasını bekle (en fazla 24 saat, 20 dk aralıkla)."""
        from .eylemler import tarih_metni
        from .telegram import gonder
        son = dt.datetime.now() + dt.timedelta(hours=BEKLEME_SAAT)
        gonder(f"{ders} {tarih_metni(tarih, saat=False)} kaydı Panopto'da henüz hazır değil. Hazır olunca "
               f"(en fazla {BEKLEME_SAAT} saat) indirip notunu çıkaracağım.")
        log(f"ders notu: {ders} {tarih} için Panopto bekleniyor (en geç {son:%d.%m %H:%M})")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            os.path.join(PANOPTO_ARAC, "panopto-bekle.ps1"), "-Ders", _kod(ders) or ders,
                            "-Tarih", tarih, "-SonTarih", f"{son:%Y-%m-%d %H:%M}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=(BEKLEME_SAAT + 1) * 3600, creationflags=GIZLI, cwd=PANOPTO_ARAC)
        if r.returncode == 2:
            raise RuntimeError("Panopto çerezlerinin süresi dolmuş: Chrome'da 'Get cookies.txt LOCALLY' ile "
                               f"{os.path.join(PANOPTO_ARAC, 'cookies.txt')} yeniden dışa aktarılmalı")
        if r.returncode == 3:
            raise RuntimeError(f"{BEKLEME_SAAT} saat beklendi, kayıt Panopto'ya gelmedi")

    def __init__(self, bildir):
        """bildir(ders, tarih, not_metni|None, hata|None, arayarak)"""
        self.bildir = bildir
        self.kilit = threading.Lock()   # aynı anda tek hazırlık (GPU ve indirme)
        self.suren = set()

    def notu_oku(self, ders, tarih):
        k = ders_klasoru(ders)
        if not k:
            return None
        not_, _, _ = gun_durumu(k, tarih)
        if not not_:
            return None
        with open(not_, encoding="utf-8") as f:
            return f.read()

    def hazirla(self, ders, tarih, arayarak=True):
        """Arka planda indir → dök → not çıkar. Dönüş: kullanıcıya söylenecek kısa durum."""
        if limit.dolu_mu():
            raise RuntimeError(limit.bekle_mesaji())
        try:
            dt.date.fromisoformat(tarih)
        except ValueError:
            raise ValueError(f"tarih YYYY-AA-GG olmalı: {tarih!r}")
        anahtar = (_kod(ders) or ders, tarih)
        if anahtar in self.suren:
            return "bu dersin hazırlığı zaten sürüyor"
        self.suren.add(anahtar)
        threading.Thread(target=self._hazirla, args=(ders, tarih, arayarak, anahtar), daemon=True,
                         name="ders-notu").start()
        return "hazırlık başladı"

    def _hazirla(self, ders, tarih, arayarak, anahtar):
        try:
            for deneme in range(2):
                try:
                    with self.kilit:
                        metin = self._adimlar(ders, tarih)
                    break
                except Isleniyor as e:
                    if deneme:  # bekledikten sonra hâlâ yoksa vazgeç
                        raise RuntimeError(str(e))
                    self._isleme_bekle(ders, tarih)  # kilidin dışında: başka ders notları beklemesin
            self.bildir(ders, tarih, metin, None, arayarak)
        except Exception as e:
            log(f"ders notu hazırlanamadı ({ders} {tarih}):", repr(e)[:200])
            self.bildir(ders, tarih, None, str(e)[:300], arayarak)
        finally:
            self.suren.discard(anahtar)

    def _adimlar(self, ders, tarih):
        k = ders_klasoru(ders)
        mp4 = gun_durumu(k, tarih)[1] if k else []
        if not mp4:  # 1) indir
            log(f"ders notu: indiriliyor {ders} {tarih}")
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                os.path.join(PANOPTO_ARAC, "panopto-indir.ps1"), "-Ders", _kod(ders) or ders,
                                "-Tarih", tarih], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=3600, creationflags=GIZLI, cwd=PANOPTO_ARAC)
            if r.returncode == 2:
                raise RuntimeError("Panopto çerezlerinin süresi dolmuş: Chrome'da 'Get cookies.txt LOCALLY' ile "
                                   f"{os.path.join(PANOPTO_ARAC, 'cookies.txt')} yeniden dışa aktarılmalı")
            k = ders_klasoru(ders)
            mp4 = gun_durumu(k, tarih)[1] if k else []
            if not mp4:
                raise Isleniyor("kayıt bulunamadı ya da Panopto'da hâlâ işleniyor: "
                                + (r.stdout + r.stderr).strip()[-200:])
        not_, mp4, dokum = gun_durumu(k, tarih)
        if not_:
            return open(not_, encoding="utf-8").read()
        eksik = [v for v, d in zip(mp4, dokum) if not d]
        if eksik:  # 2) dök (sınıf kayıtları için --v2)
            log(f"ders notu: {len(eksik)} kayıt dökülüyor {ders} {tarih}")
            ad = re.sub(r"^\d{4}-\d ", "", os.path.basename(k))
            subprocess.run([os.path.join(PANOPTO_ARAC, ".venv", "Scripts", "python.exe"),
                            os.path.join(PANOPTO_ARAC, "transkript.py"), "--v2",
                            f"--istem=Üniversite dersi: {ad}. Türkçe ders anlatımı.", *eksik],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4 * 3600,
                           creationflags=GIZLI, cwd=PANOPTO_ARAC)
            _, mp4, dokum = gun_durumu(k, tarih)
        if not any(dokum):
            raise RuntimeError("döküm çıkarılamadı")
        metin = "\n\n".join(f"--- {os.path.basename(d)}\n" + open(d, encoding="utf-8", errors="replace").read()
                            for d in dokum if d)
        ad = re.sub(r"^\d{4}-\d ", "", os.path.basename(k))
        log(f"ders notu: not yazılıyor {ad} {tarih} ({len(metin)} karakter döküm)")
        p = subprocess.run([CLAUDE, "-p", "--model", "sonnet", "--output-format", "text", "--tools", "",
                            "--strict-mcp-config", "--no-session-persistence"],
                           input=NOT_ISTEMI.format(ders=ad, tarih=tarih, dokum=metin[:180000]),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                           creationflags=GIZLI, cwd=KOK)
        not_metni = (p.stdout or "").strip()
        limit.kontrol(not_metni, "ders notu")
        if len(not_metni) < 200 or "session limit" in not_metni or "Failed to authenticate" in not_metni:
            raise RuntimeError(f"not yazılamadı: {not_metni[:150]}")
        with open(os.path.join(k, f"{tarih} - Ders Notu.md"), "w", encoding="utf-8") as f:
            f.write(not_metni + "\n")
        log(f"ders notu hazır: {ad} {tarih}")
        return not_metni
