"""
Регрессионные тесты на триггеры и парсинг AI Ассистента.
Прогоняет 1000+ фраз через regex'ы и проверяет что AI не ошибётся.

Запуск:
    python tests/test_ai_triggers.py
"""
import re
import sys


# ============================================================
# 1. ТРИГГЕР "ОПЕРАТОР" — фразы клиента которые вызывают prompt 1/2/3
# ============================================================
OPERATOR_RE = re.compile(
    r"\b(оператор\w*|менеджер\w*|"
    r"саппорт\w*|support|"
    r"жив\w+\s+человек\w*|реальн\w+\s+человек\w*|"
    r"с\s+человеком|к\s+человеку|"
    r"позови\s+\w+|позвать\s+\w+|"
    r"ассистент[,.\s]+позови|ассистент[,.\s]+позвать)\b",
    re.IGNORECASE,
)

# Должны триггерить меню 1/2/3
TRIGGER_POSITIVE = [
    "позови оператора",
    "Позови оператора",
    "ПОЗОВИ ОПЕРАТОРА",
    "нужен менеджер",
    "Хочу с менеджером",
    "позовите оператор пожалуйста",
    "Ассистент позови оператора",
    "ассистент, позови оператора",
    "Ассистент позвать менеджера",
    "хочу с живым человеком поговорить",
    "Можно живого человека?",
    "поговори с реальным человеком",
    "к человеку",
    "с человеком обсудить",
    "позовите саппорта",
    "нужен support",
    "позвать кого-то живого",
    "оператора нужно",
    "менеджера дайте",
    "оператор позвоните",
]

# НЕ должны триггерить (это не запрос оператора)
TRIGGER_NEGATIVE = [
    "продаю за другого человека",
    "ИП на другого человека",
    "это для моего человека",
    "у нас человек на ИП",
    "альфу + точку",
    "сбер беру",
    "как чекнуть QR на ВТБ",
    "сколько за Альфу",
    "беру за 200",
    "хорошо, давайте",
    "согласен",
    "цена 400$ ок",
    "перевод на USDT",
    "что такое гарант",
    "когда выплата",
    "дайте инструкцию",
    "когда сделаете",
    "тут много людей",  # "много людей" ≠ "позови человека"
    "человеческий язык пожалуйста",  # просто прилагательное
    "выручка идёт мимо",
    "как считается комиссия",
    "карту привязал",
    "нет паспорта",
    "когда выплачиваете",
    "точка устраивает",
    "брак был у вас?",
]


def test_operator_trigger():
    """Триггер 'оператор' должен ловить запросы + игнорировать обычный диалог."""
    passed = 0
    failed = []
    for phrase in TRIGGER_POSITIVE:
        if OPERATOR_RE.search(phrase.lower()):
            passed += 1
        else:
            failed.append(f"NEG: {phrase!r} должен триггерить, но НЕТ")
    for phrase in TRIGGER_NEGATIVE:
        if not OPERATOR_RE.search(phrase.lower()):
            passed += 1
        else:
            failed.append(f"POS: {phrase!r} НЕ должен триггерить, но ДА")
    total = len(TRIGGER_POSITIVE) + len(TRIGGER_NEGATIVE)
    print(f"[operator_trigger] {passed}/{total} прошло")
    if failed:
        for f in failed[:10]:
            print("  ❌", f)
    return len(failed) == 0


# ============================================================
# 2. HELP_MARKERS — короткие сообщения которые снимают silent
# ============================================================
HELP_MARKERS = (
    "?", "помог", "помощ", "не получ", "не работ", "не пойм",
    "не понимаю", "сколько", "когда", "куда", "что дальше",
    "застр", "ошибк", "не приходит", "не вижу",
    "привет", "здравств", "есть кто",
    "да", "нет", "ок", "хорошо", "норм", "согласен", "согласна",
    "подходит", "идет", "идёт", "договорились", "понятно",
    "понял", "ясно", "good", "ok", "yes", "no",
    "цена", "цене", "дорого", "дешев", "торг", "скид",
    "метод", "оплат", "выплат", "перевод", "карт", "юсдт", "usdt",
    "гарант", "континентал", "continental",
)


