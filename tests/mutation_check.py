"""
Проверка самих тестов: ловят ли они настоящие баги.

Зачем это нужно. Тест, который не падает ни при каком баге, ничего не охраняет,
но при этом создаёт ложное чувство защиты. Здесь мы по одному возвращаем в код
ошибки, которые раньше искали руками, и смотрим, кто из тестов их поймает.

Приложение разбито на модули (пакет aitheatre/), поэтому у каждого бага указан
файл, в котором он живёт: раньше баг был просто «в файле», теперь — в конкретном
месте. Копируется весь проект целиком (приложение, тесты, static/), правки
вносятся в копию, рабочие файлы не меняются. Ollama и спектакль не запускаются.

Запуск из папки проекта:

    venv/Scripts/python.exe tests/mutation_check.py
"""

import contextlib
import importlib.util
import io
import shutil
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

# Что скопировать в проверочную папку: приложение, тесты, точка входа и то,
# на что тесты смотрят (клиент Socket.IO, рендерер формул, иконка). Точка входа
# тоже нужна: иначе из копии нельзя проверить, что она ведёт в то же приложение.
COPY_DIRS = ("aitheatre", "tests", "static")
COPY_FILES = ("favicon.ico", "ollama_debate_web.py")

# Заголовок бага -> список правок: (файл, что заменить, на что заменить).
# Каждый заголовок — реальная ошибка из истории проекта.
BUGS = {
    "ссылку на настройку оставили без имени модуля": [
        ("aitheatre/ollama_api.py",
         '"options": options or settings.OPTIONS,',
         '"options": options or OPTIONS,'),
    ],
    "формулы снова съедает markdown": [
        ("aitheatre/text.py",
         "return MATH_SEGMENT_RE.sub(stash, text), stashed",
         "return text, stashed"),
    ],
    "судью снова закрепили за одним характером": [
        ("aitheatre/show.py",
         "            character = draw_character()",
         '            character = "pedant" if template.get("is_judge") '
         "else draw_character()"),
    ],
    "тема берётся только из поля формы": [
        ("aitheatre/web.py",
         '    topic = str(data.get("topic", "") or "").strip() or '
         '(show.session.topic or "").strip()',
         '    topic = str(data.get("topic", "") or "").strip()'),
    ],
    "top_k без проверки границ": [
        ("aitheatre/show.py",
         '            if key == "top_k" and number < 1:\n'
         '                # С нулём или отрицательным значением Ollama '
         'отклонит весь запрос\n'
         '                return f"{name}: «top_k» должен быть не меньше 1"',
         "            if False:\n"
         "                pass"),
    ],
    "характер судьи теряется при переименовании состава": [
        ("aitheatre/show.py",
         '        if raw.get("avatar_keywords") is not None:',
         '        entry.pop("instruction", None)\n'
         '        if raw.get("avatar_keywords") is not None:'),
    ],
    "повтор в списке имён снова даёт двух «Галин»": [
        ("aitheatre/show.py",
         "    available_female_names = list(dict.fromkeys(settings.FEMALE_NAMES))",
         "    available_female_names = settings.FEMALE_NAMES.copy()"),
        ("aitheatre/settings.py",
         '    "Варвара", "Диана", "Елизавета", "Кристина", "Нелли"',
         '    "Варвара", "Диана", "Елизавета", "Кристина", "Нелли", "Галина"'),
    ],
    "пост не получает класс роли (полосы пропадают)": [
        ("aitheatre/page.py",
         "postDiv.className = `post post-role-${role}`",
         "postDiv.className = `post`"),
    ],
    "у роли нет своего цвета на тёмной сцене": [
        ("aitheatre/page.py",
         "        body.dark.role-marks .post.post-role-judge { border-left-color: #8f5cae; }\n",
         ""),
    ],
    "пульт снова теряет top_k, min_p и seed": [
        ("aitheatre/page.py",
         "        const PARAM_KEYS = ['temperature', 'top_p', 'top_k', 'min_p',\n"
         "                            'repeat_penalty', 'presence_penalty', "
         "'frequency_penalty', 'seed'];",
         "        const PARAM_KEYS = ['temperature', 'top_p', 'repeat_penalty', "
         "'presence_penalty', 'frequency_penalty'];"),
    ],
    "у участника пропало поле min_p": [
        ("aitheatre/page.py",
         "paramField('min_p', 'min_p', '0.01', 0, 1,",
         "paramField('hidden_min_p', 'min_p', '0.01', 0, 1,"),
    ],
    "страница снова тянет шрифты из интернета": [
        ("aitheatre/page.py",
         "        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и",
         "        @import url('https://fonts.googleapis.com/css2?family=Oswald:wght@400;700&display=swap');\n"
         "        /* Внешних шрифтов здесь нет: вся страница рисуется системными Georgia и"),
    ],
    "правила судьи не переживают «Новый спектакль»": [
        ("aitheatre/show.py",
         "        self.judge_rules = judge_rules",
         "        self.judge_rules = list(settings.DEFAULT_JUDGE_RULES)"),
    ],
    "новый характер оказался клоном «Педанта»": [
        ("aitheatre/settings.py",
         '        "params": {"temperature": 0.6, "top_p": 1.0, "min_p": 0.4, "top_k": 30,\n'
         '                   "repeat_penalty": 1.2, "presence_penalty": 0.4,\n'
         '                   "frequency_penalty": 0.3},',
         '        "params": {"temperature": 0.2, "top_p": 0.5, "repeat_penalty": 1.10,\n'
         '                   "presence_penalty": 0.05, "frequency_penalty": 0.05},'),
    ],
    "опечатка в имени параметра характера": [
        ("aitheatre/settings.py",
         '"min_p": 0.4, "top_k": 30', '"minp": 0.4, "top_k": 30'),
    ],
}


