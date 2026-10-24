"""Telegram sesli mesajlarını yazıya çevirir (faster-whisper large-v3-turbo, CUDA).

Tanıma servis sürecinde değil, ayrı ve uzun yaşayan bir alt süreçte (`alt_surec.Isci`) çalışır: cuDNN DLL'i gibi bir
sorun CTranslate2'yi düşürse bile servis ayakta kalır, o sesli mesaj için hata döner, sonraki mesaj yeni bir süreç
açar. Model alt süreçte ilk sesli mesajda yüklenir (birkaç saniye; oyun açıkken çok daha uzun sürebilir). Alt süreç 10
dakika (`SES_BOSTA_SN`) istek almazsa tamamen kapanır, böylece GPU belleği gerçekten boşalır. Ses dosyası (Telegram'ın
OGG/Opus'u) PyAV ile doğrudan okunur. Kod, arşivdeki telefon katmanının `Cevirmen`'inden
(arsiv/telefon-arama/telefon/ses.py) uyarlandı.
"""
import glob
import os
import re
import sys
import threading
import time

from . import alt_surec
from .ayarlar import ayar, log

MODEL = ayar("SES_MODEL", "large-v3-turbo")
BOSTA_KAPAT_SN = int(ayar("SES_BOSTA_SN", "600"))
ZAMAN_ASIMI_TABAN = 300     # sn; ilk istekte model yükleme de buna dahil
ZAMAN_ASIMI_SINIRI = 1800   # sn; dosya boyutuyla artan sürenin üst sınırı
IPUCU = ("kullanıcı, asistanı Claude'a Telegram'dan sesli mesaj gönderiyor. Claude, Notion, ödev, hatırlatma, BIL, "
         "GitHub, commit, pull request, İEU Learn, Zoom, Blokchain.")
# Whisper'ın sessizlikte uydurduğu kalıplar
HALUSINASYON = re.compile(
    r"altyaz[ıi]|abone ol|izledi[ğg]iniz i[çc]in|bir sonraki videoda|m\.k\.|sesli betimleme|^\W*$", re.I)

_durum = {"model": None, "cihaz": None}
_isci_kilit = threading.Lock()
_isci_nesne = None


# ------------------------------------------------------------ servis tarafı
def _isci():
    global _isci_nesne
    with _isci_kilit:
        if _isci_nesne is None:
            _isci_nesne = alt_surec.Isci("ses tanıma", alt_surec.komut(
                f"from telefon import ses_tanima; ses_tanima.isci_ana({BOSTA_KAPAT_SN})"))
        return _isci_nesne


def cevir(yol):
    """Ses dosyasını Türkçe metne çevir (alt süreçte). Anlaşılır konuşma yoksa boş metin döner. Alt süreç düşer ya da
    zaman aşımına uğrarsa AltSurecHatasi, tanıma hata verirse RuntimeError fırlar (ikisi de medya döngüsünde yakalanır)."""
    sinir = min(ZAMAN_ASIMI_SINIRI, ZAMAN_ASIMI_TABAN + os.path.getsize(yol) / 4096)  # ~1 sn / sn ses (32 kbps)
    try:
        cevap = _isci().iste({"yol": os.path.abspath(yol)}, sinir)
    except alt_surec.AltSurecHatasi as e:
        log("ses tanıma:", e)
        raise
    for satir in cevap.get("gunluk") or []:
        log(satir)
    if "hata" in cevap:
        log("ses tanıma: çözülemedi:", cevap["hata"][:200])
        raise RuntimeError(f"ses tanıma başarısız: {cevap['hata'][:150]}")
    if cevap.get("ses_sn") is not None:
        log(f"ses tanıma: {cevap['ses_sn']:.1f} sn ses, {cevap.get('sure', 0):.1f} sn sürdü ({cevap.get('cihaz')})")
    return cevap.get("metin") or ""


