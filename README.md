# INCIScraper

Bu proje [INCIDecoder](https://incidecoder.com) sitesinden marka, ürün ve
bileşen verilerini toplamayı amaçlayan bağımsız bir kazıyıcı içerir. Uygulama
hiçbir üçüncü parti bağımlılık gerektirmez; HTML çözümlemeleri dahili olarak
sağlanan hafif bir ayrıştırıcı ile yapılır.

## Kurulum

1. Python 3.11 veya üzeri bir sürüm kullanın.
2. Depoyu klonladıktan sonra sanal bir ortam oluşturmanız tavsiye edilir (zorunlu
   değildir).
3. Proje bağımlılık gerektirmediğinden ek bir yükleme adımı yoktur.

## Kullanım

Komut satırı arayüzü `main.py` dosyasında bulunur. Varsayılan davranış, tüm
pipeline adımlarını (markalar → ürünler → ürün detayları) sırasıyla
çalıştırmaktır. Uygulama başlarken veritabanındaki mevcut durumu özetleyen bir
“iş yükü” raporu yazar ve daha önce tamamlanmış aşamaları otomatik olarak
atlar; bu sayede kısa süreli oturumlarda (örneğin iki dakikalık terminal
limitleri) işlem yarıda kalsa bile komutu yeniden çalıştırarak kaldığınız
yerden devam edebilirsiniz.

```bash
python main.py
```

Belirli adımları çalıştırmak için `--step` parametresini kullanabilirsiniz:

```bash
# Yalnızca marka listelerini topla
python main.py --step brands

# Önceden kaydedilmiş markalar için ürün listelerini güncelle
python main.py --step products

# Ürün detaylarını (açıklama, içerikler, görseller vb.) güncelle
python main.py --step details
```

Diğer yararlı parametreler:

- `--db`: Kullanılacak SQLite veritabanının yolu (varsayılan: `incidecoder.db`).
- `--images-dir`: İndirilen ürün görsellerinin kaydedileceği dizin (varsayılan: `images`).
- `--base-url`: Gerekirse INCIDecoder için alternatif bir kök URL tanımlar
  (örneğin yerel testler için).
- `--max-pages`: Marka listesi toplama adımında indirilecek sayfa sayısını sınırlar.
- `--resume/--no-resume`: Tüm pipeline'ı çalıştırırken tamamlanmış adımları
  atlayıp atlamama davranışını belirler (varsayılan: `--resume`).
- `--log-level`: Günlük seviyesini değiştirir (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`).

## Veritabanı Şeması

Scraper aşağıdaki tabloları oluşturur:

- **brands** – Marka isimleri ve URL bilgileri.
- **products** – Ürün isimleri, marka ilişkisi, açıklamalar, görsel yolu ve
  ürün seviyesindeki özet bilgiler.
- **ingredients** – Her bir bileşenin detay sayfasından toplanan veriler.
- **product_ingredients** – Ürünler ile bileşenler arasındaki çoktan çoğa ilişki
  ve varsa kısa açıklama/tooltip içerikleri.

Tüm tablolar `UNIQUE` kısıtları ve durum bayrakları (`products_scraped`,
`details_scraped`) sayesinde tekrar çalıştırmalara dayanıklıdır. Scraper her
başlatıldığında veritabanı şemasını doğrular; beklenmeyen tablo veya sütunlar
tespit edilirse otomatik olarak kaldırılır.

## Notlar

- Uygulama ağ trafiği sırasında nazik olmak için her HTTP isteği arasında kısa
  bir gecikme ekler.
- Çalışma ortamınızda dış ağ erişimi yoksa scraper gerçek verileri toplayamaz;
  bu durumda kodun incelenmesi veya sahte HTML ile test edilmesi gerekir.
- INCIDecoder sayfalarındaki içerik yapısı değişirse ilgili ayrıştırma
  fonksiyonlarının güncellenmesi gerekebilir.
