# attempt123

Basit, bağımlılıksız ve üretim kalitesinde bir **retry / attempt** yardımcı kütüphanesi.

Geçici hataları (ağ, rate-limit, geçici servis kesintileri vb.) otomatik olarak yeniden denemek için tasarlandı. Hem senkron hem de **async** kodu destekler.

## Özellikler

- **Exponential backoff** + jitter (equal ±%25 veya **AWS full jitter**)
- Maksimum deneme sayısı ve toplam zaman aşımı kontrolü
- **`attempt_timeout`**: her denemeyi ayrı sınırlar; asılı I/O tüm bütçeyi yemez (async'te görev iptal edilir)
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
- **Retry budget**: kayar pencerede retry oranını sınırlar; bozuk bir bağımlılık tüm kapasiteyi retry ile yemesin
- **Bulkhead**: eşzamanlı uçuştaki çağrı sayısını sınırlar; yavaş bir bağımlılık thread/task havuzunu kilitlemesin
- **`fallback`**: denemeler, bütçe veya **toplam timeout** bitince hata fırlatmak yerine yedek yol çalıştırır (`GiveUpContext`)
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

### Deneme başına timeout

`timeout` tüm retry döngüsünün üst sınırıdır. `attempt_timeout` *tek bir çağrıyı* sınırlar.
Asılı bir socket, `max_attempts` dolmadan önce tüm süreyi yiyebilir; deneme sınırı
`TimeoutError` üretir, history / circuit / budget'a kaydeder ve (ayarlandıysa)
yeniden dener veya fallback'e düşer.

Async yolda `asyncio.wait_for` görevi iptal eder. Sync yolda bekleyen işçi daemon
thread olarak bırakılır — Python kullanıcı kodundan thread öldüremez; döngü yine de ilerler.

```python
from attempt import attempt

quote = attempt(
    lambda: market.ticker(symbol),
    max_attempts=4,
    base_delay=0.1,
    jitter="full",
    attempt_timeout=1.5,
    timeout=5.0,
    fallback=lambda: cache.get("last_good_quote"),
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

### Retry budget

Limiter *her* denemeyi yavaşlatır. Budget yalnızca yeniden denemeleri keser: penceredeki
isteklerin en fazla `retry_ratio` kadarı retry olabilir; düşük trafikte `min_retries`
tabanı korunur. Bütçe bitince son hata hemen yükseltilir — ekstra sleep / ekstra yük yok.

```python
from attempt import RetryBudget, attempt

payments = RetryBudget(
    window=10.0,
    retry_ratio=0.2,
    min_retries=10,
    name="payments",
)

result = attempt(
    lambda: charge(order),
    max_attempts=5,
    base_delay=0.2,
    jitter="full",
    budget=payments,
)
```

Aynı `RetryBudget` örneğini tüm çağrı noktalarında paylaşın. Outage sırasında 1000
istekten yalnızca ~200'si retry eder; kalanı fail-fast olur.

### Bulkhead

Limiter *zaman içindeki hızı* keser. Bulkhead *aynı anda kaç çağrının uçuşta*
olabileceğini keser. Yavaş bir upstream + retry, worker havuzunu kolayca kilitler;
paylaşılan bir `Bulkhead` o sızıntıyı izole eder.

Slot yalnızca asıl çağrı süresince tutulur — backoff uykusu kapasite yemez.
Slot yoksa timeout bütçesi içinde beklenir, yetmezse `BulkheadFullError`.

```python
from attempt import Bulkhead, BulkheadFullError, attempt

payments = Bulkhead(max_concurrent=8, name="payments")

try:
    result = attempt(
        lambda: charge(order),
        max_attempts=4,
        base_delay=0.2,
        jitter="full",
        bulkhead=payments,
        timeout=2.0,
    )
except BulkheadFullError as exc:
    # exc.bulkhead.inflight / .available
    shed_load()
```

Circuit → limiter → bulkhead sırasıyla kontrol edilir. Aynı örneği tüm çağrı
noktalarında paylaşın. Retry dışında `with Bulkhead(...)` veya
`try_acquire()` / `acquire_async()` da kullanılabilir.

### Fallback

Denemeler tükenince, `retry_if` hayır deyince, retry bütçesi bitince veya **toplam
timeout** dolunca varsayılan davranış hata yükseltmektir. `fallback` verildiğinde
bunun yerine yedek yol çalışır: önbellek, varsayılan payload, alternatif servis.

`fallback` ya argümansız çağrılır ya da bir `GiveUpContext` alır (`exception`,
`result`, `history`, `attempts`). Async yolda fallback kendisi de `async` olabilir.

```python
from attempt import GiveUpContext, attempt

def stale_quote(ctx: GiveUpContext):
    # ctx.exception / ctx.result / ctx.history kullanılabilir
    return cache.get("last_good_quote") or {"price": None, "stale": True}

quote = attempt(
    lambda: market.ticker(symbol),
    max_attempts=4,
    base_delay=0.15,
    jitter="full",
    fallback=stale_quote,
)
```

Decorator:

```python
from attempt import retry

@retry(max_attempts=3, base_delay=0.2, fallback=lambda: {"ok": False})
def ping():
    ...
```

Başarılı bir deneme fallback'i çağırmaz. Circuit / rate-limit / bulkhead hataları
hâlâ kendi exception'larını yükseltir; fallback asıl iş fonksiyonunun tükettiği
denemeler ve genel deadline içindir.

## Testler

```bash
pip install -e ".[test]"
python -m pytest tests/ -v
```

## Lisans

MIT