def should_unmute(text):
    """Снимает ли это сообщение silent."""
    lc = text.lower()
    if any(m in lc for m in HELP_MARKERS):
        return True
    if len(lc.split()) <= 3:  # короткие сообщения
        return True
    return False


# Должны СНИМАТЬ silence (клиент продолжает диалог)
UNMUTE_POSITIVE = [
    "да",
    "Нет",
    "ОК",
    "ок ладно",
    "норм",
    "согласен на 200$",
    "?",
    "цена дорого",
    "когда выплата?",
    "гарант хочу",
    "перевод как?",
    "помогите",
    "не получается",
    "застрял в анкете",
    "когда оператор будет?",
    "привет",
    "что дальше",
    "понятно",
    "ясно",
    "good",
    "yes",
    "yes please",
    "no thanks",
    "ладно",  # 1 слово
    "200 долларов",
    "альфу",
    "беру",
    "забыл код",  # короткое
    "USDT",  # очень короткое
    "торг возможен?",
]

# НЕ ДОЛЖНЫ снимать silence (длинные сообщения без ключевых слов)
UNMUTE_NEGATIVE = [
    "сейчас занят выйду позже свяжусь обязательно подождите минуту обсужу с партнёром",
    "у нас была очень странная история на прошлой неделе с другим банком к сожалению",
    "это сложная ситуация требует обдумывания и анализа стоит ли вообще начинать работу сейчас",
]


def test_unmute_markers():
    passed = 0
    failed = []
    for phrase in UNMUTE_POSITIVE:
        if should_unmute(phrase):
            passed += 1
        else:
            failed.append(f"должно снимать silence: {phrase!r}")
    for phrase in UNMUTE_NEGATIVE:
        if not should_unmute(phrase):
            passed += 1
        else:
            failed.append(f"НЕ должно снимать silence: {phrase!r}")
    total = len(UNMUTE_POSITIVE) + len(UNMUTE_NEGATIVE)
    print(f"[unmute_markers] {passed}/{total} прошло")
    if failed:
        for f in failed[:10]:
            print("  ❌", f)
    return len(failed) == 0


# ============================================================
# 3. ПАРСИНГ ВЫБОРА ПОДРАЗДЕЛЕНИЯ (1/2/3)
# ============================================================
def parse_dept(text):
    """Парсит ответ клиента на меню подразделения."""
    lc = text.lower().strip()
    if lc in ("1", "1️⃣", "один") or re.search(r"\bменеджер", lc):
        return "managers"
    if (lc in ("2", "2️⃣", "два")
            or re.search(r"\b(system|систем|перевяз|установ|желез|sus|сус)", lc)):
        return "system"
    if (lc in ("3", "3️⃣", "три")
            or re.search(r"\b(бухгалт|выплат|предоплат|финанс|деньг)", lc)):
        return "accounting"
    return None


DEPT_TESTS = [
    ("1", "managers"),
    ("2", "system"),
    ("3", "accounting"),
    ("Менеджера", "managers"),
    ("менеджер пожалуйста", "managers"),
    ("системщика", "system"),
    ("перевязка нужна", "system"),
    ("установить лк", "system"),
    ("на железо", "system"),
    ("бухгалтерия", "accounting"),
    ("по выплате", "accounting"),
    ("предоплата", "accounting"),
    ("финансовый вопрос", "accounting"),
    ("деньги", "accounting"),
    ("привет", None),
    ("Альфа", None),
    ("когда выплата", "accounting"),  # содержит "выплат"
]


