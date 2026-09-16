"""
Автотесты AI Театра.

Запуск из папки проекта:

    venv/Scripts/python.exe -m unittest discover -s tests -v

Набор не ходит никуда в интернет: Ollama и поток спектакля подменяются
заглушками, поэтому тесты проходят и без запущенной Ollama, и не мешают
идущему спектаклю. Облачный шлюз проверяется на своём сервере-заглушке
на 127.0.0.1 и случайном порту — запрос к нему должен быть настоящим,
иначе проверять форму запроса нечего. Все проверки — на живом коде проекта,
а не на его копии.

Приложение разбито на модули (пакет aitheatre/), и подменять функцию нужно там,
где она живёт: модули вызывают друг друга по адресу (`ollama_api.ask_model`),
поэтому правка в «своём» модуле видна всем, а копия имени в другом файле — нет.
Помогает `app_module_of(имя)` ниже.

Что покрыто (каждая группа — это место, где ошибку раньше ловили руками):
  * устройство — модули не ссылаются на имена, которых в них нет, а точка
    входа ведёт в то же приложение;
  * формулы — LaTeX больше не съедается markdown, а цены в долларах не
    превращаются в формулы;
  * тема — пустое поле в форме не мешает запуску, если тема есть на сервере;
  * инструкции — личные инструкции участников, руководства модератора и
    правила судьи реально попадают в системный промпт;
  * состав — переименование, пол, подмена модели, откат при ошибке;
  * жребий характеров — судья тянет наравне со всеми, повторы допустимы;
  * аватары — кириллица в имени файла и пол в ключевых словах;
  * VRAM — оценка контекста ведёт себя монотонно и уважает бюджет;
  * страница целиком — скрипт разбирается так же, как в браузере (одна
    синтаксическая ошибка убивает его целиком, и тогда молчат все кнопки),
    а питоновский `\n` в шаблоне не рвёт строку JS пополам.
"""

import builtins
import collections
import copy
import http.server
import importlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import symtable
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import aitheatre

from aitheatre import (avatars, cloud, deps, ollama_api, page, search, settings, show,
                       text, tooltext)
from aitheatre import web as web_app

# Приложение разбито на модули, и подменять функцию нужно там, где она живёт:
# модули вызывают друг друга по полному адресу (ollama_api.check_models_available),
# поэтому правка в «своём» модуле видна всем. app_module_of находит нужный модуль
# по имени — так подмена бьёт в цель, а не в копию имени в другом файле.
APP_MODULES = (settings, deps, text, tooltext, search, avatars, cloud, ollama_api, show,
               page, web_app)

# Набор не ходит в сеть — и это надо держать, а не надеяться на это. Пробы
# способностей модели (takes_tools_now → check_model_tools_support,
# model_supports_thinking) уходили НАСТОЯЩИМ запросом к Ollama: без неё —
# до 10 секунд ожидания таймаута, а через прокси-шлюз в системе — до двух
# секунд на отказ. Сорок таких проб превращали набор в полминуты ожидания
# и засоряли чужой лог «Ошибка проверки capabilities».
# Подменяем здесь, на весь прогон; проверке, которой нужен свой ответ,
# достаточно поставить свою подмену поверх (см. TestCloudGateway.setUp) —
# она снимается первой и возвращает эту.
NO_NETWORK = (
    ("check_model_tools_support", mock.Mock(return_value=False)),
    ("model_supports_thinking", mock.Mock(return_value=False)),
    ("fetch_ollama_models", mock.Mock(return_value=({}, None))),
    ("fetch_loaded_models", mock.Mock(return_value=([], None))),
    ("fetch_gpu_memory", mock.Mock(return_value={})),
)
for _name, _probe in NO_NETWORK:
    mock.patch.object(ollama_api, _name, _probe).start()


def app_module_of(name):
    """Модуль, в котором живёт имя приложения."""
    for module in APP_MODULES:
        if hasattr(module, name):
            return module
    raise AssertionError(f"нет такого имени приложения: {name}")


def _printed_text(fake_print):
    """Всё, что было напечатано, одной строкой — чтобы проверять консоль."""
    return " ".join(str(call.args[0]) for call in fake_print.call_args_list if call.args)


def takes_tools(value: bool):
    """Считать, что шлюз принимает (или нет) инструмент поиска — у любой модели.

    Возвращает откат: набор «моделей без инструмента» общий на весь процесс,
    и проверка, которая не вернёт его как было, оставит след для следующей —
    а правила про поиск в промпте теперь зависят от того, дали ли инструмент.
    """
    patcher = mock.patch.object(cloud, "model_takes_tools", lambda model: value)
    patcher.start()
    return patcher.stop


def cloud_setting(test, name, value):
    """Поставить настройку облака на время проверки.

    Настройки облака читаются из settings.py и только оттуда: .env держит ключ
    и адрес шлюза, потому что это данные облачного сервиса. Раньше эти же
    настройки можно было задать строкой в .env, и проверки выставляли их через
    окружение — теперь правится сама настройка, а после проверки возвращается.
    """
    test.addCleanup(setattr, settings, name, getattr(settings, name))
    setattr(settings, name, value)


# Настройки облака теперь живут в settings.py, и человек правит их под себя:
# включает поиск, меняет окно, выключает числа характеров. Прогон не должен это
# наследовать — иначе проверки вроде «в теле только модель, сообщения и stream»
# падали бы на машине, где хозяин включил поиск у себя. Возвращаем эти
# переключатели к тому, с чем проект отдаётся, а нужное каждой проверке она
# выставляет сама (см. cloud_setting)
CLOUD_DEFAULTS = (
    ("ENABLE_SEARCH", True), ("SEARCH_BEFORE_REPLY", True), ("MIN_SEARCHES", 1),
    ("CLOUD_SEND_PARAMS", False), ("CLOUD_SEND_TOOLS", False),
    ("CLOUD_SEND_MESSAGE_NAMES", False), ("CLOUD_PASS_OLLAMA_EXTRAS", False),
    ("CLOUD_STREAM", True), ("CLOUD_SHOW_THINKING", True),
    ("CLOUD_LIMIT_PARAMS", True), ("CLOUD_NUM_CTX", 32768),
    ("CLOUD_MAX_TOKENS", 0), ("CLOUD_TURN_LIMIT", 120),
    ("CLOUD_SEARCH_TIME", 60),
    ("MAX_SEARCHES", 3), ("SEARCH_MAX_RESULTS", 5), ("MAX_SEARCH_ATTEMPTS", 2),
    # Цена хода читается у шлюза, а у прогона шлюза нет: пустой путь значит
    # «не спрашивать вовсе» — иначе набор ходил бы в интернет за балансом.
    # Кому это нужно — ставит путь сам (см. TestTurnReport про цену хода)
    ("CLOUD_BALANCE_PATH", ""),
)


def setUpModule():
    """Набор не должен зависеть от вашего .env и не пишет в проект.

    Настройки облака приложение поднимает из .env при импорте, поэтому прогон
    на машине, где включены облачные переключатели, вёл бы себя иначе, чем
    на чистой. Убираем их из окружения — каждая проверка сама решает, что ей
    нужно, а не наследует чужие настройки.

    И второе: ДАМП спектакля пишется на каждом ходу, а прогон не должен
    оставлять следы в проекте — уводим файл в временную папку на время набора.
    """
    for name in cloud.CLOUD_ENV_NAMES + (settings.CLOUD_KEY_ENV, "CLOUD_KEY_ENV"):
        os.environ.pop(name, None)

    global SAVED_SETTINGS
    SAVED_SETTINGS = {name: getattr(settings, name) for name, _value in CLOUD_DEFAULTS}
    for name, value in CLOUD_DEFAULTS:
        setattr(settings, name, value)

    # Пауза после хода — для браузера, а не для проверок: ходов в наборе десятки,
    # и каждая половина секунды ожидания делает набор в полминуты
    # (см. settings.TURN_PAUSE).
    # Проверка, которой пауза понадобится, поставит её себе сама, как и прочие
    # настройки (см. cloud_setting)
    SAVED_SETTINGS["TURN_PAUSE"] = settings.TURN_PAUSE
    settings.TURN_PAUSE = 0

    global SCRATCH_DIR, SAVED_DUMP_FILE
    SCRATCH_DIR = tempfile.TemporaryDirectory()
    SAVED_DUMP_FILE = settings.DUMP_FILE
    settings.DUMP_FILE = Path(SCRATCH_DIR.name) / "damp.md"


def tearDownModule():
    """После прогона возвращаем настройки как были и убираем временную папку."""
    settings.DUMP_FILE = SAVED_DUMP_FILE
    for name, value in SAVED_SETTINGS.items():
        setattr(settings, name, value)
    SCRATCH_DIR.cleanup()


def referenced_globals(source, filename):
    """Имена, которые модуль ищет в своих глобалиях, а не заводит сам.

    Локальные переменные и параметры сюда не попадают — их не видно снаружи,
    поэтому они не могут «уехать» в другой файл.
    """
    found = set()

    def walk(table):
        for symbol in table.get_symbols():
            if symbol.is_referenced() and not symbol.is_assigned() \
                    and not symbol.is_parameter() and not symbol.is_imported():
                if table.get_type() == "module" or symbol.is_global():
                    found.add(symbol.get_name())
        for child in table.get_children():
            walk(child)

    walk(symtable.symtable(source, filename, "exec"))
    return found


# ---------------------------------------------------------------- устройство

class TestModuleLayout(unittest.TestCase):
    """Пакет должен оставаться связным: модули говорят друг с другом по адресу.

    При разбиении одного большого файла на модули легко получить ссылку на имя,
    которое уехало в соседний файл: Python скажет о ней только в момент вызова,
    то есть на редкой ветке — например, когда что-то пойдёт не так с сетью.
    Этот тест смотрит в таблицу символов и находит такие ссылки до запуска.
    """

    def app_modules(self):
        """Модули приложения в порядке знакомства (константы -> зависимости)."""
        order = ("settings", "deps", "text", "tooltext", "search", "avatars", "cloud",
                 "ollama_api", "show", "page")
        return [(name, getattr(aitheatre, name)) for name in order] + [("web", web_app)]

    def test_every_module_imports(self):
        for name, module in self.app_modules():
            with self.subTest(module=name):
                self.assertTrue(module.__file__.endswith(f"{name}.py"))

    def test_no_module_refers_to_a_name_it_does_not_have(self):
        for name, module in self.app_modules():
            with self.subTest(module=name):
                source = Path(module.__file__).read_text(encoding="utf-8")
                referenced = referenced_globals(source, module.__file__)
                missing = sorted(
                    symbol for symbol in referenced
                    if symbol not in module.__dict__ and not hasattr(builtins, symbol))
                self.assertEqual(
                    missing, [],
                    f"{name}.py ссылается на имена, которых в нём нет: {missing}")

    def test_entry_point_starts_the_same_app(self):
        """«python ollama_debate_web.py» и «python .» должны вести в приложение,
        а не в свою копию — иначе правки в пакете их не касались бы."""
        entry = importlib.import_module("ollama_debate_web")
        self.assertIs(entry.main, web_app.main)


# ---------------------------------------------------------------- утилиты

def make_session():
    """Свежая сессия со своим составом — тесты не трогают состав приложения."""
    session = show.DebateSession()
    session.load_new_cast()
    return session


def cast_payload(session):
    """То, что присылает пульт на POST /api/participants."""
    return [copy.deepcopy(p) for p in session.runtime_participants]


def strip_session_patch(test, session):
    """Подменяет глобальную сессию проекта на тестовую."""
    patcher = mock.patch.object(show, "session", session)
    patcher.start()
    test.addCleanup(patcher.stop)


def ai_participants(session):
    return [p for p in session.runtime_participants if p.get("model") != "human"]


def judge_of(session):
    return next(p for p in session.runtime_participants if p.get("is_judge"))


def plain_participant(session):
    return next(p for p in session.runtime_participants
                if p.get("model") != "human" and not p.get("is_judge"))


def non_judge_ai(session):
    return next(p for p in session.runtime_participants
                if p.get("model") != "human" and not p.get("is_judge")
                and not p.get("is_moderator"))


# ---------------------------------------------------------------- формулы

class TestFormulas(unittest.TestCase):
    """LaTeX должен доезжать до страницы живым, а markdown — не портить его."""

    def test_inline_formula_becomes_math_element(self):
        html = text.markdown_to_html(
            r"Предел $\lim_{x\to 0}\frac{\sin x}{x}$ равен единице")
        self.assertIn('<span class="math"', html)
        self.assertIn('data-display="0"', html)
        self.assertNotIn("$", html, "знаки $ должны быть израсходованы")

    def test_underscores_inside_formula_survive(self):
        # Ровно тот баг: markdown превращал _ в <em> и формула рассыпалась
        html = text.markdown_to_html(r"$a_1 + b_2 = c_3$")
        self.assertIn("a_1 + b_2 = c_3", html)
        self.assertNotIn("<em>", html)

    def test_block_formula_is_marked_as_display(self):
        html = text.markdown_to_html("Считаем:\n\n$$\\int_0^1 x^2 dx$$\n")
        self.assertIn('data-display="1"', html)
        self.assertIn("math-block", html)

    def test_bracket_and_paren_forms_work(self):
        self.assertIn('class="math"', text.markdown_to_html(r"\(a+b\)"))
        self.assertIn('data-display="1"', text.markdown_to_html(r"\[a+b\]"))

    def test_latex_escaped_braces_survive(self):
        html = text.markdown_to_html(r"Множество $\{a_n\}$ сходится")
        self.assertIn(r"\{a_n\}", html)

    def test_money_is_not_math(self):
        html = text.markdown_to_html("Курс $5 и $7 за штуку")
        self.assertNotIn('class="math"', html)
        self.assertIn("$5", html)
        self.assertIn("$7", html)

    def test_unclosed_formula_is_left_as_text(self):
        # Некорректный LaTeX не должен ронять разметку
        html = text.markdown_to_html(r"Обрыв $\frac{1}{ и всё")
        self.assertIsInstance(html, str)
        self.assertIn("и всё", html)

    def test_bold_and_formula_live_together(self):
        html = text.markdown_to_html(r"**Важно**: $x^2$ растёт")
        self.assertIn("<strong>Важно</strong>", html)
        self.assertIn('<span class="math"', html)
        self.assertIn("x^2", html)

    def test_formula_latex_is_escaped_for_html(self):
        # LaTeX отдаётся в атрибут/текст, угловые скобки не должны ломать разметку
        html = text.markdown_to_html(r"$a < b$")
        self.assertNotIn("<b>", html)


# ---------------------------------------------------------------- аватары

class TestAvatarNames(unittest.TestCase):

    def test_cyrillic_is_kept(self):
        self.assertEqual(avatars.sanitize_avatar_name("дипломат женщина"),
                         "дипломат_женщина")

    def test_yo_letter_is_kept(self):
        self.assertIn("ё", avatars.sanitize_avatar_name("актёр"))

    def test_service_characters_become_underscores(self):
        self.assertEqual(avatars.sanitize_avatar_name("кот/пёс: 2"),
                         "кот_пёс__2")

    def test_keywords_always_mention_gender(self):
        # Иначе поисковик отдаёт женщине «мужчину-геолога»
        for _ in range(15):
            for person in show.build_new_cast():
                if person.get("model") == "human":
                    continue
                keywords = person.get("avatar_keywords", "")
                expected = "женщина" if person["gender"] == "female" else "мужчина"
                self.assertTrue(keywords.endswith(expected),
                                f"«{keywords}» без слова «{expected}»")


# ---------------------------------------------------------------- характер

class TestCharacterLottery(unittest.TestCase):
    """Жребий общий для всех ролей: и судья, и модератор тянут наравне."""

    def test_all_presets_can_be_drawn(self):
        available = {key for key, value in settings.CHARACTER_PRESETS.items()
                     if key != "custom" and value.get("params")}
        drawn = {show.draw_character() for _ in range(500)}
        self.assertEqual(drawn, available)

    def test_judge_is_not_pinned_to_one_character(self):
        drawn = set()
        for _ in range(60):
            judge = judge_of(make_session())
            drawn.add(judge.get("preset"))
        self.assertGreater(len(drawn), 3,
                           "характер судьи разыгрывается недостаточно свободно")

    def test_repeats_are_allowed(self):
        # Два одинаковых характера в одном спектакле — это разрешено
        shows = 80
        with_repeats = 0
        for _ in range(shows):
            chars = [p.get("preset") for p in ai_participants(make_session())]
            if len(chars) != len(set(chars)):
                with_repeats += 1
        self.assertGreater(with_repeats, 0, "повторы внутри спектакля не встречаются")

    def test_parameters_stay_within_ollama_limits(self):
        for key in settings.CHARACTER_PRESETS:
            for _ in range(120):
                params, think = show.character_parameters(key)
                self.assertIn(think, ("auto", "on", "off"))
                for name, value in params.items():
                    if name in ("top_p", "min_p"):
                        self.assertTrue(0.0 <= value <= 1.0, f"{key}.{name}={value}")
                    elif name == "temperature":
                        self.assertTrue(0.0 <= value <= 2.0, f"{key}.{name}={value}")
                    elif name == "top_k":
                        self.assertGreaterEqual(value, 1)
                    elif name != "seed":
                        self.assertGreaterEqual(value, 0.0, f"{key}.{name}={value}")

    def test_explicit_config_wins_over_lottery(self):
        """Числа, вписанные в PARTICIPANTS, важнее жребия."""
        template = {"model": "r1", "temperature": 0.25, "top_p": 0.5}
        with mock.patch.object(settings, "PARTICIPANTS", [template]):
            for _ in range(20):
                person = show.build_new_cast()[0]
                self.assertEqual(person["temperature"], 0.25)
                self.assertEqual(person["top_p"], 0.5)


# ---------------------------------------------------------------- характеры

class TestCharacterPresets(unittest.TestCase):
    """Наборы характеров должны отличаться друг от друга и говорить на языке Ollama.

    Характер — это готовые числа, и жребий выбирает из всего списка наугад.
    Два набора с одинаковыми числами — один характер под двумя именами: сцена
    становится однообразнее, а причина не видна. Опечатка в имени параметра
    тише: такое число просто не уезжает в Ollama, и набор работает не так,
    как написано в его подсказке.
    """

    def _real_presets(self):
        return {key: preset for key, preset in settings.CHARACTER_PRESETS.items()
                if preset.get("params")}

    def test_no_two_characters_have_the_same_numbers(self):
        seen = {}
        for key, preset in self._real_presets().items():
            with self.subTest(character=key):
                signature = tuple(sorted(preset["params"].items()))
                self.assertNotIn(signature, seen,
                                 f"«{key}» — клон «{seen.get(signature)}»")
                seen[signature] = key

    def test_every_character_is_ready_for_the_pult(self):
        """Пульт берёт из набора подпись, подсказку и группу — без них в списке
        характеров будет пустая строка или безымянная группа."""
        for key, preset in self._real_presets().items():
            with self.subTest(character=key):
                self.assertTrue(str(preset.get("label", "")).strip())
                self.assertTrue(str(preset.get("hint", "")).strip())
                self.assertIn(preset.get("group"), ("manual", "balanced", "extreme"))
                self.assertIn(preset.get("think", "auto"), settings.THINK_MODES)

    def test_characters_speak_only_in_known_parameters(self):
        """Опечатка в имени числа — тихий отказ: в Ollama такое поле не уезжает,
        а набор будет выглядеть как задумано."""
        known = set(settings.PER_PARTICIPANT_OPTION_KEYS)
        for key, preset in self._real_presets().items():
            unknown = sorted(set(preset["params"]) - known)
            with self.subTest(character=key):
                self.assertEqual(unknown, [], f"«{key}»: неизвестные параметры {unknown}")

        unknown_drift = sorted(set(settings.CHARACTER_DRIFT) - known)
        self.assertEqual(unknown_drift, [],
                         f"разброс задан для неизвестных параметров: {unknown_drift}")


# ---------------------------------------------------------------- состав

class TestCastStructure(unittest.TestCase):

    def test_names_and_emojis_are_unique(self):
        for _ in range(150):
            cast = show.build_new_cast()
            names = [p["display_name"] for p in cast]
            emojis = [p["avatar_emoji"] for p in cast]
            self.assertEqual(len(names), len(set(names)), names)
            self.assertEqual(len(emojis), len(set(emojis)), emojis)

    def test_source_lists_have_no_duplicates(self):
        """Лишний повтор в списке давал двух «Галин» в одном спектакле: имена
        выдаются через .remove(), а он убирает только одно вхождение."""
        for name in ("MALE_NAMES", "FEMALE_NAMES", "PROFESSIONS",
                     "AVATAR_EMOJIS_MALE", "AVATAR_EMOJIS_FEMALE",
                     "AVATAR_EMOJIS_NEUTRAL"):
            with self.subTest(list=name):
                seq = getattr(app_module_of(name), name)
                repeated = [item for item, count in collections.Counter(seq).items()
                            if count > 1]
                self.assertEqual(repeated, [], f"повторы в {name}")

    def test_pools_are_wide_enough_for_the_whole_cast(self):
        """Имён и профессий должно хватать на всех, иначе участники получат
        одинаковые запасные «Участник»/«человек»."""
        size = len(settings.PARTICIPANTS)
        for name in ("MALE_NAMES", "FEMALE_NAMES", "PROFESSIONS"):
            with self.subTest(list=name):
                self.assertGreaterEqual(len(set(getattr(app_module_of(name), name))), size)

    def test_duplicates_in_a_pool_do_not_leak_into_the_cast(self):
        """Даже если повтор в список вернут, спектакль не получит двух тёзок.

        Мужские и женские имена здесь разные: это две независимые колоды,
        и повтор внутри одной из них не должен выйти на сцену.
        """
        male = ["ПовторМ"] * 3 + [f"Мужское{i}" for i in range(8)]
        female = ["ПовторЖ"] * 3 + [f"Женское{i}" for i in range(8)]
        with mock.patch.object(settings, "MALE_NAMES", male), \
                mock.patch.object(settings, "FEMALE_NAMES", female):
            for _ in range(40):
                names = [p["display_name"] for p in show.build_new_cast()]
                self.assertEqual(len(names), len(set(names)), names)

    def test_gender_matches_emoji_pool(self):
        male = set(settings.AVATAR_EMOJIS_MALE)
        female = set(settings.AVATAR_EMOJIS_FEMALE)
        for _ in range(10):
            for person in show.build_new_cast():
                emoji = person["avatar_emoji"]
                if person["gender"] == "male" and emoji in female:
                    self.fail(f"мужчине достался женский эмодзи {emoji}")
                if person["gender"] == "female" and emoji in male:
                    self.fail(f"женщине достался мужской эмодзи {emoji}")

    def test_humans_have_no_generation_params(self):
        for person in show.build_new_cast():
            if person["model"] == "human":
                for key in settings.PER_PARTICIPANT_OPTION_KEYS:
                    self.assertNotIn(key, person,
                                     "человеку нечего передавать в Ollama")

    def test_judge_gets_its_instruction(self):
        judge = judge_of(make_session())
        self.assertTrue(judge["instruction"].strip())


# ---------------------------------------------------------------- правки состава

class TestCastEditor(unittest.TestCase):

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self._patch(ollama_api, "check_models_available",
                    return_value={"ok": True, "missing": [], "error": None})
        self._patch(ollama_api, "unload_model")

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rename_gender_and_model(self):
        payload = cast_payload(self.session)
        payload[0]["display_name"] = "Новое Имя"
        payload[0]["gender"] = "female"
        payload[1]["model"] = "q9"
        self.assertEqual(show.apply_cast_patch(payload), "")

        cast = self.session.runtime_participants
        self.assertEqual(cast[0]["display_name"], "Новое Имя")
        self.assertEqual(cast[0]["gender"], "female")
        self.assertEqual(cast[1]["model"], "q9")

    def test_a_place_can_be_made_human(self):
        """Живой участник — это место с моделью «human»: его надо суметь создать.

        Пульт раньше молчал про модель живого места, а молчание сервер читал как
        «модель не менялась» — и место, которое режиссёр только что сделал живым,
        возвращалось из модели, ничего не сказав
        """
        payload = cast_payload(self.session)
        payload[0]["model"] = "human"
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[0]["model"], "human")

    def test_a_place_can_become_a_model_again(self):
        """И обратно: человек за местом — не приговор, модель возвращается так же."""
        human_indexes = [i for i, p in enumerate(self.session.runtime_participants)
                         if p["model"] == "human"]
        self.assertTrue(human_indexes, "в составе должен быть живой участник")
        payload = cast_payload(self.session)
        for index in human_indexes:
            payload[index]["model"] = "r1"
        self.assertEqual(show.apply_cast_patch(payload), "")
        cast = self.session.runtime_participants
        for index in human_indexes:
            self.assertEqual(cast[index]["model"], "r1")

    def test_a_new_place_can_be_created_as_a_human(self):
        """➕ в пульте даёт место с моделью, но человеком оно становится тем же полем."""
        payload = cast_payload(self.session)
        payload.append({"cast_id": "", "role": "participant", "display_name": "Гость",
                        "gender": "male", "model": "human", "avatar_keywords": "гость"})
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[-1]["model"], "human")

    def test_an_empty_model_field_does_not_make_a_human(self):
        """Пустое поле — «как было»: стёртая строка не превращает участника в человека."""
        before = self.session.runtime_participants[1]["model"]
        self.assertNotEqual(before, "human")
        payload = cast_payload(self.session)
        payload[1]["model"] = ""
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[1]["model"], before)

    def test_temperature_is_updated_and_can_be_cleared(self):
        payload = cast_payload(self.session)
        payload[0]["temperature"] = 1.25
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[0]["temperature"], 1.25)

        payload = cast_payload(self.session)
        payload[0]["temperature"] = ""
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertNotIn("temperature", self.session.runtime_participants[0],
                         "пустое поле значит «как в модели», параметр не отправляем")

    def test_an_empty_cast_is_rejected(self):
        """Спектаклю некому играть: пустой список — ошибка, а не «убрать всех»."""
        self.assertIn("хотя бы одно", show.apply_cast_patch([]))

    def test_a_place_without_an_id_is_a_new_place_not_an_error(self):
        """Незнакомое место — это новое место, и сервер сам даёт ему имя.

        Раньше тот же ответ был «в составе шесть участников, получено один»,
        и добавить участника из пульта было нельзя.
        """
        self.assertEqual(show.apply_cast_patch([{"model": "r1", "role": "participant"}]), "")
        cast = self.session.runtime_participants
        self.assertEqual(len(cast), 1)
        self.assertTrue(cast[0]["display_name"].strip())
        self.assertTrue(cast[0]["cast_id"])

    def test_empty_name_is_rejected(self):
        payload = cast_payload(self.session)
        payload[0]["display_name"] = "   "
        self.assertTrue(show.apply_cast_patch(payload))

    def test_duplicate_names_are_rejected(self):
        payload = cast_payload(self.session)
        payload[0]["display_name"] = payload[1]["display_name"]
        self.assertIn("разными", show.apply_cast_patch(payload))

    def test_bad_numbers_are_rejected(self):
        cases = (("temperature", 3.5), ("temperature", -0.1), ("top_p", 1.5),
                 ("min_p", -0.2), ("repeat_penalty", -1), ("top_k", 0))

        for key, value in cases:
            with self.subTest(key=key):
                payload = cast_payload(self.session)
                payload[0][key] = value
                self.assertTrue(show.apply_cast_patch(payload), f"{key} принят зря")

    def test_cast_is_unchanged_when_a_later_participant_is_bad(self):
        """Запрос применяется целиком или отклоняется целиком."""
        payload = cast_payload(self.session)
        before = copy.deepcopy(self.session.runtime_participants)
        payload[0]["display_name"] = "Другое Имя"
        payload[-1]["temperature"] = 99
        self.assertTrue(show.apply_cast_patch(payload))
        self.assertEqual(self.session.runtime_participants[0]["display_name"],
                         before[0]["display_name"])

    def test_judge_keeps_its_instruction_after_rename(self):
        payload = cast_payload(self.session)
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        before = self.session.runtime_participants[judge_index]["instruction"]
        payload[judge_index]["display_name"] = "Судья Новый"
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[judge_index]["instruction"],
                         before)

    def test_published_posts_are_not_rewritten(self):
        """Прошлые реплики не меняются: имя меняется только у будущих."""
        self.session.add_post("Старое Имя", "r1", "текст", 1)
        payload = cast_payload(self.session)
        payload[0]["display_name"] = "Новое Имя"
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.assertEqual(self.session.posts[0]["display_name"], "Старое Имя")


# ---------------------------------------------------------------- живая реплика