def copy_project(destination: Path, patches):
    """Копирует приложение и тесты в отдельную папку и вносит правки в копию."""
    for name in COPY_DIRS:
        # Папка переиспользуется на каждом баге: копируем заново, а не поверх
        shutil.rmtree(destination / name, ignore_errors=True)
        shutil.copytree(PROJECT_ROOT / name, destination / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in COPY_FILES:
        shutil.copy2(PROJECT_ROOT / name, destination / name)
    for relative, text in patches.items():
        (destination / relative).write_text(text, encoding="utf-8")


def forget_app_modules():
    """Сбрасывает всё уже загруженное, чтобы в следующий раз взялось из копии.

    Без точки входа в этом списке остался бы модуль предыдущей копии: папка уже
    удалена, а из памяти он бы ещё отвечал — и тест на точку входа падал бы
    на ровном месте, маскируя настоящие находки.
    """
    for name in [n for n in sys.modules
                 if n == "aitheatre" or n.startswith("aitheatre.")
                 or n in ("ollama_debate_web", "__main__",
                          "test_theatre", "tests", "tests.test_theatre")]:
        del sys.modules[name]


def run_suite(project_dir: Path):
    """Прогон набора против указанной копии проекта. Возвращает имена упавших."""
    forget_app_modules()
    sys.path.insert(0, str(project_dir))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "test_theatre", project_dir / "tests" / "test_theatre.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules["test_theatre"] = module
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            total = suite.countTestCases()
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        sys.path.remove(str(project_dir))
    caught = sorted({test.id().split(".")[-1]
                     for test, _ in result.failures + result.errors})
    return total, caught


def main():
    ok = True

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        copy_project(project_dir, {})
        total, caught = run_suite(project_dir)
        if caught:
            print(f"[эталон] неповреждённый код, тестов {total}: ПАДАЮТ {caught}")
            ok = False
        else:
            print(f"[эталон] неповреждённый код, тестов {total}: OK")
        print()

        print("Подбрасываем баги по одному и смотрим, кто их поймает:")
        print("-" * 68)

        original = {relative: (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                    for patches in BUGS.values() for relative, _, _ in patches}

        for title, patches in BUGS.items():
            files = sorted({relative for relative, _, _ in patches})
            changed, broken = {}, False
            for relative, old, new in patches:
                text = changed.get(relative, original[relative])
                if old not in text:
                    print(f"⚠️  {title}: не нашёл место в {relative} — проверка невозможна")
                    broken = True
                    ok = False
                    continue
                changed[relative] = text.replace(old, new, 1)
            if broken:
                continue

            copy_project(project_dir, changed)
            _, caught = run_suite(project_dir)
            mark = ", ".join(files)
            if caught:
                print(f"✅ [{mark}] {title}")
                print(f"     поймали: {', '.join(caught[:5])}")
            else:
                print(f"❌ [{mark}] {title} — НЕ пойман ни одним тестом")
                ok = False

    print()
    print("-" * 68)
    print("ИТОГ:", "все баги ловятся" if ok else "есть слепые места")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
