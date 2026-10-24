"""Ödev değerlendirmesi (yalnızca kullanıcı isteyince): <ODEV_KLASORU>/<ders>/<ödev>/ klasörüne kullanıcı ödev
dosyasını/metnini koyar; sohbette ⟦ODEV {"ders":"BIL 283","odev":"One Pager Ödevi"}⟧ ile arka planda bir
`claude -p` (yalnız okuma araçları) klasördeki dosyaları ve o dersin Panopto notlarını okuyup ne istendiğini, tahmini
süreyi ve adımları çıkarır. Sonuç klasöre "Değerlendirme.md" olarak yazılır; özet ve dosya Telegram'a gider.
Yeni ödev maili gelince klasör kendiliğinden açılır (uyari.teslim_isle)."""
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import zipfile

from . import limit
from .ayarlar import KOK as DEPO_KOKU, ayar, log

KOK = ayar("ODEV_KLASORU", os.path.join(DEPO_KOKU, "ortak", "odevler"))
CLAUDE = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
GIZLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SONUC_ADI = "Değerlendirme.md"
YONETICI = None
ISTEM = """Kullanıcının bir ödevini değerlendiriyorsun ({ders} {ders_adi} dersi — {odev}, son teslim: {teslim}). Kullanıcı bir meslek yüksekokulu Bilgisayar Programcılığı öğrencisi ve yazılım mühendisi. Ödevi yapma, yalnızca değerlendir.

Ödev klasörü: {klasor}
{dosyalar}
Dersin notları (varsa, hocanın ödevle ilgili söyledikleri burada olabilir): {notlar}

Read/Glob/Grep ile dosyaları oku. Sonra Türkçe şu başlıklarla yaz:
## Ne isteniyor
## Teslim biçimi ve değerlendirme ölçütleri
## Tahmini süre (aralık ver ve neden o kadar sürdüğünü bir cümleyle açıkla)
## Adım adım plan
## Belirsiz ya da hocaya sorulması gereken noktalar
Dosya yoksa ya da yetersizse bunu açıkça söyle ve yalnızca başlık ile ders notlarına dayanan tahminleri "tahmin" diye
işaretle. Uydurma bilgi ekleme.
En sona "ÖZET:" ile başlayan, Telegram'da gönderilecek 2-4 kısa cümle yaz (ne isteniyor + tahmini süre)."""


AYRILMIS = re.compile(r"(?i)^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$")


def guvenli_ad(s):
    """Windows'ta geçerli, klasör dışına çıkamayan tek bir yol bileşeni. Önce kırpılır, sonra sondaki nokta ve
    boşluklar atılır (Windows "x ." adını "x" diye açar, os.path.join ise boşluğu korur)."""
    ad = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(s))
    ad = re.sub(r"\s+", " ", ad).strip()[:80].strip(" .")
    if not ad or set(ad) <= {"."}:
        return "adsiz"
    return "_" + ad if AYRILMIS.match(ad) else ad


def kod(s):
    """'bil283 oyun' -> 'BIL 283' (ödev klasörü adı)."""
    from .eylemler import ders_kodu, okunur_kod
    return okunur_kod(ders_kodu(s)) or None


def klasor(ders, odev, olustur=False):
    d = os.path.join(KOK, guvenli_ad(kod(ders) or ders), guvenli_ad(odev))
    if olustur:
        os.makedirs(d, exist_ok=True)
    return d


def bul(ders, odev):
    """Söylenen ödevi var olan klasörle eşle (başlık kısmen söylenebilir)."""
    ders_k = os.path.join(KOK, guvenli_ad(kod(ders) or ders))
    n = re.sub(r"[^a-z0-9çğıöşü]+", "", (odev or "").lower())
    for d in sorted(glob.glob(os.path.join(ders_k, "*"))):
        if not os.path.isdir(d) or os.path.basename(d).startswith("_"):
            continue
        a = re.sub(r"[^a-z0-9çğıöşü]+", "", os.path.basename(d).lower())
        if n and a and (n in a or (len(a) >= 4 and a in n)):
            return d
    return None


def dosyalar(d):
    return sorted(f for f in glob.glob(os.path.join(d, "*")) if os.path.isfile(f)
                  and os.path.basename(f) != SONUC_ADI and not f.endswith(".metin.txt"))


PDF_SAYFA_SINIRI = 200
ZIP_UYE_SINIRI = 30 * 2**20   # docx/pptx içindeki tek XML'in açılmış boyutu
ZIP_SINIRI = 100 * 2**20      # toplam


def _yaz(hedef, metin):
    """Önce geçici dosyaya, sonra yerine: iki iş parçacığı aynı .metin.txt'yi yazarsa yarım dosya kalmasın."""
    gecici = f"{hedef}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        f.write(metin)
    os.replace(gecici, hedef)


PDF_ZAMAN_ASIMI = 60  # sn; kötü niyetli ya da çok ağır bir PDF medya/Learn iş parçacığını kilitlemesin
_PDF_IFADE = ("from telefon import odevler, alt_surec; "
              "alt_surec.isci_dongusu(lambda i: {'metin': odevler._pdf_metni_yerel(i['yol'])}, 30)")