class TestInstanceFiles(unittest.TestCase):
    """Экземпляр спектакля: свой порт — своя папка с временными файлами.

    Раньше эти файлы лежали россыпью в корне проекта, и два запущенных театра
    (режиссёрский на 5000 и проверочный на другом порту) читали и писали один
    и тот же файл: один затирал другому сцену и стенограмму, а выглядело это
    как «настройки сами сбросились».
    """

    def setUp(self):
        # Имена файлов и порт — общие на весь процесс: после проверки возвращаем
        for name in ("PORT", "SETTINGS_FILE", "DUMP_FILE", "VRAM_MEASUREMENTS_FILE"):
            self.addCleanup(setattr, settings, name, getattr(settings, name))

    def test_the_port_comes_from_the_command_line(self):
        self.assertEqual(web_app.port_from_argv(["5001"]), 5001)
        self.assertEqual(web_app.port_from_argv(["--port", "5002"]), 5002)
        self.assertEqual(web_app.port_from_argv(["--port=5003"]), 5003)
        self.assertEqual(web_app.port_from_argv(["-p", "5004"]), 5004)
        # Без аргумента — прежний адрес: привычный `py .` ведёт туда же, куда вёл
        self.assertEqual(web_app.port_from_argv([]), settings.PORT)
        self.assertEqual(web_app.port_from_argv(["tests", "test_theatre.py"]),
                         settings.PORT, "в обычных запусках цифр нет — порт остаётся прежним")
        self.assertEqual(web_app.port_from_argv(["99999"]), settings.PORT,
                         "такого порта не бывает — брать его молча нельзя")

    def test_each_port_gets_its_own_folder(self):
        settings.use_instance(5000)
        one = (settings.SETTINGS_FILE, settings.DUMP_FILE, settings.VRAM_MEASUREMENTS_FILE)
        settings.use_instance(5077)
        two = (settings.SETTINGS_FILE, settings.DUMP_FILE, settings.VRAM_MEASUREMENTS_FILE)

        for before, after in zip(one, two):
            self.assertNotEqual(before, after, "у двух экземпляров не может быть одного файла")
        self.assertEqual(one[0].parent.name, "port-5000")
        self.assertEqual(two[0].parent.name, "port-5077")
        self.assertEqual(two[0].parent.parent, settings.INSTANCE_ROOT,
                         "все папки экземпляров живут под одним корнем, а не в корне проекта")
        self.assertEqual(settings.INSTANCE_ROOT, settings.PROJECT_ROOT / ".theatre",
                         "временные файлы живут в подпапке, а не россыпью в корне проекта")
        self.assertNotEqual(one[0].parent, settings.PROJECT_ROOT)

    def test_the_old_files_move_into_the_instance_folder(self):
        """Файлы прежних запусков переносятся в папку экземпляра.

        Иначе после этого нововведения правила судьи и стенограмма выглядели бы
        потерянными — а они никуда не делись, просто лежат теперь рядом.
        """
        root = Path(tempfile.mkdtemp())
        (root / ".theatre_settings.json").write_text(
            json.dumps({"judge_rules": ["правило"]}), encoding="utf-8")
        (root / ".theatre_thinking.md").write_text("хронология прежнего спектакля",
                                                    encoding="utf-8")

        with mock.patch.object(settings, "PROJECT_ROOT", root), \
                mock.patch.object(settings, "INSTANCE_ROOT", root / ".theatre"):
            folder, moved = settings.prepare_instance(5001)
            saved = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
            transcript = (folder / "damp.md").read_text(encoding="utf-8")

        self.assertIn(".theatre_settings.json", moved)
        self.assertIn(".theatre_thinking.md", moved)
        self.assertEqual(saved["judge_rules"], ["правило"])
        self.assertEqual(transcript, "хронология прежнего спектакля")
        self.assertEqual(settings.SETTINGS_FILE, folder / "settings.json",
                         "сцена читается из папки экземпляра, а не из корня")
        self.assertTrue((root / ".theatre_settings.json").exists(),
                        "переносим копированием: старый файл остаётся на месте")

    def test_a_run_does_not_overwrite_what_the_instance_already_has(self):
        """Своё важнее старого: уже перенесённое второй раз не переписывается."""
        root = Path(tempfile.mkdtemp())
        (root / ".theatre_settings.json").write_text(
            json.dumps({"judge_rules": ["старое"]}), encoding="utf-8")

        with mock.patch.object(settings, "PROJECT_ROOT", root), \
                mock.patch.object(settings, "INSTANCE_ROOT", root / ".theatre"):
            folder, _ = settings.prepare_instance(5000)
            (folder / "settings.json").write_text(
                json.dumps({"judge_rules": ["своё"]}), encoding="utf-8")
            settings.prepare_instance(5000)
            kept = json.loads((folder / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(kept["judge_rules"], ["своё"])

    def test_the_scene_is_read_after_the_instance_folder_is_ready(self):
        """Сцена и правила судьи читаются уже из папки экземпляра.

        Раньше они читались при импорте, то есть до того, как станет известно,
        на каком порту играет этот театр. С переносом файлов в папку это обернулось
        бы потерей настройки: файл лежит в новом месте, а чтение смотрит в корень.
        """
        show_source = Path(show.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^load_theatre_settings\(\)", show_source, re.M),
                          "чтение при импорте вернулось — оно смотрит не туда")
        web_source = Path(web_app.__file__).read_text(encoding="utf-8")
        self.assertLess(web_source.index("settings.prepare_instance(port)"),
                        web_source.index("show.load_theatre_settings()"),
                        "папка экземпляра должна заводиться раньше чтения сцены")

    def test_a_busy_port_is_not_taken_silently(self):
        """Занятый порт обязан быть виден.

        Windows разрешает двум процессам занять один порт: werkzeug ставит
        SO_REUSEADDR, а с ним занятый порт выглядит свободным — и второй `py .`
        садится рядом молча. Проверка ставится без SO_REUSEADDR именно затем,
        чтобы такой порт был виден.
        """
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        busy = holder.getsockname()[1]

        self.assertTrue(web_app.port_is_busy(busy),
                        "занятый так же, как его занимает сервер, порт должен быть виден")

    def test_the_holder_of_the_port_is_named_by_numbers(self):
        """Кто держит порт — театр называет сам, номером процесса.

        Разбор только по числам и слову LISTENING: netstat отвечает в OEM-кодировке,
        и русские заголовки от неё портятся, а числа — нет. И порт сверяется
        по концу адреса: 5000 есть и в порту 50001, и чужая строка не имеет
        права выдать себя за нашу.
        """
        netstat = (
            "Активные подключения\n\n"
            "  Имя    Локальный адрес        Внешний адрес          Состояние       PID\n"
            "  TCP    0.0.0.0:5000           0.0.0.0:0              LISTENING       33088\n"
            "  TCP    0.0.0.0:5000           0.0.0.0:0              LISTENING       21388\n"
            "  TCP    0.0.0.0:50001          0.0.0.0:0              LISTENING       777\n"
            "  TCP    127.0.0.1:5000         127.0.0.1:52100        ESTABLISHED     33088\n"
            "  TCP    0.0.0.0:11434          0.0.0.0:0              LISTENING       4242\n"
        )
        self.assertEqual(web_app.port_holders(5000, netstat), [33088, 21388],
                         "свой порт — по концу адреса, чужие порты и состояния мимо")
        self.assertEqual(web_app.port_holders(11434, netstat), [4242])
        self.assertEqual(web_app.port_holders(5300, netstat), [],
                         "никто не держит — пустой список, а не выдуманный номер")
        self.assertEqual(web_app.port_holders(5000, "netstat не нашёлся"), [],
                         "мусор на входе — тоже пустой список")

    def test_a_busy_port_is_reported_with_the_process_numbers(self):
        """О занятом порте говорят, и говорят с номерами — но порт не меняют.

        Порт остаётся тем, что попросили: `py .` обязан вести на 5000, другой
        назначается только аргументом — иначе поведение перестаёт быть
        предсказуемым. А сказать надо вот что: соединения отдаются тому, кто
        занял порт раньше, поэтому второй экземпляр жив, но недостижим.
        """
        notice = "\n".join(web_app.port_conflict_notice(5000, [33088, 21388]))

        self.assertIn("5000", notice,
                      "порт должен быть назван — он же будет в адресе браузера")
        for pid in (33088, 21388):
            self.assertIn(f"taskkill /PID {pid} /F", notice,
                          "команда должна быть готова к вставке в консоль Windows")
        self.assertIn("недостижим", notice,
                      "человек должен понять, почему его новый спектакль никто не увидит")
        # Номера процессов узнаются не всегда: тогда обязан быть способ их найти
        blind = "\n".join(web_app.port_conflict_notice(5000, []))
        self.assertIn("netstat -ano | findstr :5000", blind,
                      "без номеров надо дать команду, которой их ищут")

    def test_the_server_plays_on_the_port_it_was_given(self):
        """Порт из аргумента доходит и до сервера, и до браузера, и до файлов."""
        source = Path(web_app.__file__).read_text(encoding="utf-8")
        self.assertIn("port = port_from_argv(sys.argv[1:])", source)
        self.assertIn("settings.prepare_instance(port)", source,
                      "папка экземпляра должна заводиться до чтения настроек")
        self.assertIn("socketio.run(app, host='0.0.0.0', port=port", source)
        self.assertIn("webbrowser.open(f'http://localhost:{port}')", source,
                      "браузер должен открывать тот же порт, что и сервер")
        self.assertNotIn("free_port", source,
                         "порт не подменяется сам: он должен быть тем, что попросили")


class TestStreamingReply(unittest.TestCase):
    """Черновик реплики: то, что модель говорит прямо сейчас.

    Черновик — не пост. Он не попадает в ленту вместе с постами и не остаётся
    в памяти спектакля: иначе следующая модель прочитала бы полреплики как уже
    сказанное. И он обязан закрыться, чем бы ход ни кончился.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.drafts = []
        self.saved_interval = show._StreamingReply.INTERVAL
        # В тесте ждать нечего: порции должны доходить целиком
        show._StreamingReply.INTERVAL = 0
        self.addCleanup(setattr, show._StreamingReply, "INTERVAL", self.saved_interval)

        def answer(model, messages, participant_name, **kwargs):
            feed = kwargs.get("on_delta")
            if feed is not None:
                feed("Первая половина.", True)
                feed(" Вторая.", False)
            return f"Реплика от {participant_name}.", 0, []

        for name, value in (
            ("check_models_available",
             mock.Mock(return_value={"ok": True, "missing": [], "error": None})),
            ("unload_model", mock.Mock()),
            ("ask_model", mock.Mock(side_effect=answer)),
        ):
            patcher = mock.patch.object(ollama_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _participant() -> dict:
        """Свой участник, а не из файла настроек: тест не должен зависеть от состава."""
        return {"display_name": "Проверка", "model": "fake-model", "gender": "male"}

    def test_the_growing_reply_stays_out_of_the_feed(self):
        participant = self._participant()
        self.session.handle_ai_turn(participant, 1, on_draft=self.drafts.append)

        grown = [d for d in self.drafts if d.get("content")]
        self.assertTrue(grown, "черновик не показывался")
        self.assertEqual(grown[-1]["content"], "Первая половина. Вторая.")
        self.assertEqual(grown[-1]["display_name"], participant["display_name"])
        self.assertEqual(grown[-1]["role_icon"], "🎭", "вид у черновика как у настоящего поста")
        self.assertTrue(any(d.get("done") for d in self.drafts),
                        "ход кончился — черновик должен уступить место посту")
        # В ленте только настоящий пост: полреплики в историю не попадает
        self.assertEqual(len(self.session.posts), 1)
        self.assertIn(f"Реплика от {participant['display_name']}.", self.session.posts[0]["content"])

    def test_the_clock_of_the_turn_is_stopped_when_the_turn_ends(self):
        """Часы хода гаснут вместе с ходом: иначе сайдбар показывал бы чужое время.

        Ход заводит их сам (см. ollama_api.ask_model), а гасить их обязан тот,
        кто ведёт ход: после реплики «думает 2 мин» про модель, которая давно
        замолчала, — это то же враньё, что «спектакль закрыт» до его начала.
        """
        def answer(model, messages, participant_name, **kwargs):
            # Так это и происходит: часы заводит ход, а не зритель
            self.session.start_turn_clock(120, time.monotonic() + 120)
            return "Реплика.", 0, []

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(self._participant(), 1)

        self.assertIsNone(self.session.turn_elapsed(), "ход кончился — часы должны встать")
        self.assertIsNone(self.session.turn_left())

    def test_a_draft_is_closed_even_when_the_turn_fails(self):
        """Ход сорвался — черновик всё равно должен уйти из ленты.

        Иначе недописанная реплика так и осталась бы висеть над настоящими:
        модель уже замолчала, а текст на экране говорит, что она ещё говорит.
        """
        def explode(model, messages, participant_name, **kwargs):
            kwargs["on_delta"]("Начал говорить", True)
            raise RuntimeError("шлюз упал")

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=explode)):
            with self.assertRaises(RuntimeError):
                self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)

        self.assertTrue(any(d.get("done") for d in self.drafts),
                        "сорванный ход обязан закрыть черновик")

    def test_the_thoughts_arrive_before_the_reply(self):
        """Мысли видно, пока модель думает, и в реплику они не попадают."""
        def answer(model, messages, participant_name, **kwargs):
            # Как настоящий ask_model: порцию мыслей кладём и в журнал хода (там
            # её видят лента и ДАМП), и в черновик ленты. Без журнала проверка шла
            # бы мимо того места, где куски одной мысли размножались на сотни копий
            report = kwargs["report"]
            for piece, replace in (("Думаю о теме...", True),
                                   (" и вот что решил.", False)):
                cloud.journal_thought(report, piece, replace)
                kwargs["on_thought"](piece, replace)
            kwargs["on_delta"]("Вот ответ.", True)
            return "Вот ответ.", 0, []

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)

        thought_drafts = [d for d in self.drafts if d.get("thinking")]
        self.assertTrue(thought_drafts, "мысли не показывались")
        self.assertEqual(thought_drafts[0]["thinking"], "Думаю о теме...")
        self.assertFalse(thought_drafts[0]["answer_started"], "пока это только мысли")
        self.assertEqual(thought_drafts[-1]["thinking"], "Думаю о теме... и вот что решил.")
        self.assertTrue(thought_drafts[-1]["answer_started"],
                        "сказано слово — подпись должна стать честной")
        self.assertEqual(self.session.posts[0]["content"], "Вот ответ.")
        self.assertNotIn("Думаю", self.session.posts[0]["content"],
                         "в реплику мыслм не попадают")
        self.assertNotIn("Думаю", self.session.conversation_history[0]["content"])
        # А вот в самом посте они остаются — свёрнутым блоком: лента живёт, пока
        # идёт ход, и без этого мысли исчезали бы вместе с черновиком
        self.assertEqual(self.session.posts[0]["thinking"],
                         "Думаю о теме... и вот что решил.")
        # Одна мысль — одно событие хронологии, сколько бы кусков в неё ни пришло.
        # Из жизни: куски одной мысли клались в журнал каждый сам по себе, и у одной
        # реплики в ленте стояло 707 её копий — с одним и тем же временем
        post = self.session.posts[0]
        thoughts = [step for step in self.session.turn_report(post["id"])["steps"]
                    if step.get("kind") == "thought"]
        self.assertEqual(len(thoughts), 1,
                         "куски одной мысли — одно событие, а не по событию на кусок")
        self.assertEqual(thoughts[0]["text"], "Думаю о теме... и вот что решил.",
                         "куски должны быть в той же записи, а не потеряться")
        self.assertEqual(post["turn"]["thought_steps"], 1,
                         "по этому числу режиссёр читает сводку — врать оно не должно")

    def test_the_dump_keeps_the_whole_turn(self):
        """ДАМП хранит весь ход: тему, кто говорил, размышления и саму реплику.

        Раньше это было в двух местах и в двух видах: мысли моделей —
        в стенограмме (без реплик), а «что уехало в модель» — только в памяти
        и только у последних ходов. Разобраться по отрывкам было нельзя.
        """
        folder = Path(tempfile.mkdtemp())
        dump = folder / "damp.md"

        def answer(model, messages, participant_name, **kwargs):
            kwargs["on_thought"]("Сначала взвешу доводы.", True)
            kwargs["on_delta"]("Вот ответ.", True)
            return "Вот ответ.", 0, []

        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("Проверочная тема")
            with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
                self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)
            written = dump.read_text(encoding="utf-8")

        self.assertIn("Проверочная тема", written, "в ДАМПе должна быть тема")
        self.assertIn("Сначала взвешу доводы.", written)
        self.assertIn(self._participant()["display_name"], written)
        self.assertIn("Вот ответ.", written, "в ДАМПе ход заканчивается репликой")
        self.assertIn("### 📨 Первый запрос к модели целиком", written,
                      "виден и вход, а не только выход")
        self.assertIn("### 💬 Реплика, которой ход кончился", written)

    def test_the_rewritten_reply_is_kept_as_a_sketch(self):
        """Вторая версия реплики — главная, первая остаётся наброском.

        Так выглядит каждый ход с поиском: сперва модель отвечает сама, потом её
        просят поискать, и она отвечает заново. Раньше первая версия исчезала
        из ленты на глазах зрителя, хотя уже была прочитана и оплачена.
        """
        def answer(model, messages, participant_name, **kwargs):
            feed = kwargs["on_delta"]
            feed("Первый ответ.", True)      # сказала сама
            feed("Второй ответ.", True)      # сказала заново, после поиска
            return "Второй ответ.", 1, ["проверочный запрос"]

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)

        kept = [d for d in self.drafts if d.get("sketch")]
        self.assertTrue(kept, "сметённая реплика пропала из ленты, как раньше")
        self.assertEqual(kept[0]["sketch"], "Первый ответ.")
        self.assertEqual(kept[-1]["content"], "Второй ответ.")

        post = self.session.posts[0]
        self.assertEqual(post["content"], "Второй ответ.")
        self.assertEqual(post["sketch"], "Первый ответ.",
                         "в готовом посте прежняя версия должна остаться")

    def test_the_sketch_stays_out_of_the_memory_of_the_play(self):
        """Набросок — не сказанное вслух: в память спектакля он не идёт.

        Иначе следующая модель прочитает отброшенную версию как реплику,
        которая уже прозвучала, — и ответит на неё, а не на спектакль.
        """
        def answer(model, messages, participant_name, **kwargs):
            feed = kwargs["on_delta"]
            feed("Первый ответ.", True)
            feed("Второй ответ.", True)
            return "Второй ответ.", 1, ["проверочный запрос"]

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)

        self.assertEqual(self.session.conversation_history[0]["content"], "Второй ответ.")

    def test_the_draft_carries_live_markup(self):
        """Разметку черновик получает сразу: жирное слово не ждёт конца хода."""
        def answer(model, messages, participant_name, **kwargs):
            kwargs["on_delta"]("**Жирное** слово.", True)
            return "**Жирное** слово.", 0, []

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)

        marked = [d for d in self.drafts if d.get("content_html")]
        self.assertTrue(marked, "черновик шёл без разметки — придётся ждать конца хода")
        self.assertIn("<strong>Жирное</strong>", marked[-1]["content_html"])
        # И простой текст рядом: он нужен тем местам, где HTML не подходит
        self.assertEqual(marked[-1]["content"], "**Жирное** слово.")

    def test_a_search_asked_in_words_never_reaches_the_feed(self):
        """Просьба о поиске, написанная текстом, — не реплика.

        В настоящей ленте такие посты и появлялись: «search:web_search{query: …}»
        стоял посреди реплики, а «Источников» у поста не было вовсе — поиск
        по такой просьбе не выполнялся (см. tooltext и ollama_api).
        """
        def answer(model, messages, participant_name, **kwargs):
            feed = kwargs["on_delta"]
            feed('Сейчас поищу.\n\nsearch:web_search{query: "Сыктывкар новости"}', True)
            feed("Вот что нашлось.", True)      # ответила заново, уже после поиска
            return "Вот что нашлось.", 1, ["Сыктывкар новости"]

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)

        shown = " ".join(d.get("content") or "" for d in self.drafts)
        self.assertNotIn("web_search", shown, "сырой вызов уехал в ленту")
        self.assertNotIn("query:", shown)
        grown = [d for d in self.drafts if d.get("content")]
        self.assertEqual(grown[-1]["content"], "Вот что нашлось.")

        # И в наброске тоже: в нём оседает прежняя версия реплики, а вызов —
        # не версия реплики, а просьба
        kept = [d for d in self.drafts if d.get("sketch")]
        self.assertTrue(kept, "сметённая реплика пропала")
        self.assertNotIn("web_search", kept[0]["sketch"])

    def test_the_dump_is_written_anew_for_each_show(self):
        """В ДАМПе — только последний спектакль: его и разбирают, а не прошлые.

        Стенограмма когда-то росла вечно и чистилась «последними двадцатью
        спектаклями», которых никто и не открывал. Теперь файл переписывается,
        и в нём всегда ровно один спектакль.
        """
        dump = Path(tempfile.mkdtemp()) / "damp.md"
        dump.write_text("# ДАМП · 01.09.2026 12:00 · прежняя тема\n\nход прежнего спектакля\n",
                        encoding="utf-8")

        # Именно через начало спектакля: перезапись должна быть встроена в него,
        # а не жить отдельной функцией, которую никто не зовёт
        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("новая тема")
            written = dump.read_text(encoding="utf-8")

        self.assertNotIn("прежняя тема", written, "прежний спектакль должен быть забыт")
        self.assertNotIn("ход прежнего спектакля", written)
        self.assertIn("новая тема", written)
        self.assertIn("ДАМП", written)

    def test_the_sketch_lays_down_in_the_transcript(self):
        """Прежнюю версию реплики можно прочитать и после занавеса.

        Мыслей у модели могло и не быть, но набросок есть — и в стенограмме
        он должен оказаться и без них.
        """
        folder = Path(tempfile.mkdtemp())
        dump = folder / "damp.md"

        def answer(model, messages, participant_name, **kwargs):
            feed = kwargs["on_delta"]
            feed("Первый ответ.", True)
            feed("Второй ответ.", True)
            return "Второй ответ.", 1, ["проверочный запрос"]

        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("Проверочная тема")
            with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
                self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)
            written = dump.read_text(encoding="utf-8")

        self.assertIn("Первый ответ.", written)
        self.assertIn("Сказано раньше", written)

    def test_a_turn_without_thoughts_still_gets_into_the_dump(self):
        """Местные модели не размышляют — но их ход всё равно должен быть виден.

        Раньше без мыслей в стенограмму не писалось ничего: разбирать потом
        было нечего, хотя ход был и токены за него платились.
        """
        dump = Path(tempfile.mkdtemp()) / "damp.md"

        def answer(model, messages, participant_name, **kwargs):
            feed = kwargs["on_delta"]
            feed("Вот ответ.", True)
            return "Вот ответ.", 0, []

        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("Проверочная тема")
            with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
                self.session.handle_ai_turn(self._participant(), 1, on_draft=self.drafts.append)
            written = dump.read_text(encoding="utf-8")

        self.assertIn("Вот ответ.", written)
        self.assertNotIn("### Размышления", written,
                         "размышлений не было — и раздела быть не должно")

    def test_only_the_tail_of_the_thoughts_is_shown(self):
        """Тысяча знаков размышлений в ленте не нужна — только хвост."""
        reply = show._StreamingReply(self._participant(), 1, lambda draft: None)
        reply.think("начало " + "а" * 3000 + " самый конец мысли", True)

        tail = reply.thought_tail()
        self.assertLessEqual(len(tail), show._StreamingReply.THOUGHT_SHOWN + 1)
        self.assertTrue(tail.startswith("…"), "обрезанное должно быть видно")
        self.assertIn("конец мысли", tail, "хвост — это то, что рядом с ответом")

        brief = show._StreamingReply(self._participant(), 1, lambda draft: None)
        brief.think("короткая мысль", True)
        self.assertEqual(brief.thought_tail(), "короткая мысль")

        # А для поста храним не хвост, а всё: после спектакля обрезать нечего
        self.assertTrue(reply.thinking_full().startswith("начало"))
        self.assertIn("самый конец мысли", reply.thinking_full())

    def test_pieces_do_not_flood_the_feed(self):
        """Часть шлюзов печатает по букве: на таком потоке лента бы захлебнулась."""
        show._StreamingReply.INTERVAL = 5      # пауза больше любой порции
        sent = []
        reply = show._StreamingReply(self._participant(), 1, sent.append)

        reply.feed("Реплика пишется", True)
        reply.feed(" и ещё немного")
        reply.feed(" и вот так")

        self.assertEqual(len(sent), 1, "частые порции — одна отправка, а не три")
        self.assertEqual(sent[0]["content"], "Реплика пишется")
        reply.finish()
        self.assertTrue(sent[-1].get("done"))


# ---------------------------------------------------------------- сцена

class TestSceneEditing(unittest.TestCase):
    """Сцена собирается из пульта: добавить, убрать, переставить, сменить роль.

    Место в составе опознаётся по cast_id, а не по номеру. Это главное в правке
    состава: по номерам личная инструкция уехала бы к соседу, как только места
    переставят, — и никто бы этого не заметил до странной реплики на сцене.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        for name, value in (
            ("check_models_available",
             mock.Mock(return_value={"ok": True, "missing": [], "error": None})),
            ("unload_model", mock.Mock()),
            ("ask_model", mock.Mock(side_effect=self._answer)),
        ):
            patcher = mock.patch.object(ollama_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _answer(self, model, messages, participant_name, **kwargs):
        return f"Реплика от {participant_name}.", 0, []

    def _apply(self, payload):
        self.assertEqual(show.apply_cast_patch(payload), "")

    def test_a_new_place_arrives_with_name_emoji_and_profession(self):
        payload = cast_payload(self.session)
        was = {p["display_name"] for p in self.session.runtime_participants}
        payload.append({"model": "r1", "role": "participant"})
        self._apply(payload)

        cast = self.session.runtime_participants
        self.assertEqual(len(cast), len(payload))
        new_place = cast[-1]
        self.assertTrue(new_place["display_name"].strip())
        self.assertNotIn(new_place["display_name"], was, "двух одинаковых имён быть не должно")
        self.assertTrue(new_place["avatar_emoji"])
        self.assertTrue(new_place["avatar_keywords"].strip())
        self.assertEqual(new_place["model"], "r1")

    def test_a_removed_place_leaves_the_cast(self):
        payload = cast_payload(self.session)
        gone = payload[1]
        payload.remove(gone)
        self._apply(payload)

        names = [p["display_name"] for p in self.session.runtime_participants]
        self.assertNotIn(gone["display_name"], names)
        self.assertEqual(len(names), len(payload))

    def test_reordering_keeps_personal_settings_with_their_place(self):
        """Сердце правки состава: переставили места — настройки переехали с ними."""
        first = self.session.runtime_participants[0]
        first["instruction"] = "ЛИЧНАЯ ИНСТРУКЦИЯ ПЕРВОГО"
        first["temperature"] = 1.11
        order_before = [p["display_name"] for p in self.session.runtime_participants]
        payload = cast_payload(self.session)
        payload.reverse()
        self._apply(payload)

        cast = self.session.runtime_participants
        self.assertEqual([p["display_name"] for p in cast],
                         list(reversed(order_before)),
                         "места должны встать в том порядке, в котором их прислал пульт")
        keeper = next(p for p in cast if p["display_name"] == first["display_name"])
        self.assertEqual(keeper["instruction"], "ЛИЧНАЯ ИНСТРУКЦИЯ ПЕРВОГО")
        self.assertEqual(keeper["temperature"], 1.11)

    def test_role_is_one_per_place(self):
        """Бывший судья, став модератором, судьёй быть перестаёт."""
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        payload = cast_payload(self.session)
        payload[judge_index]["role"] = "moderator"
        self._apply(payload)

        place = self.session.runtime_participants[judge_index]
        self.assertTrue(place["is_moderator"])
        self.assertFalse(place["is_judge"], "две роли сразу — это уже не роль")
        self.assertEqual(show.cast_role(place), "moderator")

    def test_the_judge_rules_travel_with_the_role(self):
        """Правила судьи ставит сама роль: судьёй стал — получил их, перестал — отдал."""
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        self.assertEqual(self.session.runtime_participants[judge_index]["instruction"].strip(),
                         settings.DEFAULT_JUDGE_INSTRUCTION.strip())

        payload = cast_payload(self.session)
        payload[judge_index]["role"] = "participant"
        payload[0]["role"] = "judge"
        self._apply(payload)

        cast = self.session.runtime_participants
        self.assertEqual(cast[judge_index]["instruction"], "",
                         "бывший судья унёс с собой свои правила")
        self.assertTrue(cast[0]["instruction"].strip(), "новому судье нужны его правила")

    def test_a_hand_written_instruction_survives_a_role_change(self):
        """Инструкцию, написанную руками, роль не трогает — даже у судьи."""
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        self.session.runtime_participants[judge_index]["instruction"] = "МОЯ ЛИЧНАЯ ИНСТРУКЦИЯ"

        payload = cast_payload(self.session)
        payload[judge_index]["role"] = "participant"
        self._apply(payload)

        self.assertEqual(self.session.runtime_participants[judge_index]["instruction"],
                         "МОЯ ЛИЧНАЯ ИНСТРУКЦИЯ")

    def test_any_place_can_become_the_judge(self):
        payload = cast_payload(self.session)
        for item in payload:
            item["role"] = "participant"
        payload[0]["role"] = "judge"
        self._apply(payload)

        cast = self.session.runtime_participants
        self.assertEqual([show.cast_role(p) for p in cast].count("judge"), 1)
        prompt = self.session.get_system_prompt(cast[0])
        self.assertIn(settings.DEFAULT_JUDGE_INSTRUCTION.strip()[:30], prompt,
                      "судья без правил судьи — это уже не судья")

    def test_the_scene_stores_places_without_names(self):
        """Имена «Новый спектакль» разыгрывает заново, а инструкции — у места."""
        captain = self.session.runtime_participants[0]["display_name"]
        self.session.runtime_participants[0]["instruction"] = "Держись образа капитана"
        payload = cast_payload(self.session)
        payload.reverse()
        self._apply(payload)

        scene = self.session.scene
        self.assertEqual([place["role"] for place in scene],
                         [show.cast_role(p) for p in self.session.runtime_participants])
        for place in scene:
            self.assertNotIn("display_name", place)
            self.assertNotIn("avatar_emoji", place)
        # Инструкция — режиссёрская настройка, а не свойство случайного имени:
        # место с ней уезжает в сцену и переживает новый состав
        where = next(i for i, p in enumerate(self.session.runtime_participants)
                     if p["display_name"] == captain)
        self.assertEqual(scene[where]["instruction"], "Держись образа капитана")

    def test_a_new_show_keeps_the_topic(self):
        """Тема — режиссёрская настройка: её сбрасывает полный сброс, а не новый состав."""
        self.session.topic = "Кто виноват в пробках"

        self.session.new_show()

        self.assertEqual(self.session.topic, "Кто виноват в пробках")

    def test_a_new_show_keeps_personal_instructions(self):
        """Инструкция — часть места, а не имени: переименование её не теряет."""
        self.session.runtime_participants[0]["instruction"] = "Держись образа капитана"
        self.session.scene = show.scene_from_cast(self.session.runtime_participants)

        self.session.new_show()

        self.assertEqual(self.session.runtime_participants[0]["instruction"],
                         "Держись образа капитана")

    def test_a_model_moderator_is_marked_as_one_in_the_feed(self):
        """Модератором можно назначить и модель — в ленте это должно быть видно."""
        person = self.session.runtime_participants[0]
        show.set_cast_role(person, "moderator")
        self.session.handle_ai_turn(person, 1)
        self.assertEqual(self.session.posts[-1]["role"], "moderator")

    def test_new_show_keeps_the_scene_and_makes_new_names(self):
        """«Новый спектакль» меняет имена, но не роли, порядок и число мест."""
        payload = cast_payload(self.session)[1:]
        payload[0]["role"] = "moderator"
        payload[0]["model"] = "другая-модель"
        self._apply(payload)

        self.session.new_show()

        cast = self.session.runtime_participants
        self.assertEqual(len(cast), len(payload), "убранное место вернулось на сцену")
        self.assertEqual(show.cast_role(cast[0]), "moderator")
        self.assertEqual(cast[0]["model"], "другая-модель")

    def test_a_place_appears_with_a_model_even_in_an_empty_cast(self):
        """Состав можно собрать с нуля: соседа нет — берётся первая скачанная модель.

        Иначе место появлялось бы без модели, и его пришлось бы выбирать руками `
        в единственном случае, когда подсказать некому.
        """
        with mock.patch.object(settings, "PARTICIPANTS", []):
            self.session.load_new_cast()
            self.assertEqual(self.session.runtime_participants, [])
            with mock.patch.object(ollama_api, "fetch_ollama_models",
                                   mock.Mock(return_value=(
                                       {"zz-модель": {}, "aa-модель": {}}, None))):
                draft = show.draft_cast_entry()
        self.assertEqual(draft["model"], "aa-модель")

    def test_without_any_model_the_place_stays_without_one(self):
        """Нет Ollama или ничего не скачано — место без модели, а не выдуманная модель."""
        with mock.patch.object(settings, "PARTICIPANTS", []):
            self.session.load_new_cast()
            with mock.patch.object(ollama_api, "fetch_ollama_models",
                                   mock.Mock(return_value=({}, "Ollama недоступна"))):
                draft = show.draft_cast_entry()
        self.assertEqual(draft["model"], "")
        self.assertTrue(draft["display_name"].strip(), "имя всё равно должно быть")

    def test_the_role_options_are_checked_like_everything_else(self):
        """Значение опции из пульта либо разрешено, либо это ошибка, а не «как-нибудь»."""
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        payload = cast_payload(self.session)
        payload[judge_index]["role_options"] = {"scope": "какой-нибудь"}
        self.assertIn("scope", show.apply_cast_patch(payload))
        self.assertNotEqual(show.role_options(self.session.runtime_participants[judge_index]),
                            {"scope": "какой-нибудь"})

    def test_the_role_options_come_and_go_with_the_role(self):
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        payload = cast_payload(self.session)
        payload[judge_index]["role_options"] = {"scope": "act", "publicity": "public"}
        self._apply(payload)
        self.assertEqual(show.role_options(self.session.runtime_participants[judge_index]),
                         {"scope": "act", "publicity": "public"})

        # Место перестало быть судьёй: опции прежней роли с собой не уносит
        payload = cast_payload(self.session)
        payload[judge_index]["role"] = "participant"
        self._apply(payload)
        self.assertNotIn("role_options", self.session.runtime_participants[judge_index])

        # А новое место-судья получает набор своей роли по умолчанию
        payload = cast_payload(self.session)
        payload[0]["role"] = "judge"
        self._apply(payload)
        self.assertEqual(show.role_options(self.session.runtime_participants[0]),
                         {"scope": "all", "publicity": "anonymous"})

    def test_the_scene_keeps_the_role_options(self):
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        payload = cast_payload(self.session)
        payload[judge_index]["role_options"] = {"scope": "act"}
        self._apply(payload)

        place = self.session.scene[judge_index]
        self.assertEqual(place["role_options"], {"scope": "act", "publicity": "anonymous"},
                         "сцена должна помнить опции судьи целиком")

        self.session.new_show()
        self.assertEqual(show.role_options(self.session.runtime_participants[judge_index]),
                         {"scope": "act", "publicity": "anonymous"})

    def test_the_draft_place_does_not_touch_the_cast(self):
        before = [p["display_name"] for p in self.session.runtime_participants]
        draft = show.draft_cast_entry()

        self.assertTrue(draft["model"], "новому месту нужна модель — как у соседа")
        self.assertFalse(draft["is_judge"])
        self.assertFalse(draft["is_moderator"])
        for key in ("cast_id", "display_name", "avatar_emoji", "avatar_keywords", "gender"):
            self.assertIn(key, draft)
        self.assertEqual([p["display_name"] for p in self.session.runtime_participants], before)


# ---------------------------------------------------------------- опции роли

class TestRoleOptions(unittest.TestCase):
    """Опции роли судьи: что слышит он и кто слышит его.

    Обе — положение, а не выключатель: значение есть всегда, и по умолчанию оно
    такое, как спектакль игрался всегда — судья оценивает всё обсуждение,
    а слово его слышит только режиссёр. Поэтому старые сцены и файлы настроек
    ничего не теряют.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.session.topic = "Тема"
        self.judge = judge_of(self.session)
        self.other = non_judge_ai(self.session)
        self.session.conversation_history = [
            self._post("Первый", "МЫСЛЬ ПЕРВОГО АКТА", 1),
            self._post(self.judge["display_name"], "ВЕРДИКТ СУДЬИ", 1, judge=True),
            self._post("Второй", "МЫСЛЬ ВТОРОГО АКТА", 2),
        ]

    def _post(self, name, content, round_num, judge=False):
        return {"display_name": name, "content": content, "is_moderator": False,
                "is_judge": judge, "round": round_num}

    def _prompt_text(self, person, round_num=2):
        return " ".join(message["content"]
                        for message in self.session.build_messages_for_ai(person, round_num))

    def test_by_default_the_judge_sums_up_the_whole_show(self):
        text = self._prompt_text(self.judge)
        self.assertIn("МЫСЛЬ ПЕРВОГО АКТА", text)
        self.assertIn("МЫСЛЬ ВТОРОГО АКТА", text)

    def test_the_judge_can_be_limited_to_the_current_act(self):
        self.judge["role_options"] = {"scope": "act"}
        text = self._prompt_text(self.judge)
        self.assertNotIn("МЫСЛЬ ПЕРВОГО АКТА", text, "прошлый акт судье видеть незачем")
        self.assertIn("МЫСЛЬ ВТОРОГО АКТА", text)
        self.assertIn("только текущий акт", text, "и задача должна быть про этот акт")

    def test_by_default_nobody_hears_the_judge(self):
        self.assertNotIn("ВЕРДИКТ СУДЬИ", self._prompt_text(self.other))

    def test_a_public_judge_is_heard_by_the_others(self):
        self.judge["role_options"] = {"publicity": "public"}
        text = self._prompt_text(self.other)
        self.assertIn("ВЕРДИКТ СУДЬИ", text)
        self.assertIn("СУДЬЯ", text, "участник должен понимать, что это вердикт, а не реплика")

    def test_the_judge_does_not_evaluate_its_own_verdicts(self):
        """Публичность не должна подсовывать судье его же слова как выступление."""
        self.judge["role_options"] = {"publicity": "public"}
        self.assertNotIn("ВЕРДИКТ СУДЬИ", self._prompt_text(self.judge))

    def test_a_missing_or_broken_option_falls_back_to_the_default(self):
        """Файл настроек и старые сцены правятся руками — мусор не должен ломать роль."""
        for broken in ({"scope": "полдень"}, {"кухня": "all"}, "просто строка", None):
            with self.subTest(options=broken):
                self.judge["role_options"] = broken
                self.assertEqual(show.role_options(self.judge),
                                 {"scope": "all", "publicity": "anonymous"})


# ---------------------------------------------------------------- промпт

class TestSystemPrompt(unittest.TestCase):
    """Инструкции, которые правит модератор, обязаны доезжать до модели."""

    def setUp(self):
        self.session = make_session()
        self.session.topic = "Квантовая запутанность"
        strip_session_patch(self, self.session)

    def test_personal_instruction_reaches_the_prompt(self):
        person = plain_participant(self.session)
        person["instruction"] = "Отвечай только цитатами классиков"
        prompt = self.session.get_system_prompt(person)
        self.assertIn("Отвечай только цитатами классиков", prompt)

    def test_moderator_guidelines_reach_the_prompt(self):
        self.session.moderator_guidelines = ["Обсуждаем только математику"]
        prompt = self.session.get_system_prompt(non_judge_ai(self.session))
        self.assertIn("Обсуждаем только математику", prompt)

    def test_the_search_block_names_both_numbers(self):
        """Модель должна знать и минимум поисков, и потолок.

        Раньше о потолке знал только код: модель просила поиск снова и снова,
        ей молча отказывали, и со стороны это выглядело как задумавшаяся модель.
        """
        cloud_setting(self, "MIN_SEARCHES", 2)
        cloud_setting(self, "MAX_SEARCHES", 3)
        # Инструмент этой модели правда отправляется — иначе про поиск говорить
        # нечего (см. test_a_model_without_the_search_tool_is_not_asked_to_search)
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        self.addCleanup(takes_tools(True))
        person = non_judge_ai(self.session)
        prompt = self.session.get_system_prompt(person)

        self.assertIn("минимум 2 поиск", prompt)
        self.assertIn("не больше 3 поиск", prompt)

    def test_a_model_without_the_search_tool_still_knows_how_to_ask(self):
        """Модель без инструмента поиска всё равно может искать — словами.

        Тот самый случай судьи на qwen3.8-flash: в промпте было «сделай минимум
        поиск», а инструмента шлюз ей не дал — модель пыталась искать, не могла
        и две минуты молотила размышления, пока ход не оборвался по
        CLOUD_TURN_LIMIT. Правила и возможности должны совпадать, но запрет тут
        не выход: способ искать у модели есть всегда — просьба словами, её
        узнаёт и выполняет код хода (см. tooltext).
        """
        cloud_setting(self, "MIN_SEARCHES", 2)
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)   # поиск включён…
        self.addCleanup(takes_tools(False))             # …а инструмент не дошёл
        person = non_judge_ai(self.session)
        prompt = self.session.get_system_prompt(person)

        self.assertIn("не выдан протоколом", prompt,
                      "модель должна знать, чем именно искать сейчас не выйдет")
        self.assertIn(self.session.SEARCH_CALL_EXAMPLE, prompt,
                      "но способ искать должен остаться: запрет и был той петлёй")
        self.assertIn("минимум 2 поиск", prompt,
                      "минимум поисков остаётся в силе и для этого способа")

    def test_the_last_reply_does_not_travel_to_the_model_twice(self):
        """Реплика собеседника уходит в модель ровно один раз.

        Тот самый баг из жизни: последняя реплика лежала и в истории диалога,
        и ещё раз целиком — в задании «X только что сказал: "..."». Модель
        читала один и тот же текст дважды, а платили за это как за две реплики.
        """
        listener = non_judge_ai(self.session)
        speaker = next(p for p in self.session.runtime_participants
                       if p.get("model") != "human"
                       and p.get("display_name") != listener.get("display_name"))
        self.session.conversation_history.append({
            "display_name": speaker["display_name"],
            "content": "ЕДИНСТВЕННАЯ РЕПЛИКА СЦЕНЫ",
            "is_moderator": False, "is_judge": False, "round": 1})

        messages = self.session.build_messages_for_ai(listener, 2)
        joined = " ".join(str(m.get("content") or "") for m in messages)

        self.assertEqual(joined.count("ЕДИНСТВЕННАЯ РЕПЛИКА СЦЕНЫ"), 1,
                         "реплика сцены уехала в модель больше одного раза")
        self.assertIn(speaker["display_name"], joined,
                      "но сказать, кто её сказал, всё равно надо")

    def test_judge_uses_its_own_rules(self):
        self.session.judge_rules = ["ПРАВИЛО СУДЬИ ДЛЯ ПРОВЕРКИ"]
        prompt = self.session.get_system_prompt(judge_of(self.session))
        self.assertIn("ПРАВИЛО СУДЬИ ДЛЯ ПРОВЕРКИ", prompt)

    def test_common_rules_are_used_when_editor_is_non_empty(self):
        self.session.static_instructions = ["ОБЩЕЕ ПРАВИЛО ДЛЯ ПРОВЕРКИ"]
        prompt = self.session.get_system_prompt(non_judge_ai(self.session))
        self.assertIn("ОБЩЕЕ ПРАВИЛО ДЛЯ ПРОВЕРКИ", prompt)

    def test_placeholders_are_substituted(self):
        prompt = self.session.get_system_prompt(non_judge_ai(self.session))
        self.assertNotIn("{ТЕМА}", prompt)
        self.assertNotIn("{ИМЯ}", prompt)
        self.assertNotIn("{СОБЕСЕДНИКИ}", prompt)
        self.assertIn("Квантовая запутанность", prompt)

    def test_judge_instruction_is_used_when_editor_is_empty(self):
        judge = judge_of(self.session)
        judge["instruction"] = ""
        prompt = self.session.get_system_prompt(judge)
        self.assertIn(settings.DEFAULT_JUDGE_INSTRUCTION.strip()[:40], prompt)


class TestSettingsPersistence(unittest.TestCase):
    """Правила судьи переживают перезапуск: лежат в файле рядом с проектом."""

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self._patch(settings, "SETTINGS_FILE",
                    Path(self.tmpdir.name) / "settings.json")

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rules_survive_a_restart(self):
        self.session.judge_rules = ["Оценка только по фактам"]
        show.save_theatre_settings()

        self.session.judge_rules = ["временное"]
        show.load_theatre_settings()
        self.assertEqual(self.session.judge_rules, ["Оценка только по фактам"])

    def test_file_is_readable_json(self):
        self.session.judge_rules = ["Пункт 1", "Пункт 2"]
        show.save_theatre_settings()
        data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["judge_rules"], ["Пункт 1", "Пункт 2"])

    def test_new_show_keeps_rules_but_makes_a_new_cast(self):
        """Отвечает на вопрос «куда деваются правила»: новый состав, те же правила."""
        self.session.judge_rules = ["Правило судьи"]
        self.session.moderator_guidelines = ["Руководство"]
        self.session.static_instructions = ["Общее правило"]
        old_names = [p["display_name"] for p in self.session.runtime_participants]

        self.session.new_show()

        self.assertEqual(self.session.judge_rules, ["Правило судьи"])
        self.assertEqual(self.session.moderator_guidelines, ["Руководство"])
        self.assertEqual(self.session.static_instructions, ["Общее правило"])
        self.assertEqual(self.session.posts, [])
        self.assertEqual(len(self.session.runtime_participants), len(old_names))


class TestScenePersistence(unittest.TestCase):
    """Пульт целиком переживает перезапуск: состав — вместе с именами, аватарами
    и личными инструкциями, а правила, руководства и правила судьи — рядом с ним.

    Режиссёр, вернувшись к театру, хочет увидеть прежний спектакль с чистой
    историей, а не тот же состав с другими именами.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        for target, name, value in (
            (settings, "SETTINGS_FILE", Path(self.tmpdir.name) / "settings.json"),
            (ollama_api, "check_models_available",
             mock.Mock(return_value={"ok": True, "missing": [], "error": None})),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_whole_cast_comes_back_after_a_restart(self):
        """Состав возвращается как был: имена, аватары, порядок и инструкции."""
        payload = cast_payload(self.session)
        payload[0]["role"] = "judge"
        payload[1]["role"] = "participant"
        payload[2]["temperature"] = 0.33
        payload[2]["avatar_url"] = "/avatars/капитан.jpg"
        payload.reverse()
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.session.runtime_participants[0]["instruction"] = "Держись образа капитана"
        self.session.sync_cast_media()
        expected = copy.deepcopy(self.session.runtime_participants)
        before = [p["display_name"] for p in expected]
        show.save_theatre_settings()

        # Перезапуск: сессия поднимается с нуля и читает файл
        self.session.runtime_participants = []
        self.session.scene = None
        show.load_theatre_settings()

        loaded = self.session.runtime_participants
        self.assertEqual([p["display_name"] for p in loaded], before,
                         "имена должны вернуться те же — их не разыгрывают заново")
        keys = ("model", "avatar_emoji", "avatar_keywords", "avatar_url", "gender",
                "instruction", "is_judge", "is_moderator", "think", "preset")
        for was, now in zip(expected, loaded):
            for key in keys:
                self.assertEqual(now.get(key), was.get(key),
                                 f"{key} места {was['display_name']} не вернулось")
            for key in settings.PER_PARTICIPANT_OPTION_KEYS:
                self.assertEqual(now.get(key), was.get(key),
                                 f"число {key} места {was['display_name']} не вернулось")
            self.assertEqual(show.role_options(now), show.role_options(was))
        # Сцена — производное от состава: из неё «Новый спектакль» берёт места
        self.assertEqual(self.session.scene, show.scene_from_cast(expected))

    def test_the_whole_console_lives_in_one_file(self):
        """Один файл — весь пульт: состав, правила общения, руководства и судья."""
        self.session.judge_rules = ["Пункт"]
        self.session.static_instructions = ["Общее правило"]
        self.session.moderator_guidelines = ["Руководство"]
        show.save_theatre_settings()

        data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["judge_rules"], ["Пункт"])
        self.assertEqual(data["static_instructions"], ["Общее правило"])
        self.assertEqual(data["moderator_guidelines"], ["Руководство"])
        self.assertEqual([p["display_name"] for p in data["cast"]],
                         [p["display_name"] for p in self.session.runtime_participants])

    def test_the_topic_comes_back_after_a_restart(self):
        """Придумывать тему заново каждый запуск — работа, а не часть спектакля."""
        self.session.topic = "Почему небо синее"
        show.save_theatre_settings()

        self.session.topic = ""
        show.load_theatre_settings()

        self.assertEqual(self.session.topic, "Почему небо синее")

    def test_the_editor_comes_back_after_a_restart(self):
        """Раньше переживали перезапуск только правила судьи — теперь всё."""
        self.session.judge_rules = ["Пункт"]
        self.session.static_instructions = ["Общее правило"]
        self.session.moderator_guidelines = ["Руководство"]
        show.save_theatre_settings()

        self.session.judge_rules = []
        self.session.static_instructions = []
        self.session.moderator_guidelines = []
        show.load_theatre_settings()

        self.assertEqual(self.session.judge_rules, ["Пункт"])
        self.assertEqual(self.session.static_instructions, ["Общее правило"])
        self.assertEqual(self.session.moderator_guidelines, ["Руководство"])

    def test_a_new_show_takes_places_from_the_saved_console(self):
        """Сохранённый состав — материал и для «Нового спектакля»: роли, модели
        и личные инструкции мест переживают и перезапуск, и новые имена."""
        payload = cast_payload(self.session)
        payload[0]["role"] = "judge"
        payload[0]["model"] = "r1"
        self.assertEqual(show.apply_cast_patch(payload), "")
        self.session.runtime_participants[0]["instruction"] = "Строго по фактам"
        roles = [show.cast_role(p) for p in self.session.runtime_participants]
        models = [p["model"] for p in self.session.runtime_participants]
        show.save_theatre_settings()

        self.session.runtime_participants = []
        self.session.scene = None
        show.load_theatre_settings()
        self.session.load_new_cast()

        cast = self.session.runtime_participants
        self.assertEqual([show.cast_role(p) for p in cast], roles)
        self.assertEqual([p["model"] for p in cast], models)
        self.assertEqual(cast[0]["instruction"], "Строго по фактам")

    def test_a_broken_scene_in_the_file_does_not_break_the_show(self):
        """Файл лежит рядом с проектом и правится руками — сцена из него не священна."""
        settings.SETTINGS_FILE.write_text(json.dumps({
            "judge_rules": ["Пункт"],
            "scene": [{"model": "r1", "role": "дворецкий", "temperature": "горячо",
                       "think": "наверное", "preset": "неттакого"},
                      "просто строка"],
        }, ensure_ascii=False), encoding="utf-8")

        show.load_theatre_settings()

        self.assertEqual(len(self.session.scene), 1, "мусор из файла не должен становиться местом")
        self.assertEqual(self.session.scene[0]["role"], "participant")
        for key in ("temperature", "think", "preset"):
            self.assertNotIn(key, self.session.scene[0])


class TestLiveCastChanges(unittest.TestCase):
    """Спектакль читает состав заново: правки на ходу — поведение, а не слова.

    Гоняется настоящий поток спектакля, но Ollama подменена заглушкой, а пауза
    между репликами убрана: в тесте она только тянет время. Занавес опускаем
    из того же колбэка, которым спектакль сообщает о новой реплике.
    """

    def setUp(self):
        self.session = show.DebateSession()
        self.session.scene = [{"model": "r1", "role": "participant"} for _ in range(3)]
        self.session.load_new_cast()
        strip_session_patch(self, self.session)
        self.spoken = []
        self.cast_error = None
        for name, value in (
            ("ask_model", mock.Mock(side_effect=self._answer)),
            ("check_models_available",
             mock.Mock(return_value={"ok": True, "missing": [], "error": None})),
            ("unload_model", mock.Mock()),
            ("unload_other_show_models", mock.Mock()),
        ):
            patcher = mock.patch.object(ollama_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _answer(self, model, messages, participant_name, **kwargs):
        return f"Реплика от {participant_name}.", 0, []

    def _play(self, on_post):
        with mock.patch("time.sleep"):
            show.run_debate_thread("тема", on_post=on_post)

    def test_a_removed_place_does_not_speak(self):
        # Убираем среднее место: тогда занавес опускается уже после того, как
        # убранный успел бы сказать — иначе проверка ничего не стоит
        removed = self.session.runtime_participants[1]

        def on_post(post):
            self.spoken.append(post["display_name"])
            if len(self.spoken) == 1:
                self.cast_error = show.apply_cast_patch(
                    [p for p in cast_payload(self.session)
                     if p["cast_id"] != removed["cast_id"]])
            if len(self.spoken) >= 2:
                self.session.moderator_finished = True

        self._play(on_post)

        self.assertEqual(self.cast_error, "", "состав не изменился — проверять нечего")
        self.assertEqual(len(self.session.runtime_participants), 2)
        self.assertIn(self.session.runtime_participants[-1]["display_name"], self.spoken,
                      "акт должен был пойти дальше, а не встать")
        self.assertNotIn(removed["display_name"], self.spoken,
                         "убранный участник всё равно вышел на сцену")

    def test_a_place_added_mid_act_speaks_in_the_next_act(self):
        self.session.scene = [{"model": "r1", "role": "participant"}]
        self.session.load_new_cast()
        added = []

        def on_post(post):
            self.spoken.append(post["display_name"])
            if len(self.spoken) == 1:
                draft = show.draft_cast_entry()
                added.append(draft["display_name"])
                self.cast_error = show.apply_cast_patch(cast_payload(self.session) + [draft])
            if len(self.spoken) >= 3:
                self.session.moderator_finished = True

        self._play(on_post)

        self.assertEqual(self.cast_error, "")
        self.assertIn(added[0], self.spoken,
                      "новое место так и не вышло на сцену — значит акт играет старый состав")

    def test_the_live_judges_post_is_marked_as_the_judge(self):
        """Судьёй можно назначить и живого человека: в ленте он должен быть судьёй."""
        self.session.scene = [{"model": "human", "role": "judge"}]
        self.session.load_new_cast()

        worker = threading.Thread(target=show.run_debate_thread, args=("тема",),
                                  kwargs={"on_post": None})
        worker.daemon = True
        worker.start()
        deadline = time.time() + 15
        try:
            while not self.session.waiting_for_human and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.session.waiting_for_human,
                            "спектакль так и не дошёл до живой реплики")
            self.session.moderator_message = "Вердикт: 8 из 10"
            while self.session.moderator_message is not None and time.time() < deadline:
                time.sleep(0.05)
        finally:
            self.session.moderator_finished = True
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "спектакль не остановился")
        self.assertEqual(self.session.posts[-1]["role"], "judge")


# ---------------------------------------------------------------- VRAM

class TestVramHelpers(unittest.TestCase):

    INF = {"general.architecture": "llama", "llama.block_count": 32,
           "llama.attention.head_count": 32,
           "llama.attention.head_count_kv": 8,
           "llama.attention.key_length": 128,
           "llama.embedding_length": 4096}

    def _with_model(self, base_bytes, info=None):
        # info={} — валидный случай «модель ничего о себе не рассказала»,
        # поэтому не путаем его с «аргумент не передан»
        info = self.INF if info is None else info
        return [
            mock.patch.object(ollama_api, "resolve_model_name", side_effect=lambda m, a: m),
            mock.patch.object(ollama_api, "fetch_model_info", return_value=info),
            mock.patch.object(ollama_api, "vram_base_bytes",
                              return_value=(base_bytes, 0)),
        ]

    def test_kv_cache_grows_with_context(self):
        small = ollama_api.estimated_kv_bytes(self.INF, 4096)
        large = ollama_api.estimated_kv_bytes(self.INF, 8192)
        self.assertGreater(large, small)
        self.assertEqual(large, small * 2)

    def test_suggested_context_fits_the_budget(self):
        budget = 8 * 10 ** 9
        patchers = self._with_model(4 * 10 ** 9)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        safe, need = ollama_api.suggest_safe_ctx("m1", 16384, budget, {})
        self.assertIsNotNone(safe)
        self.assertLessEqual(need, budget)
        self.assertEqual(safe % settings.VRAM_SAFE_CTX_STEP, 0)
        self.assertLessEqual(safe, 16384)

    def test_no_room_means_no_suggestion(self):
        patchers = self._with_model(8 * 10 ** 9)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        safe, need = ollama_api.suggest_safe_ctx("m1", 16384, 8 * 10 ** 9, {})
        self.assertIsNone(safe)
        self.assertEqual(need, 0)

    def test_unknown_model_is_handled(self):
        patchers = self._with_model(10 ** 9, info={})
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(ollama_api.suggest_safe_ctx("m1", 8192, 8 * 10 ** 9, {}),
                         (None, 0))


# ---------------------------------------------------------------- токены

class TestTokenHelpers(unittest.TestCase):

    def test_estimate_is_positive_for_a_word(self):
        self.assertGreaterEqual(text.estimate_tokens("привет"), 1)
        self.assertGreaterEqual(text.estimate_tokens("hello"), 1)
        self.assertGreaterEqual(text.estimate_tokens(""), 0)

    def test_estimate_grows_with_text(self):
        self.assertGreater(text.estimate_tokens("слово " * 50),
                           text.estimate_tokens("слово"))

    def test_history_is_trimmed_to_fit_context(self):
        messages = [{"role": "user", "content": "длинный текст " * 1200}
                    for _ in range(6)]
        trimmed = text.trim_history_by_tokens(messages, system_prompt_tokens=100)
        self.assertLess(len(trimmed), len(messages),
                        "история должна обрезаться под контекст")

    def test_short_history_is_kept_as_is(self):
        messages = [{"role": "user", "content": "ок"}]
        self.assertEqual(text.trim_history_by_tokens(messages, 10), messages)


class TestCloudContextWindow(unittest.TestCase):
    """Своё окно контекста у облачного участника.

    Историю сцены приложение режет под окно той модели, которая говорит.
    Мерилось оно олламовским: num_ctx 16384 минус 8192 на ответ — около
    7 тысяч токенов, и в логе это было видно как «доступно: 6935». Облачный
    участник из-за этого забывал начало обсуждения, хотя шлюз принял бы
    в разы больше; сцену теперь мерят по CLOUD_NUM_CTX.
    """

    REEL = "реплика сцены "

    def _long_history(self, reels=900, count=2):
        return [{"role": "user", "content": self.REEL * reels}
                for _ in range(count)]

    def _ollama_fit(self):
        """Сколько истории влезало в старое, олламовское окно."""
        return (settings.OPTIONS["num_ctx"] - settings.OPTIONS["num_predict"]
                - settings.CONTEXT_SAFETY_MARGIN - 100)

    def test_cloud_gets_its_own_window(self):
        ctx, _predict = settings.context_budget("cloud:qwen/qwen3.8-flash")
        self.assertEqual(ctx, settings.CLOUD_NUM_CTX)
        self.assertGreater(ctx, settings.OPTIONS["num_ctx"],
                           "облачное окно должно быть шире олламовского")
        self.assertEqual(settings.context_budget("qwen3:8b"),
                         (settings.OPTIONS["num_ctx"], settings.OPTIONS["num_predict"]),
                         "у местных моделей остаётся своё окно")

    def test_cloud_history_is_not_cut_by_the_ollama_measure(self):
        messages = self._long_history()
        total = sum(text.estimate_tokens(m["content"]) for m in messages)
        self.assertGreater(total, self._ollama_fit(),
                           "история должна быть больше старого окна — иначе проверять нечего")
        self.assertEqual(len(text.trim_history_by_tokens(
            messages, 100, model="cloud:qwen/qwen3.8-flash")), len(messages),
            "облачный участник помнит всю сцену")
        self.assertLess(len(text.trim_history_by_tokens(messages, 100, model="qwen3:8b")),
                        len(messages), "местная модель по-прежнему живёт в своём окне")

    def test_cloud_speaker_in_the_show_keeps_the_scene(self):
        session = make_session()
        strip_session_patch(self, session)
        person = non_judge_ai(session)
        session.conversation_history = [
            {"display_name": "Первый", "content": "ПЕРВАЯ-РЕПЛИКА " + self.REEL * 900,
             "is_moderator": False, "is_judge": False, "round": 1},
            {"display_name": "Второй", "content": "ВТОРАЯ-РЕПЛИКА " + self.REEL * 900,
             "is_moderator": False, "is_judge": False, "round": 1},
        ]
        person["model"] = "cloud:qwen/qwen3.8-flash"
        cloud_scene = " ".join(m["content"]
                               for m in session.build_messages_for_ai(person, 1))
        self.assertIn("ПЕРВАЯ-РЕПЛИКА", cloud_scene,
                      "начало сцены должно доехать до облачной модели")
        person["model"] = "qwen3:8b"
        local_scene = " ".join(m["content"]
                               for m in session.build_messages_for_ai(person, 1))
        self.assertNotIn("ПЕРВАЯ-РЕПЛИКА", local_scene,
                         "та же сцена в олламовское окно не влезает")

    def test_zero_window_means_the_whole_scene(self):
        with mock.patch.object(settings, "CLOUD_NUM_CTX", 0):
            messages = self._long_history(reels=900, count=40)
            self.assertEqual(text.trim_history_by_tokens(messages, 100, model="cloud:x/y"),
                             messages, "CLOUD_NUM_CTX = 0 — истории не режут вовсе")

    def test_an_unbounded_window_is_paid_for_at_every_turn(self):
        """CLOUD_NUM_CTX = 0 — это не «память без границ», а счёт без границ.

        Ноль ставят, думая про память модели, а платит за него режиссёр: история
        уезжает целиком не один раз, а на каждом ходу и на каждом круге поиска.
        Поэтому в консоли об этом говорится — и ровно один раз за запуск, иначе
        строка-предупреждение на каждом ходу превратилась бы в шум, который
        перестают читать.
        """
        with mock.patch.object(settings, "CLOUD_NUM_CTX", 0), \
                mock.patch.object(text, "_UNBOUNDED_HISTORY_WARNED", False):
            with mock.patch("builtins.print") as first:
                text.trim_history_by_tokens(self._long_history(reels=50, count=2), 100,
                                            model="cloud:x/y")
            with mock.patch("builtins.print") as second:
                text.trim_history_by_tokens([{"role": "user", "content": "слово"}], 0,
                                            model="cloud:x/y")

        self.assertIn("CLOUD_NUM_CTX", _printed_text(first))
        self.assertIn("оплач", _printed_text(first),
                      "про плату за каждый ход надо сказать словами, а не намёком")
        self.assertNotIn("💸", _printed_text(second),
                         "предупреждение повторяется на каждом ходу")

    def test_window_smaller_than_the_answer_reserve_keeps_the_scene(self):
        # Запас задан положительным нарочно: при нуле (CLOUD_MAX_TOKENS = 0)
        # он и не откладывается вовсе, и узкое окно проверять было бы нечем.
        # Здесь запас (8192) больше самого окна — и историю он съесть не должен
        with mock.patch.object(settings, "CLOUD_NUM_CTX", 4096), \
                mock.patch.object(settings, "CLOUD_MAX_TOKENS", 8192):
            messages = self._long_history(reels=40, count=1)
            self.assertEqual(len(text.trim_history_by_tokens(messages, 10, model="cloud:x/y")),
                             len(messages),
                             "узкое окно не должно отнимать историю целиком")

    def test_the_answer_reserve_of_the_cloud_is_its_own_setting(self):
        """Место под ответ у облака — CLOUD_MAX_TOKENS, а не олламовский предел.

        Иначе «я готов платить за генерацию, но не за историю» словами не
        выражается: и то и другое было одним числом num_predict.
        """
        with mock.patch.object(settings, "CLOUD_MAX_TOKENS", 512):
            window, reserve = settings.context_budget("cloud:qwen/qwen3.8-flash")

        self.assertEqual(window, settings.CLOUD_NUM_CTX)
        self.assertEqual(reserve, 512)

    def test_zero_answer_seat_means_no_limit_at_all(self):
        """CLOUD_MAX_TOKENS = 0 — это ноль, а не олламовское число.

        Так и просил режиссёр: за генерацию ответа платить не жалко, жалко
        за длинную историю, которую гоняют токенами в каждом запросе. Поэтому
        ноль здесь значит «ответ не ограничиваем»: и max_tokens не уходит,
        и место под ответ в окне не откладывается. Иначе местный num_predict
        съедал бы часть облачного окна, и история резалась бы ни за что.
        """
        with mock.patch.object(settings, "CLOUD_MAX_TOKENS", 0), \
                mock.patch.dict(settings.OPTIONS, {"num_predict": 8192}):
            window, reserve = settings.context_budget("cloud:qwen/qwen3.8-flash")
            messages = [{"role": "user", "name": "икс", "content": "реплика " * 50}]
            _trimmed, report = text.trim_history_with_report(
                messages, 100, model="cloud:qwen/qwen3.8-flash")

        self.assertEqual(window, settings.CLOUD_NUM_CTX)
        self.assertEqual(reserve, 0, "ollaмовский предел ответа у облака не при чём")
        self.assertEqual(report["reserve"], 0)
        self.assertEqual(report["available"],
                         settings.CLOUD_NUM_CTX - settings.CONTEXT_SAFETY_MARGIN - 100,
                         "всё окно, кроме технического запаса, отдано истории")

    def test_the_window_lives_in_the_settings_file_not_in_dotenv(self):
        """Окно правится в settings.py, а строка в .env его больше не двигает.

        В .env остаются только ключ и адрес шлюза — данные вашего облачного
        сервиса. Всё остальное про облако должно жить в одном месте: пока
        настройку можно было задать и там, и тут, правящий одно не знал, что
        решает другое.
        """
        with mock.patch.dict(os.environ, {"CLOUD_NUM_CTX": "8192"}):
            self.assertEqual(settings.context_budget("cloud:qwen/qwen3.8-flash")[0],
                             settings.CLOUD_NUM_CTX,
                             "строка из .env больше не меняет окно")
        with mock.patch.object(settings, "CLOUD_NUM_CTX", 8192):
            self.assertEqual(settings.context_budget("cloud:qwen/qwen3.8-flash")[0], 8192,
                             "настройку из settings.py должно быть слышно")

    def test_dotenv_holds_only_the_key_and_the_address(self):
        """В .env читаются ровно две вещи: ключ и адрес шлюза.

        Смотрит в сам код: если облачная настройка снова начнёт читаться из
        окружения, её имя окажется вне CLOUD_ENV_NAMES — и на файл настроек,
        где оно объявлено, станет два хозяина.
        """
        extra = set()
        for filename in (cloud.__file__, settings.__file__):
            source = Path(filename).read_text(encoding="utf-8")
            extra |= set(re.findall(
                r'os\.environ(?:\.get\()?\s*\[?\s*["\'](CLOUD_[A-Z0-9_]+)["\']', source))
        self.assertEqual(extra - set(cloud.CLOUD_ENV_NAMES), set(),
                         f"эти строки читаются из .env, хотя им место в "
                         f"{cloud.CLOUD_SETTINGS_FILE}: {sorted(extra)}")

        # И каждая переехавшая настройка должна быть в файле настроек:
        # подсказка «поставьте её там» без самой настройки — это ложь
        for name in cloud.CLOUD_MOVED_TO_SETTINGS:
            with self.subTest(setting=name):
                self.assertTrue(hasattr(settings, name),
                                f"{name} нет в settings.py — переезду некуда")
                self.assertNotIn(name, cloud.CLOUD_ENV_NAMES,
                                 f"{name} осталась в обоих местах сразу")

    def test_the_log_names_whose_window_is_used(self):
        with mock.patch("builtins.print") as fake_print:
            text.trim_history_by_tokens([{"role": "user", "content": "привет"}], 10,
                                        model="cloud:qwen/qwen3.8-flash")
        printed = " ".join(str(call.args[0]) for call in fake_print.call_args_list if call.args)
        self.assertIn("из окна облака", printed,
                      "по строке в логе должно быть видно, чьё это окно")


class TestTurnReport(unittest.TestCase):
    """Отчёт о ходе: что именно вошло в запрос — и что происходило по порядку.

    В ленте виден только ответ, а вопросы «почему модель не помнит начало сцены»
    и «откуда взялось это число токенов» требовали лезть в консоль. Теперь
    у каждой реплики есть её ход целиком: откуда он взялся (окно, история,
    обрезка), что делал по порядку (запросы с их входом и выводом, поиски
    с формулировкой и находками) и чем кончился.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.session.topic = "Проверочная тема"

    @staticmethod
    def _participant(model: str = "fake-model") -> dict:
        return {"display_name": "Проверка", "model": model, "gender": "male",
                "instruction": "ГОВОРИ КОРОТКО"}

    def _turn(self, participant: dict = None, reply: str = "Вот ответ.",
              extra: list = None, search_count: int = 0, steps: list = None):
        """Один ход с подделанным шлюзом. extra — то, что ход дописывает в запрос."""
        participant = participant or self._participant()

        def answer(model, messages, participant_name, **kwargs):
            # Настоящий ask_model дописывает в этот же список результаты поиска
            if extra:
                messages.extend(copy.deepcopy(extra))
            # И так же наполняет журнал хода: у настоящего это делают сам запрос
            # (числа) и цикл поиска (формулировка и находки)
            if steps:
                kwargs["report"]["steps"] = copy.deepcopy(steps)
                kwargs["report"]["tokens_in"] = 3431
                kwargs["report"]["tokens_out"] = 1246
            return reply, search_count, ["проверочный запрос"] * search_count

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            self.session.handle_ai_turn(participant, 1)
        return self.session.posts[-1]

    def test_the_input_of_a_turn_is_counted_over_all_its_requests(self):
        """Ввод хода — это все его запросы, а не только первый.

        Поиск — ещё один круг, и вся история уезжает к модели заново: за ход
        с пятью поисками она оплачивается шесть раз. Одним числом «токенов
        на ввод» (это первый запрос) ход не описать — отсюда и расхождение
        с кабинетом шлюза, где видны все запросы.
        """
        steps = [
            {"kind": "ask", "n": 1, "tokens_in": 2871},
            {"kind": "search", "n": 1, "query": "раз", "results": "нашлось",
             "limit": 5},
            {"kind": "ask", "n": 2, "tokens_in": 5189},
            # Вендор чисел не назвал — считаем своим весом, иначе в сумме
            # была бы дыра там, где запрос был
            {"kind": "ask", "n": 3, "tokens_in_est": 6400},
        ]
        post = self._turn(steps=steps)

        self.assertEqual(post["turn"]["asks"], 3)
        self.assertEqual(post["turn"]["tokens_in_total"], 2871 + 5189 + 6400,
                         "сумма по всем запросам хода, а не по первому")

    def test_the_added_tail_counts_what_is_really_in_it(self):
        """Хвост хода посчитан по самим сообщениям, а не «по паре на каждый поиск».

        В отчёте стояла именно эта фраза, и с числами она не сходилась: «3 сообщ.
        — по паре на каждый поиск» — как так? Пара — привычный случай, а не
        всегдашний: три поиска одним вызовом дают одну просьбу и три ответа,
        а напоминаний в ходу может не быть вовсе (см. added_kinds).
        """
        extra = [
            # Один вызов, три поиска в нём, и ни одного напоминания
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "function": {"name": "search_web", "arguments": {}}},
                {"id": "2", "function": {"name": "search_web", "arguments": {}}},
                {"id": "3", "function": {"name": "search_web", "arguments": {}}}]},
            {"role": "tool", "tool_name": "search_web", "name": "search_web",
             "content": "нашлось раз"},
            {"role": "tool", "tool_name": "search_web", "name": "search_web",
             "content": "нашлось два"},
            # Отказ по лимиту — тоже сообщение хвоста, но это не найденное
            {"role": "tool", "tool_name": "search_web", "name": "search_web",
             "content": "[лимит поисков исчерпан]"},
        ]
        post = self._turn(extra=extra, search_count=3)

        self.assertEqual(post["turn"]["added_kinds"],
                         {"asks": 1, "results": 2, "refusals": 1, "nudges": 0},
                         "хвост считается по сообщениям: просьбы, найденное, отказы "
                         "и просьбы приложения словами")
        self.assertEqual(post["turn"]["extra_messages"], 4,
                         "и общее число сообщений хвоста — рядом со счётом по частям")

    def test_the_two_kinds_of_trouble_are_counted_apart(self):
        """Отказ в поиске и молчание — разные заминки, и в сводке они врозь.

        Модель, попросившая поиск сверх лимита, просто хотела больше, чем ей
        дали, — реплика при этом есть. А молчание — это когда реплики нет вовсе.
        Общее число «заминок» говорило «что-то было», не говоря что.
        """
        steps = [
            {"kind": "refused", "text": "просит ещё поиск «раз», но лимит 3 исчерпан"},
            {"kind": "silence", "text": "пустой ответ: размышления заняли весь бюджет"},
            # Принудительный поиск заминкой не считается: это правила работают,
            # а не что-то сломалось
            {"kind": "force", "text": "модель ответила без поиска — прошу поиск"},
        ]
        post = self._turn(steps=steps)

        self.assertEqual(post["turn"]["search_refusals"], 1,
                         "отказ в поиске — своё число")
        self.assertEqual(post["turn"]["silences"], 1,
                         "молчание — своё: это уже беда, а не просьба поискать ещё")
        self.assertNotIn("problems", post["turn"],
                         "общего «сколько-то заминок» быть не должно — оно ни о чём")

    def test_the_trim_report_names_the_window_and_what_was_dropped(self):
        messages = [{"role": "user", "name": f"говорун{i}",
                     "content": "реплика сцены " * 900} for i in range(4)]
        # Окно и запас задаём явно и узко: при CLOUD_MAX_TOKENS = 0 (значит
        # «ответ не ограничиваем») окно 32 768 не режет ничего, и обрезать было
        # бы нечего. Здесь проверяется сам отчёт об обрезке, а не её отсутствие
        narrow = 8192
        with mock.patch.object(settings, "CLOUD_MAX_TOKENS", 512), \
                mock.patch.object(settings, "CLOUD_NUM_CTX", narrow):
            trimmed, report = text.trim_history_with_report(
                messages, 100, model="cloud:qwen/qwen3.8-flash")

        self.assertEqual(report["window"], narrow)
        self.assertTrue(report["cloud"], "окно должно быть облачным — по говорящей модели")
        self.assertLess(len(trimmed), len(messages))
        self.assertEqual(report["messages_before"], len(messages))
        self.assertEqual(report["messages_after"], len(trimmed))
        self.assertEqual(report["messages_after"] + len(report["removed"]), len(messages),
                         "выброшенное должно быть перечислено, а не потеряно молча")
        self.assertTrue(report["removed"], "часть истории не влезла — об этом надо сказать")
        early = report["removed"][0]
        self.assertIn("говорун", early["speaker"])
        self.assertGreater(early["tokens"], 0)
        self.assertTrue(early["preview"], "у выброшенного должно остаться начало")
        self.assertNotIn("\n", early["preview"], "в отчёте начало идёт одной строкой")
        self.assertEqual(report["kept_tokens"],
                         sum(text.estimate_tokens(m["content"]) for m in trimmed))

    def test_a_local_window_is_not_called_a_cloud_one(self):
        trimmed, report = text.trim_history_with_report(
            [{"role": "user", "content": "привет"}], 10, model="qwen3:8b")

        self.assertEqual(report["window"], settings.OPTIONS["num_ctx"])
        self.assertFalse(report["cloud"])
        self.assertFalse(report["unbounded"])
        self.assertEqual(report["removed"], [], "обрезки не было — и выброшенных нет")
        self.assertEqual(report["messages_after"], len(trimmed))

    def test_the_post_carries_the_request_that_was_sent(self):
        """У реплики модели есть отчёт — и в нём ровно то, что читала модель."""
        post = self._turn()

        self.assertIsNotNone(post["turn"], "у реплики нет отчёта о ходе")
        payload = self.session.turn_report(post["id"])
        self.assertIsNotNone(payload)
        self.assertEqual(post["turn"]["messages"], len(payload["messages"]))
        self.assertEqual(payload["summary"]["tokens"],
                         sum(m["tokens"] for m in payload["messages"]))

        system = payload["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("ГОВОРИ КОРОТКО", system["content"],
                      "личная инструкция — тоже часть отправленного")
        self.assertIn("Проверочная тема", system["content"], "тема уезжает в системном промпте")
        self.assertEqual(payload["budget"]["system_tokens"], system["tokens"])
        self.assertEqual(payload["who"]["model"], "fake-model")
        self.assertEqual(payload["who"]["name"], "Проверка")

    def test_the_snapshot_is_taken_before_the_search_rounds(self):
        """Результаты поиска дописываются в тот же запрос уже по ходу дела.

        Снимок должен остаться тем, с чего ход начался: иначе «что вошло»
        показывало бы конец хода вместо начала, и по нему нельзя было бы понять,
        что именно модель прочитала перед первым словом.
        """
        post = self._turn(extra=[{"role": "tool", "name": "search",
                                  "content": "НАЙДЕННОЕ-ПОЗЖЕ"}], search_count=1)

        payload = self.session.turn_report(post["id"])
        sent = " ".join(m["content"] for m in payload["messages"])
        self.assertNotIn("НАЙДЕННОЕ-ПОЗЖЕ", sent, "отчёт снят уже после поиска")
        self.assertIn("НАЙДЕННОЕ-ПОЗЖЕ",
                      " ".join(m["content"] for m in payload["added"]),
                      "дописанное ходом должно быть видно отдельно")
        self.assertEqual(payload["summary"]["extra_messages"], 1)
        self.assertGreater(payload["summary"]["extra_tokens"], 0)
        self.assertEqual(payload["summary"]["search_rounds"], 1)

    def test_every_turn_of_the_show_is_kept(self):
        """Отчёты держатся за весь спектакль, а не за последние несколько ходов.

        Смотреть в них хочется как раз тогда, когда что-то пошло не так, — и это
        может быть первая реплика («что мы вообще отправили модели?»). Новый
        спектакль начинает список заново: отчёты прежних ходов к нему не годятся.
        """
        for _ in range(5):
            self._turn()

        self.assertEqual(len(self.session.turn_log), 5)
        for post in self.session.posts:
            self.assertIsNotNone(self.session.turn_report(post["id"]),
                                 "отчёт о ходе пропал — а разбирать по нему")

        self.session.start_show("Тема")
        self.assertEqual(self.session.turn_log, {},
                         "новый спектакль — новые отчёты: прежние забыты")

    def test_the_turn_names_the_numbers_and_the_search_queries(self):
        """В отчёте видно и что вошло в запрос, и что ход делал по порядку.

        Именно этого не хватало: «запросов было три» и «нашли вот это» лежало
        в разных местах, а связь — только в голове. И числа запроса не надо
        угадывать: вход отличается от вывода, размышления названы отдельно.
        """
        steps = [
            {"kind": "ask", "n": 1, "tokens_in": 3431, "tokens_out": 1246,
             "reasoning_tokens": 1160, "finish_reason": "length", "tools": True},
            {"kind": "search", "n": 1, "query": "Сыктывкар население 2025",
             "limit": 3, "results": "1. Республика Коми — 737 000 человек"},
            {"kind": "refused", "text": "просит ещё поиск «раз», но лимит 3 исчерпан"},
        ]
        post = self._turn(steps=steps)
        payload = self.session.turn_report(post["id"])

        self.assertEqual(post["turn"]["asks"], 1, "в сводке — сколько было запросов")
        # Заминки названы по отдельности: отказ в поиске сверх лимита — это ещё
        # не беда, а молчание — уже беда. Одним числом они говорили бы «что-то
        # было», не говоря что
        self.assertEqual(post["turn"]["search_refusals"], 1,
                         "отказ в поиске должен быть назван отдельно")
        self.assertEqual(post["turn"]["silences"], 0,
                         "молчания в этом ходу не было — и приписывать его нельзя")
        self.assertEqual([step["kind"] for step in payload["steps"]],
                         ["ask", "search", "refused"], "шаги должны идти по порядку")
        search = payload["steps"][1]
        self.assertIn("население", search["query"], "без формулировки запроса поиск бесполезен")
        self.assertIn("737 000", search["results"], "и без найденного тоже")

    def test_the_timeline_names_the_time_and_the_weight_of_each_event(self):
        """У каждого события хронологии есть время, а у запроса и поиска — вес.

        Без времени на вопрос «чем модель занималась две минуты» ответить нечем,
        а без веса найденного не понять, за что именно заплачено входными
        токенами: найденное едет к модели сверх истории.
        """
        started, ended = 1_700_000_000.0, 1_700_000_012.345
        steps = [
            {"kind": "ask", "n": 1, "t": started, "t_end": ended,
             "tokens_in": 3431, "tokens_out": 1246, "tokens_in_est": 3400,
             "reasoning_tokens": 1160, "finish_reason": "length", "tools": True},
            {"kind": "thought", "n": 1, "t": started + 0.5, "t_end": ended - 0.5,
             "text": "Прикидываю доводы.", "tokens": 63},
            {"kind": "search", "n": 1, "query": "Сыктывкар население",
             "limit": 3, "t": ended + 0.1, "t_end": ended + 1.2,
             "results": "1. Республика Коми — 737 000 человек", "tokens": 900},
        ]
        post = self._turn(steps=steps)
        payload = self.session.turn_report(post["id"])

        ask, thought, search = payload["steps"]
        number = re.compile(r"^\d\d:\d\d:\d\d\.\d\d\d$")
        self.assertRegex(ask["clock"], number, "у запроса должно быть время начала")
        self.assertRegex(ask["clock_end"], number, "и время окончания")
        self.assertRegex(thought["clock"], number, "и размышления — обычное событие со временем")
        self.assertRegex(search["clock_end"], number)
        self.assertEqual(post["turn"]["thought_steps"], 1,
                         "о размышлениях должно быть сказано в свёрнутой строке")
        # Проверяем числа, а не слова вокруг них: переименование подписи ничего
        # не ломает, и краснеть от него набор не должен (см. правило в tests/mutation_check.py)
        self.assertIn(show.numbers_word(3431), show.ask_line(ask),
                      "в строке запроса — число ввода от вендора")
        self.assertIn(show.numbers_word(3400), show.ask_line(ask),
                      "рядом с числом вендора виден и наш счёт")
        self.assertIn(show.numbers_word(1246), show.ask_line(ask),
                      "и вывод, иначе «ввод» не с чем сравнить")
        silent = show.ask_line({"kind": "ask", "tokens_in_est": 3400})
        self.assertIn(show.numbers_word(3400), silent,
                      "вендор чисел не дал — наш счёт остаётся, дыры быть не должно")
        self.assertNotEqual(show.ask_line(ask), silent,
                            "есть вендорские числа или нет — строка об этом говорит")
        line = show.step_markdown(search)
        self.assertIn("население", line, "в хронологии ДАМПа видна формулировка запроса")
        self.assertIn("900", line, "и вес найденного")

    def test_the_scene_count_comes_from_the_report_and_zero_is_zero(self):
        """Числа хода — те же в отчёте и в шапке, и ноль сцены остаётся нулём.

        Из жизни: рядом стояли два числа про разное (все сообщения запроса и
        только сцена) — читалось как ошибка арифметики. Хуже: ноль сообщений
        сцены подменялся общим числом сообщений, и в отчёте появлялось число,
        которого в запросе не было. Проверяем данные, а не слова вокруг них:
        эти числа уезжают и в ленту, и в ДАМП.
        """
        folder = Path(tempfile.mkdtemp())
        dump = folder / "damp.md"
        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("Проверочная тема")
            post = self._turn()

        budget = self.session.turn_report(post["id"])["budget"]
        self.assertEqual(budget["messages_after"], 0,
                         "сцены в этом ходу нет — и в отчёте ноль, а не все сообщения запроса")
        self.assertLessEqual(budget["messages_after"], budget["messages_before"],
                             "сцена — часть запроса, больше него быть не может")

    def test_the_header_shows_every_number_it_was_given(self):
        """Шапка хода — способ прочитать отчёт глазами: числа доезжают все.

        Проверяем на числах, а не на словах: если число теряется по дороге
        от отчёта к ДАМП, режиссёр видит пустоту там, где данные есть.
        Запас здесь ненулевой — этим и отличается от проверки про ноль
        (см. test_a_zero_answer_seat_is_not_called_zero_tokens).
        """
        turn = {
            "who": {"name": "Проверка", "model": "cloud:x", "role": "judge",
                    "role_name": "Судья", "round": 2, "time": "12:00"},
            "budget": {"kind": "cloud", "window": 32768, "reserve": 8192,
                       "safety": 500, "available": 24076, "system_tokens": 807,
                       "messages_before": 11, "messages_after": 2,
                       "kept_tokens": 1447},
            "summary": {"messages": 4, "tokens": 2254, "window_kind": "cloud"},
            "messages": [],
        }
        text = show.dump_turn_header(1, turn)
        for number in (32768, 8192, 500, 24076, 807, 1447, 2254):
            self.assertIn(show.numbers_word(number), text,
                          f"число {number} отчёт знает, и в шапке оно должно быть")
        # А это — против пропажи самого числа: если сцена в шапке не называется,
        # то и от правки числа текст не изменится (слова тут ни при чём)
        for key, value in (("messages_after", 3), ("kept_tokens", 9999)):
            other = dict(turn, budget=dict(turn["budget"], **{key: value}))
            self.assertNotEqual(text, show.dump_turn_header(1, other),
                                f"шапка не заметила правки {key} — число не показано")

    def test_a_zero_answer_seat_is_not_called_zero_tokens(self):
        """Ноль в запасе — это «ответ не ограничиваем», а не «0 токенов на ответ».

        Именно так это у облака и есть (CLOUD_MAX_TOKENS = 0). Строка
        «0 оставлено на ответ модели» читалась бы ровно наоборот и заставляла бы
        искать, куда эти токены пропали.
        """
        line = show.window_line({"kind": "cloud", "window": 32768, "reserve": 0,
                                 "safety": 500, "available": 32268})

        self.assertIn("на ответ ничего не зарезервировано", line)
        self.assertIn("CLOUD_MAX_TOKENS = 0", line)
        self.assertNotIn("0 оставлено на ответ", line)

    def test_a_message_without_text_says_what_it_has_instead(self):
        """«assistant · 0 токенов» объясняет себя: тело пустое, а поля — вот они.

        Именно этого не хватало в ДАМПе: ход модели, целиком состоящий
        из просьбы о поиске, — это пустой текст плюс поле tool_calls. Запись
        «0 токенов» была верным сигналом (что-то было!) и ни капли смысла:
        ни какие поля там есть, ни с какими значениями.
        """
        folder = Path(tempfile.mkdtemp())
        dump = folder / "damp.md"

        def answer(model, messages, participant_name, **kwargs):
            # Так это и происходит в жизни: модель просит поиск (текста нет, есть
            # вызов) и получает найденное — оба сообщения приложение дописало само
            messages.append({
                "role": "assistant", "content": "", "name": participant_name.lower(),
                "tool_calls": [{"id": "call-1", "type": "function",
                                 "function": {"name": "search_web",
                                              "arguments": json.dumps(
                                                  {"query": "Сыктывкар новости"})}}]})
            messages.append({"role": "tool", "content": "Сыктывкар — столица Коми.",
                             "tool_name": "search_web", "name": "search_web"})
            return "Вот ответ.", 1, ["Сыктывкар новости"]

        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("Проверочная тема")
            with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
                self.session.handle_ai_turn(self._participant(), 1)
            written = dump.read_text(encoding="utf-8")

        self.assertIn("### ✍️ Что приложение дописало в запрос", written)
        self.assertIn("0 токенов текста", written,
                      "ноль — это ноль ТЕКСТА, и так и надо писать")
        self.assertIn("тела текста нет", written)
        self.assertIn("`tool_calls` = 1 вызов(ов): search_web «Сыктывкар новости»", written,
                      "надо назвать поле и его значение, а не оставить одно число")
        self.assertIn("ответ инструмента поиска", written,
                      "у найденного тоже должно быть сказано, что это такое")

    def test_the_price_of_a_turn_comes_from_the_balance_not_from_tariffs(self):
        """Цена хода — разница остатков на ключе: тарифов мы не знаем, счёт знает шлюз.

        Умножать токены на цену не на чем: цены шлюза живут в его каталоге
        и меняются. А остаток он отдаёт сам — и разница «до» и «после» это уже
        факт со счёта, вместе с размышлениями, поисками и кэшем.
        """
        gateway = FakeGateway(balances=[100.0, 97.5])
        self.addCleanup(gateway.stop)
        folder = Path(tempfile.mkdtemp())
        dump = folder / "damp.md"

        def answer(model, messages, participant_name, **kwargs):
            return "Вот ответ.", 0, []

        participant = self._participant()
        participant["model"] = "cloud:qwen/qwen3.8-flash"   # за местную платить нечем
        with mock.patch.object(settings, "DUMP_FILE", dump), \
                mock.patch.object(settings, "CLOUD_BASE_URL", gateway.base_url), \
                mock.patch.object(settings, "CLOUD_API_KEY", "test-key-price"), \
                mock.patch.object(settings, "CLOUD_BALANCE_PATH", "/proxyapi/balance"), \
                mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
            show.start_dump("Проверочная тема")
            self.session.handle_ai_turn(participant, 1)
            written = dump.read_text(encoding="utf-8")

        self.assertAlmostEqual(self.session.spent, 2.5, places=2,
                               msg="цена спектакля — сумма цен ходов")
        self.assertAlmostEqual(self.session.posts[-1]["turn"]["spent"], 2.5, places=2,
                               msg="цена хода должна доехать до ленты")
        self.assertIn("баланс ключа", written)
        self.assertIn("за этот ход списано 2,50 ₽", written,
                      "рубли должны быть с копейками и с запятой, как на ценнике")
        # Остаток читается дважды на ход, и оба раза — настоящим запросом
        paths = [request["path"] for request in gateway.requests]
        self.assertEqual(paths.count("/proxyapi/balance"), 2,
                         "остаток нужен и до хода, и после него — иначе нет разницы")

    def test_the_turn_says_how_long_it_lasted(self):
        """Время хода — рядом с ценой: ожидание — такая же плата за реплику.

        Считает его сам ход — от сборки сообщений до готовой реплики, а не
        страница: часы у браузера и у сервера разные, и «с 05:39 до 05:41»
        у зрителя значило бы гадание (см. refresh_turn_report).
        """
        def slow_answer(model, messages, participant_name, **kwargs):
            time.sleep(0.05)          # такой ход, что его видно секундомером
            return "Вот ответ.", 0, []

        with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=slow_answer)):
            self.session.handle_ai_turn(self._participant(), 1)

        summary = self.session.posts[-1]["turn"]
        self.assertIsNotNone(summary.get("seconds"), "ход обязан назвать своё время")
        self.assertGreaterEqual(summary["seconds"], 0.05,
                                "время хода меньше самого хода быть не может")

    def test_the_dump_says_how_long_the_turn_lasted(self):
        """В ДАМПе у хода тоже есть время: его читают, чтобы понять, что было."""
        # Секунды словами, а не измерением: «135.4 с» в тексте не читается
        self.assertEqual(show.duration_words(135), "2 мин 15 с")
        self.assertEqual(show.duration_words(120), "2 мин",
                         "«2 мин 0 с» читается хуже, чем «2 мин»")
        self.assertEqual(show.duration_words(45), "45 с")

        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "damp.md"

            def slow_answer(model, messages, participant_name, **kwargs):
                time.sleep(1.1)      # ход, который длился хотя бы секунду
                return "Вот ответ.", 0, []

            with mock.patch.object(settings, "DUMP_FILE", dump), \
                    mock.patch.object(ollama_api, "ask_model",
                                      mock.Mock(side_effect=slow_answer)):
                show.start_dump("Проверочная тема")
                self.session.handle_ai_turn(self._participant(), 1)
                written = dump.read_text(encoding="utf-8")

        self.assertIn("**Ход длился:** 1 с", written,
                      "у хода в файле должно быть сказано, сколько он шёл — словами")

    def test_the_balance_address_keeps_the_host_whole(self):
        """Адрес остатка собирается из адреса шлюза, но хоста не калечит.

        Эти два знака (/v1) есть и в имени хоста (v1.example.com), и отрезать
        по первому вхождению значило бы собрать «https:» вместо адреса.
        """
        for base, expected in (("https://api.proxyapi.ru/v1", "https://api.proxyapi.ru"),
                               ("https://api.proxyapi.ru/v1/", "https://api.proxyapi.ru"),
                               ("https://api.example.com/openai/v1",
                                "https://api.example.com/openai"),
                               ("https://v1.example.com/v1", "https://v1.example.com"),
                               ("http://v1.example.com", "http://v1.example.com")):
            with mock.patch.object(settings, "CLOUD_BASE_URL", base), \
                    mock.patch.object(settings, "CLOUD_BALANCE_PATH", "/proxyapi/balance"):
                self.assertEqual(cloud.balance_url(), expected + "/proxyapi/balance", base)

    def test_a_local_turn_does_not_ask_the_gateway_about_money(self):
        """У местной модели платить не за что — и остаток у шлюза не спрашивают.

        Два запроса на каждый ход ради места, где списания быть не может, —
        это и лишний шум в чужой статистике, и лишняя задержка спектаклю.
        """
        gateway = FakeGateway(balances=[100.0, 97.5])
        self.addCleanup(gateway.stop)
        participant = self._participant()
        participant["model"] = "qwen3:8b"   # местная

        with mock.patch.object(settings, "CLOUD_BASE_URL", gateway.base_url), \
                mock.patch.object(settings, "CLOUD_API_KEY", "test-key-local"), \
                mock.patch.object(settings, "CLOUD_BALANCE_PATH", "/proxyapi/balance"), \
                mock.patch.object(ollama_api, "ask_model",
                                  mock.Mock(return_value=("Вот ответ.", 0, []))):
            self.session.handle_ai_turn(participant, 1)

        self.assertEqual([request["path"] for request in gateway.requests], [],
                         "местный ход не должен трогать шлюз вовсе")
        self.assertEqual(self.session.spent, 0.0)

    def test_a_key_without_the_balance_permission_says_so_once(self):
        """Нет разрешения на баланс — нет и цены, но спектакль от этого не падает.

        По умолчанию ключам этот запрос запрещён, и шлюз отвечает 403. Это
        не поломка театра: токены остаются точными, а про цену надо сказать
        словами и один раз, а не отказом на каждом ходу.
        """
        self.addCleanup(cloud.balance_told, False)
        self.addCleanup(cloud._BALANCE_CACHE.update, {"at": 0.0, "data": None})
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"{}"))
        opener = mock.Mock()
        opener.open.side_effect = error

        with mock.patch.object(settings, "CLOUD_BALANCE_PATH", "/proxyapi/balance"), \
                mock.patch.object(settings, "CLOUD_API_KEY", "test-key-403"), \
                mock.patch.object(cloud.urllib.request, "build_opener",
                                  mock.Mock(return_value=opener)):
            data, problem = cloud.balance(force=True)
            second, again = cloud.balance(force=True)

        self.assertIsNone(data)
        self.assertIn("Запрос баланса", problem,
                      "надо назвать разрешение, которое включается в кабинете ключа")
        self.assertIn("403", problem)
        # А вот сама попытка повторяется: разрешение включается в кабинете ключа,
        # и заставлять перезапускать театр ради этого не надо — цена появится сама
        self.assertEqual((second, again), (None, problem),
                         "разрешение могут включить по ходу: надо снова попробовать")

    def test_the_answer_code_is_translated_into_words(self):
        """Код ответа вендора — словами: «конец: tool_calls (слов модель не сказала…)».

        finish_reason и есть тот самый код ответа, о котором спрашивал режиссёр:
        сам по себе он ничего не говорит, а ход им и объясняется.
        """
        line = show.ask_line({"n": 1, "tokens_in": 3431, "tokens_out": 1246,
                              "finish_reason": "tool_calls", "tools": True})

        self.assertIn("конец: tool_calls", line)
        self.assertIn("попросила вызвать инструмент", line,
                      "у кода должен быть перевод, иначе он читается как код")
        # Код, которого мы не знаем, не выдумывается: он остаётся как есть
        unknown = show.ask_line({"n": 1, "tokens_in": 1, "tokens_out": 1,
                                 "finish_reason": "something_new"})
        self.assertIn("конец: something_new", unknown)
        self.assertNotIn("(None)", unknown)

    def test_the_dump_is_written_while_the_turn_is_still_going(self):
        """ДАМП пишется по ходу дела, а не в конце: у оборванного хода не было следов.

        Именно этого не хватало в случае судьи: две минуты размышлений, оборванный
        ход — и в файле ни строчки, потому что весь ход писался одной записью
        в самом конце. Теперь событие уходит в файл в тот момент, когда случилось,
        а в конце дописывается только реплика.
        """
        folder = Path(tempfile.mkdtemp())
        dump = folder / "damp.md"
        seen = {}

        def answer(model, messages, participant_name, **kwargs):
            # Так же, как настоящий ask_model: запрос открыт, мысли текут
            report = kwargs["report"]
            cloud.journal_ask_start(report, tokens_in_est=1000, tools=True, messages=3)
            cloud.journal_thought(report, "Прикидываю доводы.", True)
            kwargs["on_thought"]("Прикидываю доводы.", True)
            # Ход ещё не кончился — а файл уже должен говорить, чем модель занята
            seen["mid_turn"] = dump.read_text(encoding="utf-8")
            cloud.journal_ask(report, {"tokens_in": 1000, "tokens_out": 50})
            kwargs["on_delta"]("Вот ответ.", True)
            return "Вот ответ.", 0, []

        drafts = []
        with mock.patch.object(settings, "DUMP_FILE", dump):
            show.start_dump("Проверочная тема")
            with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
                self.session.handle_ai_turn(self._participant(), 1, on_draft=drafts.append)
            written = dump.read_text(encoding="utf-8")

        mid = seen.get("mid_turn", "")
        self.assertIn("### 🧭 Хронология хода", mid,
                      "хронология должна начинаться до запроса")
        self.assertIn("запрос 1", mid, "запрос должен быть виден, пока модель думает")
        self.assertIn("Прикидываю доводы.", mid, "размышления должны течь в файл сразу")
        self.assertNotIn("### 💬 Реплика", mid, "реплики в середине хода ещё нет")
        self.assertIn("Вот ответ.", written)
        self.assertIn("### 💬 Реплика, которой ход кончился", written)
        self.assertEqual(written.count("### 📨 Первый запрос к модели целиком"), 1,
                         "ход должен попасть в файл один раз, а не дважды")
        # Строка запроса и шапка мыслей появляются в файле сразу, без итоговых
        # чисел, — а когда числа есть, файл переписывает ту же строку, а не
        # оставляет её недописанной и не заводит вторую (см. dump_fix_step)
        open_ask = [line for line in mid.splitlines() if "**запрос 1**" in line]
        self.assertEqual(len(open_ask), 1, "строка запроса — одна")
        self.assertNotIn("ввод", open_ask[0],
                         "до ответа числа вендора ещё неизвестны, и строка не выдумывает их")
        ask_lines = [line for line in written.splitlines() if "**запрос 1**" in line]
        self.assertEqual(len(ask_lines), 1, "после правки строка всё та же — одна")
        self.assertIn("ввод 1 000", ask_lines[0],
                      "число вендора должно доехать в ту же строку")
        self.assertIn("вывод 50", ask_lines[0])
        self.assertIn("→", ask_lines[0], "и время окончания запроса — тоже")
        thought_lines = [line for line in written.splitlines() if "**размышления**" in line]
        self.assertEqual(len(thought_lines), 1)
        self.assertIn("→", thought_lines[0],
                      "у мыслей тоже должно появиться время окончания")
        turned = self.session.turn_report(self.session.posts[-1]["id"])
        weight = [step for step in turned["steps"]
                  if step.get("kind") == "thought"][0]["tokens"]
        self.assertIn(show.numbers_word(weight), thought_lines[0],
                      "вес в файле — тот же, что в отчёте, то есть итоговый")

    def test_a_search_round_lands_in_the_turn_step_by_step(self):
        """Ход собирается по шагам из настоящих ответов шлюза, а не из догадок.

        Здесь настоящий HTTP-ответ в формате OpenAI: сначала модель просит поиск,
        потом говорит по найденному. В отчёте должно быть видно ровно это и по
        порядку — запрос с числами входа и вывода, поиск с формулировкой
        и со всем принесённым, и второй запрос.
        """
        gateway = FakeGateway(texts=[
            json.dumps({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "search_web",
                              "arguments": json.dumps(
                                  {"query": "Сыктывкар население"})}}]}}]}),
            json.dumps({"choices": [{"message": {"content": "Столица Коми."},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3431, "completion_tokens": 1246}}),
        ])
        self.addCleanup(gateway.stop)
        saved = (settings.CLOUD_BASE_URL, settings.CLOUD_API_KEY)
        self.addCleanup(setattr, settings, "CLOUD_BASE_URL", saved[0])
        self.addCleanup(setattr, settings, "CLOUD_API_KEY", saved[1])
        settings.CLOUD_BASE_URL = gateway.base_url
        settings.CLOUD_API_KEY = "test-key-1234567890"
        self.addCleanup(cloud._MODELS_WITHOUT_TOOLS.clear)
        self.addCleanup(cloud._NO_TOOL_CHOICE.clear)
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)

        participant = {"display_name": "Проверка", "model": "cloud:openai/gpt-5-nano",
                       "gender": "male"}
        with mock.patch.object(ollama_api, "search_web",
                               mock.Mock(return_value="НАЙДЕНО ПОИСКОМ")):
            self.session.handle_ai_turn(participant, 1)

        payload = self.session.turn_report(self.session.posts[-1]["id"])
        kinds = [step["kind"] for step in payload["steps"]]
        self.assertEqual(kinds[:3], ["ask", "search", "ask"],
                         f"шаги хода разъехались: {kinds}")
        first, search, second = payload["steps"][:3]
        self.assertTrue(first["tools"], "надо видеть, ушёл ли инструмент поиска")
        self.assertNotIn("tokens_in", first,
                         "шлюз не прислал чисел — выдумывать их нельзя")
        # У каждого события — своё время: и когда запрос ушёл, и когда пришёл ответ.
        # Без этого «чем модель занималась две минуты» узнать неоткуда
        number = re.compile(r"^\d\d:\d\d:\d\d\.\d\d\d$")
        self.assertRegex(first["clock"], number, "у запроса нет времени отправки")
        self.assertRegex(first["clock_end"], number, "у запроса нет времени ответа")
        self.assertRegex(search["clock"], number, "у поиска нет времени")
        self.assertRegex(search["clock_end"], number)
        self.assertGreater(search["tokens"], 0, "вес найденного должен быть виден")
        self.assertEqual(search["query"], "Сыктывкар население")
        self.assertIn("НАЙДЕНО ПОИСКОМ", search["results"])
        self.assertEqual(second["tokens_in"], 3431, "вход второго запроса должен быть виден")
        self.assertEqual(second["tokens_out"], 1246)
        self.assertEqual(payload["summary"]["asks"], 2, "в сводке — сколько было запросов")

    def test_the_search_numbers_come_from_the_settings(self):
        """Все числа поиска — из настроек, а не из чисел, спрятанных в коде.

        Раньше минимум был настройкой, а максимум — тройкой в ask_model, и узнать
        о нём можно было только прочитав код, который про лимит молчал.
        """
        for name, value in (("MIN_SEARCHES", 2), ("MAX_SEARCHES", 4),
                            ("SEARCH_MAX_RESULTS", 3), ("MAX_SEARCH_ATTEMPTS", 1)):
            cloud_setting(self, name, value)

        self.assertEqual(ollama_api.search_limits(), (2, 4, 3, 1))

    def test_a_model_cannot_order_more_results_than_allowed(self):
        """Сколько результатов приносить — решает режиссёр, а не модель.

        В вызове инструмента модель просит своё число (обычно «побольше»),
        и раньше оно уезжало в поиск как есть. Сверх настроек не ходим: лишние
        находки — это лишние входные токены в каждом следующем запросе.
        """
        asked = {}
        cloud_setting(self, "SEARCH_MAX_RESULTS", 2)

        def fake_ddgs(kind, method, query, max_results):
            asked["max"] = max_results
            return [], None

        with mock.patch.object(search, "ddgs_search", side_effect=fake_ddgs):
            ollama_api.search_web("Сыктывкар")
            self.assertEqual(asked["max"], 2, "без просьбы модели — своё число из настроек")
            ollama_api.search_web("Сыктывкар", 9)
            self.assertEqual(asked["max"], 2, "модель не может заказать больше, чем разрешено")

    def test_a_human_reply_has_nothing_to_show(self):
        post = show.create_post("Живой", "human", "сказано руками", 1)
        self.assertIsNone(post["turn"], "человек ничего никуда не отправлял")

    def test_the_page_asks_for_the_turn_by_the_same_route(self):
        routes = {rule.rule for rule in web_app.app.url_map.iter_rules()}
        self.assertIn("/api/post/<int:post_id>/turn", routes)
        self.assertIn("/api/post/${box.dataset.postId}/turn", page.HTML_TEMPLATE,
                      "страница спрашивает отчёт не по тому адресу, что есть у сервера")


class TestPlayKeptInDump(unittest.TestCase):
    """Спектакль живёт в ДАМПе: и посты, и ходы, — и возвращается из него.

    Реплики и отчёты о ходах лежали только в памяти процесса: перезапуск
    означал пустую сцену, хотя записан был весь спектакль. Теперь файл — это
    сам спектакль, и читается он тем же, чем писался: разделы называются
    одними словами (см. TURN_SECTIONS), а строки собираются теми же функциями,
    что их и пишут.
    """

    # Портрет участника: у поста в ленте на этом месте стоит картинка
    FACE = "/avatars/test_face_1.jpg"

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.session.topic = "Возвращённая тема"
        # Куда черновик реплики отдаёт то, что рисует в ленте на ходу: без этого
        # у хода не будет ни размышлений, ни наброска (см. _StreamingReply)
        self.drafts = []

    @staticmethod
    def _participant(model: str = "fake-model") -> dict:
        return {"display_name": "Проверка", "model": model, "gender": "female",
                "avatar_emoji": "🦊", "instruction": "ГОВОРИ КОРОТКО"}

    def _play(self, folder) -> tuple:
        """Спектакль целиком: ход с поиском, мыслями и наброском, потом человек.

        Возвращает файл ДАМПа и сессию, в которой этот спектакль был отыгран:
        сверять прочитанное надо именно с ней, а не с тем, что мы написали
        здесь второй раз.
        """
        dump = Path(folder) / "damp.md"
        now = time.time()
        steps = [
            {"kind": "ask", "n": 1, "t": now, "t_end": now + 5.5, "tokens_in": 3431,
             "tokens_in_est": 3400, "tokens_out": 1246, "reasoning_tokens": 1160,
             "finish_reason": "tool_calls", "tools": True},
            {"kind": "search", "n": 1, "t": now + 6, "t_end": now + 6.8, "limit": 5,
             "query": "Сыктывкар население", "results": "НАЙДЕНО ПОИСКОМ: 250 тысяч",
             "tokens": 900},
            {"kind": "ask", "n": 2, "t": now + 7, "t_end": now + 9, "tokens_in": 5189,
             "tokens_in_est": 5000, "tokens_out": 700, "finish_reason": "stop",
             "tools": True},
            {"kind": "refused", "t": now + 9.1,
             "text": "просит ещё поиск «раз», но лимит 5 исчерпан"},
        ]
        extra = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "function": {"name": "search_web",
                                        "arguments": {"query": "Сыктывкар население"}}}]},
            {"role": "tool", "tool_name": "search_web", "name": "search_web",
             "content": "НАЙДЕНО ПОИСКОМ: 250 тысяч"},
        ]

        def answer(model, messages, participant_name, **kwargs):
            kwargs["on_thought"]("Сначала взвешу доводы.", True)
            kwargs["on_delta"]("Набросок ответа.", True)
            kwargs["on_delta"]("Вот ответ.", True)
            messages.extend(copy.deepcopy(extra))
            # Настоящий шлюз кладёт события в журнал именно так, и они уезжают
            # в файл в тот же миг (см. cloud.journal_push) — иначе в ДАМПе
            # не осталось бы ни запросов, ни поиска, и сверять было бы нечего
            for step in copy.deepcopy(steps):
                cloud.journal_push(kwargs["report"], step)
            return "Вот ответ.", 1, ["Сыктывкар население"]

        # Портрет у участников бывает: в ленте на месте эмодзи стоит картинка.
        # Аватар — свойство участника, а не реплики (см. show.post_view), поэтому
        # лицо лежит в составе, а в записи — одной строкой на весь спектакль
        # (см. show.cast_line): иначе после возврата из ДАМПа лицо пропадёт
        face = self.FACE
        (Path(folder) / "test_face_1.jpg").write_bytes(b"")
        participant = dict(self._participant(), avatar_url=face)
        self.session.runtime_participants.extend([
            dict(participant),
            {"display_name": "Живой", "model": "human", "gender": "female",
             "avatar_emoji": "📣", "avatar_url": face},
        ])
        self.session.sync_cast_media()

        with mock.patch.object(settings, "DUMP_FILE", dump), \
                mock.patch.object(settings, "AVATAR_DIR", Path(folder)):
            show.start_dump("Возвращённая тема")
            with mock.patch.object(ollama_api, "ask_model", mock.Mock(side_effect=answer)):
                self.session.handle_ai_turn(participant, 1,
                                            on_draft=self.drafts.append)
            # И живая реплика — тоже часть спектакля: у неё нет ни запросов,
            # ни поиска, но в ленте она стоит между машинными
            self.session.add_post("Живой", "human", "Сказано руками.", 1, gender="female")
            written = dump.read_text(encoding="utf-8")
        return written, dump

    def test_the_play_comes_back_the_same_from_the_dump(self):
        """Спектакль читается назад целиком: реплики, их ходы и числа ходов.

        Это и есть взаимозаменяемость: файл и лента содержат одно и то же,
        поэтому по файлу лента и собирается заново. Сверяем с той самой
        сессией, в которой спектакль отыгран, а не с пересказом.
        """
        with tempfile.TemporaryDirectory() as folder:
            written, _ = self._play(folder)
            # Портреты лежат в avatars/, и в проверке — свой, временный: файл,
            # за которым ничего нет, лицом не считается (см. _avatar_that_exists)
            with mock.patch.object(settings, "AVATAR_DIR", Path(folder)):
                play = show.parse_dump(written)

        self.assertIsNotNone(play, "свой же ДАМП не читается")
        self.assertEqual(play["topic"], "Возвращённая тема")
        played = self.session.posts
        self.assertEqual([p["display_name"] for p in play["posts"]],
                         [p["display_name"] for p in played])
        for original, restored in zip(played, play["posts"]):
            for key in ("content", "round", "role", "role_name", "gender",
                        "gender_symbol", "model_used", "timestamp", "avatar_url"):
                self.assertEqual(restored[key], original[key], key)

        machine = played[0]["id"]
        original_turn = self.session.turn_report(machine)
        restored_turn = play["turns"][machine]
        # Сводка хода считается заново по его же частям — и должна сойтись
        # с тем, что театр показывал на ходу
        for key in ("asks", "search_rounds", "thought_steps", "messages", "tokens",
                    "extra_messages", "extra_tokens", "removed_messages",
                    "tokens_in_total", "window_kind"):
            self.assertEqual(restored_turn["summary"][key], original_turn["summary"][key],
                             f"число «{key}» не вернулось из ДАМПа")
        self.assertEqual(int(round(original_turn["summary"]["seconds"])),
                         restored_turn["summary"]["seconds"],
                         "время хода в файле округлено до секунд — но не потеряно")
        for key in ("window", "reserve", "safety", "available", "system_tokens",
                    "kept_tokens", "messages_after"):
            self.assertEqual(restored_turn["budget"][key], original_turn["budget"][key], key)
        self.assertEqual([step["kind"] for step in restored_turn["steps"]],
                         [step["kind"] for step in original_turn["steps"]],
                         "хронология хода должна вернуться шаг в шаг")
        self.assertEqual([m["role"] for m in restored_turn["messages"]],
                         [m["role"] for m in original_turn["messages"]])
        self.assertEqual([m["tokens"] for m in restored_turn["messages"]],
                         [m["tokens"] for m in original_turn["messages"]],
                         "вес сообщений запроса должен считаться тем же счётом")
        self.assertEqual([m["content"] for m in restored_turn["added"]],
                         [m["content"] for m in original_turn["added"]])
        fallback = next(step for step in restored_turn["steps"] if step["kind"] == "search")
        self.assertEqual(fallback["query"], "Сыктывкар население")
        self.assertEqual(fallback["results"], "НАЙДЕНО ПОИСКОМ: 250 тысяч")
        asked = next(step for step in restored_turn["steps"] if step["kind"] == "ask")
        self.assertEqual(asked["tokens_in"], 3431)
        self.assertEqual(asked["tokens_out"], 1246)
        self.assertEqual(asked["finish_reason"], "tool_calls")
        self.assertRegex(asked["clock"], r"^\d\d:\d\d:\d\d\.\d\d\d$",
                         "время события должно вернуться, иначе «чем занимался ход» не ответить")
        self.assertEqual(restored_turn["sketch"], played[0]["sketch"])
        self.assertEqual(restored_turn["answer"], played[0]["content"])
        self.assertIn("Сначала взвешу доводы.", restored_turn["thinking"],
                      "мысли модели — тоже часть хода")

    def test_another_format_is_not_read_half_way(self):
        """Чужой формат записи не читается вовсе, а не наполовину.

        Формат будет меняться вместе с тем, как устроена запись, и тогда
        прежний ДАМП может не подойти. Прочитать из него половину хуже, чем
        ничего: непрочитанная реплика в ленте выглядела бы как «модель
        промолчала в этом ходу» — и это была бы неправда.
        """
        with tempfile.TemporaryDirectory() as folder:
            written, _ = self._play(folder)
        self.assertIn("Формат записи: " + str(show.DUMP_FORMAT), written,
                      "в файле должен быть номер формата — по нему и читают")

        older = written.replace("Формат записи: " + str(show.DUMP_FORMAT),
                                "Формат записи: 1")
        self.assertIsNone(show.parse_dump(older))
        self.assertIsNone(show.parse_dump("# ДАМП · 01.09.2026 12:00 · прежний\n\nход\n"),
                          "прежний ДАМП без номера формата тоже не наш")

    # Прежняя запись того же семейства: формат 3 — лицо говорящего лежало
    # в каждой реплике, а строки состава в шапке не было вовсе (см. cast_line)
    OLD_SUBVERSION = (
        "# ДАМП · 01.09.2026 12:00 · порт 5000\n\n"
        "Формат записи: 3\n\n"
        "**Тема:**\n> Прежняя тема\n\n"
        "## 1 · 10:00 · 🦊 Мария ♀ · fake-model · Участник · Акт 1\n\n"
        "### 👤 Кто говорит и когда\n\n"
        "**Кто:** **Мария** ♀ · fake-model · Акт 1 · 10:00\n"
        "**Аватар:** /avatars/test_face_1.jpg\n\n"
        "### 💬 Реплика, которой ход кончился\n\n"
        "> Сказано раньше.\n")

    def test_an_older_sub_version_is_read_as_is(self):
        """Под-версия записи читается без вопросов: менялось только содержимое.

        Формат растёт по-разному: пока меняется то, что лежит в записи (лицо
        участника переехало из реплики в состав), прошлый номер остаётся
        в списке читаемых (см. DUMP_FORMATS_READABLE) — и спрашивать не о чем.
        """
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "test_face_1.jpg").write_bytes(b"")
            with mock.patch.object(settings, "AVATAR_DIR", Path(folder)):
                play = show.parse_dump(self.OLD_SUBVERSION)

        self.assertIsNotNone(play, "под-версия записи должна читаться как есть")
        self.assertEqual(play["format"], 3)
        self.assertEqual(play["topic"], "Прежняя тема")
        self.assertEqual(play["posts"][0]["avatar_url"], self.FACE,
                         "у прежнего формата лицо лежало в самой реплике")
        self.assertIn(3, show.DUMP_FORMATS_READABLE)

    def test_a_foreign_record_gives_back_its_reply_not_silence(self):
        """Чужую запись читают по строкам: реплика там — последний текст.

        Разделы прежних версий театра назывались иначе, и знакомых среди них нет.
        Взять из такой записи пустоту значило бы показать реплику без слов —
        то есть соврать про молчание модели (см. _last_quoted).
        """
        stranger = (
            "# ДАМП · 01.09.2026 12:00 · порт 5000\n\n"
            "## 1 · 10:00 · 🦊 Мария ♀ · fake-model · Участник · Акт 1\n\n"
            "### Что вошло в запрос к модели\n\n"
            "- №1 · system · system · 12 токенов\n"
            "> Ты — Мария, участник обсуждения.\n\n"
            "### Реплика\n\n"
            "> А вот и мой ответ.\n")
        play = show.parse_dump(stranger, tolerant=True)

        self.assertIsNotNone(play, "терпимое чтение должно пробовать, а не сдаваться")
        self.assertEqual(len(play["posts"]), 1)
        self.assertEqual(play["posts"][0]["content"], "А вот и мой ответ.",
                         "реплика чужой записи — последний текст, а не пустота")
        self.assertIsNone(show.parse_dump(stranger),
                          "строго чужой формат по-прежнему не читается")

    def test_a_foreign_format_is_asked_about_and_not_guessed(self):
        """Расхождение форматов — вопрос, а не догадка, и решает его режиссёр.

        Прочитать чужую запись можно, но обещать нечего: непрочитанная реплика
        в ленте выглядела бы как «модель промолчала». Поэтому без ответа ничего
        не трогается, зато варианты называются вслух (см. ask_about_dump).
        """
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            stranger = dump.read_text(encoding="utf-8").replace(
                f"Формат записи: {show.DUMP_FORMAT}", "Формат записи: 7")
            dump.write_text(stranger, encoding="utf-8")
            self.session.posts = []
            shown = io.StringIO()
            with mock.patch.object(settings, "DUMP_FILE", dump), \
                    mock.patch.object(sys, "stdin", io.StringIO("")), \
                    mock.patch.object(sys, "stdout", shown):
                self.assertEqual(show.load_play_from_dump(), 0)
            self.assertEqual(self.session.posts, [],
                             "без ответа сцена должна остаться пустой")
            for option in ("[ч]", "[п]", "[Enter]"):
                self.assertIn(option, shown.getvalue(), "варианты должны быть названы")
            # А с согласием — читается: это и есть «попытаться открыть»
            with mock.patch.object(settings, "DUMP_FILE", dump), \
                    mock.patch.object(settings, "AVATAR_DIR", Path(folder)):
                self.assertEqual(show.load_play_from_dump("read"), 2,
                                 "согласие читать чужую запись должно вернуть реплики")

    def test_the_answer_to_the_question_decides_the_mode(self):
        """Ответ режиссёра и есть выбор: отменить, читать или доиграть."""
        for answer, expected in (("ч", "read"), ("п", "continue"), ("", "skip"),
                                 ("продолжить", "continue"), ("не знаю", "skip")):
            with mock.patch.object(sys, "stdin",
                                   mock.Mock(isatty=mock.Mock(return_value=True))), \
                    mock.patch.object(sys, "stdout", io.StringIO()), \
                    mock.patch.object(builtins, "input", mock.Mock(return_value=answer)):
                self.assertEqual(show.ask_about_dump(7), expected, answer)

    def test_a_play_returned_for_continuation_starts_from_its_own_replies(self):
        """«Доиграть» — значит не начинать с чистого листа.

        Прежние реплики остаются в ленте и становятся историей для моделей,
        а акт и цена продолжаются: это всё тот же спектакль (см. start_show).
        """
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            played = [p["display_name"] for p in self.session.posts]
            price = self.session.spent
            self.session.posts = []
            self.session.turn_log = {}
            self.session.finished = False
            self.session.restored = False
            with mock.patch.object(settings, "DUMP_FILE", dump):
                self.assertEqual(show.load_play_from_dump("continue"), 2)
                self.assertTrue(self.session.resume_ready,
                                "«доиграть» — это разрешение, а не согласие смотреть")
                self.session.start_show("Продолжение")

        self.assertEqual([p["display_name"] for p in self.session.posts], played,
                         "продолжение стёрло прежние реплики")
        self.assertEqual(len(self.session.conversation_history), 2,
                         "прежние реплики должны стать историей для моделей")
        self.assertTrue(self.session.resumed)
        self.assertFalse(self.session.restored, "спектакль идёт сейчас, а не был раньше")
        self.assertEqual(self.session.spent, price,
                         "цена продолжения — вся цена спектакля")

    def test_reading_without_continuation_still_starts_an_empty_lane(self):
        """Без согласия доиграть старт всё равно с чистого листа."""
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            with mock.patch.object(settings, "DUMP_FILE", dump):
                show.load_play_from_dump("read")
                self.assertEqual(len(self.session.posts), 2)
                self.session.start_show("Новый")

        self.assertEqual(self.session.posts, [])
        self.assertEqual(self.session.conversation_history, [])
        self.assertFalse(self.session.resumed)

    def test_the_models_hear_the_replies_of_the_returned_play(self):
        """Продолжение — это разговор с прежними репликами, а не с чистого листа.

        Иначе модель отвечала бы не на то, что говорилось на сцене, а в пустоту:
        история собирается из тех же реплик, что вернулись в ленту
        (см. start_show и build_messages_for_ai).
        """
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            self.session.posts = []
            self.session.turn_log = {}
            with mock.patch.object(settings, "DUMP_FILE", dump):
                show.load_play_from_dump("continue")
                self.session.start_show("Продолжение")
            aloud = [p["content"] for p in self.session.conversation_history]
            messages = self.session.build_messages_for_ai(self._participant(), 2)

        self.assertIn("Вот ответ.", aloud,
                      "реплика модели из прежнего спектакля должна стать историей")
        self.assertIn("Сказано руками.", aloud, "и реплика человека тоже")
        spoken = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("Вот ответ.", spoken, "прежняя реплика не доехала до модели")
        self.assertIn("Сказано руками.", spoken)

    def test_the_dump_keeps_the_old_records_and_appends_the_continuation(self):
        """ДАМП дописывается, а не переписывается: прежнее — это то же самое начало."""
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            before = dump.read_text(encoding="utf-8")
            self.session.posts = []
            self.session.turn_log = {}
            with mock.patch.object(settings, "DUMP_FILE", dump):
                show.load_play_from_dump("continue")
                self.session.start_show("Продолжение")
                show.start_dump("Продолжение", keep=self.session.resumed)
                self.session.add_post("Живой", "human", "Сказано после занавеса.", 2,
                                      gender="female")
            written = dump.read_text(encoding="utf-8")
            play = show.parse_dump(written)

        self.assertIn("Формат записи: " + str(show.DUMP_FORMAT), written[:200],
                      "шапка продолжения должна остаться прежней")
        self.assertIn("**Продолжение спектакля:**", written)
        self.assertLess(written.index("## 1 · "), written.index("**Продолжение спектакля:**"),
                        "запись о продолжении должна идти после прежних реплик")
        self.assertNotEqual(before, written)
        self.assertEqual([p["id"] for p in play["posts"]], [1, 2, 3],
                         "прежние реплики стёрлись или номер новой не продолжился")
        self.assertIn("Сказано после занавеса.", play["posts"][-1]["content"])

    def test_the_portrait_comes_back_with_the_reply(self):
        """У вернувшейся реплики то же лицо: портрет назван в составе записи.

        Аватар — свойство участника, а не реплики, поэтому в файле он лежит один
        на весь спектакль, строкой «Состав» (см. cast_line). По ней лицо
        и возвращается — даже если в пульте состав уже другой: место может
        уйти из труппы, а лицо его реплик остаться тем же.
        """
        with tempfile.TemporaryDirectory() as folder:
            written, dump = self._play(folder)
            head = next(line for line in written.splitlines()
                        if line.startswith("**Состав:** "))
            self.assertIn(f"🦊 Проверка ♀ · Участник · fake-model · {self.FACE}", head,
                          "адрес портрета должен быть в строке состава")
            # Состав мог смениться: имена другие — лиц в нём нет вовсе
            self.session.avatars = {}
            self.session.avatar_emojis = {}
            self.session.runtime_participants = []
            with mock.patch.object(settings, "AVATAR_DIR", Path(folder)):
                play = show.parse_dump(dump.read_text(encoding="utf-8"))

        self.assertEqual([p["avatar_url"] for p in play["posts"]],
                         [self.FACE, self.FACE],
                         "портрет должен вернуться из состава в записи, а не из пульта")

    def test_a_portrait_that_is_gone_falls_back_to_the_emoji(self):
        """Портрет, за которым нет файла, — не лицо: у поста остаётся эмодзи.

        Записи живут дольше картинок: портрет можно удалить или подобрать заново.
        Пустая рамка вместо лица хуже эмодзи, поэтому адрес из записи проверяется.
        """
        with tempfile.TemporaryDirectory() as folder:
            written, _ = self._play(folder)
        empty = tempfile.mkdtemp()
        with mock.patch.object(settings, "AVATAR_DIR", Path(empty)):
            play = show.parse_dump(written)

        self.assertEqual([p["avatar_url"] for p in play["posts"]], [None, None],
                         "адрес портрета, за которым нет файла, — не лицо")
        self.assertTrue(all(p["avatar_emoji"] for p in play["posts"]),
                        "без портрета у поста должен остаться эмодзи")

    def test_the_heading_carries_the_time_and_the_price_back(self):
        """Время хода и его цена — в шапке записи, и читаются обратно.

        Это два числа, которых задним числом не посчитать: часы уже не идут,
        а остаток на ключе — только у шлюза. Поэтому они и лежат в шапке,
        рядом с остальной сводкой (см. turn_summary_line).
        """
        who, seconds, spent = show._parse_heading(
            "22:01 · 🦊 Элина · cloud:qwen/qwen3.8-flash · Судья · Акт 2 · запросов 3 · "
            "поисков 5 · ⏱ 2 мин 15 с · 💰 1,89 ₽")

        self.assertEqual(who["emoji"], "🦊")
        self.assertEqual(who["name"], "Элина")
        self.assertEqual(who["role_name"], "Судья")
        self.assertEqual(who["round"], 2)
        self.assertEqual(who["time"], "22:01")
        self.assertEqual(seconds, 135)
        self.assertAlmostEqual(spent, 1.89, places=2)
        # Имя без значка — тоже обычное дело: значок может быть и не задан
        self.assertEqual(show._parse_heading("22:01 · Элина · human · Участник · Акт 1")[0]["name"],
                         "Элина")

    def test_the_who_line_names_the_gender_like_the_lane(self):
        """Пол стоит рядом с именем и в файле, и в ленте: это часть «кто говорит».

        Без него одна и та же реплика в файле и на странице читалась бы про
        разных людей: у поста в ленте значок есть (см. postHeaderHtml).
        """
        who = {"name": "Проверка", "model": "cloud:x", "round": 2, "time": "12:00",
               "gender": "female"}
        line = show.who_line({"who": who})
        self.assertIn("Проверка", line)
        self.assertIn("♀", line)
        self.assertNotIn("♂", line)
        self.assertIn("Акт 2", line)
        self.assertIn("♂", show.who_line({"who": dict(who, gender="male")}))
        self.assertIn("who.gender", page.HTML_TEMPLATE,
                      "в ленте пол тоже должен быть виден — иначе сверять нечего")

    def test_the_play_returns_to_the_session_after_a_restart(self):
        """Перезапуск показывает прежний спектакль: реплики, ходы и их цена.

        Посты и отчёты живут в памяти процесса, а у нового процесса она своя:
        без этого лента начиналась бы с пустой сцены, хотя спектакль записан.
        """
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            played = [p["display_name"] for p in self.session.posts]
            machine = self.session.posts[0]["id"]
            # Перезапуск: тот же файл, чистая память
            self.session.posts = []
            self.session.turn_log = {}
            self.session.finished = False
            self.session.restored = False
            with mock.patch.object(settings, "DUMP_FILE", dump):
                count = show.load_play_from_dump()

        self.assertEqual(count, 2, "вернулись не все реплики")
        self.assertEqual([p["display_name"] for p in self.session.posts], played)
        self.assertTrue(self.session.finished, "прежний спектакль доигран, а не идёт сейчас")
        self.assertFalse(self.session.running)
        self.assertTrue(self.session.restored, "в ленте надо сказать, что это прежний спектакль")
        self.assertEqual(self.session.current_round, 1)
        self.assertIsNotNone(self.session.turn_report(machine),
                             "ход вернулся вместе с репликой")
        self.assertEqual(self.session.topic, "Возвращённая тема")

    def test_a_new_show_forgets_the_previous_one_on_disk_too(self):
        """«Новый спектакль» и «Полный сброс» стирают прежний спектакль и в файле.

        Иначе кнопка значила бы «забудь, пока я не закрыл окно»: следующий
        запуск вернул бы то, от чего только что отказались (см. forget_play).
        """
        with tempfile.TemporaryDirectory() as folder:
            _, dump = self._play(folder)
            with mock.patch.object(settings, "DUMP_FILE", dump):
                self.session.new_show()
                self.assertEqual(self.session.posts, [])
                self.assertFalse(self.session.restored)
                self.assertFalse(dump.exists(),
                                 "«Новый спектакль» оставил прежний ДАМП на диске")

                show.start_dump("Другая тема")
                self.session.reset_to_defaults()
                self.assertFalse(dump.exists(),
                                 "«Полный сброс» оставил прежний ДАМП на диске")
                self.assertEqual(self.session.spent, 0.0,
                                 "счёт за прежний спектакль сбросом не сбрасывается — значит "
                                 "он вернётся в ленту")

    def test_the_file_says_about_a_turn_what_the_page_shows(self):
        """Разделы записи идут в том же порядке и теми же словами, что в ленте.

        Один и тот же отчёт читают в двух местах, и разойтись в порядке значило
        бы заставить читателя собирать картину заново в каждом из них. Порядок
        берётся у самих разделов (см. TURN_SECTIONS), но проверять его надо
        против страницы: порядок — это единственное, чего в одном словаре
        не видно.
        """
        body = page.HTML_TEMPLATE[page.HTML_TEMPLATE.index("function turnBodyHtml("):]
        body = body[:body.index("function loadTurnBox(")]
        self.assertEqual(list(show.TURN_SECTIONS),
                         ["who", "place", "history", "sketch", "cut", "added",
                          "request", "answer"],
                         "порядок разделов в файле должен быть порядком ленты")
        positions = [body.index(title.lstrip("#").strip())
                     for title in show.TURN_SECTIONS.values()]
        self.assertEqual(positions, sorted(positions),
                         "на странице разделы идут не в том порядке, что в файле")
        turn = {"who": {"name": "Варвара", "model": "cloud:x", "role_name": "Судья",
                        "round": 1, "time": "22:19", "gender": "female", "emoji": "🧛‍♀️"},
                "summary": {"asks": 2, "tokens": 890, "seconds": 135, "spent": 1.89,
                            "removed_messages": 1},
                "budget": {}, "messages": [],
                "removed": [{"speaker": "Кто-то", "tokens": 300, "preview": "начало"}],
                "added": [{"role": "tool", "name": "search_web", "tokens": 5,
                           "content": "нашлось", "note": ""}]}
        written = (show.dump_turn_header(1, turn)
                   + show.dump_turn_tail({"content": "сказано", "sketch": "набросок"}, turn))
        for title in show.TURN_SECTIONS.values():
            self.assertIn(title, written,
                          f"раздела «{title}» нет в записи — только в ленте")
        # И шапка записи собирается тем же заведением, что сводка ленты: без
        # этой сверки сводку можно было бы из файла выбросить незаметно
        self.assertIn(show.turn_heading(1, turn), written,
                      "шапка записи — это сводка того же хода, по ней и решают, читать ли его")
        self.assertIn(show.turn_summary_line(turn["summary"]),
                      show.turn_heading(1, turn))

    def test_the_added_tail_is_told_the_same_way_in_both_places(self):
        """О дописанном хвосте в файле сказано то же, что в ленте.

        В файле стояла фраза про «полных текстов здесь нет нарочно», а тексты
        рисовались следующей строкой — то есть файл врал о самом себе (см.
        added_purpose против addedPurpose на странице).
        """
        summary = {"added_kinds": {"asks": 1, "results": 2, "refusals": 1, "nudges": 0},
                   "extra_tokens": 1200}
        added = [{"role": "tool", "name": "search_web", "tokens": 900,
                  "content": "НАЙДЕНО", "note": ""}] * 2

        purpose = show.added_purpose(summary, added)
        for words in ("2 сообщ.", "1 200", "просьба вызвать инструмент — 1",
                      "найденное по ней — 2", "отказ по лимиту поисков — 1"):
            self.assertIn(words, purpose)
        self.assertNotIn("Полных текстов здесь нет",
                         show.dump_turn_tail({"content": "сказано"},
                                             {"summary": summary, "added": added}),
                         "файл не должен утверждать, что текстов нет, если они есть")


class TestTurnPanel(unittest.TestCase):
    """Блок о ходе на странице: один вместо трёх, свёрнут, тёмен и по требованию."""

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def block(self) -> str:
        start = self.page.index("function postTurnHtml(")
        return self.page[start:self.page.index("function addPost(", start)]

    def test_the_block_appears_only_where_there_is_a_turn(self):
        body = self.block()
        self.assertIn("post.turn", body)
        self.assertIn("return ''", body, "без отчёта блока быть не должно")

    def test_the_turn_is_one_block_instead_of_three(self):
        """Размышления, «сказано раньше» и «что уехало» — теперь один блок.

        Раньше это были три свёрнутые простыни рядом с каждой репликой, и связь
        между ними приходилось держать в голове: тут числа, там поиски.
        """
        self.assertNotIn("function postThinkingHtml", self.page)
        self.assertNotIn("function postSketchHtml", self.page)
        add = self.page[self.page.index("const postDiv = document.createElement"):]
        add = add[:add.index("insertBefore")]
        self.assertIn("${postTurnHtml(post)}", add, "блок не попал в саму реплику")
        for gone in ("postThinkingHtml(post)", "postSketchHtml(post)"):
            self.assertNotIn(gone, add, "у реплики остался прежний отдельный блок")

    def test_the_block_is_collapsed_and_the_text_is_fetched_on_demand(self):
        body = self.block()
        self.assertIn('<details class="post-thinking post-prompt"', body)
        self.assertNotIn('<details open', body, "отчёт хода весит как сцена — пусть ждёт клика")
        self.assertIn("data-post-id", body)
        start = self.page.index("function addPost(")
        add_body = self.page[start:self.page.index("function upsertStreamPost(", start)]
        self.assertIn("loadTurnBox(turnBox)", add_body,
                      "отчёт нечем подгрузить — блок останется пустым")

    def test_the_turn_has_its_own_colour_in_both_themes(self):
        self.assertIn(".post-prompt {", self.page)
        self.assertIn("body.dark .post-prompt", self.page)
        self.assertIn("body.dark .prompt-text", self.page,
                      "белое поле на тёмной сцене уже случалось — у текста должна быть своя темнота")
        self.assertIn("body.dark .prompt-step", self.page,
                      "шаги хода — тоже текст, и им тоже нужна тёмная тема")

    def body_html(self) -> str:
        """Что именно рисуется внутри раскрытого блока о ходе."""
        start = self.page.index("function turnBodyHtml(")
        return self.page[start:self.page.index("function loadTurnBox(", start)]

    def test_every_section_is_framed_and_titled(self):
        """Разделы отчёта отделены друг от друга и названы.

        Раньше это была одна простыня одинаковых серых строк: «Окно говорящего»,
        «Запрос к модели состоял из», «Хронология» — понять, где кончается одно
        и начинается другое, было нельзя. Теперь у каждого раздела имя и рамка.
        """
        self.assertIn(".prompt-block {", self.page)
        self.assertIn("body.dark .prompt-block", self.page,
                      "рамке нужна тёмная тема: белое поле на тёмной сцене уже было")
        self.assertIn(".prompt-block-title {", self.page)
        self.assertIn(".prompt-block-purpose {", self.page,
                      "в разделе с числами без назначения словами они ничего не значат")
        body = self.body_html()
        self.assertIn("function turnBlock(", self.page,
                      "разделы должны собираться одним заведением")
        self.assertIn('<section class="prompt-block">', self.page)
        self.assertIn("turnBlock(", body)
        self.assertGreaterEqual(body.count("turnBlock("), 6,
                                "разделов должно быть столько же, сколько частей отчёта")

    def test_the_section_frames_are_dark_on_the_dark_stage(self):
        """Рамка раздела на тёмной сцене — тёмная: белое на белом у нас уже было.

        Проверяется не «строка есть», а сам цвет: светлая рамка на тёмной сцене
        не ошибка синтаксиса — её просто не видно, и увидит это только зритель.
        """
        match = re.search(
            r"body\.dark \.prompt-block \{ border-color: (#[0-9a-fA-F]{6}); \}",
            self.page)
        self.assertIsNotNone(match, "у рамки раздела нет правила для тёмной сцены")
        color = match.group(1)
        red, green, blue = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        self.assertLess((red + green + blue) / 3, 80,
                        f"рамка {color} на тёмной сцене не читается")

    def test_the_sections_are_named_the_same_in_the_page_and_the_dump(self):
        """Отчёт один — и разделы в нём названы одними словами в обоих местах.

        Читают его и в ленте (раскрытый блок у реплики), и в файле ДАМПа
        (см. show.TURN_SECTIONS). Разойтись в названиях разделов значит
        заставить читателя гадать, один это раздел или два разных.
        """
        body = self.body_html()
        for title in show.TURN_SECTIONS.values():
            words = title.lstrip("#").strip()
            self.assertIn(words, body, f"на странице нет раздела «{words}»")

    def test_the_body_goes_through_the_turn_step_by_step(self):
        """Внутри блока — хронология: шаги, числа, найденное и всё, что вошло."""
        body = self.body_html()
        self.assertIn("WINDOW_WORDS", body, "должно быть видно, чьим окном мерили")
        self.assertIn("removed_messages", body)
        self.assertIn("promptRemovedHtml(data.removed)", body)
        self.assertIn("promptMessagesHtml(data.messages)", body,
                      "надо показать и системный промпт, и историю")
        self.assertIn("data.steps", body, "шаги хода — главное в этом блоке")
        self.assertIn("turnAskText(step)", body, "запрос без чисел не объясняет, откуда они")
        self.assertIn("step.query", body, "у поиска должна быть формулировка запроса")
        self.assertIn("step.results", body, "и то, что по нему нашлось")
        self.assertIn("promptMessagesHtml(data.added)", body,
                      "дописанное приложением — тоже сообщения запроса, и пустые из них "
                      "надо объяснить, а не сводить к одному числу")
        self.assertIn("m.note", self.page, "у сообщения без текста должно быть объяснение")
        self.assertIn("0 токенов текста", self.page,
                      "«0 токенов» — верный сигнал, но без «текста» он ничего не говорит")

    def test_the_ask_line_explains_what_the_numbers_mean(self):
        """Строка о запросе называет оба числа, размышления и смысл кода ответа.

        «3431 + 1246» в ленте — два числа про разное (что вошло в запрос и что
        вернул вендор), и без подписей не читаются. Проверяем не сами подписи,
        а что оба числа и смысл кода вообще попадают в строку.
        """
        start = self.page.index("function turnAskText(")
        body = self.page[start:self.page.index("function ", start + 10)]
        self.assertIn("step.tokens_in", body, "в строке должно быть число ввода")
        self.assertIn("step.tokens_out", body, "и вывод — без него сравнивать не с чем")
        self.assertIn("tokens_in_est", body,
                      "вендор чисел не дал — подставить сюда наш счёт обязательно")
        self.assertIn("reasoning_tokens", body, "размышления считаются в вывод — их надо назвать")
        self.assertIn("FINISH_WORDS", body,
                      "код ответа без перевода читается как код: нужен его смысл")
        self.assertIn("tool_calls:", self.page,
                      "самый частый такой код — ход, в котором модель попросила инструмент")


# --------------------------------------------- правила, цена и простыни

class TestRulesPanel(unittest.TestCase):
    """Вкладка «Правила и инструкции» — и есть редактор: отдельного входа нет.

    Раньше её содержимое пряталось за кнопкой «🔧 Открыть редактор»: лишнее
    нажатие на каждой правке, а прятать было нечего — право править и открывать
    одну и ту же вкладку дважды значит не открыть вовсе.
    """

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def test_the_editor_is_open_from_the_start(self):
        self.assertNotIn("Открыть редактор", self.page)
        self.assertNotIn("toggleInstructionsEditor", self.page,
                         "кнопки нет — и переключателю нечего делать")
        self.assertIn('<div id="instructionsEditor">', self.page)
        self.assertNotIn('id="instructionsEditor" style="display:none', self.page,
                         "редактор снова прячется — это и была прежняя кнопка")

    def test_the_rules_are_loaded_without_a_click(self):
        """Вкладка открыта — значит наполняться она обязана при загрузке."""
        start = self.page.index("refreshMemory();")
        self.assertIn("loadInstructionsForEdit();", self.page[start:start + 400],
                      "без вызова при загрузке вкладка откроется пустой")

    def test_the_saved_editor_stays_on_the_page(self):
        """После «Применить изменения» редактор остаётся на месте."""
        start = self.page.index("function saveInstructions()")
        body = self.page[start:self.page.index("function finishDebate()", start)]
        self.assertNotIn("instructionsEditor').style.display = 'none'", body)


class TestStatusPriceAndLongTexts(unittest.TestCase):
    """В сайдбаре — счёт за весь спектакль всегда; в отчёте хода — три строки.

    Стоимость копится от начала спектакля (см. show._note_money), и раньше она
    пропадала ровно там, где о ней спокойнее всего знать: на ходу человека и под
    занавесом. А длинные тексты (снимок запроса, найденное, размышления) лежали
    простынями на несколько экранов, хотя проматываются целиком.
    """

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def test_the_price_stands_in_every_state_of_the_show(self):
        start = self.page.index("function applyStatus(")
        body = self.page[start:self.page.index("function sendModeratorMessage(", start)]
        self.assertEqual(body.count("spentLine(data)"), 3,
                         "цена нужна в трёх состояниях: чужой ход, ваш ход, занавес")

    def test_a_long_text_is_three_lines_with_a_click_to_open(self):
        self.assertIn(".prompt-text.clamped {", self.page)
        self.assertIn(".prompt-text.clamped.expanded", self.page)
        self.assertIn("body.dark .prompt-text.clamped", self.page,
                      "свёрнутый текст на тёмной сцене — тоже текст")
        start = self.page.index("function loadTurnBox(")
        body = self.page[start:self.page.index("function postTurnHtml(", start)]
        self.assertIn("clampLongTexts(body)", body,
                      "свёртку нечем включить: простыни останутся простынями")


# ---------------------------------------------------------------- посты

class TestPosts(unittest.TestCase):

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)

    def test_role_and_gender_are_rendered(self):
        judge = show.create_post("Судья", "q1", "текст", 1,
                                    role="judge", gender="female")
        self.assertEqual(judge["role_icon"], "⚖️")
        self.assertEqual(judge["role_name"], "Судья")
        self.assertEqual(judge["gender_symbol"], "♀")
        self.assertEqual(judge["gender"], "female")

    def test_content_html_is_built_from_content(self):
        post = show.create_post("Имя", "r1", r"Формула $x^2$ тут", 1)
        self.assertIn('<span class="math"', post["content_html"])
        self.assertIn("$x^2$", post["content"])

    def test_ids_do_not_repeat(self):
        self.session.posts = []
        first = show.create_post("А", "r1", "1", 1)
        self.session.posts.append(first)
        second = show.create_post("Б", "r1", "2", 1)
        self.assertNotEqual(first["id"], second["id"])


# ---------------------------------------------------------------- вид ленты

class TestRoleMarks(unittest.TestCase):
    """Цветная полоса роли слева у реплик (кнопка «Цвет ролей» в подвале).

    Проверяем связку «роль от сервера → класс на посте → правило в стилях»:
    если появится четвёртая роль, тест напомнит, что ей нужен свой цвет.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.page = page.HTML_TEMPLATE

    def test_every_role_produced_by_the_app_has_a_stripe(self):
        roles = {
            show.create_post("А", "r1", "текст", 1)["role"],
            show.create_post("Б", "r1", "текст", 1, role="moderator")["role"],
            show.create_post("В", "r1", "текст", 1, role="judge")["role"],
        }
        self.assertEqual(roles, {"participant", "moderator", "judge"})
        for role in roles:
            with self.subTest(role=role):
                self.assertIn(f".post.post-role-{role} ", self.page,
                              f"у роли {role} нет цвета в стилях")

    def test_stripe_has_its_own_dark_colour(self):
        for role in ("participant", "moderator", "judge"):
            with self.subTest(role=role):
                self.assertIn(f"body.dark.role-marks .post.post-role-{role} ", self.page)

    def test_feed_gets_side_padding_only_with_role_marks(self):
        """Без боковых отступов цветная граница ложится впритык к буквам."""
        self.assertIn("body.role-marks .post { padding-left:", self.page)
        self.assertIn(".post { background: #ffffff; border: none; border-top: 1px solid #000000; padding: 40px 0;",
                      self.page)

    def test_post_gets_its_role_class(self):
        self.assertIn("postDiv.className = `post post-role-${post.role || 'participant'}`",
                      self.page)

    def test_the_growing_reply_is_built_from_the_same_head_as_a_post(self):
        """Черновик и настоящая реплика — один человек, а не два разных.

        Если у растущей реплики была бы своя разметка шапки, аватар с ролью
        и цвет полосы разъехались бы со готовым постом — и это выглядело бы
        как подмена говорящего на середине.
        """
        self.assertIn("function postHeaderHtml(post)", self.page)
        self.assertIn("element.innerHTML = `<div class=\"post-avatar\">${postAvatarHtml(draft)}",
                      self.page)
        # И та же полоса роли: без класса роли черновик терял бы цвет на ходу
        self.assertIn("element.className = `post post-role-${draft.role || 'participant'} streaming`",
                      self.page)

    def test_the_thoughts_stay_in_the_finished_reply(self):
        """Готовый пост хранит мысли — шагом хронологии: читать можно и после занавеса.

        Отдельным разделом они были лишними: размышления — такое же событие
        хода, как запрос и поиск, и стоят в общем порядке со своим временем.
        """
        start = self.page.index("function turnBodyHtml(")
        body = self.page[start:self.page.index("function loadTurnBox(", start)]
        self.assertIn("step.kind === 'thought'", body,
                      "мысли пропали из хронологии готовой реплики")
        self.assertIn("escapeHtml(step.text", body,
                      "мысли показываем текстом, а не разметкой")
        self.assertIn("stepClock(step)", body,
                      "у события хронологии должно быть время")
        # И в свёрнутой строке видно, что они были
        self.assertIn("info.thought_steps", self.page)
        # У черновика свой — открытый — блок: там видно, что модель ещё думает
        self.assertIn('<details class="post-thinking" open>', self.page)

    def test_the_thoughts_have_their_own_dim_block(self):
        """Мысли — не реплика: у них свой бледный блок над ответом."""
        self.assertIn(".post-thinking { margin: 0 0 18px 0;", self.page)
        self.assertIn("body.dark .post-thinking {", self.page)
        self.assertIn("thoughts.querySelector('.thinking-text').textContent = thought;",
                      self.page)
        self.assertIn("draft.answer_started ? '💭 мысли' : '💭 размышляет'", self.page)
        # Разметка в черновике собирается на каждой порции — текст жирнеет на глазах
        self.assertIn("postText.innerHTML = draft.content_html || escapeHtml(draft.content || '')",
                      self.page)

    def test_the_rewritten_reply_is_shown_as_said_earlier(self):
        """Прежняя версия реплики не пропадает в тишину, а живёт свёрткой.

        Модель отвечает, потом её просят поискать, и она отвечает заново. Текст
        первой версии уже мелькнул в ленте (и оплачен) — значит, у него должно
        быть место и в черновике, и в готовом посте.
        """
        self.assertIn('post-thinking post-sketch', self.page)
        self.assertIn('🌱 сказано раньше ·', self.page)
        # В черновике этот блок есть сразу: он заполняется по мере хода
        self.assertIn("const sketch = element.querySelector('.post-sketch');", self.page)
        self.assertIn("sketch.querySelector('.thinking-text').textContent = said;", self.page)
        # А в готовой реплике набросок — раздел того же блока о ходе, а не
        # отдельная простыня рядом с репликой (см. TestTurnPanel)
        start = self.page.index("function turnBodyHtml(")
        body = self.page[start:self.page.index("function loadTurnBox(", start)]
        self.assertIn("data.sketch", body, "набросок пропал из готового поста")
        self.assertIn("SKETCH_HINT", body, "и пояснение к нему тоже должно остаться")
        # У мыслей свой блок, и селектор их не должен хватать набросок вместо них
        self.assertIn("element.querySelector('.post-thinking:not(.post-sketch)')", self.page)

    def test_the_said_before_block_has_its_own_look(self):
        """«Сказано раньше» — не мысли, и выглядят иначе: читается как версия."""
        self.assertIn(".post-sketch { border-left-color:", self.page)
        self.assertIn("body.dark .post-sketch { border-left-color:", self.page)
        self.assertIn(".post-sketch .thinking-hint {", self.page)

    def test_toggle_is_wired_and_remembered(self):
        self.assertIn('id="rolesBtn"', self.page)
        self.assertIn("onclick=\"toggleRoleMarks()\"", self.page)
        self.assertIn("classList.toggle('role-marks'", self.page)
        self.assertIn("localStorage.setItem('theatreRoleMarks'", self.page)
        self.assertIn("localStorage.getItem('theatreRoleMarks')", self.page)


# ---------------------------------------------------------------- тонкая настройка

class TestTuningPanel(unittest.TestCase):
    """«⚙ Тонкая настройка» должна уметь всё, что принимает сервер.

    Список чисел лежит в двух местах: PER_PARTICIPANT_OPTION_KEYS в приложении
    и PARAM_KEYS в пульте. Разошлись — и параметр можно задать только в
    PARTICIPANTS: из пульта его не видно, в строке «Уйдёт в модель» тоже.
    Так уже было с top_k, min_p и seed: их принимал сервер, а пульт — нет.
    """

    PARAM_KEYS_RE = re.compile(r"const PARAM_KEYS = \[(.*?)\];", re.DOTALL)

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def test_pult_lists_the_same_parameters_as_the_server(self):
        match = self.PARAM_KEYS_RE.search(self.page)
        self.assertIsNotNone(match, "в пульте не нашёлся список PARAM_KEYS")
        listed = re.findall(r"'([^']+)'", match.group(1))
        self.assertEqual(listed, list(settings.PER_PARTICIPANT_OPTION_KEYS),
                         "пульт и сервер разошлись в списке параметров генерации")

    def test_every_parameter_has_a_field_in_the_editor(self):
        missing = [key for key in settings.PER_PARTICIPANT_OPTION_KEYS
                   if f"paramField('{key}'" not in self.page]
        self.assertEqual(missing, [], f"у этих параметров нет поля в пульте: {missing}")

    def test_editor_sends_parameters_from_the_one_list(self):
        """Свой список чисел внутри collectCast когда-то и обрезал пульт до пяти."""
        start = self.page.index("function collectCast()")
        body = self.page[start:self.page.index("function saveCast()", start)]
        self.assertIn("PARAM_KEYS.forEach", body)
        self.assertNotIn("'temperature'", body,
                         "список чисел в пульте должен быть один — PARAM_KEYS")


class TestScenePanel(unittest.TestCase):
    """Пульт должен уметь всё, что умеет сцена: добавить, убрать, переставить, роль.

    Проверяется по разметке, как TestTuningPanel: сервер про такую потерю не
    скажет ничего — он просто не получит запроса.
    """

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def test_every_scene_action_has_a_control(self):
        for control in ("addCast()", "removeCast(", "moveCast(", "setCastRole(",
                        "resetEverything()"):
            with self.subTest(control=control):
                self.assertIn(control, self.page, f"в пульте нет управления {control}")

    def test_every_endpoint_the_page_calls_exists_on_the_server(self):
        """Страница и сервер — одна пара: переименованный маршрут иначе не заметить.

        Разметку не исполняет ни один тест — переименованный на сервере маршрут
        замечается только тогда, когда кнопку нажмёт режиссёр.
        """
        called = set(re.findall(r"fetch\(['\"`](/api/[^'\"`]+)", self.page))
        self.assertTrue(called, "в пульте не нашлось ни одного обращения к серверу")
        rules = [rule.rule for rule in web_app.app.url_map.iter_rules()
                 if rule.rule.startswith("/api/")]

        def known(url):
            # В разметке номер поста и ключевые слова — подстановки, в маршрутах — <...>
            clean = re.sub(r"\$\{[^}]*\}", "X", url).split("?")[0].rstrip("/")
            return any(re.fullmatch(re.sub(r"<[^>]+>", "X", rule).rstrip("/"), clean)
                       for rule in rules)

        missing = sorted(url for url in called if not known(url))
        self.assertEqual(missing, [],
                         f"страница зовёт маршруты, которых нет на сервере: {missing}")

    def test_the_role_list_matches_the_server(self):
        match = re.search(r"const CAST_ROLES = \[(.*?)\];", self.page, re.DOTALL)
        self.assertIsNotNone(match, "в пульте не нашёлся список ролей")
        self.assertEqual(re.findall(r"value: '([^']+)'", match.group(1)),
                         list(show.CAST_ROLES),
                         "пульт и сервер разошлись в списке ролей")

    def test_the_editor_sends_the_place_id_and_the_role(self):
        """Без cast_id перестановка уехала бы к соседу, а роль было бы не сменить."""
        start = self.page.index("function collectCast()")
        body = self.page[start:self.page.index("function saveCast()", start)]
        self.assertIn("cast_id: p.cast_id", body)
        self.assertIn("role: roleOf(p)", body)

    def test_the_option_lists_match_the_server(self):
        """Пульт и сервер должны сходиться и в значениях опций роли, а не только в ролях."""
        match = re.search(r"const ROLE_OPTIONS = \{(.*?)\n        \};", self.page, re.DOTALL)
        self.assertIsNotNone(match, "в пульте не нашёлся список опций роли")
        listed = re.findall(r"(\w+): \[(.*?)\],\n", match.group(1), re.DOTALL)
        self.assertTrue(listed, "в пульте нет ни одной опции")
        for key, body in listed:
            with self.subTest(option=key):
                values = re.findall(r"value: '([^']+)'", body)
                self.assertEqual(values, list(show.ROLE_OPTIONS["judge"][key]),
                                 f"пульт и сервер разошлись в опции {key}")

    def test_the_editor_sends_the_role_options(self):
        start = self.page.index("function collectCast()")
        body = self.page[start:self.page.index("function saveCast()", start)]
        self.assertIn("entry.role_options", body,
                      "без этого опция роли осталась бы только на экране")

    def test_the_role_option_field_is_named_like_the_collector_looks_for_it(self):
        """Поле опции и сборщик состава обязаны называть её одинаково.

        Поле собирается в карточке, а читает его collectCast по id: разойдись они
        хоть на символ — выбранное положение роли молча не доедет до сервера,
        а место будет играть не так, как обещает подсказка.
        """
        start = self.page.index("const roleOptionsBlock")
        filler = self.page[start:self.page.index("return ''", start)]
        for piece in ("id=\"' + key + '-' + idx + '\"",
                      "ROLE_OPTION_NAMES[key]", "ROLE_OPTION_HINTS[key]"):
            with self.subTest(piece=piece):
                self.assertIn(piece, filler, "опция роли собирается не из ключа и номера места")

        start = self.page.index("function collectCast()")
        body = self.page[start:self.page.index("function saveCast()", start)]
        self.assertIn("document.getElementById(`${key}-${idx}`)", body)
        self.assertIn("ROLE_OPTIONS[roleOf(p)]", body,
                      "без этого места с новой ролью унесли бы чужие опции")

    def test_every_name_pasted_into_the_markup_is_declared(self):
        """Имя, подставленное в разметку карточки, но нигде не объявленное, роняет её целиком.

        Так уже случилось: в карточку добавили roleOptionsBlock, а сам блок собрать
        забыли — скрипт падал на первой же карточке, и пульт показывал пустоту.
        Ни один тест этого не видел, потому что разметку никто не исполняет.
        """
        for line in self.page.splitlines():
            match = re.fullmatch(r"\s*\+\s*([A-Za-z_$][\w$]*)\s*", line)
            if not match:
                continue
            name = match.group(1)
            with self.subTest(name=name):
                self.assertRegex(
                    self.page,
                    r"(?:const|let|var|function)\s+" + re.escape(name) + r"\b",
                    f"в разметку карточки подставляется {name}, а такого имени в странице нет")

    def test_an_unchosen_model_is_not_silently_replaced(self):
        """Пустое место — это «модель ещё не выбрана», а не «первая модель Ollama».

        Раньше поле было списком, и браузер сам подставлял первую модель, когда
        подходящего варианта не находилось: перестановка соседа тихо записывала её
        в участника. Теперь поле — текст: браузеру подставлять нечего, и пустое
        значение так и остаётся пустым.
        """
        self.assertIn('<input type="text" id="model-', self.page)
        self.assertIn("— выберите модель —", self.page)
        self.assertNotIn('<select id="model-', self.page)

    def test_the_sidebar_shows_whether_the_judge_is_public(self):
        """Судья-публичный меняет спектакль на ходу — это должно быть видно сразу."""
        start = self.page.index("function updateSidebarParticipants()")
        body = self.page[start:self.page.index("function saveInstructions()", start)]
        self.assertIn("roleOptionsOf(p).publicity", body,
                      "в сайдбаре не видно, слышат ли судью остальные")

    def test_the_new_controls_use_the_routes_that_exist(self):
        for route in ("/api/participants/draft", "/api/settings/reset"):
            with self.subTest(route=route):
                self.assertIn(route, self.page)
                self.assertIn(route, {rule.rule for rule in web_app.app.url_map.iter_rules()})


# ---------------------------------------------------------------- облако

class FakeGateway:
    """Заглушка облачного шлюза — настоящий HTTP-сервер в отдельном потоке.

    Подменять здесь нечего: модуль обязан отправить настоящий запрос и разобрать
    настоящий ответ, иначе проверка формы запроса не значит ничего. Сервер
    слушает 127.0.0.1 и порт, который выберет система, — в интернет тесты не ходят.
    """

    def __init__(self, models=("qwen/qwen3.7-flash",), content="Канберра.",
                 status=200, body_text=None, statuses=(), stream_text=None, texts=(),
                 balances=()):
        self.requests = []        # что до нас донеслось: метод, путь, ключ, тело
        self.balances = list(balances)   # остаток на ключе: очередь для «до» и «после»
        self.models = list(models)
        self.content = content
        self.status = status
        self.statuses = list(statuses)   # очередь кодов ответа: для повторов после 429
        self.body_text = body_text    # сырой ответ вместо обычного: ошибки и мусор
        self.stream_text = stream_text    # поток «data: …» вместо обычного ответа
        self.texts = list(texts)          # очередь тел ответа (первое может быть отказом)

        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                gateway.requests.append({
                    "method": method,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "body": json.loads(raw) if raw else None,
                })
                payload = gateway.answer(self.path)
                self.send_response(gateway.next_status())
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def log_message(self, *args):
                pass    # иначе каждый запрос печатался бы в вывод тестов

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        # Свой цикл вместо serve_forever: остановка такого сервера ждала бы
        # полсекунды (интервал опроса), а вспомогательный сервер — это доли
        # миллисекунды, иначе 34 прогона проверки тестов каждый раз съедали минуту
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def next_status(self) -> int:
        """Код ответа: очередь задана — отдаём по одному, иначе всегда один и тот же."""
        return self.statuses.pop(0) if self.statuses else self.status

    def _serve(self):
        self.httpd.timeout = 0.1
        while True:
            try:
                self.httpd.handle_request()
            except (OSError, ValueError):
                break    # сокет закрыт — просили остановиться

    def answer(self, path: str) -> bytes:
        # Остаток на ключе — свой адрес и свой ответ: он не модель, и очередь
        # ответов шлюза он тратить не должен (иначе одна проверка съедала бы
        # заготовленный ответ другой). Значений два и больше — берём по одному:
        # именно так читается цена хода, «до» и «после»
        if path.rstrip("/").endswith("/balance"):
            if not self.balances:
                value = 0.0
            elif len(self.balances) == 1:
                value = self.balances[0]
            else:
                value = self.balances.pop(0)
            return json.dumps({"balance": value}).encode("utf-8")
        if self.texts:
            # Очередь тел ответа: нужна там, где первый ответ — отказ, а второй
            # должен быть настоящим (иначе повторить запрос нечего)
            return self.texts.pop(0).encode("utf-8")
        if self.stream_text is not None:
            return self.stream_text.encode("utf-8")
        if self.body_text is not None:
            return self.body_text.encode("utf-8")
        if path.endswith("/models"):
            payload = {"object": "list",
                       "data": [{"id": name} for name in self.models]}
        else:
            payload = {"choices": [{"message": {"content": self.content}}],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def last_request(self) -> dict:
        return self.requests[-1]

    def stop(self):
        self.httpd.server_close()
        self.thread.join(timeout=2)


class TestSearchAskedInWords(unittest.TestCase):
    """Модель может попросить поиск не протоколом, а текстом.

    Обе формы — из настоящей ленты: так просили z-ai/glm-5.3-flash («search:
    web_search{query: …}» посреди реплики) и google/gemma-4-31b-it («call:
    google_search:search{queries:[…]}» вместо реплики целиком). Вызов уезжал
    в пост, поиск не выполнялся, и «Источников» в ленте не было.
    """

    def test_the_call_from_the_feed_is_understood(self):
        clean, queries = tooltext.take_calls(
            "Открываю вкладки и делаю быстрый поиск.\n\n"
            'search:web_search{query: "Сыктывкар новости сейчас"}\n\n'
            "Так, пока Владимир крутит запросы, внесу свою лепту.")

        self.assertEqual(queries, ["Сыктывкар новости сейчас"])
        self.assertEqual(clean,
                         "Открываю вкладки и делаю быстрый поиск.\n\n"
                         "Так, пока Владимир крутит запросы, внесу свою лепту.")

    def test_the_google_shape_with_several_queries(self):
        clean, queries = tooltext.take_calls(
            'call:google_search:search{queries:["последние новости Сыктывкар", '
            '"мероприятия Сыктывкар филармония"]}')

        self.assertEqual(queries, ["последние новости Сыктывкар",
                                   "мероприятия Сыктывкар филармония"])
        self.assertEqual(clean, "", "кроме просьбы в такой реплике ничего нет")

    def test_a_call_wrapped_in_tags(self):
        clean, queries = tooltext.take_calls(
            'Проверю погоду. <tool_call>{"name": "search_web", '
            '"arguments": {"query": "погода Сыктывкар"}}</tool_call>')

        self.assertEqual(queries, ["погода Сыктывкар"])
        self.assertEqual(clean, "Проверю погоду.")

    def test_an_unfinished_call_does_not_stay_in_the_reply(self):
        """Оборванный по времени ход оставлял в реплике половину вызова."""
        clean, queries = tooltext.take_calls('Поищу.\n\nsearch:web_search{query: "Сыктывкар')

        self.assertEqual(clean, "Поищу.")
        self.assertEqual(queries, [])

    def test_ordinary_speech_is_untouched(self):
        """Скобки и слово search в обычной реплике — не повод что-то вынимать."""
        for speech in ("Функция search возвращает список, а параметр {query} в ней строковый.",
                       "Итог: **2:1** в пользу Сыктывкара — {отличный} результат."):
            with self.subTest(speech=speech):
                self.assertEqual(tooltext.take_calls(speech), (speech, []))

    def test_the_same_query_twice_asks_once(self):
        _clean, queries = tooltext.take_calls(
            'search_web{query: "погода Сыктывкар"} и ещё раз '
            'search:google_search{query: "Погода сыктывкар"}')

        self.assertEqual(queries, ["погода Сыктывкар"],
                         "один и тот же запрос — один поиск")


class TestCloudGateway(unittest.TestCase):
    """Облачный участник: ход уходит на шлюз в формате OpenAI.

    Заглушка — живой сервер (см. FakeGateway). Проверяется форма запроса (имя
    модели без префикса, ключ в заголовке, числа наверху, а не в «options»),
    разбор ответа и то, что ключ никуда не утекает.
    """

    KEY = "test-key-1234567890"
    MODEL = "cloud:qwen/qwen3.7-flash"

    def setUp(self):
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.stop)
        self.saved = (settings.CLOUD_BASE_URL, settings.CLOUD_API_KEY)
        self.addCleanup(self._restore_settings)
        settings.CLOUD_BASE_URL = self.gateway.base_url
        settings.CLOUD_API_KEY = self.KEY
        # Ключ берётся ещё и из переменной окружения, а набор обязан вести себя
        # одинаково на любой машине: без этого «ключа нет» ничего бы не проверяло
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        # Ключ может лежать в настоящем окружении или быть поднят из .env при
        # импорте: для чистоты опыта убираем все имена, под которыми он заводится
        for name in ("CLOUD_API_KEY", settings.CLOUD_KEY_ENV, "CLOUD_KEY_ENV"):
            os.environ.pop(name, None)
        self.addCleanup(self._clear_cache)
        # Отказ от инструмента поиска — тоже память процесса: без этого одна
        # проверка влияла бы на следующую
        self.addCleanup(cloud._MODELS_WITHOUT_TOOLS.clear)
        self.addCleanup(cloud._NO_TOOL_CHOICE.clear)
        # Никаких зависимостей от запущенной Ollama: список местных моделей,
        # проверка «умеет ли модель размышлять» (у каждой модели это /api/show!)
        # и опрос загруженных моделей подменяются на мгновенные заглушки
        for name, value in (
            ("fetch_ollama_models", mock.Mock(return_value=({}, None))),
            ("model_supports_thinking", mock.Mock(return_value=False)),
            ("fetch_loaded_models", mock.Mock(return_value=([], None))),
            ("fetch_gpu_memory", mock.Mock(return_value={})),
        ):
            patcher = mock.patch.object(ollama_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_settings(self):
        settings.CLOUD_BASE_URL, settings.CLOUD_API_KEY = self.saved

    def _clear_cache(self):
        cloud._MODELS_CACHE = {"at": 0.0, "names": [], "error": None}

    def test_the_request_goes_in_the_openai_shape(self):
        content, tools = cloud.chat(
            self.MODEL, [{"role": "user", "content": "Привет!"}],
            options={"temperature": 0.8, "min_p": 0.2, "num_predict": 512})

        sent = self.gateway.last_request()
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["authorization"], f"Bearer {self.KEY}")
        self.assertEqual(sent["body"]["model"], "qwen/qwen3.7-flash",
                         "префикс пульта на шлюз уезжать не должен")
        self.assertEqual(sent["body"]["messages"],
                         [{"role": "user", "content": "Привет!"}])
        self.assertNotIn("options", sent["body"], "вложенные числа — диалект Ollama")
        self.assertNotIn("think", sent["body"], "и think тоже")
        self.assertEqual(content, "Канберра.")
        self.assertEqual(tools, [])

    def test_the_body_is_as_simple_as_possible(self):
        """Сначала — самый простой разговор: модель, сообщения и stream.

        Каждый лишний ключ в теле — повод для «400 Bad Request», а двадцать
        таких ответов за минуту уводят ключ в паузу (и всё получает 429).
        """
        cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!",
                                 "name": "иван"}],
                   options={"temperature": 0.8, "seed": 7})

        sent = self.gateway.last_request()["body"]
        self.assertEqual(sorted(sent), ["messages", "model", "stream"],
                         "в теле только модель, сообщения и stream")
        self.assertEqual(sent["messages"], [{"role": "user", "content": "Привет!"}],
                         "русское имя в поле name шлюз отвергает: оно уезжать не должно")

    def test_the_numbers_and_tools_can_be_turned_on(self):
        """Числа характеров и поиск — не запрет, а отдельные выключатели."""
        for name in ("CLOUD_SEND_PARAMS", "CLOUD_SEND_TOOLS",
                     "CLOUD_SEND_MESSAGE_NAMES"):
            cloud_setting(self, name, True)

        cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!", "name": "иван"}],
                   options={"temperature": 0.8, "num_predict": 512, "min_p": 0.2})

        sent = self.gateway.last_request()["body"]
        self.assertEqual(sent["temperature"], 0.8)
        self.assertEqual(sent["max_tokens"], 512, "num_predict у OpenAI — max_tokens")
        self.assertNotIn("min_p", sent, "в схеме OpenAI такого поля нет")
        self.assertEqual(sent["tools"][0]["function"]["name"], "search_web")
        self.assertEqual(sent["messages"][0]["name"], "иван")

    def test_the_numbers_of_a_character_are_brought_into_the_vendor_range(self):
        """Характеры придуманы для Ollama, а у вендора свой диапазон.

        «Мусор на трёх языках сразу» — не характер модели, а температура за её
        краем: у GLM рабочая температура до 0.99, а в наборе «Хаос» — 2.0.
        Поэтому число отдаём границей, а не как есть.
        """
        self.addCleanup(cloud._LIMIT_WARNED.clear)

        sent = cloud.translate_options({"temperature": 2.0, "top_p": 1.0,
                                        "presence_penalty": 1.5}, self.MODEL)
        self.assertEqual(sent["temperature"], 1.0, "за краем диапазона — граница, а не 2.0")
        self.assertEqual(sent["presence_penalty"], 1.0)
        self.assertEqual(sent["top_p"], 1.0, "в пределах диапазона число не трогаем")

        cloud_setting(self, "CLOUD_LIMIT_PARAMS", False)
        self.assertEqual(cloud.translate_options({"temperature": 2.0}, self.MODEL)["temperature"],
                         2.0, "выключатель на месте: без него числа уезжают как есть")

    def test_a_turn_that_never_ends_is_cut_by_the_clock(self):
        """Зациклившуюся модель останавливает срок на ход целиком.

        Кусочки от неё приходят исправно, поэтому CLOUD_TIMEOUT её не ловит:
        модель молотит случайные токены полчаса, а спектакль всё стоит.
        """
        self.gateway.stream_text = self._sse(self._piece("мусор"))
        with mock.patch.object(cloud, "turn_limit", mock.Mock(return_value=0.000001)):
            content, tools = cloud.chat(self.MODEL, [{"role": "user", "content": "?"}],
                                        on_delta=lambda piece, replace: None)

        self.assertIn("CLOUD_TURN_LIMIT", content, "об обрыве надо сказать вслух")
        self.assertEqual(tools, [])

    def test_the_cut_explains_itself_and_names_the_search_trouble(self):
        """Обрыв хода должен называть и срок, и причину, а не врать про «без предела».

        Тот самый случай судьи: в ленте появилось «ход длился дольше 120 с
        и оборван … (CLOUD_TURN_LIMIT, 0 — без предела)» — хотя предел был, и он
        же и сработал. А в этом случае самое важное — что правила про поиск
        в промпте были, а инструмента модель не получила: именно из этого
        рождается петля, в которой модель не переходит к ответу.
        """
        self.gateway.stream_text = self._sse(self._piece("мусор"))
        cloud_setting(self, "CLOUD_SEND_TOOLS", False)
        cloud_setting(self, "ENABLE_SEARCH", True)
        with mock.patch.object(cloud, "turn_limit", mock.Mock(return_value=0.000001)):
            content, _ = cloud.chat(self.MODEL, [{"role": "user", "content": "?"}],
                                    on_delta=lambda piece, replace: None, report={})

        self.assertNotIn("без предела", content,
                         "срок был — и именно он сработал")
        self.assertIn("не отправлен", content,
                      "в этом и причина петли: про поиск в промпте — а инструмента нет")

    def test_a_cut_turn_keeps_what_was_said(self):
        """Оборванный по сроку ход отдаёт сказанное: половина реплики лучше ничего."""
        class SlowStream:
            """Поток, который приходит по кусочку, а не весь сразу."""

            def __iter__(self):
                yield self._line("Ска")
                time.sleep(0.5)      # шлюз «думает» дольше отведённого срока
                yield self._line("зал.")

            @staticmethod
            def _line(text):
                piece = {"choices": [{"delta": {"content": text}}]}
                return f"data: {json.dumps(piece, ensure_ascii=False)}".encode("utf-8")

        pieces = []
        answer = cloud._read_stream(SlowStream(),
                                    lambda piece, replace: pieces.append(piece),
                                    None, time.monotonic() + 0.05)

        self.assertTrue(answer["cut"], "ход обязан оборваться по сроку")
        self.assertEqual(answer["content"], "Ска", "сказанное не теряется")
        self.assertEqual(pieces, ["Ска"], "лента уже показала первый кусок")

    @staticmethod
    def _sse(*chunks) -> str:
        """Поток шлюза: куски строками «data: {…}», в конце — «data: [DONE]»."""
        lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks]
        return "\n\n".join(lines) + "\n\ndata: [DONE]\n\n"

    @staticmethod
    def _piece(text: str) -> dict:
        """Одна порция текста в потоке: так её отдают шлюзы OpenAI."""
        return {"choices": [{"delta": {"content": text}}]}

    def test_a_streamed_reply_survives_without_a_draft_in_the_feed(self):
        """Поток без черновика в ленте — не поломка шлюза.

        Ход без черновика приходит с одним только сборщиком размышлений,
        и получателя текста у него нет вовсе: звать его было нельзя. Кончилось
        это не ошибкой в консоли, а ответом модели, в котором человек читал
        «шлюз недоступен по адресу …: 'NoneType' object is not callable».
        """
        self.gateway.stream_text = self._sse(self._piece("Канберра."))
        cloud_setting(self, "CLOUD_SEND_TOOLS", False)

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}],
                                     on_delta=None, on_thought=lambda piece, replace: None)

        self.assertEqual(content, "Канберра.",
                         "реплика должна доехать целиком, а не превратиться в ошибку")
        self.assertNotIn("NoneType", content)

        # И то же самое через ход целиком: поток есть, черновика нет
        self.gateway.stream_text = self._sse(self._piece("Канберра."))
        spoken, _count, _queries = ollama_api.ask_model(
            self.MODEL, [{"role": "user", "content": "Привет!"}],
            participant_name="Проверка")
        self.assertEqual(spoken, "Канберра.")

    def test_the_reply_grows_in_pieces_while_the_model_speaks(self):
        """Ответ умеет приходить по кускам, а собираться в тот же целый текст."""
        self.gateway.stream_text = self._sse(self._piece("Кан"), self._piece("бер"),
                                             self._piece("ра."))
        pieces = []
        content, tools = cloud.chat(
            self.MODEL, [{"role": "user", "content": "Привет!"}],
            on_delta=lambda piece, replace: pieces.append((piece, replace)))

        self.assertTrue(self.gateway.last_request()["body"]["stream"],
                        "поток надо попросить у шлюза")
        self.assertEqual(content, "Канберра.", "целый текст собирается из кусков")
        self.assertEqual(pieces, [("Кан", True), ("бер", False), ("ра.", False)],
                         "первый кусок начинает ответ заново, остальные — продолжают его")
        self.assertEqual(tools, [])

    def test_a_tool_call_is_glued_from_the_stream(self):
        """Вызов инструмента в потоке разрезан на части — их надо склеить.

        Имя и аргументы приходят фрагментами строки: прочитать первый фрагмент
        как готовый вызов — значит потерять поиск (у инструмента осталась бы
        половина имени, а аргументы вообще не разобрались бы).
        """
        self.gateway.stream_text = self._sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_7", "type": "function",
                 "function": {"name": "search_", "arguments": '{"query": "сто'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "web",
                                          "arguments": 'лица"}'}}]}}]},
        )

        # Поток просим только тогда, когда есть кому его показывать: без
        # получателя порций ответ читался бы целиком, и разбирать его как поток
        # было бы незачем
        content, tools = cloud.chat(self.MODEL, [{"role": "user", "content": "?"}],
                                    on_delta=lambda piece, replace: None)

        self.assertEqual(content, "")
        self.assertEqual(tools[0]["function"]["name"], "search_web")
        self.assertEqual(tools[0]["function"]["arguments"], {"query": "столица"})
        self.assertEqual(tools[0]["id"], "call_7")

    def test_a_gateway_that_ignores_the_stream_still_answers(self):
        """Просить поток — не повод потерять реплику: годится и обычный ответ."""
        self.gateway.body_text = json.dumps(
            {"choices": [{"message": {"content": "Канберра."}}]})

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}],
                                     on_delta=lambda piece, replace: None)

        self.assertEqual(content, "Канберра.")

    def test_streaming_can_be_switched_off(self):
        """Выключатель на месте: без него ответ приходит целиком, как раньше."""
        cloud_setting(self, "CLOUD_STREAM", False)

        pieces = []
        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}],
                                     on_delta=lambda piece, replace: pieces.append(piece))

        self.assertFalse(self.gateway.last_request()["body"]["stream"])
        self.assertEqual(pieces, [], "поток выключен — порций быть не должно")
        self.assertEqual(content, "Канберра.")

    def test_the_thinking_comes_in_its_own_channel(self):
        """Размышления — не реплика: у них свой получатель.

        Шлюз отдаёт их отдельным полем (у proxyapi — «reasoning»), и лента рисует
        их своим бледным блоком над ответом. В реплику они не попадают.
        """
        self.gateway.stream_text = self._sse(
            {"choices": [{"delta": {"reasoning": "Сначала подумаю"}}]},
            {"choices": [{"delta": {"reasoning": ", а потом скажу."}}]},
            self._piece("Сказал."))
        reply, thoughts = [], []

        content, _tools = cloud.chat(
            self.MODEL, [{"role": "user", "content": "Привет!"}],
            on_delta=lambda piece, replace: reply.append((piece, replace)),
            on_thought=lambda piece, replace: thoughts.append((piece, replace)))

        self.assertEqual(content, "Сказал.", "в реплике только сказанное")
        self.assertEqual(reply, [("Сказал.", True)],
                         "мыслм не считаются началом реплики")
        self.assertEqual(thoughts, [("Сначала подумаю", True), (", а потом скажу.", False)],
                         "первая порция мыслей начинает их заново, остальные — продолжают")

    def test_the_thinking_under_another_name_is_understood(self):
        """У DeepSeek и vLLM то же поле зовётся иначе — читаем и его."""
        self.gateway.stream_text = self._sse(
            {"choices": [{"delta": {"reasoning_content": "Думаю по-другому"}}]})
        thoughts = []
        cloud.chat(self.MODEL, [{"role": "user", "content": "?"}],
                   on_delta=lambda piece, replace: None,
                   on_thought=lambda piece, replace: thoughts.append(piece))
        self.assertEqual(thoughts, ["Думаю по-другому"])

    def test_the_thinking_is_not_shown_when_switched_off(self):
        """Выключатель на месте: тогда лента показывает только сказанное."""
        cloud_setting(self, "CLOUD_SHOW_THINKING", False)
        self.gateway.stream_text = self._sse(
            {"choices": [{"delta": {"reasoning": "Мысль"}}]}, self._piece("Ответ."))
        thoughts = []
        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "?"}],
                                     on_delta=lambda piece, replace: None,
                                     on_thought=lambda piece, replace: thoughts.append(piece))
        self.assertEqual(thoughts, [])
        self.assertEqual(content, "Ответ.")

    def test_a_new_openai_model_gets_the_token_field_it_asks_for(self):
        """Новые модели OpenAI отвергают max_tokens и просят max_completion_tokens.

        Об этом шлюз пишет сам, поэтому поле переименовывается по его же словам
        и запоминается. Иначе ход пропадал бы на каждом круге, а серия отказов
        уводила бы ключ в паузу, и всё превращалось бы в 429.
        """
        self.addCleanup(cloud._RENAMED_MAX_TOKENS.discard, self.MODEL)
        # Отказ возникает именно из-за числа характера: без него поля в теле нет
        cloud_setting(self, "CLOUD_SEND_PARAMS", True)
        self.gateway.statuses = [400, 200]
        self.gateway.texts = [
            json.dumps({"error": {"message": "Unsupported parameter: 'max_tokens' is not "
                                                "supported with this model. Use "
                                                "'max_completion_tokens' instead."}}),
            json.dumps({"choices": [{"message": {"content": "Канберра."}}]}),
        ]

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}],
                                     options={"num_predict": 500})

        self.assertEqual(content, "Канберра.", "ход должен состояться, а не пропасть")
        self.assertEqual(self.gateway.requests[0]["body"]["max_tokens"], 500)
        self.assertNotIn("max_tokens", self.gateway.requests[1]["body"])
        self.assertEqual(self.gateway.requests[1]["body"]["max_completion_tokens"], 500)
        self.assertEqual(cloud.max_tokens_field(self.MODEL), "max_completion_tokens",
                         "запомнили — чтобы отказ не повторился на следующем ходу")

    def test_a_crowded_gateway_is_asked_again_after_a_pause(self):
        """429 — просьба сбавить темп: повторяем, а не считаем ход сломанным."""
        self.gateway.statuses = [429, 200]
        settings.CLOUD_RETRY_DELAYS = (0, 0, 0)   # ждать в тесте нечего
        self.addCleanup(setattr, settings, "CLOUD_RETRY_DELAYS", (1, 2, 4))

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertEqual(content, "Канберра.")
        self.assertEqual(len(self.gateway.requests), 2, "после 429 запрос повторяется")

    def test_the_other_errors_are_not_repeated(self):
        """4xx (кроме 429) — про сам запрос: повторы только продлили бы паузу по ключу."""
        self.gateway.status = 400
        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertIn("HTTP 400", content)
        self.assertEqual(len(self.gateway.requests), 1)

    def test_a_cloud_turn_goes_through_the_common_entrance(self):
        """Весь остальной код зовёт модель через ask_model_with_tools — и облако тоже."""
        content, _tools = ollama_api.ask_model_with_tools(
            self.MODEL, [{"role": "user", "content": "Столица Австралии?"}])
        self.assertEqual(content, "Канберра.")
        self.assertTrue(self.gateway.requests, "ход не дошёл до шлюза")
        # От этой метки зависит принудительный поиск в ask_model: спросить
        # у Ollama про облачную модель нельзя, поэтому её ставит сам облачный путь —
        # и только тогда, когда инструмент правда отправляется
        self.assertEqual(ollama_api.MODELS_TOOLS_SUPPORT[self.MODEL], cloud.send_tools())

    def test_a_stale_dotenv_line_points_to_the_settings_file(self):
        """Строка, переехавшая в settings.py, не молчит, а говорит, куда её нести.

        Из образца .env в старом виде такие строки копировали пачкой, и после
        переезда они остались бы в файле — а вместе с ними и включённый поиск,
        который теперь не включается. Молча не применённую настройку потом ищут
        в шлюзе; поэтому приложение называет строку и печатает готовую строку
        для settings.py — с тем самым значением, что стояло в .env.
        """
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".env").write_text(
                "CLOUD_API_KEY=test-key\n"
                "Включить: CLOUD_SEND_PARAMS=1\n"
                "CLOUD_SEND_TOOLS=1\n"
                "CLOUD_RETRY_DELAYS=1, 2, 4\n",
                encoding="utf-8")
            env = mock.patch.dict(os.environ)
            env.start()
            self.addCleanup(env.stop)
            for name in cloud.CLOUD_ENV_NAMES:
                os.environ.pop(name, None)
            with mock.patch.object(settings, "PROJECT_ROOT", Path(folder)):
                with mock.patch("builtins.print") as fake_print:
                    cloud._load_dotenv()
        printed = _printed_text(fake_print)

        self.assertIn("CLOUD_SEND_TOOLS = True", printed,
                      "переезд обязан подсказать готовую строку для settings.py")
        self.assertIn("CLOUD_SEND_PARAMS = True", printed,
                      "и подпись перед именем — это та же настройка")
        self.assertIn("CLOUD_RETRY_DELAYS = (1, 2, 4)", printed,
                      "подсказка должна быть того же типа, что в settings.py, "
                      "а не строкой «1, 2, 4»")
        self.assertIn(cloud.CLOUD_SETTINGS_FILE, printed)
        self.assertNotIn("CLOUD_SEND_TOOLS", os.environ,
                         "строку из .env применять нельзя: у настройки один хозяин")
        self.assertEqual(os.environ.get("CLOUD_API_KEY"), "test-key",
                      "ключ из .env по-прежнему подхватывается")

    def test_an_unknown_cloud_line_is_not_applied_silently(self):
        """Опечатку в имени (CLOUD_SEND_TOOL) нельзя принимать за настройку."""
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".env").write_text("CLOUD_SEND_TOOL=1\n", encoding="utf-8")
            env = mock.patch.dict(os.environ)
            env.start()
            self.addCleanup(env.stop)
            os.environ.pop("CLOUD_SEND_TOOL", None)
            with mock.patch.object(settings, "PROJECT_ROOT", Path(folder)):
                with mock.patch("builtins.print") as fake_print:
                    cloud._load_dotenv()

        self.assertNotIn("CLOUD_SEND_TOOL", os.environ,
                         "непонятную строку применять нельзя: о ней надо сказать, а не угадывать")
        self.assertIn("CLOUD_SEND_TOOL", _printed_text(fake_print),
                      "о лишней строке надо сказать словами")

    def test_the_search_round_trip_goes_in_the_openai_shape(self):
        """Круг поиска держится на идентификаторах: без них шлюз не поймёт ответ.

        У Ollama вызов инструмента и его результат связаны именем, у OpenAI —
        полем tool_calls с id в сообщении ассистента и полем tool_call_id
        в ответе инструмента.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)

        cloud.chat(self.MODEL, [
            {"role": "user", "content": "Столица Австралии?"},
            {"role": "assistant", "content": "", "name": "иван",
             "tool_calls": [{"function": {"name": "search_web",
                                          "arguments": {"query": "столица Австралии"}}}]},
            {"role": "tool", "tool_name": "search_web", "name": "search_web",
             "content": "Канберра"},
        ])

        sent = self.gateway.last_request()["body"]["messages"]
        call = sent[1]["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "search_web")
        self.assertIsInstance(call["function"]["arguments"], str,
                              "у OpenAI аргументы — строка JSON, а не словарь")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"query": "столица Австралии"})
        self.assertEqual(sent[2]["role"], "tool")
        self.assertEqual(sent[2]["tool_call_id"], call["id"],
                         "результат обязан ссылаться на вызвавший его вызов")
        self.assertEqual(sent[2]["content"], "Канберра")

    def test_a_number_the_vendor_does_not_know_is_dropped_and_remembered(self):
        """OpenAI-форма не знает repeat_penalty — и шлюз говорит это сам.

        min_p, top_k и repeat_penalty включаются одной настройкой
        (CLOUD_PASS_OLLAMA_EXTRAS), то есть выборочно их не выключить: либо поле
        уходит всем, либо никому. У вендора, который его не понимает, каждый ход
        падал одинаково — 400, а за серией отказов шёл 429. Теперь названное
        шлюзом поле убирается из запроса и запоминается для этой модели,
        а остальные числа остаются.
        """
        self.addCleanup(cloud._DROPPED_PARAMS.clear)
        cloud_setting(self, "CLOUD_SEND_PARAMS", True)
        cloud_setting(self, "CLOUD_PASS_OLLAMA_EXTRAS", True)
        self.gateway.statuses = [400, 200]
        self.gateway.texts = [
            json.dumps({"error": {"message": "Unknown parameter: 'repeat_penalty'.",
                                   "type": "invalid_request_error"}}),
            json.dumps({"choices": [{"message": {"content": "Канберра."}}]}),
        ]

        content, _tools = cloud.chat(
            self.MODEL, [{"role": "user", "content": "Привет!"}],
            options={"temperature": 0.5, "top_k": 40, "repeat_penalty": 1.2})

        self.assertEqual(content, "Канберра.", "ход должен состояться, а не пропасть")
        self.assertEqual(self.gateway.requests[0]["body"]["repeat_penalty"], 1.2)
        second = self.gateway.requests[1]["body"]
        self.assertNotIn("repeat_penalty", second, "лишнее поле уходит")
        self.assertEqual(second["temperature"], 0.5,
                         "остальные числа трогать нельзя — шлюз на них не жаловался")
        self.assertEqual(second["top_k"], 40)

        # И запомнили: со следующего хода поле не уходит вовсе, а отказ не повторяется
        cloud.chat(self.MODEL, [{"role": "user", "content": "Ещё раз"}],
                   options={"repeat_penalty": 1.2})
        self.assertNotIn("repeat_penalty", self.gateway.last_request()["body"])

    def test_a_missing_number_is_not_confused_with_a_refused_tool(self):
        """Причина отказа названа полем — значит, дело в нём, а не в поиске.

        Раньше на любой 400 из тела выбрасывались инструменты: искать причину
        приходилось человеку, а ход кончался ошибкой в ленте, хотя поиск был ни
        при чём.
        """
        for name in ("CLOUD_SEND_PARAMS", "CLOUD_SEND_TOOLS", "CLOUD_PASS_OLLAMA_EXTRAS"):
            cloud_setting(self, name, True)
        self.addCleanup(cloud._DROPPED_PARAMS.clear)
        self.gateway.statuses = [400, 200]
        self.gateway.texts = [
            json.dumps({"error": {"message": "Unknown parameter: 'repeat_penalty'."}}),
            json.dumps({"choices": [{"message": {"content": "Канберра."}}]}),
        ]

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}],
                                     options={"repeat_penalty": 1.2})

        self.assertEqual(content, "Канберра.")
        self.assertIn("tools", self.gateway.requests[1]["body"],
                      "инструмент поиска был ни при чём — его нельзя выбрасывать")

    def test_the_search_is_demanded_before_the_first_word(self):
        """Поиск требуется сразу: первая версия реплики не рождается вовсе.

        Раньше ход шёл так: модель отвечала сама, потом её просили поискать,
        и она отвечала заново — первый ответ мелькал в ленте и исчезал, а токены
        за него были оплачены. Теперь инструмент требуется в самом первом запросе
        хода, и реплика в ленте начинается один раз.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        self.gateway.texts = [
            json.dumps({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "search_web",
                              "arguments": "{\"query\": \"столица Австралии\"}"}}]}}]}),
            # Второй ответ — потоком: именно так видно, сколько раз начиналась
            # реплика в ленте (порции идут в on_delta)
            "data: " + json.dumps({"choices": [{"delta": {"content": "Канберра."}}]})
            + "\n\ndata: [DONE]\n\n",
        ]
        pieces = []

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="Канберра")):
            content, count, queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Столица Австралии?"}],
                participant_name="Проверка",
                on_delta=lambda piece, replace=False: pieces.append((piece, replace)))

        self.assertEqual(content, "Канберра.")
        self.assertEqual(count, 1, "поиск должен быть ровно один")
        self.assertEqual(queries, ["столица Австралии"])
        first = self.gateway.requests[0]["body"]
        self.assertEqual(first["tool_choice"], "required",
                         "поиск надо требовать с первого же запроса")
        self.assertEqual(first["tools"][0]["function"]["name"], "search_web")
        # Первой версии реплики не было: черновик начался ровно один раз
        self.assertEqual(len(pieces), 1, f"реплика выходила в ленту {len(pieces)} раза")
        self.assertTrue(pieces[0][1], "первая порция начинает реплику заново")

    def test_a_search_asked_in_words_is_understood_and_done(self):
        """Модель, написавшая просьбу о поиске текстом, всё равно получает поиск.

        Раньше это было тупиком: вызов уезжал в реплику, поиск не выполнялся,
        и «Источников» у поста не появлялось — хотя модель именно их и просила.
        Ответ инструмента ей тоже нельзя слать: протокол она не удержала,
        на ответ с выдуманным нами id шлюз ответил бы отказом.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        self.gateway.texts = [
            json.dumps({"choices": [{"message": {"content":
                'Сейчас поищу.\n\nsearch:web_search{query: "Сыктывкар новости"}'}}]}),
            json.dumps({"choices": [{"message": {"content": "Сыктывкар — столица Коми."}}]}),
        ]

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="Вот что нашлось")):
            content, count, queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового в Сыктывкаре?"}],
                participant_name="Проверка")

        self.assertEqual(content, "Сыктывкар — столица Коми.")
        self.assertEqual(count, 1, "поиск по текстовой просьбе не выполнился")
        self.assertEqual(queries, ["Сыктывкар новости"])
        messages = self.gateway.requests[1]["body"]["messages"]
        self.assertNotIn("tool", [m["role"] for m in messages],
                         "модель, не удержавшая протокол, ответа инструмента не поймёт")
        self.assertNotIn("tool_calls", messages[-2],
                         "выдуманный нами id — не её вызов: протокол с ней не держим")
        self.assertIn("Вот что нашлось", messages[-1]["content"])
        self.assertIn("Сыктывкар новости", messages[-1]["content"])

    def test_the_size_of_the_cloud_answer_sits_next_to_the_window(self):
        """Вторая половина стола: сколько облачной модели позволено ответить.

        Местной модели это число задано в OPTIONS и уходит в Ollama вместе с
        запросом. Облачному участнику оно не уходило вовсе: ответ не ограничивало
        ничто, кроме CLOUD_TURN_LIMIT в 120 секунд, и «хаотичная» модель успевала
        наговорить сколько угодно за наши токены.
        """
        cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertNotIn("max_tokens", self.gateway.last_request()["body"],
                         "по умолчанию ответ не ограничиваем: шлюз решает сам")

        cloud_setting(self, "CLOUD_MAX_TOKENS", 700)
        cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        body = self.gateway.last_request()["body"]
        self.assertEqual(body["max_tokens"], 700)
        self.assertNotIn("temperature", body,
                         "предел ответа — не характер: числа характеров не включаются")

    def test_a_model_that_refuses_tools_is_asked_again_without_them(self):
        """400 на инструмент — не приговор модели: говорим с ней без поиска.

        Иначе каждый ход возвращал бы ошибку, а серия 400 уводит ключ в паузу.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        self.gateway.statuses = [400, 200]

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])

        self.assertEqual(content, "Канберра.", "разговор должен состояться")
        self.assertEqual(len(self.gateway.requests), 2)
        self.assertNotIn("tools", self.gateway.requests[1]["body"])
        self.assertFalse(cloud.model_takes_tools(self.MODEL), "отказ надо запомнить")
        # И в общую метку это тоже попадает: иначе ход снова потребует поиск
        ollama_api.ask_model_with_tools(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertFalse(ollama_api.MODELS_TOOLS_SUPPORT[self.MODEL])

    def test_a_model_that_refuses_tools_is_not_sent_hunting_for_a_search(self):
        """После «инструмент не принят» ход кончается, а не просит поиск заново.

        В логе это выглядело так: шлюз сказал «не принимаю инструмент», приложение
        это запомнило — и тут же напечатало «Принудительный поиск». То есть
        у «думающей» модели отнимался ещё один ход целиком, и он снова упирался
        в CLOUD_TURN_LIMIT: вместо готовой реплики — ещё две минуты ожидания.
        Спрашивать поиск у той, кто инструмент не берёт, не за что — он не сработает.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        # Настоящий отказ по инструменту: шлюз называет поле, которого не знает,
        # и следом за этим отдаёт обычный ответ — именно так это было в логе
        self.gateway.statuses = [400, 200]
        self.gateway.texts = [
            json.dumps({"error": {"message": "Unknown parameter: 'tools'."}}),
            json.dumps({"choices": [{"message": {"content": "Канберра."}}]}),
        ]

        content, count, _queries = ollama_api.ask_model(
            self.MODEL, [{"role": "user", "content": "Привет!"}],
            participant_name="Проверка")

        self.assertEqual(content, "Канберра.")
        self.assertEqual(count, 0, "поиска не было и требовать его не за что")
        self.assertEqual(len(self.gateway.requests), 2,
                         "после отказа модель послали искать — а инструмент она не берёт")
        self.assertFalse(cloud.model_takes_tools(self.MODEL), "отказ надо было запомнить")

    def test_a_refused_tool_choice_does_not_take_the_search_away(self):
        """Отказ по tool_choice — не повод отобрать у модели поиск.

        Это разные возможности: инструментом модель пользуется сама, а «обязан
        вызвать его прямо сейчас» понимает не каждый шлюз. Раньше любой 400
        с инструментом в теле заканчивался одним и тем же: инструмент выкидывался
        и запоминалось «модель его не принимает». Так судья на qwen3.8-flash
        остался без поиска до конца процесса — и, по требованию своих же правил,
        ушёл думать в двухминутную петлю.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        self.gateway.statuses = [400, 200]
        self.gateway.texts = [
            json.dumps({"error": {"message": "Unknown parameter: 'tool_choice'."}}),
            json.dumps({"choices": [{"message": {"content": "Канберра."}}]}),
        ]

        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}],
                                     tool_choice="any")

        self.assertEqual(content, "Канберра.")
        self.assertEqual(len(self.gateway.requests), 2)
        retry = self.gateway.requests[1]["body"]
        self.assertIn("tools", retry, "инструмент поиска отбирать было не за что")
        self.assertNotIn("tool_choice", retry,
                         "а вот требование вызова повторять не надо")
        self.assertTrue(cloud.model_takes_tools(self.MODEL),
                        "модель с поиском, а не без него")
        # И в следующий раз требование не уезжает снова: отказ запомнен
        cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}], tool_choice="any")
        self.assertNotIn("tool_choice", self.gateway.last_request()["body"])

    def test_the_turn_has_a_round_for_every_allowed_search(self):
        """Кругов хода должно хватать на все поиски, разрешённые настройками.

        Кругов было жёстко восемь, а потолок поисков — настройка. Режиссёр мог
        поднять MAX_SEARCHES, и часть поисков просто не состоялась бы — молча:
        ни в ленте, ни в консоли об этом не было бы ни слова.
        """
        calls = [json.dumps({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": f"call-{n}", "type": "function",
             "function": {"name": "search_web",
                          "arguments": json.dumps({"query": f"запрос-{n}"})}}]}}]})
            for n in range(10)]
        calls.append(json.dumps({"choices": [{"message": {
            "content": "Сыктывкар — столица Коми."}}]}))
        self.gateway.texts = calls
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        cloud_setting(self, "MAX_SEARCHES", 10)

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")):
            content, count, queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового?"}],
                participant_name="Роман")

        self.assertEqual(count, 10, "разрешённые поиски должны состояться все")
        self.assertEqual(len(queries), 10)
        self.assertEqual(content, "Сыктывкар — столица Коми.")

    def _search_call(self, query: str = "Сыктывкар") -> str:
        """Ответ шлюза: модель не говорит ни слова, а просит поиск."""
        return json.dumps({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "call-1", "type": "function",
             "function": {"name": "search_web",
                          "arguments": json.dumps({"query": query})}}]}}]})

    def test_a_model_that_searched_and_said_nothing_is_asked_for_the_reply(self):
        """Модель искала и промолчала — её просят сказать реплику, а не искать.

        Так вёл себя Дмитрий на qwen3.8-flash: поиск делал охотно, а слов
        не сказал ни одного, и ход кончался «Модель не дала ответ». Просить
        у него поиск не за что — он его только что сделал, зато найденное
        у него уже есть, и не хватает ровно одного: реплики словами.
        И от MIN_SEARCHES это не зависит: здесь он 0, то есть искать
        не заставляем вовсе — а про молчание после поиска всё равно говорим.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        cloud_setting(self, "MIN_SEARCHES", 0)
        cloud_setting(self, "MAX_SEARCH_ATTEMPTS", 1)
        self.gateway.texts = [
            self._search_call(),
            json.dumps({"choices": [{"message": {"content": ""}}]}),
            json.dumps({"choices": [{"message": {"content": "Сыктывкар — столица Коми."}}]}),
        ]

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")):
            content, count, _queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового?"}],
                participant_name="Дмитрий")

        self.assertEqual(count, 1, "поиск был ровно один")
        self.assertEqual(content, "Сыктывкар — столица Коми.")
        self.assertEqual(len(self.gateway.requests), 3,
                         "промолчавшую модель обязаны спросить ещё раз")
        again = self.gateway.requests[2]["body"]
        self.assertIn("Поиск уже выполнен", again["messages"][-1]["content"],
                      "в просьбе надо назвать, что искать больше нечего")
        self.assertNotIn("tool_choice", again,
                         "требовать вызов инструмента тут не за что: поиск уже был")

    def test_the_reply_is_asked_for_only_as_many_times_as_allowed(self):
        """Поправок молчащей модели — ровно столько, сколько в настройке.

        Каждая поправка — это ещё один оплаченный запрос к шлюзу, поэтому
        бесконечно просить нельзя. После разрешённых попыток остаётся та же
        последняя попытка без инструмента, что и была, — и молчание
        называется молчанием, а не выдаётся за реплику.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        cloud_setting(self, "MIN_SEARCHES", 0)
        cloud_setting(self, "MAX_SEARCH_ATTEMPTS", 2)
        silent = json.dumps({"choices": [{"message": {"content": ""}}]})
        self.gateway.texts = [self._search_call(), silent, silent, silent, silent]

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")):
            content, count, _queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового?"}],
                participant_name="Дмитрий")

        # Считаем не упоминания в истории (она растёт), а сами просьбы:
        # просьба — это последнее сообщение того запроса, которым её отправили
        asked = sum(1 for request in self.gateway.requests
                    if "Поиск уже выполнен"
                    in str(request["body"]["messages"][-1].get("content") or ""))
        self.assertEqual(count, 1)
        self.assertEqual(asked, 2, "поправок должно быть ровно по настройке")
        self.assertEqual(len(self.gateway.requests), 5,
                         "поиск, две поправки и последняя попытка — и ни одного лишнего круга")
        self.assertNotIn("tools", self.gateway.requests[-1]["body"],
                         "последняя попытка идёт без инструмента")
        self.assertTrue(content.startswith("[Модель не дала ответ]"),
                        f"молчание надо назвать, а не выдать за реплику: {content!r}")

    def test_a_search_buys_the_turn_more_time(self):
        """Состоявшийся поиск добавляет ходу времени — но у срока есть предел.

        Поиск — это ещё один круг: модель попросила искать, получила найденное
        и отвечает заново. Пока срок считался внутри каждого запроса, у хода
        с десятью поисками срока не было вовсе — десять раз по CLOUD_TURN_LIMIT.
        А «свежий срок на каждый поиск» — та же беда с другого конца: ход,
        который ищет, не кончился бы никогда. Поэтому поиск даёт надбавку.
        """
        cloud_setting(self, "CLOUD_TURN_LIMIT", 100)
        cloud_setting(self, "CLOUD_SEARCH_TIME", 60)
        left = cloud.turn_deadline() - time.monotonic()
        self.assertAlmostEqual(left, 100, delta=3,
                               msg="основа срока — предел хода, без надбавок")

        deadlines = []

        def one_search_then_answer(*_args, **kwargs):
            deadlines.append(kwargs.get("deadline"))
            if len(deadlines) == 1:
                return "", [{"id": "call-1", "type": "function",
                             "function": {"name": "search_web",
                                          "arguments": json.dumps({"query": "погода"})}}]
            return "Готово.", []

        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        cloud_setting(self, "MAX_SEARCHES", 1)
        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")), \
                mock.patch.object(cloud, "chat", side_effect=one_search_then_answer):
            content, count, _queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового?"}],
                participant_name="Роман")

        self.assertEqual(count, 1)
        self.assertEqual(content, "Готово.")
        self.assertEqual(len(deadlines), 2, "поиск — это ещё один запрос к модели")
        self.assertIsNotNone(deadlines[0], "у облачного хода срок обязан быть")
        self.assertIsNotNone(deadlines[1],
                             "срок, посчитанный в начале хода, должен доехать "
                             "до каждого его запроса, а не считаться заново")
        self.assertAlmostEqual(deadlines[1] - deadlines[0], 60, delta=3,
                               msg="состоявшийся поиск даёт ходу ещё времени")

    def test_the_sidebar_sees_the_clock_of_the_turn_and_its_bonus(self):
        """Сайдбар знает, сколько ход уже идёт и сколько ему осталось.

        Срок — на ход целиком, и каждый поиск его продлевает. Не знай этого
        сайдбар — «осталось» шло бы к нулю, а ход всё шёл бы: часы врали бы
        ровно в тот миг, когда на них смотрят.
        """
        cloud_setting(self, "CLOUD_TURN_LIMIT", 300)
        cloud_setting(self, "CLOUD_SEARCH_TIME", 60)
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        cloud_setting(self, "MAX_SEARCHES", 1)
        session = show.DebateSession()
        seen = []

        def one_search_then_answer(*_args, **_kwargs):
            seen.append((session.turn_elapsed(), session.turn_left()))
            if len(seen) == 1:
                return "", [{"id": "call-1", "type": "function",
                             "function": {"name": "search_web",
                                          "arguments": json.dumps({"query": "погода"})}}]
            return "Готово.", []

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")), \
                mock.patch.object(cloud, "chat", side_effect=one_search_then_answer):
            ollama_api.ask_model(self.MODEL, [{"role": "user", "content": "?"}],
                                 participant_name="Роман", show_session=session)

        self.assertEqual(len(seen), 2, "поиск — это ещё один запрос к модели")
        self.assertIsNotNone(seen[0][0], "часы обязаны идти уже на первом запросе")
        self.assertAlmostEqual(seen[0][1], 300, delta=3,
                               msg="остаток считается от срока хода")
        self.assertAlmostEqual(seen[1][1], 360, delta=3,
                               msg="поиск обязан продлить и видимый остаток")
        self.assertEqual(session.turn_extra, 60)

    def test_an_error_is_not_taken_for_an_answer(self):
        """Текст ошибки — не реплика: поиск за ним не просят.

        Раньше «[ОШИБКА: …]» считалась сказанным словом, и после оборванного хода
        приложение просило у модели поиск — то есть ещё один ход ожидания вместо
        честно показанной ошибки.
        """
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        calls = []

        def failing(*_args, **kwargs):
            calls.append(kwargs.get("tool_choice"))
            return "[ОШИБКА: ход длился дольше 120 с и оборван]", []

        with mock.patch.object(cloud, "chat", side_effect=failing):
            content, count, _queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Привет!"}],
                participant_name="Проверка")

        self.assertIn("[ОШИБКА:", content)
        self.assertEqual(count, 0, "ошибка не повод искать")
        self.assertEqual(len(calls), 1, f"после ошибки сделано ещё {len(calls) - 1} запрос(а)")

    def test_a_silent_gateway_explains_itself_in_words(self):
        """«The read operation timed out» ни о чём не говорит — пишем словами.

        Ждать по-настоящему нечего: молчание шлюза изображает открыватель,
        который сразу говорит «не дождался». Проверяем не секунды, а текст:
        по нему человек решает, что крутить.
        """
        def silent(*_args, **_kwargs):
            raise TimeoutError("The read operation timed out")

        settings.CLOUD_TIMEOUT = 3
        self.addCleanup(setattr, settings, "CLOUD_TIMEOUT", 180)
        opener = mock.Mock(open=mock.Mock(side_effect=silent))
        with mock.patch.object(cloud.urllib.request, "build_opener",
                               mock.Mock(return_value=opener)):
            content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])

        self.assertIn("не ответила за 3 с", content)
        self.assertIn("CLOUD_TIMEOUT", content, "подсказка обязана называть рычаг")
        self.assertNotIn("timed out", content)

    def test_a_key_with_odd_symbols_is_explained_in_words(self):
        """Заголовки HTTP бывают только латинскими: кириллица в ключе падала бы кодеком."""
        settings.CLOUD_API_KEY = "ключ-скопированный-из-письма"
        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertIn("не латинские символы", content)
        self.assertNotIn("latin-1", content)
        self.assertEqual(self.gateway.requests, [])

    def test_a_key_copied_with_quotes_still_works(self):
        """Ключ часто копируют вместе с «Bearer » или кавычками — это не ошибка человека."""
        settings.CLOUD_API_KEY = f'"Bearer {self.KEY}"'
        self.assertTrue(cloud.is_configured())
        cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertEqual(self.gateway.last_request()["authorization"], f"Bearer {self.KEY}")

    def test_without_a_key_the_answer_says_what_to_do(self):
        settings.CLOUD_API_KEY = ""
        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertIn(settings.CLOUD_KEY_ENV, content)
        self.assertEqual(self.gateway.requests, [], "без ключа ходить к шлюзу некуда")

    def test_the_key_does_not_come_back_in_the_error(self):
        """Ошибку шлюза мы показываем прямо в ленте спектакля — ключ туда не должен."""
        self.gateway.status = 401
        self.gateway.body_text = json.dumps(
            {"error": {"message": "неверный ключ"},
             "seen": f"Authorization: Bearer {self.KEY}"})
        content, _tools = cloud.chat(self.MODEL, [{"role": "user", "content": "Привет!"}])
        self.assertNotIn(self.KEY, content)
        self.assertIn("401", content)
        self.assertIn("***", content)

    def test_the_model_list_comes_from_the_gateway(self):
        names, error = cloud.fetch_models(force=True)
        self.assertEqual(names, ["qwen/qwen3.7-flash"])
        self.assertIsNone(error)
        self.assertEqual(self.gateway.last_request()["path"], "/v1/models")

    def test_a_cloud_model_is_checked_on_the_gateway_not_in_ollama(self):
        self.assertTrue(ollama_api.check_models_available([self.MODEL])["ok"])

        wrong = ollama_api.check_models_available(["cloud:нет-такой-модели"])
        self.assertFalse(wrong["ok"])
        message = ollama_api.models_problem_message(wrong)
        self.assertIn("нет-такой-модели", message)
        self.assertNotIn("ollama pull", message, "скачивать облачную модель бессмысленно")

    def test_the_key_is_read_from_the_environment_under_any_known_name(self):
        """Ключ принимается под всеми именами, что встречались в документации."""
        for name in ("CLOUD_API_KEY", settings.CLOUD_KEY_ENV, "CLOUD_KEY_ENV"):
            with self.subTest(name=name):
                os.environ[name] = self.KEY
                self.assertEqual(cloud.api_key(), self.KEY,
                                 f"ключ из переменной {name} не подхватился")
                del os.environ[name]

    def test_dotenv_lines_are_parsed_without_touching_the_environment(self):
        parsed = cloud._parse_dotenv(
            "# комментарий\n\nCLOUD_API_KEY=\"в кавычках\"\n"
            "CLOUD_BASE_URL = https://example.test/v1  \n")
        self.assertEqual(parsed, {"CLOUD_API_KEY": "в кавычках",
                                  "CLOUD_BASE_URL": "https://example.test/v1"})
        # Реальная переменная окружения важнее строки из .env: ключ, заданный
        # в системе, файл перекрывать не должен
        os.environ["CLOUD_BASE_URL"] = "http://real-env/v1"
        self.assertEqual(cloud.base_url(), "http://real-env/v1")

    def test_the_gateway_is_not_conducted_through_the_local_proxy(self):
        """Шлюз — сам прокси до OpenAI: вести его ещё и через свой прокси = ломать запрос.

        Прокси ставится сразу с двух сторон: в окружении (HTTP_PROXY) и в самом
        urllib — так выглядит системная настройка Windows. Открыватель по
        умолчанию слушается обоих, а нам нужен свой, без прокси вовсе.

        Одного окружения здесь мало: запрос идёт на 127.0.0.1, а для местных
        адресов urllib прокси и сам не применяет — то есть проверка проходила
        и на вредительской правке `build_opener()` без прокси (см. мутацию
        «запрос к шлюзу снова ведётся через локальный прокси»). Поэтому
        закрываем и вторую дверь: ей называем мёртвый прокси для всех адресов
        и запрещаем обход.
        """
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"    # мёртвый порт: достучаться нельзя
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
        dead = {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"}
        # Оба запроса к шлюзу — и реплика, и остаток на ключе: открывателей
        # в cloud.py два, и оба обязаны прокси не слушаться
        cloud_setting(self, "CLOUD_BALANCE_PATH", "/proxyapi/balance")
        self.gateway.balances = [100.0]
        with mock.patch.object(urllib.request, "getproxies", lambda: dict(dead)), \
                mock.patch.object(urllib.request, "proxy_bypass", lambda host: False):
            content, _tools = cloud.chat(
                self.MODEL, [{"role": "user", "content": "Привет!"}])
            chat_path = self.gateway.last_request()["path"]
            reading, problem = cloud.balance(force=True)

        self.assertEqual(content, "Канберра.")
        self.assertEqual(chat_path, "/v1/chat/completions")
        self.assertEqual(problem, "", "остаток не доехал — запрос ушёл через прокси")
        self.assertIsNotNone(reading)

    def test_without_a_key_a_cloud_participant_is_not_ready(self):
        settings.CLOUD_API_KEY = ""
        status = ollama_api.check_models_available([self.MODEL])
        self.assertFalse(status["ok"])
        self.assertIn(settings.CLOUD_KEY_ENV, ollama_api.models_problem_message(status))

    def test_a_cast_of_cloud_models_does_not_worry_about_vram(self):
        """Облачная модель видеопамяти не занимает — и ругаться тут не на что."""
        fit = ollama_api.check_vram_fit([self.MODEL, "human"])
        self.assertTrue(fit["checked"])
        self.assertFalse(fit["warnings"])
        self.assertEqual(fit["error"], "")

    def test_the_panel_list_shows_cloud_models_with_their_prefix(self):
        data = web_app.app.test_client().get("/api/models").get_json()
        self.assertIn(self.MODEL, data["cloud_models"])
        self.assertTrue(data["cloud"]["configured"])

    def test_the_key_never_reaches_the_page(self):
        """Ключ — серверный: страница и ответы ей о нём не говорят."""
        self.assertNotIn(self.KEY, page.HTML_TEMPLATE)
        session = make_session()
        session.runtime_participants = session.runtime_participants[:1]
        session.runtime_participants[0]["model"] = self.MODEL
        strip_session_patch(self, session)
        client = web_app.app.test_client()
        for route in ("/api/models", "/api/participants", "/api/status"):
            with self.subTest(route=route):
                self.assertNotIn(self.KEY, client.get(route).get_data(as_text=True))

    def test_the_standalone_olama_helpers_still_work(self):
        """Облако не должно было сломать обычный путь: местная модель — не облачная."""
        self.assertFalse(cloud.is_cloud_model("r1"))
        self.assertEqual(cloud.bare_model_name("r1"), "r1")
        self.assertEqual(cloud.untranslated_options({"min_p": 0.2, "temperature": 0.8}), ["min_p"])


class TestATurnThatStayedSilent(unittest.TestCase):
    """Молчание хода должно объяснять себя само.

    Так выглядел ход gpt-5-nano из настоящей ленты: три поиска выполнены, а пост
    вышел «[Модель не дала ответ]» — и по нему нельзя было понять ни что модель
    делала (она ещё раз просила поиск? ушла в размышления?), ни что крутить
    в настройках. Хуже того, последняя попытка была точной копией неудавшейся:
    инструмент уезжал снова, а «think=False» — поле Ollama, которое облачный путь
    не читает вовсе. Модель, застрявшая на просьбах поискать, получала тот же
    самый вопрос.
    """

    KEY = "test-key-1234567890"
    MODEL = "cloud:openai/gpt-5-nano"

    def setUp(self):
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.stop)
        self.saved = (settings.CLOUD_BASE_URL, settings.CLOUD_API_KEY)
        self.addCleanup(self._restore)
        settings.CLOUD_BASE_URL = self.gateway.base_url
        settings.CLOUD_API_KEY = self.KEY
        self.addCleanup(cloud._MODELS_WITHOUT_TOOLS.clear)
        self.addCleanup(cloud._NO_TOOL_CHOICE.clear)
        cloud_setting(self, "CLOUD_SEND_TOOLS", True)
        # Ответ, съеденный размышлениями: текста нет, а вывод посчитан
        self.gateway.texts = [json.dumps({
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5084, "completion_tokens": 1196,
                      "completion_tokens_details": {"reasoning_tokens": 1160}}})] * 2

    def _restore(self):
        settings.CLOUD_BASE_URL, settings.CLOUD_API_KEY = self.saved

    def test_the_silence_is_explained_in_words(self):
        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")):
            content, _count, _queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового?"}],
                participant_name="Роман")

        self.assertTrue(content.startswith("[Модель не дала ответ]"),
                        "молчание остаётся молчанием, а не превращается в реплику")
        self.assertIn("предел вывода", content,
                      "надо назвать причину: размышления съели весь вывод")
        self.assertIn("1160", content, "и сколько именно ушло в размышления")
        self.assertFalse(ollama_api.is_answer(content),
                         "объяснение причины не делает молчание репликой")

    def test_the_last_try_differs_from_the_failed_one(self):
        """Последняя попытка не поводит модель по тому же кругу третий раз.

        Раньше она шла с тем же инструментом, что и неудавшаяся, а думающая
        облачная модель просьбу поискать понимает только через него: вместо слов
        приходил четвёртый вызов поиска, и ход кончался молчанием.
        """
        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")):
            ollama_api.ask_model(self.MODEL, [{"role": "user", "content": "Что нового?"}],
                                 participant_name="Роман")

        self.assertEqual(len(self.gateway.requests), 2,
                         "попыток должно быть две: ход и последняя")
        self.assertNotIn("tools", self.gateway.requests[1]["body"],
                         "без инструмента модель обязана сказать словами")
        last_words = self.gateway.requests[1]["body"]["messages"][-1]["content"]
        self.assertIn("Поиска больше не будет", last_words,
                      "модель должна узнать, что искать больше нечего: иначе повторит")

    def test_a_model_stuck_on_search_still_gets_to_speak(self):
        """Четыре поиска подряд, лимит — три: ход заканчивается репликой, а не молчанием.

        Раньше четвёртая просьба тихо превращалась в «[лимит поисков исчерпан]»,
        и об этом в логе не было ни слова — со стороны это выглядело как модель,
        которая думает и ничего не говорит.
        """
        def search_call(query):
            return json.dumps({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": f"call-{query}", "type": "function",
                 "function": {"name": "search_web",
                              "arguments": json.dumps({"query": query})}}]}}]})

        self.gateway.texts = [search_call("раз"), search_call("два"), search_call("три"),
                              search_call("четыре"),
                              json.dumps({"choices": [{"message": {
                                  "content": "Сыктывкар — столица Коми."}}]})]

        with mock.patch.object(ollama_api, "search_web", mock.Mock(return_value="нашлось")):
            content, count, queries = ollama_api.ask_model(
                self.MODEL, [{"role": "user", "content": "Что нового?"}],
                participant_name="Роман")

        self.assertEqual(content, "Сыктывкар — столица Коми.")
        self.assertEqual(count, 3, "больше трёх поисков за ход не бывает")
        self.assertEqual(queries, ["раз", "два", "три"], "четвёртый поиск не выполнялся")


