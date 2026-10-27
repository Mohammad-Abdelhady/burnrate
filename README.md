# 🔥 BurnRate

**Find out where your AI coding tokens go, and stop the waste.**
Free, open source, and 100% local. Your code never leaves your machine.

[العربية ↓](#-بالعربي)

---

## Why

On our founder's own 30 days of Claude Code usage:

| | |
|---|---|
| Usage value (API prices) | **$652** |
| Share spent re-reading context | **81%** |
| Share spent on answers (output) | **8%** |
| Cost of one 713-message session | **$195** (about 30% of the month) |
| Estimated savings with leaner sessions | **~52%** |

Every message re-sends the whole conversation, so long sessions get more expensive with every turn. On a subscription, that's why you hit your weekly limit early. On the API, that's why the bill is high.

*Data from one user. Costs are API-equivalent estimates and savings are simulated. Your results will differ.*

## What it does

| Feature | Command |
|---|---|
| Spend report (model, project, session, token type) | `python burnrate.py` |
| Same report as an HTML page | `python burnrate.py --html report.html` |
| Context analysis + "start fresh at N tokens" simulation | included in the report |
| Live meter in its own window | `python burnrate.py --watch` |
| Status line inside Claude Code (terminal) | `python burnrate.py --install-statusline` |
| Try it with sample data | `python burnrate.py --demo` |
| Troubleshooting | `python burnrate.py --diagnose` |

Options: `--days 7` to change the period, `--json out.json` to export raw results.

## Install

1. Install **Python 3.8+**. On Windows, get it from the Microsoft Store or python.org.
2. Download `burnrate.py`.
3. Open a terminal in that folder and run `python burnrate.py`.

No other dependencies. Works on Windows, macOS and Linux.

## Compatibility

| Tool | Support |
|---|---|
| Claude Code | ✅ Full |
| Claude Desktop (Code sessions) | ✅ Report and live meter |
| OpenAI Codex CLI | 🧪 Experimental |
| Cursor, Copilot, Gemini CLI | 🔜 Planned |
| Chat apps (Claude.ai, ChatGPT) | ❌ They don't store usage locally |

## Privacy

BurnRate reads **only metadata** from local log files: model names, token counts, timestamps, project folder names and tool names.
It never reads your code, your prompts or the model's answers, and **it never sends anything anywhere**. It's one file, so you can read it all.

## Prices

Costs use the per-token prices in the `PRICES` table at the top of `burnrate.py`. They're estimates, so check them against the official pricing pages and edit them if needed.

## License

MIT. See [LICENSE](LICENSE).

---

<div dir="rtl" align="right">

## 🇪🇬 بالعربي

**اعرف استهلاك أدوات البرمجة بالذكاء الاصطناعي رايح فين، ووقّف الهدر.**
مجانية، ومفتوحة المصدر، وبتشتغل على جهازك بالكامل. الكود بتاعك عمره ما بيطلع من جهازك.

### ليه؟

لما شغّلنا الأداة على استخدام مؤسس المشروع لمدة ٣٠ يوم، لقينا إن **٨١٪** من الاستهلاك راح على إعادة قراية المحادثة مع كل رسالة، و**٨٪** بس راح على الإجابات. وجلسة واحدة فيها ٧١٣ رسالة كلّفت لوحدها حوالي **٣٠٪** من الشهر.

كل رسالة جديدة بتبعت المحادثة كلها من الأول، فالجلسات الطويلة بتغلى مع كل رسالة. ولو إنت مشترك، ده السبب إنك بتوصل للحد الأسبوعي بدري.

*البيانات من مستخدم واحد، والأرقام تقديرية. نتايجك هتختلف.*

### الأوامر

لعرض التقرير:

</div>

```
python burnrate.py --html report.html
```

<div dir="rtl" align="right">

لتشغيل العدّاد المباشر في نافذة لوحده:

</div>

```
python burnrate.py --watch
```

<div dir="rtl" align="right">

للتجربة ببيانات تجريبية:

</div>

```
python burnrate.py --demo
```

<div dir="rtl" align="right">

### التثبيت

ثبّت بايثون إصدار ٣٫٨ أو أحدث، وحمّل ملف الأداة، وافتح الطرفية في نفس الفولدر، وشغّل أي أمر من اللي فوق.

### الخصوصية

الأداة بتقرا **الأرقام بس**: أسماء الموديلات، وأعداد الاستهلاك، والتواريخ، وأسماء المشاريع. ومش بتقرا الكود ولا رسايلك، **ومش بتبعت أي حاجة لأي مكان**.

### الترخيص

ترخيص مفتوح المصدر يسمح بالاستخدام والتعديل بحرية.

</div>
