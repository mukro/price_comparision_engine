# docs/MOBILE_APP_INTEGRATION.md
# Price Comparison Engine — Mobile App On-Device OCR Integration Guide
# Last Updated: 2026-07-31

---

## 📱 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER'S MOBILE DEVICE                      │
│                                                                  │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │ Screenshot  │───▶│ On-Device  │───▶│ Structured JSON     │  │
│  │ (captured)  │    │ OCR Engine  │    │ {price, product,   │  │
│  │             │    │ (ML Kit /   │    │  vendor, geo_hash}  │  │
│  │ NEVER       │    │  Tesseract /│    │                     │  │
│  │ leaves      │    │  CoreML)    │    │ NO raw image        │  │
│  │ device!     │    │             │    │ NO GPS coordinates  │  │
│  └─────────────┘    └─────────────┘    └─────────────────────┘  │
│                                                  │               │
└──────────────────────────────────────────────────┼───────────────┘
                                                   │ HTTPS POST
                                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PRICE COMPARISON BACKEND                      │
│                                                                  │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────┐ │
│  │ OCR Submission  │───▶│ Verification    │───▶│ Community  │ │
│  │ API             │    │ Score Engine    │    │ Validation │ │
│  │ (/submissions)  │    │ (auto-approve   │    │ (voting)   │ │
│  │                 │    │  if >85 score)  │    │            │ │
│  └─────────────────┘    └─────────────────┘    └─────────────┘ │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Key Privacy Principle:** The screenshot is processed entirely on the user's device. Only extracted text/numbers are transmitted to the server.

---

## 🔐 Privacy Checklist for Mobile App

| Data | On Device | Sent to Server | Why |
|------|-----------|----------------|-----|
| Raw screenshot | ✅ Processed locally | ❌ NEVER | Privacy |
| Price text | ✅ Extracted via OCR | ✅ Yes | Core data |
| Product name | ✅ Extracted via OCR | ✅ Yes | Matching |
| Vendor domain | ✅ Parsed from URL | ✅ Yes | Attribution |
| GPS coordinates | ❌ Not collected | ❌ NEVER | Privacy |
| Geohash (approx) | ✅ Derived from GPS | ✅ Optional | Local relevance |
| Device ID | ✅ Hashed (SHA-256) | ✅ Hash only | Reputation |
| User name/email | ❌ Not needed | ❌ NEVER | Privacy |
| Screenshot pixels | ❌ Hashed for dedup | ✅ Hash only | Deduplication |

---

## 🛠️ OCR Engine Options

### Option 1: Google ML Kit (Recommended for Android/iOS)

```kotlin
// Android (Kotlin)
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions

class OcrProcessor {
    private val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)

    fun processScreenshot(bitmap: Bitmap, callback: (OcrResult) -> Unit) {
        val image = InputImage.fromBitmap(bitmap, 0)

        recognizer.process(image)
            .addOnSuccessListener { visionText ->
                val result = parsePriceFromText(visionText.text)
                callback(result)
                // bitmap is NOT uploaded — only result JSON
            }
            .addOnFailureListener { e ->
                callback(OcrResult(error = e.message))
            }
    }

    private fun parsePriceFromText(rawText: String): OcrResult {
        // Regex to extract price, product name, stock status
        val priceRegex = Regex("""[₹$€]\s?([\d,]+(?:\.\d{2})?)""")
        val priceMatch = priceRegex.find(rawText)

        return OcrResult(
            price = priceMatch?.groupValues?.get(1)?.replace(",", "")?.toDoubleOrNull(),
            currency = "INR", // Detect from symbol
            productName = extractProductName(rawText),
            inStock = !rawText.contains("out of stock", ignoreCase = true)
        )
    }
}
```

```swift
// iOS (Swift)
import MLKitTextRecognition
import MLKitVision

class OcrProcessor {
    private let recognizer = TextRecognizer.textRecognizer()

    func processScreenshot(_ image: UIImage, completion: @escaping (OcrResult) -> Void) {
        let visionImage = VisionImage(image: image)
        visionImage.orientation = image.imageOrientation

        recognizer.process(visionImage) { result, error in
            guard let result = result else {
                completion(OcrResult(error: error?.localizedDescription))
                return
            }
            let parsed = self.parsePrice(from: result.text)
            completion(parsed)
            // image is NOT uploaded
        }
    }
}
```

