# attempt123

Basit, bağımlılıksız ve üretim kalitesinde bir **retry / attempt** yardımcı kütüphanesi.

Geçici hataları (ağ, rate-limit, geçici servis kesintileri vb.) otomatik olarak yeniden denemek için tasarlandı. Hem senkron hem de **async** kodu destekler.

## Özellikler

- **Exponential backoff** + jitter (thundering herd’ü önler)
- Maksimum deneme sayısı ve toplam zaman aşımı kontrolü
- Belirli exception türlerini yakalama / yok sayma
- Hem decorator hem de fonksiyon arayüzü
- **Async desteği** (`async_attempt` / `@async_retry`) — sadece standart kütüphane
- **RetryError**: son exception + deneme sayısını birlikte taşıyan sarmalayıcı
- Tamamen standart kütüphane — ekstra bağımlılık yok
- Tip ipuçları ve kapsamlı docstring’ler

## Kurulum

```bash
pip install -e .
# veya sadece dosyayı kopyalayın
```

## Hızlı Kullanım

### Decorator olarak (sync)

```python
from attempt import retry

@retry(max_attempts=5, base_delay=0.5, max_delay=10.0, exceptions=(ConnectionError, TimeoutError))
def fetch_data(url: str) -> dict:
    # geçici hata verebilecek kod
    ...
```

### Fonksiyon olarak (sync)

```python
from attempt import attempt

result = attempt(
    lambda: requests.get("https://api.example.com/data").json(),
    max_attempts=4,
    base_delay=1.0,
    exceptions=(requests.RequestException,),
)
```

### Async kullanım

```python
from attempt import async_retry, async_attempt

@async_retry(max_attempts=5, base_delay=0.5, exceptions=(ConnectionError,))
async def fetch_async(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

# veya doğrudan:
result = await async_attempt(
    lambda: some_async_call(),
    max_attempts=3,
    base_delay=1.0,
)
```

### RetryError ile daha anlamlı hata mesajı

```python
from attempt import attempt, RetryError

try:
    attempt(fragile_fn, max_attempts=3, reraise_as_retry_error=True)
except RetryError as e:
    print(e.attempts)          # 3
    print(e.last_exception)    # orijinal exception
```

## API Özeti

| Parametre                 | Varsayılan     | Açıklama                                      |
|---------------------------|----------------|-----------------------------------------------|
| `max_attempts`            | `3`            | Maksimum deneme sayısı                        |
| `base_delay`              | `1.0`          | İlk bekleme süresi (saniye)                   |
| `max_delay`               | `60.0`         | Üst sınır bekleme süresi                      |
| `exponential_base`        | `2.0`          | Üstel çarpan                                  |
| `jitter`                  | `True`         | Rastgele ±%25 jitter ekle                     |
| `exceptions`              | `(Exception,)` | Yakalanacak exception sınıfları               |
| `on_retry`                | `None`         | Her yeniden denemede çağrılacak callback      |
| `timeout`                 | `None`         | Toplam maksimum çalışma süresi (saniye)       |
| `reraise_as_retry_error`  | `False`        | Başarısızlıkta `RetryError` yükselt           |

**Sync API:** `attempt()`, `@retry`  
**Async API:** `async_attempt()`, `@async_retry`  
**Hata sınıfı:** `RetryError` (`last_exception`, `attempts`)

## Testler

```bash
pip install -e ".[test]"
python -m pytest tests/ -v
```

Async testler için `pytest-asyncio` önerilir (otomatik algılanır).

## Lisans

MIT