class TestCloudPanel(unittest.TestCase):
    """Пульт должен честно показывать, что модель играет в интернете."""

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def suggestions(self) -> str:
        start = self.page.index("function renderModelSuggestions()")
        return self.page[start:self.page.index("function collectCast()", start)]

    def test_the_suggestion_list_tells_local_from_cloud(self):
        body = self.suggestions()
        self.assertIn("cloudModels", body)
        self.assertIn("☁️", body, "по значку видно, что реплика уйдёт в интернет")

    def test_the_living_participant_is_in_the_suggestion_list(self):
        """«human» — такая же модель места: без неё человека не посадить за стол из пульта."""
        body = self.suggestions()
        self.assertIn("'human'", body)
        self.assertIn("🧑 Живой участник", body)

    def cast_body(self) -> str:
        start = self.page.index("function collectCast()")
        return self.page[start:self.page.index("function saveCast()", start)]

    def test_the_living_participants_model_is_sent_too(self):
        """Пульт молчал про модель живого места — и оно молча возвращалось из модели."""
        body = self.cast_body()
        model_line = next(line for line in body.splitlines() if "entry.model = " in line)
        self.assertIn("pick(`model-${idx}`", model_line)
        self.assertNotIn("human", model_line)
        self.assertLess(body.index("entry.model = "), body.index("if (p.model !== 'human') {"),
                        "модель уходит всегда, а числа — только у моделей")

    def test_the_suggestions_are_one_list_for_the_whole_cast(self):
        """У шлюза сотни моделей: своя копия списка в каждой карточке — тысячи строк разметки."""
        self.assertEqual(self.page.count('<datalist id="modelList">'), 1)
        self.assertIn('list="modelList"', self.page)

    def test_the_model_can_be_typed_in_by_hand(self):
        """Поле — текст, а не строгий список: вписать cloud:вендор/модель можно всегда."""
        self.assertIn('<input type="text" id="model-', self.page)
        self.assertIn("впишите cloud:", self.page)

    def test_the_cloud_models_come_to_the_page(self):
        self.assertIn("data.cloud_models", self.page)

    def test_the_cloud_explains_itself_when_it_is_not_configured(self):
        """Пустой раздел «Облако» без объяснения выглядит как поломка, а не как «нет ключа»."""
        self.assertIn("data.cloud", self.page)
        self.assertIn("cloudHint", self.page)

    def test_the_ready_warning_uses_the_servers_words(self):
        """Причины бывают местные и облачные: текст собирает сервер, а не страница."""
        start = self.page.index("function renderModelsWarning(")
        body = self.page[start:self.page.index("function searchAvatar(", start)]
        self.assertIn("status.message", body)
        self.assertNotIn("Проверьте, что Ollama запущена", body)


