# attempt123

Basit, bağımlılıksız ve üretim kalitesinde bir **retry / attempt** yardımcı kütüphanesi.

Geçici hataları (ağ, rate-limit, geçici servis kesintileri vb.) otomatik olarak yeniden denemek için tasarlandı.

## Özellikler

- **Exponential backoff** + jitter (thundering herd’ü önler)
- Maksimum deneme sayısı ve toplam zaman aşımı kontrolü
- Belirli exception türlerini yakalama / yok sayma
- Hem decorator hem de fonksiyon arayüzü
- Tamamen standart kütüphane — ekstra bağımlılık yok
- Tip ipuçları ve kapsamlı docstring’ler

## Kurulum

```bash
pip install -e .
# veya sadece dosyayı kopyalayın
```

## Hızlı Kullanım

### Decorator olarak

```python
from attempt import retry

@retry(max_attempts=5, base_delay=0.5, max_delay=10.0, exceptions=(ConnectionError, TimeoutError))
def fetch_data(url: str) -> dict:
    # geçici hata verebilecek kod
    ...
```

### Fonksiyon olarak

```python
from attempt import attempt

result = attempt(
    lambda: requests.get("https://api.example.com/data").json(),
    max_attempts=4,
    base_delay=1.0,
    exceptions=(requests.RequestException,),
)
```

## API Özeti

| Parametre          | Varsayılan     | Açıklama                                      |
|--------------------|----------------|-----------------------------------------------|
| `max_attempts`     | `3`            | Maksimum deneme sayısı                        |
| `base_delay`       | `1.0`          | İlk bekleme süresi (saniye)                   |
| `max_delay`        | `60.0`         | Üst sınır bekleme süresi                      |
| `exponential_base` | `2.0`          | Üstel çarpan                                  |
| `jitter`           | `True`         | Rastgele ±%25 jitter ekle                     |
| `exceptions`       | `(Exception,)` | Yakalanacak exception sınıfları               |
| `on_retry`         | `None`         | Her yeniden denemede çağrılacak callback      |

## Testler

```bash
python -m pytest tests/ -v
```

## Lisans

MIT