def pdf_metni(yol):
    """PDF'in sayfa sayfa metni, ayrı bir alt süreçte (en fazla PDF_ZAMAN_ASIMI sn). Taranmış (görüntü) PDF'te metin
    çıkmaz: bunu açıkça yazar. Zaman aşımı, alt sürecin düşmesi ya da okunamayan PDF: günlüğe yazar, None döner."""
    from . import alt_surec
    try:
        cevap = alt_surec.tek_sefer("PDF metni", _PDF_IFADE, {"yol": os.path.abspath(yol)}, PDF_ZAMAN_ASIMI)
    except Exception as e:
        log("ödev: PDF okunamadı:", os.path.basename(yol), str(e)[:150])
        return None
    if "hata" in cevap:  # pypdf bozuk/şifreli dosyada çeşitli hatalar fırlatıyor
        log("ödev: PDF okunamadı:", os.path.basename(yol), cevap["hata"][:100])
        return None
    return cevap.get("metin")


def _pdf_metni_yerel(yol):
    """pdf_metni'nin alt süreçte çalışan çekirdeği."""
    from pypdf import PdfReader
    okuyucu = PdfReader(yol)
    parca = []
    for i, sayfa in enumerate(okuyucu.pages[:PDF_SAYFA_SINIRI], 1):
        metin = (sayfa.extract_text() or "").strip()
        if metin:
            parca.append(f"--- sayfa {i} ---\n{metin}")
    metin = "\n\n".join(parca)
    if len(metin) < 50:
        return f"(Bu PDF'ten metin çıkarılamadı; {len(okuyucu.pages)} sayfa, büyük ihtimalle taranmış görüntü.)"
    if len(okuyucu.pages) > PDF_SAYFA_SINIRI:
        metin += f"\n\n(İlk {PDF_SAYFA_SINIRI} sayfa alındı, toplam {len(okuyucu.pages)}.)"
    return metin


