"""Beyne giden görüntüler: büyük fotoğrafı küçültür, taranmış (metinsiz) PDF'in ilk sayfalarını görüntüye çevirir.

Güvenilmeyen dosyanın çözülmesi (Pillow'un ve PDFium'un yerel C kodu) servis sürecinde değil, `alt_surec.tek_sefer`
ile açılan ayrı bir süreçte ve süre sınırıyla yapılır: bozuk ya da kötü niyetli bir dosya servisi düşüremez,
kilitleyemez. Servis süreci Pillow'u hiç içe aktarmaz; alt süreçten gelen cevabı da yeniden denetler (tür ve boyut).
"""
import base64
import io
import os

from .ayarlar import log

RESIM_SINIRI = int(3.5 * 2**20)   # sohbet.RESIM_SINIRI ile aynı (base64'lü hâli API'nin 5 MB görüntü sınırının altında)
UZUN_KENAR = 2000                 # fotoğrafın en uzun kenarı (px); daha büyüğü küçültülür
PDF_UZUN_KENAR = 1600             # taranmış PDF sayfasının görüntüsü (px)
PDF_SAYFA = 3                     # taranmış PDF'ten beyne giden en çok sayfa
EN_COK_PIKSEL = 50_000_000        # bundan çok pikselli görüntü açılmaz (sıkıştırma bombası)
GIRDI_SINIRI = 25 * 2**20         # bundan büyük görüntü dosyasına hiç bakılmaz (Telegram zaten 20 MB'a izin veriyor)
JPEG_KALITE = (85, 70, 55)
GORUNTU_ZAMAN_ASIMI = 30          # sn
PDF_ZAMAN_ASIMI = 60              # sn
TARANMIS_ISARETI = "(Bu PDF'ten metin çıkarılamadı"   # odevler._pdf_metni_yerel'in metinsiz PDF notu
_IFADE = "from telefon import medya, alt_surec; alt_surec.isci_dongusu(medya._cocuk_isle, 30)"


# ------------------------------------------------------------ servis süreci tarafı
def _cocukta(istek, zaman_asimi, ad):
    """İsteği alt süreçte işlet; cevap (dict) ya da hata/zaman aşımında None (günlüğe yazılır)."""
    from . import alt_surec
    try:
        cevap = alt_surec.tek_sefer(ad, _IFADE, istek, zaman_asimi)
    except Exception as e:
        log(f"medya: {ad} yapılamadı:", os.path.basename(istek.get("yol", "")), str(e)[:150])
        return None
    if "hata" in cevap:
        log(f"medya: {ad} yapılamadı:", os.path.basename(istek.get("yol", "")), str(cevap["hata"])[:150])
        return None
    return cevap


def _gecerli(resim, sinir):
    """Alt süreçten gelen (tür, base64) beklenen biçimde mi: yalnızca sınırın altındaki JPEG kabul edilir."""
    return (isinstance(resim, (list, tuple)) and len(resim) == 2 and resim[0] == "image/jpeg"
            and isinstance(resim[1], str) and len(resim[1]) * 3 // 4 <= sinir)


def goruntu_eki(yol, bayt, tur, sinir=RESIM_SINIRI):
    """Beyne gidecek görüntü (tür, base64) ya da None. Bayt ya da piksel sınırını aşan görüntü alt süreçte JPEG'e
    küçültülür. Pillow'un tanımadığı ya da alt sürecin işleyemediği görüntü sınırın altındaysa eskisi gibi olduğu gibi
    gider; piksel bombası ve küçültülemeyen büyük görüntü hiç gitmez."""
    cevap = _cocukta({"is": "goruntu", "yol": os.path.abspath(yol), "sinir": sinir}, GORUNTU_ZAMAN_ASIMI,
                     "görüntü küçültme") or {}
    if cevap.get("resim"):
        return tuple(cevap["resim"]) if _gecerli(cevap["resim"], sinir) else None
    if cevap.get("red"):
        log("medya: görüntü gönderilmedi:", os.path.basename(yol), str(cevap["red"])[:100])
        return None
    return (tur, base64.b64encode(bayt).decode()) if len(bayt) <= sinir else None


def taranmis_mi(icerik):
    """pdf_metni sonucu metinsiz (taranmış) bir PDF'i mi anlatıyor. None (okunamadı) taranmış sayılmaz."""
    return icerik is not None and (not icerik.strip() or icerik.lstrip().startswith(TARANMIS_ISARETI))


def pdf_goruntuleri(yol, sinir=RESIM_SINIRI):
    """Taranmış PDF'in ilk PDF_SAYFA sayfası [(tür, base64), ...], alt süreçte çizilir. Hata ya da zaman aşımında []."""
    cevap = _cocukta({"is": "pdf", "yol": os.path.abspath(yol), "sinir": sinir}, PDF_ZAMAN_ASIMI,
                     "PDF görüntüsü") or {}
    resimler = cevap.get("resimler") if isinstance(cevap.get("resimler"), list) else []
    return [tuple(r) for r in resimler[:PDF_SAYFA] if _gecerli(r, sinir)]


# ------------------------------------------------------------ alt süreç tarafı (Pillow ve PDFium yalnızca burada)
def _pil():
    import warnings
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = EN_COK_PIKSEL  # Pillow 2 katını aşanı açarken DecompressionBombError fırlatır
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)  # arası aşağıda elle reddedilir
    return Image