def test_dept_parse():
    passed = 0
    failed = []
    for text, expected in DEPT_TESTS:
        got = parse_dept(text)
        if got == expected:
            passed += 1
        else:
            failed.append(f"{text!r} → got={got!r} expected={expected!r}")
    print(f"[dept_parse] {passed}/{len(DEPT_TESTS)} прошло")
    if failed:
        for f in failed[:10]:
            print("  ❌", f)
    return len(failed) == 0


# ============================================================
# 4. ПАРСИНГ МЕТОДА ОПЛАТЫ
# ============================================================
def parse_payment_method(text):
    """Простой парсер метода — копия логики из knowledge."""
    lc = text.lower()
    if "usdt" in lc or "трс" in lc or "trc" in lc or "юсдт" in lc:
        return "USDT_TRC20"
    if "до перевяз" in lc or ("гарант" in lc and ("до" in lc or "before" in lc)):
        return "GUARANTOR_BEFORE_WORK"
    if "после перевяз" in lc or ("гарант" in lc and "после" in lc):
        return "GUARANTOR_AFTER"
    if "до отработ" in lc:
        return "GUARANTOR_BEFORE"
    if "континентал" in lc or "continental" in lc:
        return "GUARANTOR_AFTER"  # дефолт — после
    if "до отработки" in lc:
        return "GUARANTOR_BEFORE"
    return None


PAYMENT_TESTS = [
    ("USDT TRC20", "USDT_TRC20"),
    ("usdt", "USDT_TRC20"),
    ("Юсдт", "USDT_TRC20"),
    ("гарант до перевязки", "GUARANTOR_BEFORE_WORK"),
    ("Continental до перевяза", "GUARANTOR_BEFORE_WORK"),
    ("гарант после", "GUARANTOR_AFTER"),
    ("Continental после перевязки", "GUARANTOR_AFTER"),
    ("continental", "GUARANTOR_AFTER"),
    ("привет", None),
    ("сколько денег", None),
    ("Альфу хочу", None),
]


def test_payment_parse():
    passed = 0
    failed = []
    for text, expected in PAYMENT_TESTS:
        got = parse_payment_method(text)
        if got == expected:
            passed += 1
        else:
            failed.append(f"{text!r} → got={got!r} expected={expected!r}")
    print(f"[payment_parse] {passed}/{len(PAYMENT_TESTS)} прошло")
    if failed:
        for f in failed[:10]:
            print("  ❌", f)
    return len(failed) == 0


# ============================================================
# 5. ТРИГГЕР ПЕРЕВЯЗ-СУККЕСС от CRM-бота
# ============================================================
def matches_perevyaz_success(text):
    txt = text.lower()
    return (
        ("перевязка лк" in txt and "успешно выполнена" in txt)
        or ("перевязан и в работе" in txt and "уточняется у клиента" in txt)
        or ("перевязка" in txt and "успешно" in txt)
        or ("карточка" in txt and "перевязан" in txt)
    )


PEREVYAZ_TESTS_POS = [
    "✅ Перевязка ЛК Альфа успешно выполнена.",
    "Перевязка Альфа успешно завершена",
    "ЛК Точка перевязан и в работе, метод оплаты: уточняется у клиента",
    "Карточка #lk0042 перевязан, всё ок",
]

PEREVYAZ_TESTS_NEG = [
    "Привет",
    "Альфа 400$",
    "Клиент написал что-то",
    "Заберём ЛК",
    "оплата прошла",  # это про оплату не перевяз
]


def test_perevyaz_match():
    passed = 0
    failed = []
    for phrase in PEREVYAZ_TESTS_POS:
        if matches_perevyaz_success(phrase):
            passed += 1
        else:
            failed.append(f"должно матчиться: {phrase!r}")
    for phrase in PEREVYAZ_TESTS_NEG:
        if not matches_perevyaz_success(phrase):
            passed += 1
        else:
            failed.append(f"НЕ должно матчиться: {phrase!r}")
    total = len(PEREVYAZ_TESTS_POS) + len(PEREVYAZ_TESTS_NEG)
    print(f"[perevyaz_match] {passed}/{total} прошло")
    if failed:
        for f in failed[:10]:
            print("  ❌", f)
    return len(failed) == 0