### Option 2: Tesseract (Cross-platform, offline)

```dart
// Flutter example
import 'package:tesseract_ocr/tesseract_ocr.dart';

Future<OcrResult> processScreenshot(String imagePath) async {
  final text = await TesseractOcr.extractText(imagePath);
  return parsePriceFromText(text);
}
```

### Option 3: On-Device LLM (Future-proof)

```python
# Using a small on-device model (e.g., Gemma 2B quantized)
# Extract structured data from screenshot text

prompt = """
Extract from this product screenshot text:
- product_name
- price (numeric only)
- currency (INR/USD/EUR)
- brand (if visible)
- in_stock (true/false)
- mrp (if shown)

Text: {raw_ocr_text}

Return JSON only.
"""
```

---

## 📤 Submission API Specification

### Endpoint

```
POST /api/v1/submissions/ocr
Content-Type: application/json
```

### Request Body

```json
{
  "price": 1299.00,
  "currency": "INR",
  "product_name": "iPhone 15 128GB Blue",
  "brand": "Apple",
  "vendor_domain": "amazon.in",
  "vendor_app_name": "Amazon Shopping",
  "in_stock": true,
  "stock_text": "In stock",
  "offer_url": "https://www.amazon.in/dp/B0CHX1W1XY",
  "mrp_price": 1499.00,
  "discount_percent": 13.3,
  "ocr_confidence": 0.94,
  "ocr_engine": "mlkit",
  "geo_hash": "tdr1v9",
  "device_hash": "a3f5c8...e9d2",  // SHA-256 of device_id
  "device_os": "Android",
  "app_version": "2.1.0",
  "screenshot_hash": "b7e2a1...f4c9",  // SHA-256 of screenshot pixels
  "captured_at": "2026-07-31T14:30:00Z"
}
```

### Generating device_hash (One-way)

```kotlin
// Android
fun getDeviceHash(context: Context): String {
    val androidId = Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
    return sha256(androidId + Build.BOARD + Build.BRAND)
}

private fun sha256(input: String): String {
    return MessageDigest.getInstance("SHA-256")
        .digest(input.toByteArray())
        .joinToString("") { "%02x".format(it) }
}
```

```swift
// iOS
func getDeviceHash() -> String {
    let deviceId = UIDevice.current.identifierForVendor?.uuidString ?? UUID().uuidString
    let data = deviceId.data(using: .utf8)!
    return SHA256.hash(data: data).compactMap { String(format: "%02x", $0) }.joined()
}
```

### Generating geo_hash (Privacy-preserving location)

```kotlin
// Convert GPS to geohash with ~5km precision (5 characters)
fun locationToGeohash(lat: Double, lng: Double): String {
    return GeoHash.geoHashStringWithCharacterPrecision(lat, lng, 5)
}
```

### Generating screenshot_hash (Deduplication)

```kotlin
// Hash screenshot pixels (NOT the image file)
fun getScreenshotHash(bitmap: Bitmap): String {
    val pixels = IntArray(bitmap.width * bitmap.height)
    bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
    val byteBuffer = ByteBuffer.allocate(pixels.size * 4)
    byteBuffer.asIntBuffer().put(pixels)
    return sha256(byteBuffer.array().toString())
}
```

---

## 📱 Complete Mobile Flow