# ---------------------------------------------------------------- опрос статуса

class TestStatusPolling(unittest.TestCase):
    """Оборванная связь — ещё не закрытый театр, и опрос не должен умирать с первой неудачи.

    Так уже случилось: в момент старта спектакля один пустой ответ (закрытое
    keep-alive соединение или занятый сервер, пока модель грузится в память)
    заставлял страницу объявить «Театр закрыт» и прекратить опрос навсегда:
    спектакль шёл дальше, а лента замирала.
    """

    def setUp(self):
        self.page = page.HTML_TEMPLATE
        start = self.page.index("function updatePosts()")
        self.body = self.page[start:self.page.index("function sendModeratorMessage()", start)]
        self.catch = self.body[self.body.index(".catch(err =>"):]

    def test_a_single_failure_does_not_close_the_theatre(self):
        self.assertIn("statusFailures++", self.catch, "неудачи не считаются")
        self.assertIn("if (statusFailures < STATUS_FAILURES_BEFORE_CLOSED) return;", self.catch)
        # Порог обязан стоять до остановки опроса — иначе он бесполезен
        self.assertLess(self.catch.index("statusFailures < STATUS_FAILURES_BEFORE_CLOSED"),
                        self.catch.index("stopPolling()"),
                        "опрос останавливается раньше, чем мы убедились, что сервера нет")

    def test_a_successful_poll_resets_the_failures(self):
        self.assertIn("statusFailures = 0;", self.body,
                      "после удачного опроса счётчик неудач не сбрасывается")

    def test_a_bad_answer_is_not_read_as_if_it_were_json(self):
        self.assertIn("if (!r.ok) throw new Error", self.body,
                      "ответ 500 в виде HTML снова выдаст себя за закрытый театр")


