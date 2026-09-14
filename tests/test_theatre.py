"""
Автотесты AI Театра.

Запуск из папки проекта:

    venv/Scripts/python.exe -m unittest discover -s tests -v

Набор ничего не запускает и никуда не ходит: Ollama и поток спектакля
подменяются заглушками, поэтому тесты проходят и без запущенной Ollama,
и не мешают идущему спектаклю. Все проверки — на живом коде проекта,
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
  * VRAM — оценка контекста ведёт себя монотонно и уважает бюджет.
"""

import builtins
import collections
import copy
import importlib
import json
import re
import symtable
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import aitheatre

from aitheatre import avatars, deps, ollama_api, page, search, settings, show, text
from aitheatre import web as web_app

# Приложение разбито на модули, и подменять функцию нужно там, где она живёт:
# модули вызывают друг друга по полному адресу (ollama_api.check_models_available),
# поэтому правка в «своём» модуле видна всем. app_module_of находит нужный модуль
# по имени — так подмена бьёт в цель, а не в копию имени в другом файле.
APP_MODULES = (settings, deps, text, search, avatars, ollama_api, show, page, web_app)


def app_module_of(name):
    """Модуль, в котором живёт имя приложения."""
    for module in APP_MODULES:
        if hasattr(module, name):
            return module
    raise AssertionError(f"нет такого имени приложения: {name}")


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
        order = ("settings", "deps", "text", "search", "avatars", "ollama_api",
                 "show", "page")
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

    def test_humans_keep_model_human(self):
        """Живому участнику нельзя подсунуть модель: он говорит сам."""
        payload = cast_payload(self.session)
        human_indexes = [i for i, p in enumerate(self.session.runtime_participants)
                         if p["model"] == "human"]
        self.assertTrue(human_indexes, "в составе должен быть живой участник")
        for index in human_indexes:
            payload[index]["model"] = "r1"
        self.assertEqual(show.apply_cast_patch(payload), "")
        cast = self.session.runtime_participants
        for index in human_indexes:
            self.assertEqual(cast[index]["model"], "human")

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
        payload = cast_payload(self.session)
        payload.reverse()
        self._apply(payload)

        scene = self.session.scene
        self.assertEqual([place["role"] for place in scene],
                         [show.cast_role(p) for p in self.session.runtime_participants])
        for place in scene:
            self.assertNotIn("display_name", place)
            self.assertNotIn("instruction", place)

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

    def test_the_draft_place_does_not_touch_the_cast(self):
        before = [p["display_name"] for p in self.session.runtime_participants]
        draft = show.draft_cast_entry()

        self.assertTrue(draft["model"], "новому месту нужна модель — как у соседа")
        self.assertFalse(draft["is_judge"])
        self.assertFalse(draft["is_moderator"])
        for key in ("cast_id", "display_name", "avatar_emoji", "avatar_keywords", "gender"):
            self.assertIn(key, draft)
        self.assertEqual([p["display_name"] for p in self.session.runtime_participants], before)


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
    """Сцена переживает перезапуск: лежит в том же файле, что правила судьи.

    В файле мест не хранятся: только роли, модели, порядок и числа. Имена
    каждый спектакль новые, а собранную сцену не хочется собирать заново.
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

    def test_saved_scene_comes_back_after_a_restart(self):
        payload = cast_payload(self.session)
        payload[0]["role"] = "judge"
        payload[1]["role"] = "participant"
        payload[2]["temperature"] = 0.33
        payload.reverse()
        self.assertEqual(show.apply_cast_patch(payload), "")
        saved = copy.deepcopy(self.session.scene)
        show.save_theatre_settings()

        # Перезапуск: сессия поднимается с нуля и читает файл
        self.session.scene = None
        show.load_theatre_settings()
        self.assertEqual(self.session.scene, saved)

    def test_rules_and_scene_live_in_one_file(self):
        self.session.judge_rules = ["Пункт"]
        self.session.scene = [{"model": "r1", "role": "judge"}]
        show.save_theatre_settings()

        data = json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["judge_rules"], ["Пункт"])
        self.assertEqual(data["scene"], [{"model": "r1", "role": "judge"}])

    def test_a_new_cast_is_built_from_the_saved_scene(self):
        self.session.scene = [{"model": "r1", "role": "participant"},
                              {"model": "human", "role": "judge"}]
        show.save_theatre_settings()

        self.session.scene = None
        show.load_theatre_settings()
        self.session.load_new_cast()

        cast = self.session.runtime_participants
        self.assertEqual([show.cast_role(p) for p in cast], ["participant", "judge"])
        self.assertEqual([p["model"] for p in cast], ["r1", "human"])
        self.assertTrue(cast[1]["instruction"].strip())

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
        self.assertIn("postDiv.className = `post post-role-${role}`", self.page)

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
                        "resetCast()"):
            with self.subTest(control=control):
                self.assertIn(control, self.page, f"в пульте нет управления {control}")

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

    def test_an_unchosen_model_is_not_silently_replaced(self):
        """Пустое место — это «модель ещё не выбрана», а не «первая модель Ollama».

        Без явного пустого варианта браузер сам показывал первую модель из списка,
        и перестановка соседа тихо записывала её в участника.
        """
        start = self.page.index("function modelOptions(")
        body = self.page[start:self.page.index("function collectCast()", start)]
        self.assertIn("— выберите модель —", body)

    def test_the_new_controls_use_the_routes_that_exist(self):
        for route in ("/api/participants/draft", "/api/participants/reset"):
            with self.subTest(route=route):
                self.assertIn(route, self.page)
                self.assertIn(route, {rule.rule for rule in web_app.app.url_map.iter_rules()})


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
                    "new_posts", "total_posts", "waiting_for_human"):
            self.assertIn(key, data)

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

    def test_reset_route_takes_the_cast_from_participants(self):
        payload = cast_payload(self.session)[1:]
        self.client.post("/api/participants", json={"participants": payload})
        self.assertEqual(len(self.session.runtime_participants), len(payload))

        data = self.client.post("/api/participants/reset", json={}).get_json()

        self.assertTrue(data["success"])
        self.assertIsNone(self.session.scene)
        self.assertEqual(len(data["participants"]), len(settings.PARTICIPANTS))

    def test_participants_post_returns_the_same_view_as_get(self):
        """После «Применить состав» пульт должен получить те же числа, что и при загрузке."""
        payload = {"participants": cast_payload(self.session)}
        data = self.client.post("/api/participants", json=payload).get_json()

        for person in data["participants"]:
            self.assertIn("role", person)
            if person.get("model") != "human":
                self.assertIn("effective_options", person)
                self.assertIn("model_defaults", person)

    def test_participants_get_explains_effective_options(self):
        data = self.client.get("/api/participants").get_json()
        self.assertIn("characters", data)
        self.assertIn("option_keys", data)
        self.assertGreater(len(data["characters"]), 5)
        for person in data["participants"]:
            if person.get("model") != "human":
                self.assertIn("effective_options", person)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
