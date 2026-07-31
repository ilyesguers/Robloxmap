# 🤖 بوت تيليجرام — زيادة لايكات فري فاير (محدّث OB54 — يوليو 2026)

بوت تيليجرام كامل (Python + aiogram 3 + aiohttp) يزيد إعجابات بروفايلات
فري فاير (Garena) **بمجرد إدخال UID** — ينشئ **حسابات ضيف جديدة تلقائياً**
لكل إعجاب، حتى الوصول للحد اليومي (~100 إعجاب).

> ✅ **تحديث 2026-07-31 (OB54):** إصلاح خطأ `Token Grant فشل: {'error': 'auth_error'}`
> نهائياً — إزالة الحسابات الجاهزة الملفَّقة + تحديث كل الروابط وصيغة الإعجاب
> إلى الطريقة الشغالة حالياً (مطابقة لمستودع محدّث 2026-07-30 بتعليق
> «confirmed working 2026-07-29»).
> التفاصيل في قسم [إصلاح 2026-07-31](#-إصلاح-2026-07-31--auth_error).

---

## ⚠️ تنبيهات — اقرأها أولاً

1. **مخالفة لشروط Garena** — الأتمتة وإنشاء الحسابات الضيفية البرمجية
   مخالفة لشروط الخدمة. الاستخدام على مسؤوليتك الخاصة (خطر حظر IP/حسابات).
2. **المفاتيح والروابط مدمجة في الكود** (`services/garena.py` أعلى الملف)
   — إذا غيّرت Garena القيم في تحديث مستقبلي، عدّلها من مكان واحد فقط.
3. **الحد اليومي ≈ 100 إعجاب** لكل UID مستهدف في اليوم، وكل حساب ضيف
   يعطي إعجاباً واحداً فقط لنفس الهدف.
4. **البروكسيات اختيارية لكنها مفيدة جداً** — توليد حسابات كثيرة من IP
   واحد قد يفعّل أنظمة مكافحة السبام.

---

## 🏗️ هيكل المشروع

```
├── main.py                        # نقطة الدخول: تهيئة البوت والمحرك
├── requirements.txt               # الاعتماديات
├── Procfile                       # أمر تشغيل Railway
├── railway.json                   # إعدادات Railway (Nixpacks)
├── .env.example                   # نموذج متغيرات البيئة (متغيران فقط!)
├── .gitignore
├── config/
│   └── settings.py                # إعدادات — كلها لها افتراضيات جاهزة
├── handlers/
│   ├── user.py                    # /start, /likes, /cancel, أزرار، FSM
│   └── admin.py                   # /stats, /broadcast, /ban, /unban, /queue
├── middlewares/
│   └── access_control.py          # منع المحظورين
├── services/
│   ├── garena.py                  # ⭐ عميل Garena الحقيقي (OB53): تسجيل ضيف + JWT + إعجاب + تحقق
│   ├── like_engine.py             # ⭐ المحرك: صف انتظار + حلقة إعجابات + حد يومي + إلغاء فوري
│   └── database.py                # SQLite: مستخدمون، إحصائيات، حظر، Rate limit
├── utils/
│   ├── logger.py                  # سجلات stdout (لـ Railway)
│   ├── validators.py              # التحقق من UID
│   └── constants.py               # مناطق التسجيل الضيفي المدعومة
└── tests/
    ├── smoke_test.py              # اختبارات كاملة بسيرفر وهمي (كلها ناجحة ✔)
    └── live_check.py              # فحص حي لسيرفرات Garena (شغّله من Railway)
```

---

## 🔄 التدفق التقني (الطريقة الحالية OB53)

```
المستخدم يرسل UID فقط
      │
      ▼
┌──────────────────────────────────────────────────┐
│ لكل إعجاب (حتى الحد اليومي ≈100):                 │
│                                                  │
│ 1) تسجيل حساب ضيف جديد:                          │
│    POST connect.garena.com/oauth/guest/register  │
│    (+ بدائل: 100067.connect / ffmconnect)        │
│    (HMAC-SHA256 Signature + كلمة سر SHA256)      │
│ 2) منح التوكن:                                   │
│    POST 100067.connect.garena.com/.../token/grant│
│    (+ بديل v2 على ffmconnect، تراجع عند 429)     │
│    → access_token + open_id                      │
│ 3) إنشاء الحساب داخل اللعبة:                     │
│    POST loginbp.ggblueshark.com/MajorRegister    │
│    (OB54: الحقل 15 = اللغة، 17 = 1 + XOR + AES)  │
│ 4) تسجيل الدخول → JWT:                           │
│    POST loginbp.ggpolarbear.com/MajorLogin       │
│    (ME أولاً على common.ggbluefox — بروتو كامل)  │
│    → token + serverUrl + lock_region             │
│ 5) إرسال الإعجاب — ★ الصيغة الجديدة OB54:        │
│    POST {serverUrl}/LikeProfile                  │
│    Bearer JWT + جسم AES(varint(uid), varint(كود  │
│    المنطقة: ME=7, IND=1, BR=2, US=9, RU=8 ...))  │
│ 6) الحد اليومي؟ → إيقاف فوري + إشعار             │
└──────────────────────────────────────────────────┘
      │
      ▼
📈 التحقق النهائي: GetPlayerPersonalShow
   (عدد الإعجابات قبل ← بعد — إثبات أن اللايك زاد فعلاً)
```

---

## 🚀 النشر على Railway مجاناً (من الآيفون — بدون كمبيوتر)

> المتطلبات: حساب GitHub + حساب Railway (تسجيل عبر Google/Apple من Safari).

1. ادمج الـ Pull Request من تطبيق GitHub على هاتفك.
2. افتح [railway.app](https://railway.app) وسجّل (رصيد تجريبي مجاني).
3. **New Project** ← **Deploy from GitHub repo** ← اختر `Robloxmap`.
4. انتظر البناء (Nixpacks يكتشف Python تلقائياً — `Procfile` موجود).
5. تبويب **Variables** — أضف متغيرين فقط:
   | المتغير | القيمة |
   |---|---|
   | `BOT_TOKEN` | من [@BotFather](https://t.me/BotFather) |
   | `ADMIN_ID` | معرّفك الرقمي (من [@userinfobot](https://t.me/userinfobot)) |

   كل ما عدا ذلك له قيم افتراضية مدمجة (روابط Garena، مفاتيح AES، التوقيعات، الحدود).

6. **Redeploy** ثم **View Logs** — يجب أن يظهر `🚀 البوت يعمل الآن...`.
7. افتح البوت في تيليجرام وأرسل `/start` — جاهز! 🎉

### تحقق سريع بعد النشر (مهم)
من تبويب **Deployments** على Railway افتح **Shell** (أو أضف أمر تشغيل مؤقت)
ونفّذ:
```bash
python tests/live_check.py
```
ينشئ حساب ضيف حقيقي ويسجّل الدخول — إذا ظهر `✅ LIVE CHECK PASSED`
فالبوت سيعمل فوراً. (عند أي خطأ انسخ النص وأرسله لي.)

---

## 🧰 أوامر البوت

### للمستخدمين
| الأمر | الوصف |
|---|---|
| `/start` | القائمة الرئيسية (أزرار تفاعلية) |
| `/likes <UID>` | بدء طلب إعجابات بسرعة |
| `/cancel` | إلغاء المهمة الحالية فوراً |

### للأدمن (ADMIN_ID فقط)
| الأمر | الوصف |
|---|---|
| `/stats` | المستخدمون/الطلبات/الإعجابات/المحظورون + حجم الصف |
| `/broadcast <نص>` | رسالة جماعية لكل المستخدمين |
| `/ban <user_id> [سبب]` | حظر مستخدم |
| `/unban <user_id>` | فك الحظر |
| `/queue` | حالة قائمة الانتظار |
| `/clear_queue` | مسح المهام المعلقة |

---

## 🧪 الاختبارات

```bash
pip install -r requirements.txt
python tests/smoke_test.py    # كل الاختبارات مع سيرفر وهمي يحاكي Garena
python tests/live_check.py    # فحص حي (شغّله من Railway — الساندبوكس يحجب الشبكة)
```

---

## ⚙️ متغيرات اختيارية (لها افتراضيات — لست مضطراً لتعيينها)

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `MAX_LIKES_PER_SESSION` | `100` | الحد الأقصى لكل جلسة |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | `1` / `3` | تأخير عشوائي بين الإعجابات |
| `PROXIES` | فارغ | بروكسيات ثابتة مفصولة بفواصل |
| `PROXY_API_URL` | فارغ | رابط API بروكسيات دوّارة |
| `RATE_LIMIT_HOURS` | `1` | انتظار المستخدم بين الطلبات |
| `DB_PATH` | `data/bot.db` | مسار قاعدة البيانات |

---

## 🛠️ ملاحظات تقنية

- **التوقيعات**: تسجيل الضيف يتطلب `HMAC-SHA256(client_secret, body)` في
  `Authorization: Signature ...` — منفذ في `services/garena.py`.
- **التشفير**: كل أجسام Protobuf تُشفّر AES-128-CBC بمفتاح وIV ثابتين
  (`Yg&tc%DEuh6%Zc^8` / `6oyZDr22E3ychjM%`).
- **المناطق المدعومة للتسجيل الضيفي**: IND, SG, BR, US, RU, TH, VN, TW, ME, CIS, BD.
- **أخطاء شائعة**: `MajorRegister HTTP 400` = غالباً المنطقة لا تدعم الضيف
  أو النيك نيم مكرر؛ `LIVE CHECK FAILED` = IP محظور (جرّب PROXIES) أو
  Garena غيّرت الثوابت (عدّلها من أعلى `garena.py`).

## 🔧 إصلاح 2026-07-31 — `auth_error`

لمن واجه: `⚠️ توقفت المهمة بعد 9 أخطاء متتالية: Token Grant فشل: {'error': 'auth_error'}`

**الأسباب الحقيقية (3):**

1. **حسابات جاهزة ملفَّقة** — جدول `SEED_GUEST_ACCOUNTS` كان يحتوي uid/hash
   مختلقين غير موجودين في Garena → كل Token Grant يرد `auth_error`، والمحرك
   يعيد اختيار نفس الحساب إلى الأبد. الحل: القائمة أصبحت **فارغة عمداً**،
   والمخزون يمتلئ فقط بحسابات حقيقية تسجَّل بنجاح، وأي حساب يرد عليه Garena
   بـ `auth_error` **يُحذف تلقائياً** من المخزون.
2. **ابتلاع سبب فشل التسجيل** — كان فشل `register_guest` يُبتلع بصمت فلا
   يظهر في السجلات سوى خطأ Token Grant اللاحق. الآن سبب فشل التسجيل يظهر في
   السجلات **وفي رسالة تيليجرام للمستخدم** (مثلاً: IP مراكز بيانات Railway
   مرفوض من Garena = `error_not_found`).
3. **تقادم OB53 → OB54** — تحديث شامل:
   - روابط MajorLogin: `ggpolarbear` (افتراضي) + `common.ggbluefox` (ME أولاً)
     + `ggblueshark` بديل، مع تجربة تلقائية بالترتيب.
   - Token Grant: `100067.connect.garena.com` رئيسي + بديل
     `ffmconnect.../api/v2/oauth/guest/token:grant` + تراجع عند HTTP 429.
   - Guest Register: `connect.garena.com` رئيسي + بديلان.
   - **صيغة LikeProfile الجديدة**: `varint(uid) + varint(كود المنطقة)` بدل
     النصوص (ME=7, IND=1, BR=2, SG=3, TH=4, VN=6, RU=8, US=9, BD=11, TW=12).
   - MajorLogin يرسل بروتو OB54 الكامل (حقول 3..100) + كشف JWT بديلة بالمسح الخام.
   - MajorRegister: الحقل 15 = لغة المنطقة + إضافة الحقل 17.
   - `ReleaseVersion: OB54` في كل الطلبات.

**ماذا تفعل الآن؟** فقط **Redeploy** على Railway بعد دمج الـ PR — لا حاجة
لتغيير أي متغيرات. إذا رأيت في السجلات سبباً مثل `error_not_found` بعد
التحديث فالمعنى أن Garena ترفض تسجيل ضيوف من IP مركز بيانات Railway —
فعّل بروكسيات (`PROXIES` أو `PROXY_API_URL`) وسيعمل من أول محاولة.
