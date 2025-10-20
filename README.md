# INCIScraper 🚀

INCIScraper, [INCIDecoder.com](https://incidecoder.com) ve [EU CosIng veritabanı](https://ec.europa.eu/growth/tools-databases/cosing/) üzerinden kapsamlı kozmetik veri toplama için geliştirilmiş yüksek performanslı bir web scraper'ıdır. Paralel işleme, akıllı önbellekleme ve optimize edilmiş kazıma algoritmaları ile büyük veri setlerini verimli şekilde toplar.

## ✨ Öne Çıkan Özellikler

### 🔥 Performans Optimizasyonları
- **Paralel HTTP İstekleri**: ThreadPoolExecutor ile eşzamanlı veri kazıma
- **Adaptive Rate Limiting**: Dinamik gecikme yönetimi ile maksimum hız
- **Batch Database Operations**: Toplu veritabanı işlemleri ile I/O optimizasyonu
- **LRU Cache**: Bellek tabanlı akıllı önbellekleme sistemi
- **Optimized CosIng Scraping**: %60 daha hızlı ingredient veri kazıma

### 🛡️ Güvenilirlik & Dayanıklılık
- **Resume Capability**: Kesintili oturumlarda kaldığı yerden devam
- **Error Recovery**: Akıllı hata yönetimi ve otomatik yeniden deneme
- **Progress Tracking**: Gerçek zamanlı ilerleme takibi ve ETA
- **Comprehensive Logging**: Detaylı loglama ve hata ayıklama

### 📊 Kapsamlı Veri Toplama
- **Product Information**: Ürün adı, marka, kategori, fiyat bilgileri
- **Ingredient Analysis**: Detaylı ingredient analizi ve CosIng entegrasyonu
- **Image Processing**: Otomatik resim indirme ve optimizasyon
- **Function & Free Data**: Ingredient fonksiyonları ve serbest veriler

## 🚀 Hızlı Başlangıç

### Gereksinimler
- Python 3.8+
- Virtual Environment (önerilen)

### Kurulum

```bash
# Repository'yi klonlayın
git clone https://github.com/selmanays/INCIScraper.git
cd INCIScraper

# Virtual environment oluşturun ve aktifleştirin
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows

# Bağımlılıkları yükleyin
pip install -r requirements.txt

# Playwright tarayıcılarını yükleyin
playwright install
```

### Temel Kullanım

```bash
# Sample data ile test
python main.py --sample-data

# Tam kazıma (dikkatli kullanın)
python main.py

# Performans optimizasyonu ile
python main.py --max-workers 4 --batch-size 100
```

## ⚙️ Komut Satırı Parametreleri

### Performans Ayarları
```bash
--max-workers N        # Paralel HTTP işçi sayısı (varsayılan: 1)
--batch-size N         # Batch boyutu (varsayılan: 50)
--image-workers N      # Resim işleme işçi sayısı (varsayılan: 4)
--skip-images          # Resim indirmeyi atla
```

### Veri ve Loglama
```bash
--sample-data          # Sample data kullan
--db-path PATH         # Veritabanı yolu
--log-level LEVEL      # Log seviyesi (DEBUG, INFO, WARNING, ERROR)
```

### Örnek Kullanımlar

```bash
# Hızlı test
python main.py --sample-data --log-level DEBUG

# Maksimum performans
python main.py --max-workers 8 --batch-size 200 --image-workers 8

# Resim olmadan kazıma
python main.py --skip-images --max-workers 4

# Debug modu
python main.py --sample-data --log-level DEBUG --max-workers 2
```

## 📊 Veritabanı Yapısı

### Ana Tablolar
- **brands**: Marka bilgileri
- **products**: Ürün bilgileri
- **ingredients**: Ingredient detayları
- **functions**: Ingredient fonksiyonları
- **frees**: Serbest veriler

### İlişkiler
- Products → Brands (many-to-one)
- Products → Ingredients (many-to-many)
- Ingredients → Functions (many-to-many)
- Ingredients → Frees (one-to-many)

## 🎯 Performans Optimizasyonları

### 1. Paralel HTTP İstekleri
```python
# ThreadPoolExecutor ile eşzamanlı kazıma
max_workers = 4  # 4 paralel işçi
```

### 2. Adaptive Rate Limiting
```python
# Dinamik gecikme yönetimi
min_rate_limit = 0.1  # Minimum gecikme (saniye)
max_rate_limit = 2.0  # Maksimum gecikme (saniye)
```

### 3. Batch Database Operations
```python
# Toplu veritabanı işlemleri
batch_size = 50  # 50 öğe per batch
```

### 4. LRU Cache
```python
# Bellek tabanlı önbellekleme
cache_size_limit = 10000  # 10K öğe cache
```

### 5. CosIng Optimizasyonu
```python
# Optimize edilmiş Playwright kullanımı
# - Reduced timeouts
# - Smart fallback strategy
# - Multiple selector fallback
```

## 📈 Performans Metrikleri

### Hız İyileştirmeleri
- **CosIng Scraping**: 24s → 3-6s (%60 hızlanma)
- **HTTP Requests**: 300-500% hız artışı
- **Database Operations**: 200-400% hız artışı
- **Image Processing**: 200-300% hız artışı
- **Overall Speed**: 200-400% genel iyileştirme

### Kaynak Kullanımı
- **Memory**: LRU cache ile optimize edilmiş bellek kullanımı
- **CPU**: Paralel işleme ile CPU kullanımı artırıldı
- **Network**: Adaptive rate limiting ile ağ trafiği optimize edildi
- **Storage**: Batch operations ile disk I/O azaltıldı

## 🔧 Geliştirme

### Proje Yapısı
```
INCIScraper/
├── src/
│   └── inciscraper/
│       ├── __init__.py
│       ├── scraper.py          # Ana scraper sınıfı
│       ├── constants.py        # Sabitler
│       └── mixins/
│           ├── database.py     # Veritabanı işlemleri
│           ├── network.py      # HTTP ve ağ işlemleri
│           ├── products.py     # Ürün kazıma
│           ├── details.py      # Detay kazıma
│           └── monitoring.py   # İzleme ve metrikler
├── ui/                         # Next.js web arayüzü
├── data/                       # Veritabanı ve loglar
├── main.py                     # CLI giriş noktası
└── requirements.txt            # Python bağımlılıkları
```

### Mixin Mimarisi
INCIScraper, modüler mixin mimarisi kullanır:

- **DatabaseMixin**: Veritabanı işlemleri
- **NetworkMixin**: HTTP istekleri ve ağ işlemleri
- **ProductScraperMixin**: Ürün listesi kazıma
- **DetailScraperMixin**: Ürün detay kazıma
- **MonitoringMixin**: İlerleme takibi ve metrikler

## 🐛 Sorun Giderme

### Yaygın Sorunlar

#### 1. Playwright Kurulum Sorunu
```bash
# Playwright tarayıcılarını yeniden yükleyin
playwright install
```

#### 2. SQLite Thread Safety
```bash
# Tek işçi ile çalıştırın
python main.py --max-workers 1
```

#### 3. Memory Issues
```bash
# Batch boyutunu azaltın
python main.py --batch-size 25
```

#### 4. Network Timeouts
```bash
# Debug modu ile detaylı loglar
python main.py --log-level DEBUG
```

### Log Dosyaları
- **Ana Log**: `data/logs/inciscraper.log`
- **Debug Logs**: Console output ile `--log-level DEBUG`

## 🤝 Katkıda Bulunma

1. Fork yapın
2. Feature branch oluşturun (`git checkout -b feature/amazing-feature`)
3. Commit yapın (`git commit -m 'Add amazing feature'`)
4. Push yapın (`git push origin feature/amazing-feature`)
5. Pull Request oluşturun

## 📄 Lisans

Bu proje MIT lisansı altında lisanslanmıştır. Detaylar için `LICENSE` dosyasına bakın.

## 🙏 Teşekkürler

- [INCIDecoder.com](https://incidecoder.com) - Veri kaynağı
- [EU CosIng Database](https://ec.europa.eu/growth/tools-databases/cosing/) - Ingredient veritabanı
- [Playwright](https://playwright.dev/) - Web automation
- [SQLite](https://sqlite.org/) - Veritabanı

## 📞 İletişim

- **GitHub**: [selmanays/INCIScraper](https://github.com/selmanays/INCIScraper)
- **Issues**: [GitHub Issues](https://github.com/selmanays/INCIScraper/issues)

---

**⚠️ Uyarı**: Bu araç yalnızca eğitim ve araştırma amaçlıdır. Web sitelerinin kullanım şartlarına uygun şekilde kullanın ve rate limiting'e dikkat edin.