def _jpeg(im, sinir):
    """RGB görüntüyü sınırın altına sığan JPEG baytlarına çevir: önce kaliteyi düşür, yetmezse boyutu."""
    Image = _pil()
    for _ in range(8):
        for kalite in JPEG_KALITE:
            b = io.BytesIO()
            im.save(b, "JPEG", quality=kalite, optimize=True)
            if b.tell() <= sinir:
                return b.getvalue()
        im = im.resize((max(1, im.width * 3 // 4), max(1, im.height * 3 // 4)), Image.LANCZOS)
    return None


def _rgb(im):
    """RGBA/LA/P (saydam) görüntüyü beyaz zemine, diğer kipleri doğrudan RGB'ye çevir."""
    Image = _pil()
    if im.mode == "P":
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA"):
        zemin = Image.new("RGB", im.size, (255, 255, 255))
        zemin.paste(im.convert("RGBA"), mask=im.getchannel("A"))
        return zemin
    return im if im.mode == "RGB" else im.convert("RGB")


def kucult(bayt, sinir=RESIM_SINIRI, uzun_kenar=UZUN_KENAR):
    """Görüntüyü JPEG'e çevir: EXIF yönü uygulanır, en uzun kenar ≤ uzun_kenar, boyut ≤ sinir.
    ("image/jpeg", bayt) ya da None (bozuk dosya, piksel bombası, Pillow hatası)."""
    try:
        Image = _pil()
        from PIL import ImageOps
        with Image.open(io.BytesIO(bayt)) as im:
            if im.width * im.height > EN_COK_PIKSEL:
                return None
            im.draft("RGB", (uzun_kenar, uzun_kenar))  # JPEG'i küçük ölçekte çöz (bellek ve süre)
            im = ImageOps.exif_transpose(im)
            im = _rgb(im)
            im.thumbnail((uzun_kenar, uzun_kenar), Image.LANCZOS)
            veri = _jpeg(im, sinir)
        return ("image/jpeg", veri) if veri else None
    except Exception:  # DecompressionBombError, bozuk/eksik dosya, desteklenmeyen kip: hiçbiri dışarı sızmasın
        return None


def _goruntu_yerel(yol, sinir):
    Image = _pil()
    with open(yol, "rb") as f:
        bayt = f.read()
    try:
        with Image.open(io.BytesIO(bayt)) as im:  # yalnızca başlık okunur
            en, boy = im.size
    except Image.DecompressionBombError:
        return {"red": "çok fazla piksel"}
    except Exception:
        return {"bilinmiyor": True}  # Pillow tanımadı: ana süreç eski davranışa döner
    if en * boy > EN_COK_PIKSEL:
        return {"red": f"çok fazla piksel ({en}×{boy})"}
    if len(bayt) <= sinir and max(en, boy) <= UZUN_KENAR:
        return {"ozgun": True}
    kucuk = kucult(bayt, sinir)
    if not kucuk:
        return {"red": "küçültülemedi"}
    return {"resim": [kucuk[0], base64.b64encode(kucuk[1]).decode()]}


def _pdf_yerel(yol, sinir):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(yol)
    try:
        cikti = []
        for i in range(min(len(pdf), PDF_SAYFA)):
            sayfa = pdf[i]
            try:
                en, boy = sayfa.get_size()
                im = sayfa.render(scale=PDF_UZUN_KENAR / max(en, boy, 1)).to_pil()
                im.thumbnail((PDF_UZUN_KENAR, PDF_UZUN_KENAR))
                veri = _jpeg(_rgb(im), sinir)
            finally:
                sayfa.close()
            if veri:
                cikti.append(["image/jpeg", base64.b64encode(veri).decode()])
        return cikti
    finally:
        pdf.close()


def _cocuk_isle(istek):
    if istek.get("is") == "goruntu":
        return _goruntu_yerel(istek["yol"], int(istek["sinir"]))
    if istek.get("is") == "pdf":
        return {"resimler": _pdf_yerel(istek["yol"], int(istek["sinir"]))}
    raise ValueError(f"bilinmeyen iş: {istek.get('is')!r}")