# ------------------------------------------------------------ alt süreç tarafı
def isci_ana(bosta_sn=BOSTA_KAPAT_SN):
    """Alt sürecin girişi: istek {"yol"}, cevap {"metin", "ses_sn", "sure", "cihaz", "gunluk"}. Günlük satırları
    servise geri gönderilir (alt süreç servis.log'a kendisi yazmaz)."""
    global log
    gunluk = []
    log = lambda *p: gunluk.append(" ".join(str(x) for x in p))  # noqa: E731

    def isle(istek):
        gunluk.clear()
        t0 = time.perf_counter()
        try:
            metin, ses_sn = _cevir_yerel(istek["yol"])
        except Exception as e:
            return {"hata": repr(e)[:500], "gunluk": list(gunluk)}
        return {"metin": metin, "ses_sn": ses_sn, "sure": time.perf_counter() - t0, "cihaz": _durum["cihaz"],
                "gunluk": list(gunluk)}
    alt_surec.isci_dongusu(isle, bosta_sn)


def _cuda_hazirla():
    """nvidia pip paketlerindeki cuBLAS/cuDNN DLL'lerini görünür yap (Windows)."""
    sp = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    for d in glob.glob(os.path.join(sp, "*", "bin")):
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _model():
    if _durum["model"] is None:
        _cuda_hazirla()
        from faster_whisper import WhisperModel
        t0 = time.perf_counter()
        try:
            _durum["model"], _durum["cihaz"] = WhisperModel(MODEL, device="cuda", compute_type="float16"), "cuda"
        except Exception as e:  # CUDA yoksa ya da sürücü sorunluysa işlemcide yavaş ama çalışır
            log("ses tanıma: CUDA açılamadı, işlemciye geçiliyor:", repr(e)[:150])
            _durum["model"], _durum["cihaz"] = WhisperModel(MODEL, device="cpu", compute_type="int8"), "cpu"
        log(f"ses tanıma: {MODEL} yüklendi ({_durum['cihaz']}, {time.perf_counter() - t0:.1f} sn)")
    return _durum["model"]


def oku_16k(yol):
    """Ses dosyasını 16 kHz mono float32 diziye çevir. faster-whisper'ın kendi okuyucusu venv'deki PyAV 19 ile
    uyumsuz (`av.open(metadata_errors=...)` kaldırılmış), bu yüzden çözme burada yapılıyor."""
    import av
    import numpy as np
    parca = []
    yeniden = av.AudioResampler(format="flt", layout="mono", rate=16000)
    with av.open(yol) as kap:
        for kare in kap.decode(audio=0):
            for k in yeniden.resample(kare):
                parca.append(k.to_ndarray().reshape(-1))
        for k in yeniden.resample(None):  # tampondaki son örnekler
            parca.append(k.to_ndarray().reshape(-1))
    return np.concatenate(parca).astype(np.float32) if parca else np.zeros(0, np.float32)


def _cevir_yerel(yol):
    """Bu süreçte çevir: (metin, ses süresi sn). Çok kısa seste ("", None)."""
    ses = oku_16k(yol)
    if len(ses) < 16000 * 0.3:
        return "", None
    try:
        parca, bilgi = _coz(_model(), ses)
    except Exception as e:  # ör. oyun açıkken CUDA belleği yetmez: bir kez işlemcide dene
        log("ses tanıma: GPU'da çözülemedi, işlemcide yeniden deneniyor:", repr(e)[:150])
        _durum["model"] = None
        from faster_whisper import WhisperModel
        _durum["model"], _durum["cihaz"] = WhisperModel(MODEL, device="cpu", compute_type="int8"), "cpu"
        parca, bilgi = _coz(_durum["model"], ses)
    return " ".join(parca).strip(), bilgi.duration


def _coz(model, ses):
    segs, bilgi = model.transcribe(ses, language="tr", beam_size=5, vad_filter=True,
                                   condition_on_previous_text=False, initial_prompt=IPUCU)
    # uydurma filtresi parça bazında: yalnızca tamamı kalıptan oluşan kısa parçalar atılır ("YouTube'da altyazıyı
    # nasıl açarım" gibi gerçek bir cümle silinmez)
    return [s.text.strip() for s in segs if s.no_speech_prob < 0.7
            and not (HALUSINASYON.search(s.text) and len(s.text.split()) <= 6)], bilgi
