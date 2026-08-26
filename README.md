# attempt123

Basit, bağımlılıksız ve üretim kalitesinde bir **retry / attempt** yardımcı kütüphanesi.

Geçici hataları (ağ, rate-limit, geçici servis kesintileri vb.) otomatik olarak yeniden denemek için tasarlandı. Hem senkron hem de **async** kodu destekler.

## Özellikler

- **Exponential backoff** + jitter (thundering herd’ü önler)
- Maksimum deneme sayısı ve toplam zaman aşımı kontrolü
- Belirli exception türlerini yakalama / yok sayma
- **`retry_if` predicate**: exception içeriğine göre yeniden deneme kararı (ör. sadece HTTP 429/503)
- **`retry_if_result` predicate**: başarılı dönüş değerine göre yeniden deneme (ör. boş liste, gövdede hata alanı)
- Hem decorator hem de fonksiyon arayüzü
- **Async desteği** (`async_attempt` / `@async_retry`) — sadece standart kütüphane
- **RetryError**: son exception / reddedilen sonuç + deneme sayısını birlikte taşıyan sarmalayıcı
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

### İnce taneli kontrol: `retry_if`

Sadece belirli durum kodlarında yeniden denemek için:

```python
from attempt import attempt

def should_retry(exc: BaseException) -> bool:
    # Örnek: HTTP hata nesnesinde status_code varsa sadece 429/503’te dene
    code = getattr(exc, "status_code", None)
    return code in (429, 503)

result = attempt(
    lambda: call_api(),
    max_attempts=5,
    base_delay=1.0,
    exceptions=(Exception,),
    retry_if=should_retry,
)
```

Decorator ile:

```python
@retry(
    max_attempts=5,
    base_delay=0.5,
    retry_if=lambda e: "rate limit" in str(e).lower(),
)
def fetch():
    ...
```

### Sonuç tabanlı yeniden deneme: `retry_if_result`

Exception fırlatmayan ama “başarısız” sayılan dönüş değerlerinde (boş liste, hata alanı olan JSON vb.) yeniden denemek için:

```python
from attempt import attempt

# Boş liste gelirse tekrar dene
items = attempt(
    lambda: fetch_items_from_api(),
    max_attempts=4,
    base_delay=0.5,
    retry_if_result=lambda r: r is None or len(r) == 0,
)

# API 200 dönse bile gövdede error varsa tekrar dene
payload = attempt(
    lambda: client.get("/resource").json(),
    max_attempts=5,
    retry_if_result=lambda body: body.get("error") is not None,
)
```

Decorator ile:

```python
@retry(retry_if_result=lambda r: not r.get("ok", False), max_attempts=3)
def call_service() -> dict:
    ...
```

Denemeler tükendiğinde `RetryError` ( `reraise_as_retry_error=True` ise) veya açıklayıcı bir `RuntimeError` yükseltilir; `RetryError.last_result` reddedilen son değeri taşır.

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

# async + sonuç predicate
result = await async_attempt(
    lambda: fetch_async_list(),
    retry_if_result=lambda r: len(r) == 0,
)
```

### RetryError ile daha anlamlı hata mesajı

```python
from attempt import attempt, RetryError

try:
    attempt(fragile_fn, max_attempts=3, reraise_as_retry_error=True)
except RetryError as e:
    print(e.attempts)          # 3
    print(e.last_exception)    # orijinal exception (veya None)
    print(e.last_result)       # reddedilen sonuç (varsa)
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
| `retry_if`                | `None`         | `exc -> bool`; False ise hemen yükselt        |
| `retry_if_result`         | `None`         | `result -> bool`; True ise sonucu reddet ve yeniden dene |
| `on_retry`                | `None`         | Her yeniden denemede çağrılacak callback      |
| `timeout`                 | `None`         | Toplam maksimum çalışma süresi (saniye)       |
| `reraise_as_retry_error`  | `False`        | Başarısızlıkta `RetryError` yükselt           |

**Sync API:** `attempt()`, `@retry`  
**Async API:** `async_attempt()`, `@async_retry`  
**Hata sınıfı:** `RetryError` (`last_exception`, `attempts`, `last_result`)

## Testler

```bash
pip install -e ".[test]"
python -m pytest tests/ -v
```

Async testler için `pytest-asyncio` önerilir (otomatik algılanır).

## Lisans

MIT