# ---------------------------------------------------------------- страница

class TestOfflinePage(unittest.TestCase):
    """Страница обязана открываться без интернета: всё нужное лежит рядом.

    В пульте и ленте нет ни одного внешнего адреса — ни скриптов с CDN, ни
    шрифтов. Найденный когда-то @import с Google Fonts никто не использовал
    (весь текст рисуют системные Georgia и Courier New), но браузер всё равно
    ждал ответа fonts.googleapis.com перед отрисовкой: без интернета пульт
    просто висел на белом экране.
    """

    # Адреса, которые браузер обязан загрузить сам (src/href у тегов и CSS @import)
    EXTERNAL_RESOURCE_RE = re.compile(
        r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)'
        r'|@import\s+(?:url\()?["\']?(https?://[^)"\';]+)'
    )

    def test_page_loads_nothing_from_the_internet(self):
        external = [next(g for g in match.groups() if g)
                    for match in self.EXTERNAL_RESOURCE_RE.finditer(page.HTML_TEMPLATE)]
        self.assertEqual(
            external, [],
            "страница не должна ждать внешних ресурсов — она живёт за прокси и без сети")

    def test_local_files_the_page_needs_are_in_the_repository(self):
        """Клиент Socket.IO и рендерер формул отдаются сервером, а не с CDN:
        если файл потеряется, лента и формулы тихо перестанут работать.

        Смотрим рядом с тестами, а не рядом с модулем приложения: в
        tests/mutation_check.py код ради проверки тестов копируется в другую
        папку, где никакого static/ нет.
        """
        root = Path(__file__).resolve().parent.parent
        missing = [name for name in ("static/socket.io.min.js", "static/temml.min.js",
                                     "favicon.ico")
                   if not (root / name).is_file()]
        self.assertEqual(missing, [], f"страница ссылается на отсутствующие файлы: {missing}")


