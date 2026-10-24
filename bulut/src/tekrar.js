// Tekrarlayan hatırlatmalar: sıradaki zaman. telefon/tekrar.py ile aynı kural, aynı örneklerle sınanır
// (test/tekrar_ornekleri.json). Zamanlar yerel duvar saati olarak "YYYY-MM-DDTHH:MM" metni; hesap UTC alanlarıyla
// yapılır ki çalışma ortamının saat dilimi sonucu değiştirmesin.

export const TR = { gunluk: "Günlük", haftaici: "Hafta içi", haftalik: "Haftalık", aylik: "Aylık" };
export const ADLAR = { gunluk: "her gün", haftaici: "hafta içi her gün", haftalik: "her hafta", aylik: "her ay" };
const YEREL = Object.fromEntries(Object.entries(TR).map(([k, v]) => [v, k]));

// "Haftalık" / "haftalik" / "Yok" / null -> "haftalik" ya da null; tanınmayan değer de null (tek seferlik sayılır).
export function kod(deger) {
  if (!deger || deger === "Yok") return null;
  if (TR[deger]) return deger;
  return YEREL[deger] || null;
}

function coz(metin) {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(metin);
  if (!m) throw new Error("geçersiz zaman: " + metin);
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
}

export function yaz(t) {
  return t.toISOString().slice(0, 16);
}

function ayinGunleri(yil, ay) {
  return new Date(Date.UTC(yil, ay + 1, 0)).getUTCDate();
}

function birAdim(t, tekrar, gun) {
  const z = new Date(t.getTime());
  if (tekrar === "gunluk") z.setUTCDate(z.getUTCDate() + 1);
  else if (tekrar === "haftalik") z.setUTCDate(z.getUTCDate() + 7);
  else if (tekrar === "haftaici") {
    do z.setUTCDate(z.getUTCDate() + 1);
    while (z.getUTCDay() === 0 || z.getUTCDay() === 6);
  } else if (tekrar === "aylik") {
    let yil = z.getUTCFullYear();
    let ay = z.getUTCMonth() + 1;
    if (ay === 12) { yil += 1; ay = 0; }
    z.setUTCDate(1);
    z.setUTCFullYear(yil, ay, Math.min(gun, ayinGunleri(yil, ay)));
  } else throw new Error("tanınmayan tekrar: " + tekrar);
  return z;
}

// zaman ve simdi: yerel "YYYY-MM-DDTHH:MM[:SS]". Dönüş: simdi'den sonraki ilk tekrar (kaçırılanlar atlanır).
export function sonraki(zaman, tekrar, simdi) {
  const bas = coz(zaman);
  const s = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d))?/.exec(simdi);
  const sinir = Date.UTC(+s[1], +s[2] - 1, +s[3], +s[4], +s[5], +(s[6] || 0));
  let z = bas;
  for (let i = 0; i < 4000; i++) {
    z = birAdim(z, tekrar, bas.getUTCDate());
    if (z.getTime() > sinir) return yaz(z);
  }
  throw new Error("sıradaki tekrar bulunamadı");
}
