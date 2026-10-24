// Sahiplenme kuralları (Hakem Durable Object'i bunları kendi depolamasıyla çağırır). Depolama yalnızca
// get/put/delete isteyen bir arayüz: testte Map tabanlı sahte depo kullanılır (test/birim.test.mjs).
//
// Kayıt: { sahip: "servis" | "bulut", ms: sahiplenme anı, teslim: Telegram'a gittiği doğrulandı mı }

export const ONARIM_MS = 5 * 60 * 1000; // bu kadar sonra hâlâ "Bekliyor" görünen kayıt onarılır ya da devralınır

const k = (anahtar) => "s:" + anahtar;

// true: sen gönder. Servis kendi sahiplendiğini (henüz teslim edilmemişse) yeniden alabilir: gönderemeden yeniden
// başladıysa hatırlatma kaybolmasın. Bulut hiçbir zaman ikinci kez almaz.
export async function sahiplen(depo, anahtar, sahip, ms = Date.now()) {
  const mevcut = await depo.get(k(anahtar));
  if (mevcut) return sahip === "servis" && mevcut.sahip === "servis" && !mevcut.teslim;
  await depo.put(k(anahtar), { sahip, ms, teslim: false });
  return true;
}

// Yalnızca verilen sahibin, henüz teslim edilmemiş sahiplenmesi bırakılır.
export async function birak(depo, anahtar, sahip) {
  const mevcut = await depo.get(k(anahtar));
  if (!mevcut || mevcut.sahip !== sahip || mevcut.teslim) return false;
  await depo.delete(k(anahtar));
  return true;
}

export async function teslim(depo, anahtar, sahip) {
  const mevcut = await depo.get(k(anahtar));
  if (!mevcut || mevcut.sahip !== sahip) return false;
  await depo.put(k(anahtar), { ...mevcut, teslim: true });
  return true;
}

// Notion'da hâlâ "Bekliyor" görünen, başkasının sahiplendiği kayıt için ne yapılmalı:
//   "kapat"  : teslim edilmiş ama Notion kapatılamamış → kaydı kapat/ilerlet (göndermeden)
//   "gonder" : servis sahiplenmiş ama ONARIM_MS boyunca teslim edemedi (çöktü, kapandı) → bulut devralır, gönderir
//   "bekle"  : henüz erken
export async function onarim(depo, anahtar, ms = Date.now()) {
  const mevcut = await depo.get(k(anahtar));
  if (!mevcut || ms - mevcut.ms <= ONARIM_MS) return "bekle";
  if (mevcut.teslim) return "kapat";
  if (mevcut.sahip === "servis") {
    await depo.put(k(anahtar), { sahip: "bulut", ms, teslim: false });
    return "gonder";
  }
  return "bekle"; // bulutun kendi teslim edilmemiş sahiplenmesi: gönderim hatasında zaten bırakılır
}

export async function kim(depo, anahtar) {
  return (await depo.get(k(anahtar))) || null;
}

// ---------------------------------------------------------------- nabız (masaüstü sustu uyarısı)
// Servis 5 dk'da bir POST /nabiz gönderir, temiz kapanırken POST /kapaniyor. Cron, son nabız eşikten eskiyse ve
// arada temiz kapanış yoksa bir kez "sustu" uyarısı yazar; uyarıdan sonra nabız gelince bir kez "yeniden açık".
// Kayıt: { ms: son nabız, kapandi: son nabızdan sonra temiz kapandı mı, uyarildi: "sustu" gitti mi,
//          acildi: "yeniden açık" yazılacak mı, sessiz_ms: uyarıdan önceki son nabız }

const NABIZ = "nabiz";

export async function nabizAl(depo, ms = Date.now()) {
  const d = (await depo.get(NABIZ)) || {};
  const acildi = Boolean(d.acildi || d.uyarildi);
  await depo.put(NABIZ, { ms, kapandi: false, uyarildi: false, acildi,
                          sessiz_ms: acildi ? (d.sessiz_ms ?? d.ms ?? null) : null });
  return acildi;
}

export async function kapaniyor(depo, ms = Date.now()) {
  const d = (await depo.get(NABIZ)) || {};
  await depo.put(NABIZ, { ...d, kapandi: true, kapanis_ms: ms });
}

// Cron'un yapacağı tek iş: { tur: "acildi" | "sustu", ms: son nabız, sessiz_ms } ya da null. Hiç nabız gelmediyse
// hiçbir şey (Worker yeni kurulduysa ya da servis eski sürümse uyarı olmasın).
export async function nabizKarar(depo, esikMs, ms = Date.now()) {
  const d = await depo.get(NABIZ);
  if (!d || !d.ms) return null;
  if (d.acildi) return { tur: "acildi", ms: d.ms, sessiz_ms: d.sessiz_ms ?? null };
  if (!d.kapandi && !d.uyarildi && ms - d.ms > esikMs) return { tur: "sustu", ms: d.ms };
  return null;
}

// Mesaj Telegram'a gitti. "sustu" kararından sonra nabız geldiyse (karar ile gönderim arasında) uyarı gitmiş olur
// ama nabız uyarıyı görmemiştir: "yeniden açık" yazılsın diye acildi kurulur.
export async function nabizBildirildi(depo, karar) {
  const d = await depo.get(NABIZ);
  if (!d) return;
  if (karar.tur === "acildi") {
    await depo.put(NABIZ, { ...d, acildi: false, sessiz_ms: null });
  } else if (d.ms === karar.ms) {
    await depo.put(NABIZ, { ...d, uyarildi: true, sessiz_ms: karar.ms });
  } else {
    await depo.put(NABIZ, { ...d, acildi: true, sessiz_ms: karar.ms });
  }
}

export async function nabizDurum(depo) {
  return (await depo.get(NABIZ)) || null;
}
