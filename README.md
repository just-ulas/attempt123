# attempt123

Basit, bağımlılıksız ve üretim kalitesinde bir **retry / attempt** yardımcı kütüphanesi.

Geçici hataları (ağ, rate-limit, geçici servis kesintileri vb.) otomatik olarak yeniden denemek için tasarlandı. Hem senkron hem de **async** kodu destekler.

## Özellikler

- **Exponential backoff** + jitter (equal ±%25 veya **AWS full jitter**)
- Maksimum deneme sayısı ve toplam zaman aşımı kontrolü
- Belirli exception türlerini yakalama / yok sayma
- **`retry_if` predicate**: exception içeriğine göre yeniden deneme kararı
- **`retry_if_result` predicate**: başarılı dönüş değerine göre yeniden deneme
- **`retry_after`**: sunucunun önerdiği bekleme süresini onurlandırır (`Retry-After` / `retry_after`)
- **HTTP-date `Retry-After`**: RFC 7231 tarih değerleri saniyeye çevrilir
- **Sonuç nesnelerinden Retry-After**: exception yükseltmeyen 429/503 Response'lar da sunucu beklemesini kullanır
- Hazır predicate'ler: `retry_if_status`, `retry_if_result_status`, `retry_if_message`, `retry_if_empty`, `retry_if_falsy`
- Predicate birleştirme: **`any_of` / `all_of` / `not_`**
- **Circuit breaker**: ardışık hatalarda çağrıları keser; half-open'da **eşzamanlı probe limiti** (`max_half_open`) ile recovery'yi izole eder; thread-safe
- **Token-bucket rate limiter**: paylaşılan kovadan token alır; retry storm / thundering herd'u keser
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

### Circuit breaker

Aynı bağımlılığa giden çağrıları paylaşan bir breaker, eşik aşılınca yeni istekleri
hemen keser (`CircuitOpenError`). `recovery_timeout` sonra half-open probe yapılır;
aynı anda en fazla `max_half_open` (varsayılan 1) çağrı probe eder, kalanlar
servisi tekrar ezmez.

```python
from attempt import CircuitBreaker, CircuitOpenError, attempt, extract_retry_after

payments = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=20.0,
    success_threshold=2,
    max_half_open=1,
    name="payments",
)

try:
    result = attempt(
        lambda: charge(order),
        max_attempts=4,
        base_delay=0.2,
        jitter="full",
        retry_after=extract_retry_after,
        circuit=payments,
    )
except CircuitOpenError as exc:
    fallback()
```

Aynı `CircuitBreaker` örneğini birden fazla `attempt` / decorator / thread arasında paylaşın.
`breaker.reset()` ile manuel kapatabilirsiniz.

### Rate limiter

Paylaşılan token-bucket, saniyedeki çağrı sayısını sınırlar. Retry döngüsü her denemeden
önce bir token alır; kova boşsa timeout bütçesi içinde bekler, yetmezse `RateLimitError`.

```python
from attempt import RateLimiter, RateLimitError, attempt

http = RateLimiter(rate=8, burst=16, name="upstream")

try:
    result = attempt(
        lambda: client.get(url),
        max_attempts=4,
        base_delay=0.1,
        limiter=http,
        timeout=5.0,
    )
except RateLimitError as exc:
    # exc.retry_after → sonraki token'a kalan saniye
    queue_for_later()
```

Limiter circuit breaker ile birlikte kullanılabilir: önce açık devre kontrolü, sonra token.
`limiter.try_acquire()` / `acquire_async()` retry dışında da çağrılabilir.

## Testler

```bash
pip install -e ".[test]"
python -m pytest tests/ -v
```

## Lisans

MIT