def ofis_metni(yol):
    """docx/pptx/pdf metnini çıkar (okuma aracı bunları açamıyor). Yanına '<ad>.metin.txt' yazar."""
    uz = yol.lower().rsplit(".", 1)[-1]
    if uz not in ("docx", "pptx", "pdf"):
        return None
    hedef = yol + ".metin.txt"
    if os.path.exists(hedef) and os.path.getmtime(hedef) >= os.path.getmtime(yol):
        return hedef
    if uz == "pdf":
        metin = pdf_metni(yol)  # hata ve zaman aşımı içeride günlüğe yazılır
        if metin is None:
            return None
        _yaz(hedef, metin)
        return hedef
    try:
        with zipfile.ZipFile(yol) as z:
            adlar = ["word/document.xml"] if uz == "docx" else sorted(
                (a for a in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", a)),
                key=lambda a: int(re.search(r"(\d+)", a.rsplit("/", 1)[-1]).group(1)))
            toplam = sum(z.getinfo(a).file_size for a in adlar)
            if toplam > ZIP_SINIRI or any(z.getinfo(a).file_size > ZIP_UYE_SINIRI for a in adlar):
                raise ValueError(f"açılmış boyut çok büyük ({toplam // 2**20} MB), zip bombası olabilir")
            parca = []
            for a in adlar:
                xml = z.read(a).decode("utf-8", "replace")
                xml = re.sub(r"</w:p>|</a:p>", "\n", xml)
                parca.append(re.sub(r"<[^>]+>", "", xml))
        _yaz(hedef, "\n\n".join(parca))
        return hedef
    except Exception as e:  # bozuk, şifreli, desteklenmeyen sıkıştırma, zip bombası: hiçbiri dışarı sızmasın
        log("ödev: ofis dosyası okunamadı:", os.path.basename(yol), repr(e)[:100])
        return None


LEARN_KOK = os.path.join(os.path.dirname(KOK), "dersler")
OFIS_UZANTILARI = (".pdf", ".docx", ".pptx")


def learn_metinlerini_hazirla():
    """Learn aynasındaki PDF/DOCX/PPTX'lerin metnini yanlarına '<ad>.metin.txt' olarak yazar (yalnızca yeni ya da
    değişmiş olanlar). Ödev değerlendirmesi ve dosya modundaki işler slaytları böylece okuyabilir."""
    yeni = 0
    for yol in glob.glob(os.path.join(LEARN_KOK, "**", "*"), recursive=True):
        if not (yol.lower().endswith(OFIS_UZANTILARI) and os.path.isfile(yol)):
            continue
        hedef = yol + ".metin.txt"
        try:  # tek bozuk dosya diğerlerini durdurmasın
            guncel = os.path.exists(hedef) and os.path.getmtime(hedef) >= os.path.getmtime(yol)
            if not guncel and ofis_metni(yol):
                yeni += 1
        except Exception as e:
            log("ödev: Learn dosyası işlenemedi:", os.path.basename(yol), repr(e)[:100])
    if yeni:
        log(f"ödev: Learn aynasında {yeni} dosyanın metni çıkarıldı")
    return yeni


def ozet():
    """Bağlam için: klasörü olan ödevler ve dosya/değerlendirme durumu."""
    sat = []
    for d in sorted(glob.glob(os.path.join(KOK, "*", "*"))):
        if not os.path.isdir(d):
            continue
        n = len(dosyalar(d))
        degerlendirme = os.path.exists(os.path.join(d, SONUC_ADI))
        sat.append(f"- {os.path.basename(os.path.dirname(d))} / {os.path.basename(d)}: "
                   f"{f'{n} dosya' if n else 'dosya yok'}{', değerlendirme var' if degerlendirme else ''}")
    if not sat:
        return []
    return ["Ödev klasörleri (ödev klasörü; ⟦ODEV⟧ ile değerlendirilir):"] + sat


class Odevler:
    def __init__(self, bildir):
        """bildir(etiket, ozet, yol, hata, arayarak)"""
        self.bildir = bildir
        self.suren = set()

    def degerlendir(self, ders, odev, teslim="bilinmiyor", arayarak=False):
        if limit.dolu_mu():
            raise RuntimeError(limit.bekle_mesaji())
        d = bul(ders, odev) or klasor(ders, odev, olustur=True)
        etiket = f"{os.path.basename(os.path.dirname(d))} — {os.path.basename(d)}"
        if d in self.suren:
            return f"{etiket} değerlendirmesi zaten sürüyor"
        self.suren.add(d)
        threading.Thread(target=self._calis, args=(d, etiket, teslim, arayarak), daemon=True,
                         name="odev-degerlendir").start()
        n = len(dosyalar(d))
        return f"{etiket}: değerlendirme başladı ({n} dosya" + (")" if n else "; klasör boş, tahmine dayalı olacak)")

    def _calis(self, d, etiket, teslim, arayarak):
        try:
            dosya = dosyalar(d)
            okunacak = [ofis_metni(f) or f for f in dosya]
            ders_kodu = os.path.basename(os.path.dirname(d))
            notlar = sorted(glob.glob(os.path.join(ayar("PANOPTO_KLASORU", os.path.join(DEPO_KOKU, "panopto")), f"*{ders_kodu}*",
                                                   "* - Ders Notu.md")))
            liste = ("Dosyalar:\n" + "\n".join(f"- {f}" for f in okunacak)) if okunacak else \
                "Klasörde dosya yok."
            learn = os.path.join(os.path.dirname(KOK), "dersler", ders_kodu)  # Learn aynası: ödev açıklaması, ekler
            if os.path.isdir(learn):
                liste += (f"\nDersin İEU Learn aynası (İçerik.md, ödev açıklamaları, ekler): {learn}. PDF/PPTX/DOCX "
                          "dosyalarının metni yanlarındaki '<dosya adı>.metin.txt' dosyalarında.")
            from .eylemler import DERS_ADLARI
            istem = ISTEM.format(ders=ders_kodu, ders_adi=DERS_ADLARI.get(ders_kodu.replace(' ', ''), ''),
                                 odev=os.path.basename(d), teslim=teslim, klasor=d, dosyalar=liste,
                                 notlar=", ".join(notlar) or "yok")
            dizinler = [d] + ([learn] if os.path.isdir(learn) else []) + sorted({os.path.dirname(n) for n in notlar})
            args = [CLAUDE, "-p", "--model", "sonnet", "--output-format", "json", "--tools", "Read,Glob,Grep",
                    "--permission-mode", "dontAsk", "--strict-mcp-config",
                    "--no-session-persistence"] + [x for k in dizinler for x in ("--add-dir", k)]
            p = subprocess.run(args, input=istem, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               cwd=d, timeout=900, creationflags=GIZLI)
            sonuc = str(json.loads(p.stdout or "{}").get("result", "")).strip()
            limit.kontrol(sonuc, "ödev değerlendirmesi")
            if len(sonuc) < 100 or "session limit" in sonuc or "Failed to authenticate" in sonuc:
                raise RuntimeError(sonuc[:200] or (p.stderr or "boş sonuç")[-200:])
            m = re.search(r"(?is)\**ÖZET:\**\s*(.+)$", sonuc)
            ozet_ = re.sub(r"\s+", " ", m.group(1)).strip()[:600] if m else sonuc[:400]
            yol = os.path.join(d, SONUC_ADI)
            with open(yol, "w", encoding="utf-8") as f:
                f.write(f"# {etiket} — değerlendirme ({dt.datetime.now():%Y-%m-%d %H:%M})\n\n{sonuc}\n")
            log(f"ödev değerlendirildi: {etiket}")
            self.bildir(etiket, ozet_, yol, None, arayarak)
        except Exception as e:
            log(f"ödev değerlendirilemedi ({etiket}):", repr(e)[:200])
            self.bildir(etiket, "", None, str(e)[:200], arayarak)
        finally:
            self.suren.discard(d)
