"""Ağır ya da güvenilmeyen işleri (ses tanıma, PDF metni) servis sürecinin dışında, ayrı bir Python sürecinde çalıştırır.

Çocuk süreç stdin'den satır satır JSON istek alır, stdout'a satır satır JSON cevap yazar. Çocuk düşerse (ör. cuDNN DLL
hatası CTranslate2'yi öldürür) ya da zaman aşımına uğrarsa öldürülür ve `AltSurecHatasi` fırlar; servis süreci etkilenmez,
sonraki istek yeni bir süreç açar. Çocuk venv'in python.exe'siyle `-I` (yalıtılmış) açılır: çalışma klasöründeki ya da
PYTHON* ortam değişkenlerindeki hiçbir şey içe aktarılmaz. venv başlatıcısı öldürülünce içindeki asıl python.exe de
ölür (başlatıcı onu "kapanınca öldür" iş nesnesine koyuyor; 2026-10-17'de denendi).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

from .ayarlar import KOK

PY = os.path.join(KOK, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
GIZLI = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AltSurecHatasi(RuntimeError):
    """Çocuk süreç düştü ya da zamanında cevap vermedi (süreç öldürüldü)."""


def komut(ifade):
    """Çocuk süreçte `ifade`yi (tek satır Python) çalıştıran komut. Proje kökü sys.path'e açıkça eklenir."""
    return [PY, "-I", "-c", f"import sys; sys.path.insert(0, {KOK!r}); {ifade}"]


def _oku(akis, kuyruk):
    try:
        for satir in iter(akis.readline, b""):
            kuyruk.put(satir)
    except (OSError, ValueError):
        pass
    kuyruk.put(None)  # süreç kapandı


class Isci:
    """Uzun yaşayan çocuk süreç. İstekler sırayla işlenir; süreç yoksa ya da kapanmışsa ilk istekte açılır."""

    def __init__(self, ad, komut, env=None):
        self.ad, self.komut, self.env = ad, komut, env
        self.surec = None
        self._satirlar = None
        self._kilit = threading.Lock()

    @property
    def calisiyor(self):
        return self.surec is not None and self.surec.poll() is None

    def _baslat(self):
        self.surec = subprocess.Popen(self.komut, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=KOK,
                                      env=self.env, creationflags=GIZLI)
        self._satirlar = queue.Queue()  # her sürecin kendi kuyruğu: eski sürecin satırları karışmasın
        threading.Thread(target=_oku, args=(self.surec.stdout, self._satirlar), daemon=True,
                         name=f"{self.ad}-cikis").start()

    def _oldur(self):
        s, self.surec = self.surec, None
        if s is None:
            return
        try:
            s.kill()
            s.wait(10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for akis in (s.stdin, s.stdout):
            try:
                akis.close()
            except (OSError, ValueError):
                pass

    def iste(self, istek, zaman_asimi):
        """İsteği gönder, cevabı (dict) döndür. Düşme ya da zaman aşımında süreci öldürüp AltSurecHatasi fırlatır."""
        with self._kilit:
            for deneme in (1, 2):
                if not self.calisiyor:
                    self._oldur()
                    self._baslat()
                try:
                    self.surec.stdin.write((json.dumps(istek) + "\n").encode())
                    self.surec.stdin.flush()
                except (OSError, ValueError):
                    pass  # süreç kapanmış: aşağıda EOF olarak görülür
                son = time.monotonic() + zaman_asimi
                while True:
                    try:
                        satir = self._satirlar.get(timeout=max(0.0, son - time.monotonic()))
                    except queue.Empty:
                        self._oldur()
                        raise AltSurecHatasi(f"{self.ad}: {zaman_asimi:.0f} sn'de cevap gelmedi, süreç kapatıldı")
                    if satir is None:
                        break
                    try:
                        cevap = json.loads(satir)
                    except ValueError:
                        continue  # protokol dışı satır
                    if isinstance(cevap, dict):
                        return cevap
                try:
                    kod = self.surec.wait(10)
                except subprocess.TimeoutExpired:
                    kod = None
                self._oldur()
                if kod == 0 and deneme == 1:
                    continue  # boşta kalıp kendiliğinden kapanırken yakalandı: yeni süreçle bir kez daha
                raise AltSurecHatasi(f"{self.ad} süreci düştü (çıkış kodu {kod})")

    def kapat(self, bekle=5):
        """stdin'i kapat (çocuk kendiliğinden çıkar); çıkmazsa öldür."""
        s = self.surec
        if s is None:
            return
        try:
            s.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            s.wait(bekle)
        except subprocess.TimeoutExpired:
            pass
        self._oldur()


def tek_sefer(ad, ifade, istek, zaman_asimi):
    """Tek iş için çocuk süreç: cevabı döndürür, iş bitince süreç kapanır. Hata: AltSurecHatasi."""
    i = Isci(ad, komut(ifade))
    try:
        return i.iste(istek, zaman_asimi)
    finally:
        i.kapat()


# ------------------------------------------------------------ çocuk süreç tarafı
def _protokol_cikisi():
    """Gerçek stdout'u protokole ayır, 1 numaralı tanımlayıcıyı boşluğa yönlendir: print'ler (ayarlar.log) ya da yerel
    kütüphanelerin yazdıkları JSON satırlarını bozmasın."""
    fd = os.dup(1)
    bos = os.open(os.devnull, os.O_WRONLY)
    os.dup2(bos, 1)
    os.close(bos)
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def isci_dongusu(isle, bosta_sn):
    """Çocuk sürecin ana döngüsü: her JSON istek için isle(istek) -> dict. stdin kapanınca ya da bosta_sn boyunca istek
    gelmezse süreç tamamen çıkar (GPU belleği de böylece gerçekten boşalır). isle'nin hatası {"hata": ...} olarak döner."""
    cikis = _protokol_cikisi()
    # stdin'i yalnızca ana iş parçacığı, yalnızca boştayken okur. Windows'ta bir iş parçacığı borudan okumayı
    # beklerken aynı tanıtıcıya dokunan başka çağrılar da kilitleniyor (faster-whisper işçisi böyle takıldı,
    # 2026-10-17); boşta kapanmayı bu yüzden ayrı bir bekçi yapar.
    durum = {"son": time.monotonic(), "mesgul": False}

    def bekci():
        while True:
            time.sleep(min(1.0, bosta_sn / 2))
            if not durum["mesgul"] and time.monotonic() - durum["son"] > bosta_sn:
                os._exit(0)  # boşta kaldı (yarışta gelen istek ana süreçte yeniden denenir)
    threading.Thread(target=bekci, daemon=True, name="bosta").start()
    while True:
        satir = sys.stdin.buffer.readline()
        if not satir:
            break  # ana süreç kapandı
        durum["mesgul"] = True
        try:
            cevap = isle(json.loads(satir))
        except Exception as e:
            cevap = {"hata": repr(e)[:500]}
        cikis.write(json.dumps(cevap) + "\n")
        cikis.flush()
        durum["son"], durum["mesgul"] = time.monotonic(), False
    cikis.flush()
    os._exit(0)