# ---------------------------------------------------------------- маршруты

class TestRoutes(unittest.TestCase):
    """Маршруты проверяются без спектакля и без Ollama."""

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        for name, value in (
            ("check_models_available", mock.Mock(
                return_value={"ok": True, "missing": [], "error": None})),
            ("run_debate_thread", mock.Mock()),
            ("check_vram_fit", mock.Mock(return_value={"checked": False})),
            ("fetch_model_parameters", mock.Mock(return_value={})),
            ("unload_model", mock.Mock()),
        ):
            patcher = mock.patch.object(app_module_of(name), name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Правка состава сохраняет сцену в файл настроек: без подмены тест
        # переписал бы настоящий .theatre_settings.json проекта
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = mock.patch.object(settings, "SETTINGS_FILE",
                                    Path(self.tmpdir.name) / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = web_app.app.test_client()

    def test_the_page_is_served_from_the_code_and_never_cached(self):
        """Страница отдаётся ровно тем, что лежит в page.py, и браузер её не держит.

        Баг был такой: вёрстку правят, `py .` обновляют — а на экране прежняя,
        и «кэш чистил, не знаю». Вёрстка живёт в памяти процесса, поэтому
        обновление страницы её не меняет вообще; но и браузерный кэш тут лишний —
        иначе «не применяется» выглядит одинаково и когда код старый, и когда
        страница старая (см. index в web.py).
        """
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""),
                      "браузеру нельзя разрешать держать страницу у себя")
        # Единственное, чем ответ отличается от шаблона, — последний перевод
        # строки: Jinja его срезает
        self.assertEqual(response.get_data(as_text=True),
                         page.HTML_TEMPLATE.rstrip("\n"),
                         "отдаётся не то, что лежит в коде: тогда правка вёрстки "
                         "не появится даже после перезапуска")

    def test_empty_topic_field_uses_the_one_on_the_server(self):
        """Тот самый баг: поле в форме стёрли, а тема на сервере осталась."""
        self.session.topic = "1"
        response = self.client.post("/api/start", json={})
        data = response.get_json()
        self.assertTrue(data["success"], data.get("error"))
        self.assertEqual(data["topic"], "1")

    def test_start_explains_missing_topic_instead_of_failing_silently(self):
        self.session.topic = ""
        data = self.client.post("/api/start", json={}).get_json()
        self.assertFalse(data["success"])
        self.assertIn("Тема не указана", data["error"])
        self.assertFalse(self.session.running)

    def test_topic_from_request_wins(self):
        self.session.topic = "старая"
        data = self.client.post("/api/start", json={"topic": "новая"}).get_json()
        self.assertEqual(data["topic"], "новая")

    def test_start_is_refused_for_participant_without_model(self):
        self.session.topic = "тема"
        self.session.runtime_participants[0]["model"] = ""
        data = self.client.post("/api/start", json={}).get_json()
        self.assertFalse(data["success"])
        self.assertIn("модель", data["error"].lower())

    def test_second_start_is_refused(self):
        self.session.topic = "тема"
        self.client.post("/api/start", json={})
        data = self.client.post("/api/start", json={}).get_json()
        self.assertFalse(data["success"])

    def test_status_has_everything_the_interface_needs(self):
        data = self.client.get("/api/status").get_json()
        for key in ("running", "finished", "topic", "current_participant",
                    "current_participant_role", "current_participant_is_moderator",
                    "new_posts", "total_posts", "waiting_for_human",
                    "turn_elapsed", "turn_left", "turn_extra"):
            self.assertIn(key, data)

    def test_the_status_carries_the_clock_of_the_turn(self):
        """Сколько ход идёт и сколько осталось — берётся из часов хода.

        Часы у браузера и у сервера разные: «осталось 2 мин» должно приезжать
        секундами, а не моментом времени, иначе это гадание.
        """
        self.session.start_turn_clock(120, time.monotonic() + 120)
        self.addCleanup(self.session.stop_turn_clock)
        data = self.client.get("/api/status").get_json()
        self.assertEqual(data["turn_limit"], 120)
        self.assertIsNotNone(data["turn_elapsed"], "часы хода должны идти")
        self.assertAlmostEqual(data["turn_left"], 120, delta=3)

        self.session.extend_turn_clock(60)
        data = self.client.get("/api/status").get_json()
        self.assertAlmostEqual(data["turn_left"], 180, delta=3,
                               msg="надбавка за поиск должна быть видна и в сайдбаре")
        self.assertEqual(data["turn_extra"], 60)

    def test_the_clock_is_off_when_no_turn_is_running(self):
        """Хода нет — и часов нет: иначе сайдбар показывал бы чужое время."""
        data = self.client.get("/api/status").get_json()
        self.assertIsNone(data["turn_elapsed"])
        self.assertIsNone(data["turn_left"])

    def test_status_role_is_empty_when_nobody_is_awaited(self):
        self.session.current_participant = judge_of(self.session)["display_name"]
        self.session.waiting_for_human = False
        data = self.client.get("/api/status").get_json()
        self.assertEqual(data["current_participant_role"], "")

    def test_status_role_of_awaited_judge(self):
        self.session.current_participant = judge_of(self.session)["display_name"]
        self.session.waiting_for_human = True
        data = self.client.get("/api/status").get_json()
        self.assertEqual(data["current_participant_role"], "judge")
        self.assertFalse(data["current_participant_is_moderator"])

    def test_status_role_of_awaited_moderator(self):
        moderator = next(p for p in self.session.runtime_participants
                         if p.get("is_moderator"))
        self.session.current_participant = moderator["display_name"]
        self.session.waiting_for_human = True
        data = self.client.get("/api/status").get_json()
        self.assertEqual(data["current_participant_role"], "moderator")
        self.assertTrue(data["current_participant_is_moderator"])

    def test_status_role_of_awaited_plain_participant(self):
        person = non_judge_ai(self.session)
        self.session.current_participant = person["display_name"]
        self.session.waiting_for_human = True
        data = self.client.get("/api/status").get_json()
        self.assertEqual(data["current_participant_role"], "")

    def test_reset_gives_a_new_cast_and_keeps_rules(self):
        self.session.judge_rules = ["Правило"]
        old_names = [p["display_name"] for p in self.session.runtime_participants]
        data = self.client.post("/api/reset", json={}).get_json()
        self.assertTrue(data["success"])
        self.assertEqual(self.session.judge_rules, ["Правило"])
        self.assertEqual(len(data["participants"]), len(old_names))

    def test_topic_endpoint_refuses_empty_text(self):
        data = self.client.post("/api/moderator/topic", json={"topic": "  "}).get_json()
        self.assertFalse(data["success"])

    def test_topic_endpoint_saves_text(self):
        data = self.client.post("/api/moderator/topic",
                                json={"topic": "Новая тема"}).get_json()
        self.assertTrue(data["success"])
        self.assertEqual(self.session.topic, "Новая тема")

    def test_judge_rules_are_saved_from_the_editor(self):
        with mock.patch.object(show, "save_theatre_settings") as save:
            data = self.client.post("/api/moderator/instructions",
                                    json={"judge_rules": ["Пункт"]}).get_json()
        self.assertTrue(data["success"])
        self.assertEqual(self.session.judge_rules, ["Пункт"])
        save.assert_called_once()

    def test_personal_instructions_are_saved_by_name(self):
        person = non_judge_ai(self.session)
        payload = {"participant_instructions": [
            {"name": person["display_name"], "instruction": "Будь краток"}]}
        self.client.post("/api/moderator/instructions", json=payload)
        stored = next(p for p in self.session.runtime_participants
                      if p["display_name"] == person["display_name"])
        self.assertEqual(stored["instruction"], "Будь краток")

    def test_editor_lists_only_ai_participants(self):
        """Редактор промптов не должен предлагать инструкции живым людям."""
        data = self.client.get("/api/moderator/instructions").get_json()
        listed = data["participant_instructions"]
        names = [p["name"] for p in listed]
        humans = [p["display_name"] for p in self.session.runtime_participants
                  if p.get("model") == "human"]
        self.assertTrue(names, "редактор не увидел ни одного участника")
        for human in humans:
            self.assertNotIn(human, names)
        self.assertTrue(any(p.get("is_judge") for p in listed),
                        "у судьи должен быть свой редактор промпта")
        self.assertIn("judge_rules", data)
        self.assertTrue(data["default_judge_prompt"].strip())

    def test_participants_post_reports_errors_as_json(self):
        data = self.client.post("/api/participants",
                                json={"participants": []}).get_json()
        self.assertFalse(data["success"])
        self.assertIn("error", data)

    def test_participants_post_accepts_a_valid_cast(self):
        payload = {"participants": cast_payload(self.session)}
        payload["participants"][0]["display_name"] = "Проверка Имени"
        data = self.client.post("/api/participants", json=payload).get_json()
        self.assertTrue(data["success"], data.get("error"))

    def test_draft_route_gives_a_place_and_keeps_the_cast(self):
        before = [p["display_name"] for p in self.session.runtime_participants]
        data = self.client.post("/api/participants/draft", json={}).get_json()

        self.assertTrue(data["success"])
        self.assertTrue(data["participant"]["display_name"].strip())
        self.assertTrue(data["participant"]["model"])
        self.assertEqual([p["display_name"] for p in self.session.runtime_participants], before)

    def test_the_full_reset_returns_the_console_to_settings(self):
        """Сброс один и он полный: и места, и правила общения, и руководства, и судья."""
        self.session.static_instructions = ["Своё правило"]
        self.session.moderator_guidelines = ["Своё руководство"]
        self.session.judge_rules = ["Своё правило судьи"]
        self.client.post("/api/participants",
                         json={"participants": cast_payload(self.session)[1:]})

        data = self.client.post("/api/settings/reset", json={}).get_json()

        self.assertTrue(data["success"], data.get("error"))
        self.assertIsNone(self.session.scene)
        self.assertEqual(len(data["participants"]), len(settings.PARTICIPANTS),
                         "состав должен вернуться к PARTICIPANTS")
        shown = self.client.get("/api/moderator/instructions").get_json()
        self.assertEqual(shown["static_instructions"], settings.DEFAULT_STATIC_INSTRUCTIONS)
        self.assertEqual(shown["moderator_messages"], [])
        self.assertEqual(shown["judge_rules"], settings.DEFAULT_JUDGE_RULES)

    def test_the_topic_is_saved_from_the_console(self):
        """Тему можно набрать в поле и не начинать спектакль — сохранится она всё равно."""
        self.client.post("/api/moderator/topic", json={"topic": "Тема из пульта"})

        data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["topic"], "Тема из пульта")

    def test_a_new_show_returns_the_topic_it_kept(self):
        self.client.post("/api/moderator/topic", json={"topic": "Тема"})

        data = self.client.post("/api/reset", json={}).get_json()

        self.assertEqual(data["topic"], "Тема")
        self.assertEqual(self.session.topic, "Тема")

    def test_the_full_reset_keeps_the_topic(self):
        """Тема — про сюжет, а не про труппу: её не сбрасывает даже полный сброс."""
        self.client.post("/api/moderator/topic", json={"topic": "Тема прошлого спектакля"})

        data = self.client.post("/api/settings/reset", json={}).get_json()

        self.assertEqual(self.session.topic, "Тема прошлого спектакля")
        self.assertEqual(data["topic"], "Тема прошлого спектакля")

    def test_the_full_reset_survives_a_restart_but_forgets_the_console(self):
        """Сброс переживает перезапуск, а тема переживает и сброс.

        Если бы файл остался как был, следующий запуск вернул бы то, от чего
        только что отказались; если бы файл удалили целиком, вместе с ним ушла бы
        и тема, которую придумывают руками.
        """
        self.client.post("/api/moderator/instructions",
                         json={"static_instructions": ["Своё правило"]})
        self.client.post("/api/moderator/topic", json={"topic": "Тема прошлого спектакля"})

        self.client.post("/api/settings/reset", json={})

        # В файле осталась одна тема: состав и правила сброс не возвращает
        data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("cast", data)
        self.assertNotIn("static_instructions", data)
        self.assertNotIn("judge_rules", data)
        self.assertEqual(data["topic"], "Тема прошлого спектакля")

        # Перезапуск: состав — из PARTICIPANTS, правила — из settings.py,
        # тема — из файла
        self.session.runtime_participants = []
        self.session.topic = ""
        show.load_theatre_settings()
        self.assertEqual(self.session.topic, "Тема прошлого спектакля")

    def test_editing_instructions_saves_the_console(self):
        """Редактор — часть пульта: без сохранения его правки жили бы до перезапуска."""
        self.client.post("/api/moderator/instructions", json={
            "static_instructions": ["Общее правило"],
            "moderator_messages": ["Руководство"],
            "judge_rules": ["Пункт судьи"],
        })

        data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["static_instructions"], ["Общее правило"])
        self.assertEqual(data["moderator_guidelines"], ["Руководство"])
        self.assertEqual(data["judge_rules"], ["Пункт судьи"])
        self.assertTrue(data["cast"], "состав — часть того же пульта")

    def test_participants_post_returns_the_same_view_as_get(self):
        """После «Применить состав» пульт должен получить те же числа, что и при загрузке."""
        payload = {"participants": cast_payload(self.session)}
        data = self.client.post("/api/participants", json=payload).get_json()

        for person in data["participants"]:
            self.assertIn("role", person)
            if person.get("model") != "human":
                self.assertIn("effective_options", person)
                self.assertIn("model_defaults", person)

    def test_the_emoji_choice_goes_into_the_cast_and_the_menu_lists_real_ones(self):
        """Выбор эмодзи — правка состава, а меню предлагает только существующие."""
        person = non_judge_ai(self.session)
        name = person["display_name"]
        emoji = (settings.AVATAR_EMOJIS_FEMALE[0] if person.get("gender") == "female"
                 else settings.AVATAR_EMOJIS_MALE[0])
        data = self.client.post("/api/participant/emoji",
                                json={"name": name, "emoji": emoji}).get_json()
        self.assertTrue(data["success"], data.get("error"))
        stored = next(p for p in data["participants"] if p["display_name"] == name)
        self.assertEqual(stored["avatar_emoji"], emoji)

        bad = self.client.post("/api/participant/emoji",
                               json={"name": name, "emoji": "🚀"}).get_json()
        self.assertFalse(bad["success"], "чужой значок ставить нельзя")

        listing = self.client.get("/api/participants").get_json()
        for pool in ("male", "female", "neutral"):
            self.assertTrue(listing["emojis"][pool], f"набор {pool} пуст")

    def test_the_lane_carries_the_face_of_the_cast(self):
        """Лицо реплики берётся из состава и в том же виде уезжает в ленту.

        Статус и событие сокета собираются одной функцией (см. status_payload):
        раньше именно тут лица и расходились с тем, что показывал пульт.
        """
        person = non_judge_ai(self.session)
        name = person["display_name"]
        self.session.posts.append(show.create_post(
            name, person.get("model", ""), "Реплика", 1, None, "🐺",
            gender=person.get("gender")))
        # Режиссёр поменял лицо в пульте и применил состав
        person["avatar_emoji"] = "👩‍🔬"
        self.session.sync_cast_media()

        data = self.client.get("/api/status").get_json()
        post = next(p for p in data["posts"] if p["display_name"] == name)
        self.assertEqual(post["avatar_emoji"], "👩‍🔬",
                         "в ленте должно стоять лицо из состава, а не из реплики")

    def test_start_reports_that_the_play_is_being_continued(self):
        """Доиграть прежний спектакль можно и из пульта, а не только по вопросу в консоли.

        Странице надо при этом знать, что прежние реплики — свои: иначе она
        стирала бы ленту перед продолжением (см. startDebate).
        """
        self.session.posts.append(show.create_post(
            "Живой", "human", "Сказано раньше.", 1, gender="male"))
        data = self.client.post("/api/start",
                                json={"topic": "Продолжение", "continue": True}).get_json()

        self.assertTrue(data["success"], data.get("error"))
        self.assertTrue(data["resumed"], "странице надо знать, что прежние реплики — свои")
        self.assertEqual(data["total_posts"], 1)
        self.assertTrue(self.session.resumed)
        self.assertFalse(self.session.resume_ready,
                         "разрешение достаётся одному старту, а не всем следующим")

    def test_a_plain_start_does_not_continue_anything(self):
        """Без просьбы о продолжении старт — обычный: прежние реплики уходят."""
        self.session.posts.append(show.create_post(
            "Живой", "human", "Сказано раньше.", 1, gender="male"))
        data = self.client.post("/api/start", json={"topic": "Новый"}).get_json()

        self.assertTrue(data["success"], data.get("error"))
        self.assertFalse(data["resumed"])
        self.assertEqual(data["total_posts"], 0, "прежние реплики должны были уйти")

    def test_participants_get_explains_effective_options(self):
        data = self.client.get("/api/participants").get_json()
        self.assertIn("characters", data)
        self.assertIn("option_keys", data)
        self.assertGreater(len(data["characters"]), 5)
        for person in data["participants"]:
            if person.get("model") != "human":
                self.assertIn("effective_options", person)


