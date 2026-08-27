# attempt123

Basit, bağımlılıksız ve üretim kalitesinde bir **retry / attempt** yardımcı kütüphanesi.

Geçici hataları (ağ, rate-limit, geçici servis kesintileri vb.) otomatik olarak yeniden denemek için tasarlandı. Hem senkron hem de **async** kodu destekler.

## Özellikler

- **Exponential backoff** + jitter (equal ±%25 veya **AWS full jitter**)
- Maksimum deneme sayısı ve toplam zaman aşımı kontrolü
- Belirli exception türlerini yakalama / yok sayma
- **`retry_if` predicate**: exception içeriğine göre yeniden deneme kararı
- **`retry_if_result` predicate**: başarılı dönüş değerine göre yeniden deneme
- **`retry_after`**: sunucunun önerdiği bekleme süresini onurlandırır (`Retry-After` / `exc.retry_after`)
- Hazır predicate'ler: `retry_if_status`, `retry_if_message`, `retry_if_empty`, `retry_if_falsy`
- **`RetryError.history`**: her denemenin exception/sonuç/gecikme kaydı
- Hem decorator hem de fonksiyon arayüzü
- **Async desteği** (`async_attempt` / `@async_retry`) — sadece standart kütüphane
- Tamamen standart kütüphane — ekstra bağımlılık yok

## Kurulum

```bash
pip install -e .
```

## Hızlı Kullanım

### Decorator / fonksiyon

```python
from attempt import retry, attempt

@retry(max_attempts=5, base_delay=0.5, exceptions=(ConnectionError, TimeoutError))
def fetch_data(url: str) -> dict:
    ...

result = attempt(
    lambda: do_call(),
    max_attempts=4,
    base_delay=1.0,
)
```

### Hazır predicate'ler

```python
from attempt import attempt, retry_if_status, retry_if_message, retry_if_empty

result = attempt(
    lambda: call_api(),
    max_attempts=5,
    retry_if=retry_if_status(429, 502, 503, 504),
)

items = attempt(
    lambda: fetch_items(),
    retry_if_result=retry_if_empty,
)
```

### HTTP Retry-After

```python
from attempt import attempt, extract_retry_after, retry_if_status

result = attempt(
    lambda: call_rate_limited_api(),
    max_attempts=6,
    base_delay=0.5,
    max_delay=30.0,
    retry_if=retry_if_status(429, 503),
    retry_after=extract_retry_after,
)
```

`extract_retry_after` sırasıyla `exc.retry_after` ve `exc.headers["Retry-After"]` değerlerine bakar. Parse edilemezse normal exponential backoff kullanılır. Önerilen süre `max_delay` ile sınırlanır.

### RetryError geçmişi

```python
from attempt import attempt, RetryError

try:
    attempt(fragile_fn, max_attempts=3, reraise_as_retry_error=True)
except RetryError as e:
    print(e.attempts, e.last_exception, e.last_result)
    for step in e.history:
        print(step.number, step.exception, step.result, step.delay)
```

### Async

```python
from attempt import async_retry, extract_retry_after

@async_retry(max_attempts=5, jitter="full", retry_after=extract_retry_after)
async def fetch_async(url: str) -> dict:
    ...
```

## API Özeti

| Parametre | Varsayılan | Açıklama |
|-----------|------------|----------|
| `max_attempts` | `3` | Maksimum deneme sayısı |
| `base_delay` | `1.0` | İlk bekleme (saniye) |
| `max_delay` | `60.0` | Üst sınır bekleme |
| `exponential_base` | `2.0` | Üstel çarpan |
| `jitter` | `True` | `True`/`"equal"`, `"full"`, `False` |
| `exceptions` | `(Exception,)` | Yakalanacak sınıflar |
| `retry_if` | `None` | `exc -> bool` |
| `retry_if_result` | `None` | `result -> bool` (True ise reddet) |
| `retry_after` | `None` | `exc -> float|None` önerilen bekleme |
| `on_retry` | `None` | Callback |
| `timeout` | `None` | Toplam süre sınırı |
| `reraise_as_retry_error` | `False` | `RetryError` yükselt |

**Yardımcılar:** `extract_retry_after`, `retry_if_status`, `retry_if_message`, `retry_if_empty`, `retry_if_falsy`

## Testler

```bash
pip install -e ".[test]"
python -m pytest tests/ -v
```

## Lisans

MIT
