# bv_extractor — Kullanım Kılavuzu

Bilimsel PDF makalelerden biyolojik varyasyon (BV) tablolarını
(CVI, CVG, CVA, Mean±SD, %95 CI) çıkarıp **Excel + JSON + metin raporu**
olarak veren araç. İki motor var:

1. **Deterministik parser** — tanıdık formatları (mean±SD) hızlı ve
   ücretsiz çıkarır. Yang 2018 referans makalesinde 11/11 test geçer.
2. **Claude (LLM) görüntü tabanlı çıkarım** — parser'ın çözemediği
   formatlar (value+CI, düz sütunlu, döndürülmüş, alışılmadık tablolar)
   için. Seçilen tabloyu resim olarak Claude Opus 4.8'e gönderir.

---

## Kurulum (tek seferlik)

1. Sanal ortam paketleri: `pip install -r requirements.txt`
   (pdfplumber, pandas, openpyxl, anthropic, pypdfium2, Pillow).
2. Claude için API anahtarı (yalnızca LLM yolu için gerekir):
   ```
   setx ANTHROPIC_API_KEY "sk-ant-..."
   ```
   Ardından PyCharm'ı / terminali **tamamen kapatıp yeniden aç** (anahtar
   yalnızca yeni açılan süreçlerde görünür). Test: yeni terminalde
   `echo $env:ANTHROPIC_API_KEY` anahtarı yazmalı.

---

## Çalıştırma

**GUI (önerilen):**
```
python -m bv_extractor
```

**Komut satırı (otomasyon/script):**
```
python -m bv_extractor.cli <pdf> -o <çıktı_klasörü>
python -m bv_extractor.cli <pdf> --engine claude --no-interactive
```

---

## GUI akışı (adım adım)

1. **PDF Aç** → makale seçilir. Sağda **"Detected table page"** panelinde
   programın bulduğu sayfa, solda ön-analiz özeti (format, sayfa) görünür.
2. **Çıkar (Extract ▶):**
   - "Use Claude (LLM)" kutusu **kapalıysa** önce deterministik parser
     denenir. Değer bulamazsa *"Claude'a göndereyim mi?"* sorulur.
   - Kutu **açıksa** doğrudan tablo seçiciye geçer.
   - Bulunan sayfa **yanlışsa** → **"Pick table manually…"** ile elle seç.
3. **Tablo seçici (picker):**
   - ◀ / ▶ : sayfa değiştir
   - **Zoom +/−** (veya `+` / `−`) : netlik için yakınlaştır
   - **Rotate ⟳** (veya `r`) : yan/döndürülmüş tabloyu dikleştir
   - Fareyle tablonun etrafına **kutu çiz** (başlık satırını da içine al)
   - **Add table ➕** : birden fazla tablo seç (farklı sayfa/döndürme olabilir)
   - **Finish ✓** (Enter) : bitir · **Esc** : iptal
   - Her tablo Claude'a **ayrı** gönderilir (yoğun tabloların karışmasını önler).
4. **Sonuç önizleme:** analitler tabloda gösterilir; token sayısı ve tahmini
   maliyet üstte yazar. Buradan:
   - **Save…** : Excel/JSON/metin yazılır.
   - **Try with Claude…** : sonuç yanlış/eksikse yeniden LLM dene
     (önceki seçimi yeniden kullanmayı teklif eder).

---

## Çıktılar

Seçilen klasöre üç dosya yazılır:

- `<ad>.xlsx` — forma giriş için geniş BV tablosu + Dataset + Rapor sayfaları
- `<ad>.json` — alan bazında kaynak/uyarı bilgisiyle uzun-form veri
- `<ad>.txt` — kopyalanabilir, insan-okur rapor

Her LLM değeri `source="llm"` ve okunduğu satır metniyle kayıtlıdır. Eksik
alan **uydurulmaz**; boş bırakılıp uyarı düşülür.

---

## Maliyet

Opus 4.8: ~$5 / milyon girdi, ~$25 / milyon çıktı token.
Pratikte **tablo başına ≈ $0.03–0.10**. Token sayısı ve tahmini maliyet hem
sonuç ekranında hem kaydedilen raporda görünür. ~100 makale taraması kabaca
$5–10 bandındadır.

---

## Yapılacaklar (sonraki aşama)

Demo sonrası, özellikle `sample_data/All_PDFs` taramasıyla:

- [ ] **LLM prompt/şema optimizasyonu** (gerçek makalelerde görülen vakalar):
  - [ ] **Devrik (transpoze) tablolar** — analitler satırda değil sütunda
        (örn. 0538 Todd). Prompt "satır = analit" varsayıyor.
  - [ ] **Short-term / long-term BV** ayrımı (örn. 0538: 6 hafta vs 9 ay) —
        şu an sadece isim alanına yazılıyor; ayrı alan gerekiyor.
  - [ ] **Literatür-karşılaştırma tabloları** — başka çalışmaların CV'leri
        sonuçlara karışmasın; çalışmanın kendi verisi ayırt edilsin.
  - [ ] **II / RCV / ICC** kolonları — birçok makalede var, şemada yok.
- [ ] **Deterministik `value_ci` ve `tabular_plain` parser'ları** — yaygın
      formatlar Claude'a gitmeden ücretsiz çözülebilsin.
- [ ] **preanalyzer yanlış-sayfa düzeltmesi** — metindeki sayılar yanlış
      sayfayı öne çıkarabiliyor (örn. 0561). Şimdilik manuel seçimle aşılıyor.
- [ ] **Birim ve çalışma-düzeyi (dataset) alanları** çoğu zaman boş —
      çıkarımı iyileştir.
- [ ] **All_PDFs toplu testi** — Claude doğruluğunu ölç, regresyon/altın-set
      oluştur (`Database papers.zip`, docx/jpg ekleri de var).
- [ ] **Maliyet düşürme (opsiyonel)** — Sonnet 4.6'ya düşürme veya prompt
      caching; mevcut fiyatlarda şart değil.
- [ ] **Çıktı doğrulama (opsiyonel)** — bir LLM'in parser/LLM çıktısını PDF'e
      karşı kontrol etmesi (özellikle sınırdaki vakalarda).

> Ayrıntılı durum ve karar günlüğü için `STATUS.md`'ye bakın.