# ------------------------------------------- доиграть прежний спектакль

class TestContinuingAPlay(unittest.TestCase):
    """Прежний спектакль можно доиграть: модели слышат сказанное, акт и цена идут дальше.

    Доиграть — это не «показать ещё раз»: прежние реплики остаются в ленте,
    уезжают моделям историей, акт нумеруется следующим, цена не обнуляется,
    а ДАМП дописывается тем же файлом (см. show.start_show, start_dump(keep=True)).
    """

    # Прежний спектакль: ход модели с ценой и реплика живого участника
    PLAY = (
        "# ДАМП · 16.09.2026 22:01 · порт 5000\n\n"
        "Формат записи: 4\n\n"
        "**Тема:**\n> Прежний разговор\n\n"
        "**Состав:** 🦊 Проверка ♀ · Участник · fake-model · — ‖ 🐺 Второй ♂ · Участник · fake-model · —\n\n"
        "## 1 · 22:01 · 🦊 Проверка ♀ · fake-model · Участник · Акт 1 · ⏱ 1 мин · 💰 2,50 ₽\n\n"
        "### 👤 Кто говорит и когда\n\n"
        "**Кто:** **Проверка** ♀ · fake-model · Акт 1 · 22:01\n\n"
        "### 💬 Реплика, которой ход кончился\n\n"
        "> Прежнее слово модели.\n\n"
        "## 2 · 22:02 · 🐺 Второй ♂ · human · Участник · Акт 1\n\n"
        "Реплика человека: никуда не отправлялась, ни токенов, ни поиска.\n\n"
        "> Сказано руками до занавеса.\n")

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.said, self.seen = [], []
        for name, value in (
            ("ask_model", mock.Mock(side_effect=self._answer)),
            ("check_models_available",
             mock.Mock(return_value={"ok": True, "missing": [], "error": None})),
            ("unload_model", mock.Mock()),
            ("unload_other_show_models", mock.Mock()),
        ):
            patcher = mock.patch.object(ollama_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dump = Path(folder.name) / "damp.md"
        self.dump.write_text(self.PLAY, encoding="utf-8")
        patcher = mock.patch.object(settings, "DUMP_FILE", self.dump)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _answer(self, model, messages, participant_name, **kwargs):
        self.seen.append([str(m.get("content") or "") for m in messages])
        return f"{participant_name} говорит дальше.", 0, []

    def returned_play(self) -> list:
        """Прежний спектакль вернулся и ждёт продолжения — как после вопроса в консоли."""
        self.assertEqual(show.load_play_from_dump("continue"), 2,
                         "прежний спектакль не вернулся — продолжать нечего")
        self.session.start_show("Прежний разговор")
        return list(self.session.posts)

    def test_the_continuation_is_played_as_the_next_act_and_keeps_the_bill(self):
        played = self.returned_play()
        self.assertEqual(self.session.current_round, 1, "акт из записи не прочитался")

        def on_post(post):
            self.said.append(post)
            self.session.moderator_finished = True

        with mock.patch("time.sleep"):
            show.run_debate_thread("Прежний разговор", on_post=on_post)

        self.assertTrue(self.said, "продолжение так и не заговорило")
        self.assertEqual(self.said[0]["round"], 2,
                         "продолжение сыграно как первый акт — прежние реплики будто пропали")
        self.assertGreaterEqual(self.session.spent, 2.5,
                                "цена прежнего спектакля не переехала")
        self.assertEqual([p["display_name"] for p in self.session.posts][:len(played)],
                         [p["display_name"] for p in played],
                         "продолжение подменило прежние реплики")
        self.assertIn("Прежнее слово модели.",
                      "\n".join("\n".join(one) for one in self.seen),
                      "модель не слышала прежнюю реплику")
        self.assertIn("Сказано руками до занавеса.",
                      "\n".join("\n".join(one) for one in self.seen))

        written = self.dump.read_text(encoding="utf-8")
        self.assertIn("**Продолжение спектакля:**", written)
        self.assertIn("## 1 · 22:01", written, "прежние записи стёрлись из файла")
        play = show.parse_dump(written)
        self.assertEqual([p["id"] for p in play["posts"]], [1, 2, 3],
                         "новая запись не дописалась в тот же файл")
        self.assertEqual(play["round"], 2)


# ---------------------------------------------------- лицо участника

class TestAvatarBelongsToTheCast(unittest.TestCase):
    """Аватар — свойство участника, а не реплики: лицо берётся из состава.

    Ради этого портрет и переехал в состав: место в пульте может получить другое
    лицо, и оно должно смениться у всех его реплик — и у новых, и у сказанных.
    А у того, кто из состава ушёл, лицо остаётся с его репликами: это и есть та
    память о портрете, ради которой его стоит держать в посте.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.session.runtime_participants = [
            {"cast_id": "cast-a", "display_name": "Мария", "model": "fake-model",
             "gender": "female", "avatar_emoji": "🦊", "avatar_url": None},
            {"cast_id": "cast-b", "display_name": "Пётр", "model": "fake-model",
             "gender": "male", "avatar_emoji": "🐺", "avatar_url": None},
        ]
        self.session.sync_cast_media()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        # Смена эмодзи сохраняет состав: без подмены тест переписал бы
        # настоящий файл пульта проекта
        patcher = mock.patch.object(settings, "SETTINGS_FILE",
                                    self.folder / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def said(self, name: str = "Мария") -> dict:
        """Сказанная реплика участника — тем же create_post, что и в спектакле."""
        return show.create_post(name, "fake-model", "Привет", 1, None,
                                self.session.avatar_emojis.get(name, "📣"),
                                gender="female")

    def test_the_face_of_a_said_reply_follows_the_cast(self):
        """Применили новый состав — сменилось лицо и у сказанных реплик."""
        post = self.said()
        self.assertEqual(show.post_view(post)["avatar_emoji"], "🦊")
        self.session.runtime_participants[0]["avatar_emoji"] = "👩‍🔬"
        self.session.sync_cast_media()

        self.assertEqual(show.post_view(post)["avatar_emoji"], "👩‍🔬",
                         "портрет места должен смениться и у сказанных реплик")
        self.assertEqual(post["avatar_emoji"], "🦊",
                         "в самой реплике лицо не переписывается — рисуют её копию")

    def test_a_face_of_the_one_who_left_the_cast_stays(self):
        """Ушедший из труппы остаётся со своим лицом в прежних репликах."""
        post = self.said()
        self.session.runtime_participants = self.session.runtime_participants[1:]
        self.session.sync_cast_media()
        self.assertEqual(show.post_view(post)["avatar_emoji"], "🦊",
                         "у ушедшего лицо остаётся с его репликами")

    def test_a_face_that_is_gone_from_disk_is_not_a_face(self):
        """За адресом портрета нет файла — лицом считается эмодзи."""
        self.session.runtime_participants[0]["avatar_url"] = \
            "/avatars/no_such_face_at_all.jpg"
        self.session.sync_cast_media()
        view = show.post_view(self.said())
        self.assertIsNone(view["avatar_url"], "пустая рамка вместо лица хуже эмодзи")
        self.assertEqual(view["avatar_emoji"], "🦊")

    def test_the_picked_emoji_goes_into_the_cast_and_stays(self):
        """Выбранный эмодзи — правка состава, и она сохраняется вместе с ним."""
        self.assertEqual(show.set_participant_emoji("Мария", "👩‍🔬"), "")
        self.assertEqual(self.session.avatar_emojis["Мария"], "👩‍🔬")
        stored = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        saved = next(p for p in stored["cast"] if p["display_name"] == "Мария")
        self.assertEqual(saved["avatar_emoji"], "👩‍🔬",
                         "эмодзи — часть состава, и он сохраняется")

    def test_a_foreign_emoji_is_refused(self):
        """Набор значков держит сервер: чужое значение в состав не попадает."""
        self.assertIn("набор", show.set_participant_emoji("Мария", "🚀"))
        self.assertIn("нет", show.set_participant_emoji("Никто", "🦊"))
        self.assertEqual(self.session.avatar_emojis["Мария"], "🦊")


# ---------------------------------------------------------------- сообщения

class TestProblemMessages(unittest.TestCase):

    def test_missing_models_are_named_with_pull_commands(self):
        message = ollama_api.models_problem_message(
            {"ok": False, "missing": ["r1", "g1"], "error": None})
        self.assertIn("r1", message)
        self.assertIn("ollama pull r1", message)

    def test_unreachable_ollama_is_explained(self):
        message = ollama_api.models_problem_message(
            {"ok": False, "missing": ["r1"], "error": "нет связи"})
        self.assertIn("нет связи", message)
        self.assertIn("Ollama", message)

    def test_no_problem_means_no_message(self):
        self.assertEqual(ollama_api.models_problem_message(
            {"ok": True, "missing": [], "error": None}), "")


# ---------------------------------------------------------------- канал и пульт

class TestLiveChannelAndPanel(unittest.TestCase):
    """Лента, состояние и пульт во время спектакля.

    Сокет — основной канал: реплика приходит готовым постом, состояние сервер
    шлёт сам, а трёхсекундный опрос остался редкой подстраховкой. И пульт на ходу
    свёрнут до заголовков: режиссёр смотрит спектакль, а раздел открывается
    только тогда, когда в нём есть дело — предупреждение готовности или ваш ход.
    """

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def test_the_status_comes_by_itself_and_the_poll_is_a_fallback(self):
        """Состояние приносит сокет, а опрос — редкая сверка на случай обрыва."""
        self.assertIn("const POLL_FALLBACK_MS = 20000;", self.page)
        self.assertIn("socket && socket.connected ? POLL_FALLBACK_MS : POLL_MS", self.page)
        self.assertIn("socket.on('status_update'", self.page)
        # Опрос и сокет рисуют одно и то же место страницы, а не каждый своё
        self.assertIn("function applyStatus(data)", self.page)
        self.assertIn(".then(data => applyStatus(data))", self.page)
        # Пустое состояние до спектакля не должно «оживить» пустой театр
        self.assertIn("if (!data.running && !data.finished && !data.total_posts) return;",
                      self.page)
        # Счётчик показанных реплик двигается только вместе с репликами: в рассылке
        # по сокету их нет, и иначе счётчик перескочил бы мимо ещё не показанного
        # поста — реплика из события просто пропала бы
        self.assertIn("if (data.posts_included && typeof data.total_posts === 'number') {",
                      self.page)

    def test_the_reply_comes_as_a_post_and_is_not_asked_for_again(self):
        """Реплика по сокету — готовый пост: перепрашивать её незачем."""
        self.assertIn("socket.on('new_post', post => {", self.page)
        self.assertIn("addNewPosts([post])", self.page)
        self.assertNotIn("'state_update'", self.page,
                         "сокет больше не дёргает опрос за каждой репликой")
        # Совпали сокет и опрос — реплика всё равно одна
        self.assertIn("if (post.id && post.id <= lastPostCount) return;", self.page)

    def test_the_whole_pult_is_collapsed_when_the_show_starts(self):
        """Спектакль пошёл — пульт свёрнут до заголовков: смотрят спектакль."""
        self.assertIn("setAllSectionsCollapsed(phase !== 'setup');", self.page)
        self.assertIn('id="sectionsToggle"', self.page)
        self.assertIn('onclick="toggleAllSections()"', self.page)
        self.assertIn("function everySectionCollapsed()", self.page)
        # Кнопка есть только на ходу: в настройке сворачивать нечего
        self.assertIn("btn.style.display = sectionsPhase === 'setup' ? 'none' : 'inline-block';",
                      self.page)

    def test_the_reply_panel_opens_on_your_turn_only(self):
        """«Ваша реплика» в спектакле раскрыта ровно на вашем ходу."""
        self.assertIn("setSectionCollapsed('turnSection', state !== 'your');", self.page)
        self.assertIn("if (!debateRunning || showFinished) return;", self.page)
        # Фаза сменилась — ход пересчитываем заново, иначе раздел не раскроется
        self.assertIn("turnSectionState = null;", self.page)


# ---------------------------------------------------------------- редакторы на тёмной сцене

class TestEditorCardsOnDarkStage(unittest.TestCase):
    """Карточки редактора на тёмной сцене красятся классом, а не инлайном.

    Так уже было: фон карточки личной инструкции прописан инлайном
    (`background:white`), а инлайн перебивает тёмную тему — имя участника
    оставалось белым на белом, и его просто не было видно.
    """

    def setUp(self):
        self.page = page.HTML_TEMPLATE

    def test_the_instruction_card_takes_its_background_from_the_theme(self):
        self.assertIn('<div class="instr-card${p.is_judge', self.page)
        self.assertIn("body.dark .instr-card { background: #141414; }", self.page)
        self.assertIn("body.dark .instr-card.judge { background: #1c1c1c; }", self.page)
        # Подсказка судьи — тоже из класса: серая на белой карточке терпимо,
        # а на тёмной уже не читается
        self.assertIn('<div class="instr-hint">', self.page)
        self.assertIn("body.dark .instr-hint {", self.page)

    def test_the_tuning_card_takes_its_background_from_the_theme(self):
        """Карточка места в «Тонкой настройке» — та же беда была бы с её подписями."""
        self.assertIn('<div class="index-card" data-participant-index=', self.page)
        self.assertIn("body.dark .index-card { background: #141414; }", self.page)

    def test_no_card_paints_itself_white_from_the_markup(self):
        """Инлайновый белый фон — та же ошибка, где бы он ни появился.

        Тема красит фон только своими правилами, поэтому белый цвет в разметке
        (в отличие от стилей) сразу значит, что тёмная сцена его не перебьёт.
        """
        self.assertNotIn("background:#ffffff", self.page)
        self.assertNotIn("background:white", self.page)


# ---------------------------------------------------------------- канал состояния

class TestStatusChannel(unittest.TestCase):
    """Состояние спектакля: один источник на опрос и на сокет.

    Раньше словарь состояния собирался в двух местах — в маршруте и в событиях —
    и они уже расходились: поле, добавленное в одно, не появлялось в другом.
    """

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        for name, value in (
            ("fetch_loaded_models", mock.Mock(return_value=([], None))),
            ("fetch_gpu_memory", mock.Mock(return_value={})),
        ):
            patcher = mock.patch.object(ollama_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = web_app.app.test_client()
        # Ткач состояния — процессный: без сброса флага следующая проверка решила
        # бы, что рассылка уже идёт, и не завела бы свою
        self.addCleanup(web_app._status_pusher.update, alive=False)

    @staticmethod
    def _wait_for(condition, seconds: float = 3.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not condition():
            time.sleep(0.005)
        return condition()

    def test_the_poll_and_the_socket_tell_the_same_state(self):
        """Один источник на два канала: иначе поле, добавленное в один, теряется."""
        self.session.topic = "Тема"
        self.session.running = True
        self.session.current_round = 2
        asked = self.client.get("/api/status?lastPostCount=0").get_json()

        sent = []
        with mock.patch.object(web_app.socketio, "emit",
                               lambda evt, data=None: sent.append((evt, data))):
            web_app.publish_status()

        event, pushed = sent[0]
        self.assertEqual(event, "status_update")
        self.assertEqual(sorted(set(asked) - {"posts"}), sorted(pushed),
                         "по сокету уезжают те же поля, что у опроса")
        # Различие ровно одно: у опроса вместе с состоянием едут реплики, а у рассылки
        # нет — об этом и говорит поле posts_included
        for key, value in pushed.items():
            if key in ("new_posts", "posts_included"):
                continue
            self.assertEqual(value, asked[key], f"поле {key} разошлось между каналами")
        self.assertEqual(pushed["new_posts"], [],
                         "реплики ходят событиями new_post, а не в состоянии")
        self.assertFalse(pushed["posts_included"])
        self.assertTrue(asked["posts_included"], "опрос обязан привезти реплики")

    def test_the_poll_still_carries_the_posts_it_was_asked_for(self):
        """Подстраховка осталась рабочей: опрос отдаёт реплики после указанной."""
        self.session.posts = [{"id": 1}, {"id": 2}, {"id": 3}]
        payload = self.client.get("/api/status?lastPostCount=2").get_json()
        self.assertEqual(payload["new_posts"], [{"id": 3}])
        self.assertEqual(payload["total_posts"], 3)

    def test_the_state_is_pushed_by_itself_while_the_show_runs(self):
        """Основной канал — сокет: состояние идёт само, пока идёт спектакль.

        Ждать опроса по три секунды, чтобы увидеть «Ищет: …», больше не нужно.
        Занавес — рассылка прекращается: лишний поток не будит процессор зря.
        """
        sent = []
        self.session.running = True
        with mock.patch.object(web_app.socketio, "emit",
                               lambda evt, data=None: sent.append((evt, data))), \
                mock.patch.object(web_app, "STATUS_PUSH_INTERVAL", 0.01):
            web_app.start_status_pusher()
            self.assertTrue(self._wait_for(lambda: len(sent) >= 2),
                            "состояние не рассылается")
            self.session.running = False
            self.assertTrue(self._wait_for(lambda: len(sent) >= 3),
                            "после занавеса должно прийти последнее состояние")
            quiet = len(sent)
            time.sleep(0.1)
            self.assertEqual(len(sent), quiet, "рассылка не остановилась")

        self.assertTrue(all(event == "status_update" for event, _ in sent))
        self.assertFalse(sent[-1][1]["running"],
                         "последнее состояние говорит, что спектакль кончился")

    def test_a_second_pusher_is_not_started(self):
        """Рассылка просится и стартом, и подключением — поток должен быть один."""
        web_app._status_pusher["alive"] = True
        with mock.patch.object(web_app.threading, "Thread") as thread:
            web_app.start_status_pusher()
        thread.assert_not_called()


# -------------------------------------------------------- страница целиком

class TestPageScript(unittest.TestCase):
    """Страница целиком: разметку не исполняет ни один питоновский тест — а браузер исполняет.

    Одна синтаксическая ошибка в скрипте убивает скрипт целиком, поэтому
    «сломалась одна строка» на странице выглядит как «перестали работать все
    кнопки»: обработчики в разметке есть, а функций `toggleTheme` и
    `toggleRoleMarks` уже нет, и в консоли — `is not defined`. Найти это в
    питоновском файле нечем: для Python шаблон — просто строка.

    Отсюда два сторожа. Первый — настоящий интерпретатор JavaScript (если он в
    системе есть): он разбирает скрипт страницы так же, как браузер. Второй
    смотрит на то, чего интерпретатор увидеть уже не может: в шаблон попал
    питоновский эскейп (`\n`), и Python превратил его в настоящий перевод
    строки — строка JS порвалась на три, а вместе с ней и весь скрипт. Для
    строки в JS нужен `\\n` — тогда до браузера доедет тот самый `\n`.
    """

    # Разметка идёт обычной строкой (`"""`, а не `r"""`), поэтому любой
    # `\n`, `\t` и `\r` в ней — это эскейп Python. Два слэша подряд (`\\n`)
    # под проверку не попадают: это уже настоящая строка JS.
    PYTHON_ESCAPE_RE = re.compile(r"(?<!\\)\\[ntr]")

    @staticmethod
    def template_source() -> str:
        """Разметка из `page.py` — ровно так, как она написана на диске."""
        source = Path(page.__file__).read_text(encoding="utf-8")
        opening = 'HTML_TEMPLATE = """'
        start = source.index(opening) + len(opening)
        return source[start:source.index('"""', start)]

    def script_blocks(self):
        """Скрипты самой страницы, без внешних подключений (клиент Socket.IO)."""
        return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                          page.HTML_TEMPLATE, re.S)

    def test_python_did_not_eat_backslashes_in_the_page(self):
        """`\n` внутри шаблона рвёт строку JS — и вместе с ней всю страницу."""
        region = self.template_source()
        found = []
        for match in self.PYTHON_ESCAPE_RE.finditer(region):
            line = region.count("\n", 0, match.start()) + 1
            found.append(f"строка {line}: {match.group(0)!r}")
        self.assertEqual(found, [], (
            "в шаблоне страницы питоновские эскейпы: они превращаются в настоящие "
            "переводы строк и ломают скрипт. Для строки в JS нужен `\\\\n`:\n  "
            + "\n  ".join(found)))

    def test_the_page_script_parses(self):
        """Скрипт страницы разбирается настоящим интерпретатором JavaScript."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node не найден: разбор скрипта не с кем сверить")
        blocks = self.script_blocks()
        self.assertTrue(blocks, "на странице не нашлось ни одного своего скрипта")
        for number, block in enumerate(blocks, 1):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                            encoding="utf-8") as handle:
                handle.write(block)
                path = handle.name
            try:
                result = subprocess.run([node, "--check", path],
                                        capture_output=True, text=True)
            finally:
                os.unlink(path)
            self.assertEqual(result.returncode, 0,
                             f"скрипт страницы (блок {number}) не разбирается:\n"
                             f"{(result.stderr or '').strip()}")

    @staticmethod
    def _function(name: str) -> str:
        """Вытащить функцию страницы по имени — со скобками по балансу.

        Проверять одни часы без остального скрипта можно именно так: разбором
        счётом скобок, а не поиском закрывающей строки — иначе первая же
        вложенная функция дала бы обрезанное тело и проверку не о том.
        """
        source = page.HTML_TEMPLATE
        start = source.index(f"function {name}(")
        depth = 0
        for index in range(start, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:index + 1]
        raise AssertionError(f"у функции {name} не нашлось конца")

    @staticmethod
    def _page_constant(name: str) -> str:
        """Строка `const <имя> = …;` со страницы — целиком, для сверки в node.

        Скобки считаются, а не ищется `;` в той же строке: у словаря и списка
        конец бывает и на следующей строке, и число вытаскивалось бы обрубленным
        (см. FINISH_WORDS — по нему страница объясняет коды ответа вендора).
        """
        source = page.HTML_TEMPLATE
        match = re.search(rf"^\s*const {name} = ", source, re.M)
        if not match:
            raise AssertionError(f"на странице нет числа {name}")
        depth = 0
        for index in range(match.end(), len(source)):
            char = source[index]
            if char in "{[(":
                depth += 1
            elif char in "}])":
                depth -= 1
            elif char == ";" and depth == 0:
                return source[match.start():index + 1].strip()
        raise AssertionError(f"у числа {name} не нашлось конца")

    def _run_in_node(self, *calls):
        """Считать часы по-настоящему: тот же код, что уедет в браузер.

        Ни один питоновский тест разметку не исполняет, а часов это касается
        больше всего: «осталось» считает браузер, и ошибка в нём видна только
        зрителю (см. turnClockText).
        """
        return self._run_page(("tokensText", "moneyText", "durationText",
                               "turnClockText", "turnSummaryParts", "spentLine",
                               "textNeedsClamp"), *calls)

    def _run_page(self, functions, *calls, constants=()):
        """Страница в node: сколько угодно её функций и чисел — в том виде,
        в каком они уедут в браузер.

        Списком, а не одной строкой: сверить с файлом надо и то, что считает
        часы, и то, чем рассказан один запрос хода (см. show.ask_line), а тянуть
        в каждый прогон всю страницу незачем.
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("node не найден: часы не с кем сверить")
        # Числа страницы (const) едут в тот же скрипт: иначе проверяли бы свою
        # копию, а не то, что уедет в браузер (см. CLAMPED_TEXT_LINES)
        script = "\n".join(self._page_constant(name)
                          for name in ("CLAMPED_TEXT_LINES",) + tuple(constants)) + "\n"
        script += "\n".join(self._function(name) for name in functions)
        script += "\nconsole.log(JSON.stringify([" + ", ".join(calls) + "]));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8") as handle:
            handle.write(script)
            path = handle.name
        try:
            # Без text=True: русская консоль Windows читает вывод node как cp1251,
            # и «2 мин» превращалось бы в кракозябры — не из-за кода, а из-за
            # кодировки чужого процесса
            result = subprocess.run([node, path], capture_output=True)
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 0,
                         "часы хода не считаются:\n"
                         + result.stderr.decode("utf-8", "replace").strip())
        return json.loads(result.stdout.decode("utf-8"))

    def test_the_time_is_said_in_words(self):
        self.assertEqual(self._run_in_node("durationText(45)", "durationText(135)",
                                           "durationText(120)"),
                         ["45 с", "2 мин 15 с", "2 мин"])

    def test_the_summary_line_names_the_trouble_apart(self):
        """В свёрнутой строке отказ в поиске и молчание — двумя разными словами.

        Общее число «заминок» говорило «что-то было» и не говорило что: отказ
        в поиске — это просьба поискать ещё, и реплика при этом есть, а молчание —
        это когда реплики нет вовсе.
        """
        out = self._run_in_node(
            "turnSummaryParts({asks: 2, search_rounds: 1, tokens: 2254, seconds: 135,"
            " spent: 1.89, search_refusals: 1, silences: 0, tokens_in_total: 14158})",
            "turnSummaryParts({asks: 1, tokens: 10, silences: 2})")
        quiet, mute = " · ".join(out[0]), " · ".join(out[1])

        # Ввод назван по всем запросам хода: один запрос — и число одно
        self.assertIn("на ввод всего 14\u00a0158 токенов", quiet)
        self.assertIn("10 токенов на ввод", mute)

        self.assertIn("⏱ 2 мин 15 с", quiet)
        self.assertIn("1,89 ₽", quiet, "цена хода должна остаться в сводке")
        self.assertIn("поиск сверх лимита 1", quiet)
        self.assertNotIn("без ответа", quiet,
                         "молчания не было — и приписывать его нельзя")
        self.assertIn("без ответа 2", mute)
        self.assertNotIn("заминок", mute,
                         "общего «заминок» больше нет: оно ни о чём не говорит")

    def test_the_heading_of_a_dump_record_repeats_the_summary_of_the_page(self):
        """Шапка записи в файле — та же сводка, что свёрнутая строка в ленте.

        На странице по этой строке решают, раскрывать ли ход; в файле такой
        строки не было, и по ДАМПу нельзя было понять, что за ход внутри,
        не прочитав его целиком. Числа одни и те же и в том же порядке
        (см. show.turn_summary_line против turnSummaryParts).
        """
        summary = {"asks": 3, "search_rounds": 5, "thought_steps": 2,
                   "tokens": 2254, "tokens_in_total": 14158, "seconds": 135,
                   "spent": 1.89, "removed_messages": 2, "search_refusals": 1,
                   "silences": 1}
        out = self._run_in_node("turnSummaryParts(" + json.dumps(summary) + ")")
        # Пробел в разрядах у страницы неразрывный (toLocaleString), а у файла
        # обычный: читателю разницы нет, а сверке она мешала бы
        page_line = " · ".join(out[0]).replace("\u00a0", " ").replace("\u202f", " ")

        self.assertEqual(show.turn_summary_line(summary), page_line,
                         "в ленте и в файле об одном ходе сказано разное")

    def test_one_request_is_told_the_same_way_in_the_file_and_in_the_lane(self):
        """Один и тот же запрос в файле и в ленте — одними словами и числами.

        В файле стояло «ввод 3 431 · вывод 1 246», а в ленте — «ввод 3 431 →
        вывод 1 246»: один и тот же ход был рассказан двумя разными способами,
        и это читалось как разные числа (см. show.ask_line против turnAskText).
        """
        steps = [{"kind": "ask", "n": 2, "tokens_in": 3431, "tokens_in_est": 3400,
                  "tokens_out": 1246, "reasoning_tokens": 1160,
                  "finish_reason": "length", "tools": True},
                 # Вендор чисел не назвал — остаётся только наш счёт
                 {"kind": "ask", "n": 3, "tokens_in_est": 3400},
                 {"kind": "ask", "n": 4, "error": "шлюз ответил HTTP 400"}]
        out = self._run_page(("escapeHtml", "tokensText", "turnAskText"),
                             *[f"turnAskText({json.dumps(step)})" for step in steps],
                             constants=("FINISH_WORDS",))
        for step, page_line in zip(steps, out):
            self.assertEqual(show.ask_line(step),
                             page_line.replace("\u00a0", " ").replace("\u202f", " "),
                             "файл и лента рассказывают один запрос по-разному")

    def test_only_a_text_longer_than_three_lines_is_clamped(self):
        """Свёртка — по настоящей высоте текста, а не по числу переводов строки.

        Одна длинная строка переносится и занимает столько же места, сколько
        десять коротких, поэтому мерой взята высота. Вместе с ней посчитаны поля:
        три строки — ещё не простыня, четвёртая уже прячется.
        """
        out = self._run_in_node(
            "textNeedsClamp(60 + 16, 20, 16)",   # ровно три строки — видно целиком
            "textNeedsClamp(80 + 16, 20, 16)",   # четыре строки — прячем
            "textNeedsClamp(380, 20, 16)",       # простыня в потолок окна
            "textNeedsClamp(240, 0, 16)")        # высота строки неизвестна — не гадаем
        self.assertEqual(out, [False, True, True, False])

    def test_the_show_price_is_named_or_silent_when_there_is_nothing_to_pay(self):
        """Строка цены для сайдбара: с копейками, а на нуле — пустая."""
        out = self._run_in_node("spentLine({spent: 12.5})", "spentLine({spent: 0})",
                                "spentLine({})")
        self.assertIn("за спектакль", out[0])
        self.assertIn("12,50 ₽", out[0])
        self.assertEqual(out[1], "", "нечего было тратить — нечего и показывать")
        self.assertEqual(out[2], "")

    def test_the_clock_counts_up_and_shows_the_bonus(self):
        """Сколько думает и сколько осталось — и с надбавкой за поиски."""
        out = self._run_in_node(
            "turnClockText({turn_elapsed: 12, turn_left: 108, turn_extra: 0})",
            "turnClockText({turn_elapsed: 12, turn_left: 168, turn_extra: 60})",
            "turnClockText({turn_elapsed: 12, turn_left: null, turn_extra: 0})",
            "turnClockText({turn_elapsed: null, turn_left: null, turn_extra: 0})")

        self.assertIn("думает 12 с", out[0])
        self.assertIn("осталось 1 мин 48 с", out[0])
        self.assertIn("продлён на 1 мин", out[1],
                      "надбавка должна быть названа, иначе часы непонятно откуда берут время")
        self.assertIn("осталось 2 мин 48 с", out[1])
        self.assertIn("думает 12 с", out[2])
        self.assertNotIn("осталось", out[2], "без срока нет и остатка")
        self.assertEqual(out[3], "", "хода нет — и часов нет")

    @staticmethod
    def _const_block(name: str) -> str:
        """`const <имя> = …;` со страницы — даже если он разложен на две строки.

        Словари вида WINDOW_WORDS занимают несколько строк, и по одной строке
        их в node не отдать (см. _page_constant).
        """
        source = page.HTML_TEMPLATE
        match = re.search(rf"^\s*const {name} = ", source, re.M)
        if not match:
            raise AssertionError(f"на странице нет {name}")
        end = source.index(";", match.end())
        return f"const {name} = " + source[match.end():end + 1]

    def _render_turn_report(self, payload: str) -> str:
        """Собрать отчёт о ходе тем же кодом, что уедет в браузер.

        Ни один питоновский тест разметку не исполняет, а разделы отчёта —
        именно разметка: по исходнику не видно, получился ли раздел с именем
        и пояснением или строкой без них (см. turnBodyHtml).
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("node не найден: настоящую сборку отчёта не с кем сверить")
        script = "\n".join(self._const_block(name) for name in (
            "WINDOW_WORDS", "ROLE_WORDS", "SKETCH_HINT", "FINISH_WORDS"))
        script += "\n" + "\n".join(self._function(name) for name in (
            "escapeHtml", "tokensText", "durationText", "stepClock", "turnAskText",
            "promptMessagesHtml", "promptRemovedHtml", "thinkingBlockHtml",
            "addedPurpose", "turnBlock", "turnBodyHtml"))
        script += f"\nconsole.log(turnBodyHtml({payload}));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8") as handle:
            handle.write(script)
            path = handle.name
        try:
            result = subprocess.run([node, path], capture_output=True)
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 0,
                         "отчёт о ходе не собирается:\n"
                         + result.stderr.decode("utf-8", "replace").strip())
        return result.stdout.decode("utf-8")

    def test_the_turn_report_renders_as_named_sections(self):
        """Каждый раздел отчёта рисуется со своим именем и своим назначением.

        Раньше это были строки одинаковой серости в один поток, и понять, где
        кончается одно и начинается другое, было нельзя. Проверка настоящая:
        отчёт собирается тем же кодом, что и в браузере.
        """
        html = self._render_turn_report(
            "{who: {name: 'Проверка', model: 'cloud:fake', round: 2, time: '12:00'},"
            " summary: {messages: 2, tokens: 900, seconds: 12, removed_messages: 1,"
            " removed_tokens: 50, extra_messages: 2, extra_tokens: 700, asks: 2},"
            " budget: {kind: 'cloud', window: 32768, reserve: 0, safety: 500,"
            " system_tokens: 800, available: 24000, messages_after: 0, kept_tokens: 0},"
            " steps: [{kind: 'ask', n: 1, clock: '12:00:01', limit: 3, tokens_in: 900,"
            " tokens_out: 40, tools: true},"
            " {kind: 'search', n: 1, clock: '12:00:02', limit: 3, query: 'проверка',"
            " results: '1. Найдено', tokens: 30},"
            " {kind: 'thought', n: 2, clock: '12:00:03', text: 'Думаю.', tokens: 5},"
            " {kind: 'note', clock: '12:00:04', text: 'Замечание.'}],"
            " removed: [{speaker: 'Борис', tokens: 50, preview: 'давняя реплика'}],"
            " added: [{role: 'assistant', name: 'проверка', tokens: 0,"
            " content: '', note: 'прошу поиск'},"
            " {role: 'tool', name: 'search', tokens: 30, content: 'Найденное'}],"
            " messages: [{role: 'system', name: 'system', tokens: 800,"
            " content: 'Система'},"
            " {role: 'user', name: 'проверка', tokens: 100, content: '<b>не тег</b>'}],"
            " sketch: 'Прежняя версия ответа.', answer: 'Вот ответ.'}")

        for title in show.TURN_SECTIONS.values():
            words = title.lstrip("#").strip()
            self.assertIn(words, html, f"раздел «{words}» не нарисовался")
        frames = html.count('<section class="prompt-block">')
        self.assertGreaterEqual(frames, len(show.TURN_SECTIONS) - 1,
                                "разделов меньше, чем частей отчёта")
        self.assertEqual(html.count("prompt-block-title"), frames)
        self.assertEqual(html.count("prompt-block-purpose"), frames,
                         "у каждого раздела должно быть сказано, зачем он")
        # Найденное, размышления и реплика — на месте: разделы не должны
        # подменить содержание одними заголовками
        self.assertIn("Найденное", html)
        self.assertIn("Думаю.", html)
        self.assertIn("Вот ответ.", html)
        self.assertIn("&lt;b&gt;не тег&lt;/b&gt;", html,
                      "тексты разделов должны быть экранированы, а не уехать тегом")

    def test_the_added_tail_is_named_by_its_real_parts(self):
        """В шапке хвоста — счёт по частям, а не «по паре на каждый поиск».

        Именно так и было написано, и с числами это не сходилось: «3 сообщ. —
        по паре на каждый поиск, плюс напоминания» просит читателя делить
        тройку на пары. Здесь в хвосте одна просьба и три ответа (три поиска
        одним вызовом) и ни одного напоминания — про пары здесь было бы враньём
        (счёт — в show.added_kinds).
        """
        html = self._render_turn_report(
            "{who: {name: 'Проверка', model: 'cloud:fake', round: 1, time: '12:00'},"
            " summary: {messages: 1, tokens: 900, extra_messages: 4,"
            " extra_tokens: 900,"
            " added_kinds: {asks: 1, results: 3, refusals: 0, nudges: 0}},"
            " budget: {kind: 'cloud', window: 32768, safety: 500},"
            " added: [{role: 'assistant', name: 'проверка', tokens: 0, content: ''},"
            " {role: 'tool', name: 'search_web', tokens: 300, content: 'Найдено'},"
            " {role: 'tool', name: 'search_web', tokens: 300, content: 'Найдено'},"
            " {role: 'tool', name: 'search_web', tokens: 300, content: 'Найдено'}],"
            " messages: [], answer: 'Ответ.'}")

        self.assertIn("4 сообщ.", html, "число сообщений хвоста — в шапке раздела")
        self.assertIn("просьба вызвать инструмент — 1", html)
        self.assertIn("найденное по ней — 3", html)
        self.assertNotIn("по паре", html, "пара — привычный случай, а не счёт")
        self.assertNotIn("напоминания", html,
                         "напоминаний в ходу не было, а слово обещало бы их")

    def test_the_emoji_menu_offers_ones_of_the_sex_plus_the_common_ones(self):
        """Меню эмодзи: свои для пола плюс общие — точно как их раздаёт сервер.

        Меню и проверка на сервере должны сходиться: клик предлагает только то,
        что действительно можно поставить (см. show.set_participant_emoji),
        иначе половина значков не приживалась бы после выбора.
        """
        setup = ("EMOJIS = {male: ['🤴', '🧔'], female: ['👰', '💃'],"
                 " neutral: ['🦊', '🎭']}; return emojiChoices")
        out = self._run_page(("emojiChoices",),
                             f"(() => {{ {setup}('female'); }})()",
                             f"(() => {{ {setup}('male'); }})()")
        self.assertEqual(out[0], ["👰", "💃", "🦊", "🎭"],
                         "значки своего пола идут первыми, потом общие")
        self.assertEqual(out[1], ["🤴", "🧔", "🦊", "🎭"])

    def test_the_page_returns_to_the_play_even_without_a_session_number(self):
        """Лента из ДАМПа показывается и тогда, когда сессии уже нет.

        Номер сессии заводит только старт спектакля, а прежний спектакль его
        не имеет вовсе. Возвращаться при этом есть к чему: реплики лежат
        в ДАМПе и приходят странице (см. tryRestoreSession). Иначе под занавесом
        было пусто, а кнопка «Доиграть» — без ленты, к которой она относится.
        """
        restore = self._function("tryRestoreSession")
        self.assertIn("data.total_posts", restore,
                      "признак возврата — реплики, а не номер сессии")
        self.assertNotIn("if (!data.session_id) return;", restore)
        self.assertIn("playRestored", restore)
        self.assertIn("updatePanel();", restore)

    def test_the_curtain_offers_to_finish_the_returned_play(self):
        """Доиграть прежний спектакль можно из пульта — и только когда есть что доигрывать.

        Кнопка живёт под занавесом и зависит от того, что лента вернулась из ДАМПа
        (см. playRestored): продолжать пустую сцену или только что сыгранный
        спектакль нечем, а старт обязан попросить продолжение у сервера.
        """
        source = page.HTML_TEMPLATE
        self.assertIn('id="continueBtn"', source)
        self.assertIn("function continueShow", source)
        panel = self._function("updatePanel")
        self.assertIn("playRestored", panel,
                      "кнопка должна зависеть от того, что это прежний спектакль")
        self.assertIn("canContinue", panel)
        start = self._function("startDebate")
        self.assertIn("resumePlay", start, "старт должен уметь попросить продолжение")
        self.assertIn("continue", start)
        # Прежние реплики при продолжении остаются: стирается лента только
        # в ветке обычного старта, и вынос очистки из неё — уже потеря реплик
        self.assertIn("if (data.resumed)", start)
        self.assertIn("data.total_posts", start)
        self.assertLess(start.index("if (data.resumed)"), start.index("innerHTML = ''"),
                        "лента должна очищаться только когда продолжения не просили")
        # И признак прежнего спектакля ставится до отрисовки пульта: иначе под
        # занавесом кнопки не видно, пока не придёт следующий опрос
        status = self._function("applyStatus")
        flags = status[status.index("showFinished = !!data.finished;"):
                       status.index("if (data.waiting_for_human)")]
        self.assertIn("playRestored", flags,
                      "признак прежнего спектакля ставится вместе с остальными флагами")
        self.assertLess(flags.index("playRestored"), flags.index("updatePanel();"),
                        "флаги должны стоять до отрисовки пульта")

    def test_the_emoji_avatar_is_clickable_and_the_name_is_not_in_the_handler(self):
        """По эмодзи в ленте можно кликнуть — и имя уезжает в данные, а не в код.

        Имя — текст режиссёра: кавычка или апостроф в нём сломали бы обработчик,
        нарисованный строкой (см. postAvatarHtml), а вместе с ним и все кнопки
        ленты — как уже бывало с toggleTheme.
        """
        avatar = self._function("postAvatarHtml")
        self.assertIn("data-emoji-for=", avatar)
        self.assertNotIn("onclick=\"openEmojiPicker", avatar,
                         "имя в обработчике — это сломанный скрипт на кавычке в имени")
        self.assertIn("avatar_url", avatar, "у картинки остаётся прежний клик")

        source = page.HTML_TEMPLATE
        self.assertIn('id="emojiMenuGrid"', source, "меню значков должно быть в разметке")
        self.assertIn("[data-emoji-for]", source, "лента слушает клик по эмодзи")
        self.assertIn("'emojiMenuGrid'", source, "выбор значка обрабатывается меню")
        self.assertIn(".emoji-choice", source)
        # В составе: у картинки — полный размер, у эмодзи — набор значков
        editor = self._function("openAvatarModal")
        self.assertIn("openEmojiPicker", editor)
        self.assertIn("avatarModalImg", editor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
