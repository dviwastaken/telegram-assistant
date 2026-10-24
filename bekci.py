"""Servis bekçisi: `python -m telefon` sürecini çalıştırır, çökerse artan beklemeyle yeniden başlatır.
pythonw.exe ile çalıştırılırsa pencere açmaz. Aynı anda iki bekçi çalışmasın diye kilit portu tutar.
Servis üst üste hızlı çökerse Telegram'dan haber verir (servisin kendi gözcüleri o durumda çalışmaz). Önce sohbet
botunu (.env `TELEGRAM_SOHBET_JETONU`) dener ki uyarı kullanıcının diğer bildirimleriyle aynı sohbete düşsün; olmazsa
yedek bota (`TELEGRAM_YEDEK_JETONU`) düşer. Servis çökmüş olsa da iki bot da yazabilir (yazmak dinleyici gerektirmez)."""
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

KOK = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(KOK, ".venv", "Scripts", "python.exe")
GUNLUK = os.path.join(KOK, "kayitlar", "bekci.log")
HATA_GUNLUGU = os.path.join(KOK, "kayitlar", "servis-stderr.log")
KILIT_PORT = 7349
HIZLI_COKUS_SN = 120
UYARI_ESIGI = 3  # üst üste bu kadar hızlı çöküşte Telegram


def log(m):
    with open(GUNLUK, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")


def _env():
    """.env'i kendisi okur (telefon paketini içe aktarmadan: bekçi servisten bağımsız kalsın)."""
    d = {}
    try:
        with open(os.path.join(KOK, ".env"), encoding="utf-8-sig") as f:
            for satir in f:
                satir = satir.strip()
                if satir and not satir.startswith("#") and "=" in satir:
                    k, v = satir.split("=", 1)
                    d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def _botlar():
    """[(ad, jeton, sohbet_id)]: önce sohbet botu, sonra yedek bot. Ayarsız olan atlanır."""
    dosya, botlar = _env(), []
    anahtarlar = ("TELEGRAM_SOHBET_JETONU", "TELEGRAM_SOHBET_ID", "TELEGRAM_YEDEK_JETONU", "TELEGRAM_YEDEK_SOHBET_ID")
    env = {k: os.environ.get(k) or dosya.get(k) for k in anahtarlar}
    yedek = (env["TELEGRAM_YEDEK_JETONU"], env["TELEGRAM_YEDEK_SOHBET_ID"])
    sohbet_id = env["TELEGRAM_SOHBET_ID"] or yedek[1] or ""
    if env["TELEGRAM_SOHBET_JETONU"] and sohbet_id:
        botlar.append(("sohbet botu", env["TELEGRAM_SOHBET_JETONU"], sohbet_id))
    if all(yedek):
        botlar.append(("yedek bot", yedek[0], yedek[1]))
    return botlar


def telegram(metin):
    """Kullanıcıya mesaj: sohbet botu, olmazsa yedek bot. Dönüş: gönderen botun adı ya da None."""
    for ad, jeton, sohbet in _botlar():
        try:
            veri = urllib.parse.urlencode({"chat_id": sohbet, "text": metin}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{jeton}/sendMessage", data=veri, timeout=15)
            log(f"telegram gönderildi ({ad})")
            return ad
        except Exception as e:
            # repr URL'yi, yani jetonu içerebilir: yalnızca tür ve HTTP kodu
            log(f"telegram gönderilemedi ({ad}): {type(e).__name__} {getattr(e, 'code', '')}".rstrip())
    return None


def dondur(yol, sinir=5 * 2**20):
    """Günlük 5 MB'ı geçtiyse .1'e çevir (bir önceki .1 silinir)."""
    try:
        if os.path.getsize(yol) > sinir:
            os.replace(yol, yol + ".1")
    except OSError:
        pass


def main():
    kilit = socket.socket()
    try:
        kilit.bind(("127.0.0.1", KILIT_PORT))
    except OSError:
        log("başka bir bekçi zaten çalışıyor, çıkılıyor")
        return
    os.makedirs(os.path.dirname(GUNLUK), exist_ok=True)
    bekleme, hizli = 5, 0
    while True:
        dondur(GUNLUK)
        dondur(HATA_GUNLUGU)
        log("servis başlatılıyor")
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with open(HATA_GUNLUGU, "a", encoding="utf-8") as err:
            p = subprocess.run([PY, "-W", "ignore", "-u", "-m", "telefon"], cwd=KOK, env=env,
                               stdout=subprocess.DEVNULL, stderr=err,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sure = time.time() - t0
        log(f"servis çıktı (kod {p.returncode}, {sure:.0f} sn çalıştı)")
        hizli = 0 if sure > HIZLI_COKUS_SN else hizli + 1
        bekleme = 5 if sure > HIZLI_COKUS_SN else min(bekleme * 2, 300)  # yalnızca hızlı çöküş döngüsünde yavaşla
        if hizli == UYARI_ESIGI:  # bir kez; düzelip yeniden çökerse tekrar
            telegram(f"⚠️ Claude asistan servisi üst üste {hizli} kez hemen çöktü (son çıkış kodu {p.returncode}).\n"
                     "Bekçi denemeye devam ediyor.\n"
                     r"Hata ayrıntısı: claude-telefon\kayitlar\servis-stderr.log")
        time.sleep(bekleme)


if __name__ == "__main__":
    sys.exit(main())