# ============================================================
# 6. MARKDOWN → HTML конвертация
# ============================================================
def md_to_html(text):
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^\*\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![<>\w])\*([^\*\n]+?)\*(?![<>\w])", r"<i>\1</i>", text)
    text = text.replace("**", "")
    return text


MD_TESTS = [
    ("**Альфа** — 400$", "<b>Альфа</b> — 400$"),
    ("`код 1234`", "<code>код 1234</code>"),
    ("*курсив*", "<i>курсив</i>"),
    ("Обычный текст", "Обычный текст"),
    ("**жирный** и `моноспейс`", "<b>жирный</b> и <code>моноспейс</code>"),
    ("**нет конца жирный", "нет конца жирный"),  # одиночные ** убираются
    ("`pre` ** *italic*", "<code>pre</code>  <i>italic</i>"),  # ** убрался
]


def test_md_html():
    passed = 0
    failed = []
    for input_, expected in MD_TESTS:
        got = md_to_html(input_)
        if got == expected:
            passed += 1
        else:
            failed.append(f"{input_!r} → got={got!r} expected={expected!r}")
    print(f"[md_to_html] {passed}/{len(MD_TESTS)} прошло")
    if failed:
        for f in failed[:5]:
            print("  ❌", f)
    return len(failed) == 0


# ============================================================
# 7. ГЕНЕРАЦИЯ 1000 СЛУЧАЙНЫХ ФРАЗ И ПРОВЕРКА
# ============================================================
import random
random.seed(42)

GREETINGS = ["привет", "Hi", "здарова", "здравствуйте", "доброго"]
BANKS = ["Альфа", "Точка", "Сбер", "ВТБ", "ПСБ", "Озон", "Райф"]
ACTIONS = ["беру", "купить", "продам", "хочу", "покажи"]
PRICES = ["200$", "150", "400 долларов", "350 у.е.", "500"]
QUESTIONS = ["сколько?", "когда?", "как?", "что делать", "правильно?"]
PADDING = ["пожалуйста", "если можно", "будь добр", "слушай"]


def gen_random_phrase():
    """Генерация случайной фразы клиента."""
    template = random.choice([
        "{greeting}",
        "{action} {bank}",
        "{action} {bank} за {price}",
        "{bank} {question}",
        "{question} {bank}",
        "{action} {bank} {padding}",
    ])
    return template.format(
        greeting=random.choice(GREETINGS),
        bank=random.choice(BANKS),
        action=random.choice(ACTIONS),
        price=random.choice(PRICES),
        question=random.choice(QUESTIONS),
        padding=random.choice(PADDING),
    )


def test_random_1000():
    """1000 случайных фраз — НИКАКАЯ не должна триггерить оператора."""
    triggered = 0
    failed = []
    for _ in range(1000):
        phrase = gen_random_phrase()
        if OPERATOR_RE.search(phrase.lower()):
            triggered += 1
            failed.append(phrase)
    # Допускаем 0 ложных срабатываний — мы не используем слова оператор/менеджер
    # в нашем генераторе. Если триггер сработал — баг.
    print(f"[random_1000] 1000 случайных фраз, ложных триггеров: {triggered}")
    if failed:
        for f in failed[:5]:
            print("  ❌ false trigger:", f)
    return triggered == 0


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("РЕГРЕССИОННЫЕ ТЕСТЫ AI ТРИГГЕРОВ")
    print("=" * 60)
    results = [
        test_operator_trigger(),
        test_unmute_markers(),
        test_dept_parse(),
        test_payment_parse(),
        test_perevyaz_match(),
        test_md_html(),
        test_random_1000(),
    ]
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"ИТОГ: {passed}/{total} тестов прошло")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
