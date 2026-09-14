"""
Проверка самих тестов: ловят ли они настоящие баги.

Зачем это нужно. Тест, который не падает ни при каком баге, ничего не охраняет,
но при этом создаёт ложное чувство защиты. Здесь мы по одному возвращаем в код
ошибки, которые раньше искали руками, и смотрим, кто из тестов их поймает.

Запуск из папки проекта:

    venv/Scripts/python.exe tests/mutation_check.py

Рабочий файл при этом не меняется: код копируется во временную папку, правки
живут только в памяти. Ollama и спектакль не запускаются.
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

# Отчёту нужны те же эмодзи (✅/❌), что и приложению, а русская консоль Windows
# работает в cp1251 и роняет печать с UnicodeEncodeError. Приложение себя так
# защищает при импорте; здесь импорта приложения нет, поэтому — тот же приём.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "ollama_debate_web.py"
TESTS = PROJECT_ROOT / "tests" / "test_theatre.py"

# Заголовок бага -> что заменить в исходнике и на что.
# Каждый заголовок — реальная ошибка из истории проекта.
BUGS = {
    "формулы снова съедает markdown": [
        ("return MATH_SEGMENT_RE.sub(stash, text), stashed",
         "return text, stashed"),
    ],
    "судью снова закрепили за одним характером": [
        ("            character = draw_character()",
         '            character = "pedant" if template.get("is_judge") '
         "else draw_character()"),
    ],
    "тема берётся только из поля формы": [
        ('    topic = str(data.get("topic", "") or "").strip() or '
         '(session.topic or "").strip()',
         '    topic = str(data.get("topic", "") or "").strip()'),
    ],
    "top_k без проверки границ": [
        ('            if key == "top_k" and number < 1:\n'
         '                # С нулём или отрицательным значением Ollama '
         'отклонит весь запрос\n'
         '                return f"{name}: «top_k» должен быть не меньше 1"',
         "            if False:\n"
         "                pass"),
    ],
    "характер судьи теряется при переименовании состава": [
        ('        if raw.get("avatar_keywords") is not None:',
         '        entry.pop("instruction", None)\n'
         '        if raw.get("avatar_keywords") is not None:'),
    ],
    "повтор в списке имён снова даёт двух «Галин»": [
        ("    available_female_names = list(dict.fromkeys(FEMALE_NAMES))",
         "    available_female_names = FEMALE_NAMES.copy()"),
        ('    "Варвара", "Диана", "Елизавета", "Кристина", "Нелли"',
         '    "Варвара", "Диана", "Елизавета", "Кристина", "Нелли", "Галина"'),
    ],
    "пост не получает класс роли (полосы пропадают)": [
        ("postDiv.className = `post post-role-${role}`",
         "postDiv.className = `post`"),
    ],
    "у роли нет своего цвета на тёмной сцене": [
        ("        body.dark.role-marks .post.post-role-judge { border-left-color: #8f5cae; }\n",
         ""),
    ],
    "пульт снова теряет top_k, min_p и seed": [
        ("        const PARAM_KEYS = ['temperature', 'top_p', 'top_k', 'min_p',\n"
         "                            'repeat_penalty', 'presence_penalty', 'frequency_penalty', 'seed'];",
         "        const PARAM_KEYS = ['temperature', 'top_p', 'repeat_penalty', "
         "'presence_penalty', 'frequency_penalty'];"),
    ],
    "у участника пропало поле min_p": [
        ("paramField('min_p', 'min_p', '0.01', 0, 1,",
         "paramField('hidden_min_p', 'min_p', '0.01', 0, 1,"),
    ],
    "страница снова тянет шрифты из интернета": [
        ("        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и",
         "        @import url('https://fonts.googleapis.com/css2?family=Oswald:wght@400;700&display=swap');\n"
         "        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и"),
    ],
    "правила судьи не переживают «Новый спектакль»": [
        ("        self.judge_rules = judge_rules",
         "        self.judge_rules = list(DEFAULT_JUDGE_RULES)"),
    ],
}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_suite(code_path):
    """Прогон набора против указанной копии приложения. Возвращает имена упавших."""
    for name in ("ollama_debate_web", "test_theatre"):
        sys.modules.pop(name, None)
    with contextlib.redirect_stdout(io.StringIO()):
        load_module("ollama_debate_web", str(code_path))
        test_module = load_module("test_theatre", str(TESTS))
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_module)
        total = suite.countTestCases()
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    caught = sorted({test.id().split(".")[-1]
                     for test, _ in result.failures + result.errors})
    return total, caught


def main():
    original = SOURCE.read_text(encoding="utf-8")
    ok = True

    with tempfile.TemporaryDirectory() as tmpdir:
        code_path = Path(tmpdir) / "bugged_module.py"

        code_path.write_text(original, encoding="utf-8")
        total, caught = run_suite(code_path)
        if caught:
            print(f"[эталон] неповреждённый код, тестов {total}: ПАДАЮТ {caught}")
            ok = False
        else:
            print(f"[эталон] неповреждённый код, тестов {total}: OK")
        print()

        print("Подбрасываем баги по одному и смотрим, кто их поймает:")
        print("-" * 68)

        for title, patches in BUGS.items():
            source = original
            broken = False
            for old, new in patches:
                if old not in source:
                    print(f"⚠️  {title}: не нашёл место в коде — проверка невозможна")
                    broken = True
                    ok = False
                    continue
                source = source.replace(old, new, 1)
            if broken:
                continue

            code_path.write_text(source, encoding="utf-8")
            _, caught = run_suite(code_path)
            if caught:
                print(f"✅ {title}")
                print(f"     поймали: {', '.join(caught[:5])}")
            else:
                print(f"❌ {title} — НЕ пойман ни одним тестом")
                ok = False

    print()
    print("-" * 68)
    print("ИТОГ:", "все баги ловятся" if ok else "есть слепые места")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