```kotlin
class PriceSubmissionFlow(private val context: Context) {

    fun submitPrice(screenshot: Bitmap, vendorUrl: String) {
        // 1. Extract text on device (NEVER upload image)
        OcrProcessor().processScreenshot(screenshot) { ocrResult ->

            // 2. Build submission payload
            val submission = OCRSubmission(
                price = ocrResult.price,
                currency = ocrResult.currency,
                productName = ocrResult.productName,
                brand = ocrResult.brand,
                vendorDomain = extractDomain(vendorUrl),
                inStock = ocrResult.inStock,
                ocrConfidence = ocrResult.confidence,
                ocrEngine = "mlkit",
                geoHash = getLastKnownGeohash(),  // Optional
                deviceHash = getDeviceHash(context),
                deviceOs = "Android",
                appVersion = BuildConfig.VERSION_NAME,
                screenshotHash = getScreenshotHash(screenshot),  // For dedup
                capturedAt = Instant.now()
            )

            // 3. Send to backend
            CoroutineScope(Dispatchers.IO).launch {
                try {
                    val response = apiService.submitOcr(submission)
                    withContext(Dispatchers.Main) {
                        showSuccess("Price submitted! Score: ${response.verificationScore}")
                    }
                } catch (e: Exception) {
                    withContext(Dispatchers.Main) {
                        showError("Failed: ${e.message}")
                    }
                }
            }

            // 4. Clean up (screenshot stays on device, no server upload)
            screenshot.recycle()
        }
    }
}
```

---

## 🎮 Gamification & Reputation

### Contributor Levels

| Level | Submissions | Accuracy | Badge |
|-------|-------------|----------|-------|
| Newbie | 0-4 | — | 🌱 |
| Contributor | 5-24 | >60% | ⭐ |
| Trusted | 25-99 | >80% | ⭐⭐ |
| Expert | 100-499 | >90% | ⭐⭐⭐ |
| Master | 500+ | >95% | 🏆 |

### Rewards
- **Points:** Each approved submission = 10 points
- **Streak bonus:** 7-day submission streak = 50 bonus points
- **Accuracy bonus:** 95%+ accuracy = 2x points
- **Redeem:** Points for premium features, gift cards, or donations

### Leaderboard API

```kotlin
// Fetch leaderboard for user's city
val leaderboard = apiService.getLeaderboard(geoHash = userGeoHash, limit = 50)
```

---

## 🧪 Testing Your Integration

### Unit Tests

```kotlin
@Test
fun testPriceExtraction() {
    val text = "Apple iPhone 15 128GB Blue\n₹1,299.00\nIn stock"
    val result = parsePriceFromText(text)
    assertEquals(1299.00, result.price)
    assertEquals("INR", result.currency)
    assertTrue(result.inStock)
}

@Test
fun testDeviceHashConsistency() {
    val hash1 = getDeviceHash(context)
    val hash2 = getDeviceHash(context)
    assertEquals(hash1, hash2)  // Same device = same hash
}
```

### Integration Test

```bash
# Test submission endpoint
curl -X POST http://localhost:8000/api/v1/submissions/ocr   -H "Content-Type: application/json"   -d '{
    "price": 999.00,
    "currency": "INR",
    "product_name": "Test Product",
    "vendor_domain": "testvendor.com",
    "ocr_confidence": 0.95,
    "device_hash": "a" * 64,
    "screenshot_hash": "b" * 64
  }'
```

---

## 📊 Performance Benchmarks

| Metric | Target | Notes |
|--------|--------|-------|
| OCR processing time | < 2s | On-device ML Kit |
| Submission API response | < 500ms | Backend validation |
| App bundle size increase | < 5MB | ML Kit is ~3MB |
| Battery impact | < 3% per submission | One-time processing |
| Network payload | < 2KB | JSON only, no images |

---

## 🔒 Security Best Practices

1. **Certificate Pinning:** Pin your API certificate to prevent MITM attacks
2. **Request Signing:** Sign submissions with device_hash + timestamp
3. **Rate Limiting:** Max 10 submissions/minute per device
4. **Root Detection:** Warn users on rooted/jailbroken devices
5. **Screenshot Encryption:** Encrypt screenshot in memory before OCR

---

## 🚀 Deployment Checklist

- [ ] OCR engine integrated and tested on device
- [ ] `device_hash` generation implemented (SHA-256)
- [ ] `geo_hash` generation implemented (5-char precision)
- [ ] `screenshot_hash` generation implemented
- [ ] Privacy policy updated to mention on-device OCR
- [ ] User consent obtained for price submissions
- [ ] Submission API endpoint tested in staging
- [ ] Error handling for poor OCR quality
- [ ] Offline queue for submissions when no network
- [ ] Contributor leaderboard UI implemented
- [ ] Community validation voting UI implemented
