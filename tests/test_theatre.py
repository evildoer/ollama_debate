"""
Автотесты AI Театра.

Запуск из папки проекта:

    venv/Scripts/python.exe -m unittest discover -s tests -v

Набор ничего не запускает и никуда не ходит: Ollama и поток спектакля
подменяются заглушками, поэтому тесты проходят и без запущенной Ollama,
и не мешают идущему спектаклю. Все проверки — на живом коде проекта,
а не на его копии.

Что покрыто (каждая группа — это место, где ошибку раньше ловили руками):
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

import collections
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ollama_debate_web as theatre


# ---------------------------------------------------------------- утилиты

def make_session():
    """Свежая сессия со своим составом — тесты не трогают состав приложения."""
    session = theatre.DebateSession()
    session.load_new_cast()
    return session


def cast_payload(session):
    """То, что присылает пульт на POST /api/participants."""
    return [copy.deepcopy(p) for p in session.runtime_participants]


def strip_session_patch(test, session):
    """Подменяет глобальную сессию проекта на тестовую."""
    patcher = mock.patch.object(theatre, "session", session)
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
        html = theatre.markdown_to_html(
            r"Предел $\lim_{x\to 0}\frac{\sin x}{x}$ равен единице")
        self.assertIn('<span class="math"', html)
        self.assertIn('data-display="0"', html)
        self.assertNotIn("$", html, "знаки $ должны быть израсходованы")

    def test_underscores_inside_formula_survive(self):
        # Ровно тот баг: markdown превращал _ в <em> и формула рассыпалась
        html = theatre.markdown_to_html(r"$a_1 + b_2 = c_3$")
        self.assertIn("a_1 + b_2 = c_3", html)
        self.assertNotIn("<em>", html)

    def test_block_formula_is_marked_as_display(self):
        html = theatre.markdown_to_html("Считаем:\n\n$$\\int_0^1 x^2 dx$$\n")
        self.assertIn('data-display="1"', html)
        self.assertIn("math-block", html)

    def test_bracket_and_paren_forms_work(self):
        self.assertIn('class="math"', theatre.markdown_to_html(r"\(a+b\)"))
        self.assertIn('data-display="1"', theatre.markdown_to_html(r"\[a+b\]"))

    def test_latex_escaped_braces_survive(self):
        html = theatre.markdown_to_html(r"Множество $\{a_n\}$ сходится")
        self.assertIn(r"\{a_n\}", html)

    def test_money_is_not_math(self):
        html = theatre.markdown_to_html("Курс $5 и $7 за штуку")
        self.assertNotIn('class="math"', html)
        self.assertIn("$5", html)
        self.assertIn("$7", html)

    def test_unclosed_formula_is_left_as_text(self):
        # Некорректный LaTeX не должен ронять разметку
        html = theatre.markdown_to_html(r"Обрыв $\frac{1}{ и всё")
        self.assertIsInstance(html, str)
        self.assertIn("и всё", html)

    def test_bold_and_formula_live_together(self):
        html = theatre.markdown_to_html(r"**Важно**: $x^2$ растёт")
        self.assertIn("<strong>Важно</strong>", html)
        self.assertIn('<span class="math"', html)
        self.assertIn("x^2", html)

    def test_formula_latex_is_escaped_for_html(self):
        # LaTeX отдаётся в атрибут/текст, угловые скобки не должны ломать разметку
        html = theatre.markdown_to_html(r"$a < b$")
        self.assertNotIn("<b>", html)


# ---------------------------------------------------------------- аватары

class TestAvatarNames(unittest.TestCase):

    def test_cyrillic_is_kept(self):
        self.assertEqual(theatre.sanitize_avatar_name("дипломат женщина"),
                         "дипломат_женщина")

    def test_yo_letter_is_kept(self):
        self.assertIn("ё", theatre.sanitize_avatar_name("актёр"))

    def test_service_characters_become_underscores(self):
        self.assertEqual(theatre.sanitize_avatar_name("кот/пёс: 2"),
                         "кот_пёс__2")

    def test_keywords_always_mention_gender(self):
        # Иначе поисковик отдаёт женщине «мужчину-геолога»
        for _ in range(15):
            for person in theatre.build_new_cast():
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
        available = {key for key, value in theatre.CHARACTER_PRESETS.items()
                     if key != "custom" and value.get("params")}
        drawn = {theatre.draw_character() for _ in range(500)}
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
        for key in theatre.CHARACTER_PRESETS:
            for _ in range(120):
                params, think = theatre.character_parameters(key)
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
        with mock.patch.object(theatre, "PARTICIPANTS", [template]):
            for _ in range(20):
                person = theatre.build_new_cast()[0]
                self.assertEqual(person["temperature"], 0.25)
                self.assertEqual(person["top_p"], 0.5)


# ---------------------------------------------------------------- состав

class TestCastStructure(unittest.TestCase):

    def test_names_and_emojis_are_unique(self):
        for _ in range(150):
            cast = theatre.build_new_cast()
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
                seq = getattr(theatre, name)
                repeated = [item for item, count in collections.Counter(seq).items()
                            if count > 1]
                self.assertEqual(repeated, [], f"повторы в {name}")

    def test_pools_are_wide_enough_for_the_whole_cast(self):
        """Имён и профессий должно хватать на всех, иначе участники получат
        одинаковые запасные «Участник»/«человек»."""
        size = len(theatre.PARTICIPANTS)
        for name in ("MALE_NAMES", "FEMALE_NAMES", "PROFESSIONS"):
            with self.subTest(list=name):
                self.assertGreaterEqual(len(set(getattr(theatre, name))), size)

    def test_duplicates_in_a_pool_do_not_leak_into_the_cast(self):
        """Даже если повтор в список вернут, спектакль не получит двух тёзок.

        Мужские и женские имена здесь разные: это две независимые колоды,
        и повтор внутри одной из них не должен выйти на сцену.
        """
        male = ["ПовторМ"] * 3 + [f"Мужское{i}" for i in range(8)]
        female = ["ПовторЖ"] * 3 + [f"Женское{i}" for i in range(8)]
        with mock.patch.object(theatre, "MALE_NAMES", male), \
                mock.patch.object(theatre, "FEMALE_NAMES", female):
            for _ in range(40):
                names = [p["display_name"] for p in theatre.build_new_cast()]
                self.assertEqual(len(names), len(set(names)), names)

    def test_gender_matches_emoji_pool(self):
        male = set(theatre.AVATAR_EMOJIS_MALE)
        female = set(theatre.AVATAR_EMOJIS_FEMALE)
        for _ in range(10):
            for person in theatre.build_new_cast():
                emoji = person["avatar_emoji"]
                if person["gender"] == "male" and emoji in female:
                    self.fail(f"мужчине достался женский эмодзи {emoji}")
                if person["gender"] == "female" and emoji in male:
                    self.fail(f"женщине достался мужской эмодзи {emoji}")

    def test_humans_have_no_generation_params(self):
        for person in theatre.build_new_cast():
            if person["model"] == "human":
                for key in theatre.PER_PARTICIPANT_OPTION_KEYS:
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
        self._patch(theatre, "check_models_available",
                    return_value={"ok": True, "missing": [], "error": None})
        self._patch(theatre, "unload_model")

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rename_gender_and_model(self):
        payload = cast_payload(self.session)
        payload[0]["display_name"] = "Новое Имя"
        payload[0]["gender"] = "female"
        payload[1]["model"] = "q9"
        self.assertEqual(theatre.apply_cast_patch(payload), "")

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
        self.assertEqual(theatre.apply_cast_patch(payload), "")
        cast = self.session.runtime_participants
        for index in human_indexes:
            self.assertEqual(cast[index]["model"], "human")

    def test_temperature_is_updated_and_can_be_cleared(self):
        payload = cast_payload(self.session)
        payload[0]["temperature"] = 1.25
        self.assertEqual(theatre.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[0]["temperature"], 1.25)

        payload = cast_payload(self.session)
        payload[0]["temperature"] = ""
        self.assertEqual(theatre.apply_cast_patch(payload), "")
        self.assertNotIn("temperature", self.session.runtime_participants[0],
                         "пустое поле значит «как в модели», параметр не отправляем")

    def test_wrong_participant_count_is_rejected(self):
        error = theatre.apply_cast_patch([{}])
        self.assertIn("участник", error.lower())

    def test_empty_name_is_rejected(self):
        payload = cast_payload(self.session)
        payload[0]["display_name"] = "   "
        self.assertTrue(theatre.apply_cast_patch(payload))

    def test_duplicate_names_are_rejected(self):
        payload = cast_payload(self.session)
        payload[0]["display_name"] = payload[1]["display_name"]
        self.assertIn("разными", theatre.apply_cast_patch(payload))

    def test_bad_numbers_are_rejected(self):
        cases = (("temperature", 3.5), ("temperature", -0.1), ("top_p", 1.5),
                 ("min_p", -0.2), ("repeat_penalty", -1), ("top_k", 0))

        for key, value in cases:
            with self.subTest(key=key):
                payload = cast_payload(self.session)
                payload[0][key] = value
                self.assertTrue(theatre.apply_cast_patch(payload), f"{key} принят зря")

    def test_cast_is_unchanged_when_a_later_participant_is_bad(self):
        """Запрос применяется целиком или отклоняется целиком."""
        payload = cast_payload(self.session)
        before = copy.deepcopy(self.session.runtime_participants)
        payload[0]["display_name"] = "Другое Имя"
        payload[-1]["temperature"] = 99
        self.assertTrue(theatre.apply_cast_patch(payload))
        self.assertEqual(self.session.runtime_participants[0]["display_name"],
                         before[0]["display_name"])

    def test_judge_keeps_its_instruction_after_rename(self):
        payload = cast_payload(self.session)
        judge_index = next(i for i, p in enumerate(self.session.runtime_participants)
                           if p.get("is_judge"))
        before = self.session.runtime_participants[judge_index]["instruction"]
        payload[judge_index]["display_name"] = "Судья Новый"
        self.assertEqual(theatre.apply_cast_patch(payload), "")
        self.assertEqual(self.session.runtime_participants[judge_index]["instruction"],
                         before)

    def test_published_posts_are_not_rewritten(self):
        """Прошлые реплики не меняются: имя меняется только у будущих."""
        self.session.add_post("Старое Имя", "r1", "текст", 1)
        payload = cast_payload(self.session)
        payload[0]["display_name"] = "Новое Имя"
        self.assertEqual(theatre.apply_cast_patch(payload), "")
        self.assertEqual(self.session.posts[0]["display_name"], "Старое Имя")


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
        self.assertIn(theatre.DEFAULT_JUDGE_INSTRUCTION.strip()[:40], prompt)


class TestSettingsPersistence(unittest.TestCase):
    """Правила судьи переживают перезапуск: лежат в файле рядом с проектом."""

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self._patch(theatre, "SETTINGS_FILE",
                    Path(self.tmpdir.name) / "settings.json")

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rules_survive_a_restart(self):
        self.session.judge_rules = ["Оценка только по фактам"]
        theatre.save_theatre_settings()

        self.session.judge_rules = ["временное"]
        theatre.load_theatre_settings()
        self.assertEqual(self.session.judge_rules, ["Оценка только по фактам"])

    def test_file_is_readable_json(self):
        self.session.judge_rules = ["Пункт 1", "Пункт 2"]
        theatre.save_theatre_settings()
        data = json.loads(theatre.SETTINGS_FILE.read_text(encoding="utf-8"))
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
            mock.patch.object(theatre, "resolve_model_name", side_effect=lambda m, a: m),
            mock.patch.object(theatre, "fetch_model_info", return_value=info),
            mock.patch.object(theatre, "vram_base_bytes",
                              return_value=(base_bytes, 0)),
        ]

    def test_kv_cache_grows_with_context(self):
        small = theatre.estimated_kv_bytes(self.INF, 4096)
        large = theatre.estimated_kv_bytes(self.INF, 8192)
        self.assertGreater(large, small)
        self.assertEqual(large, small * 2)

    def test_suggested_context_fits_the_budget(self):
        budget = 8 * 10 ** 9
        patchers = self._with_model(4 * 10 ** 9)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        safe, need = theatre.suggest_safe_ctx("m1", 16384, budget, {})
        self.assertIsNotNone(safe)
        self.assertLessEqual(need, budget)
        self.assertEqual(safe % theatre.VRAM_SAFE_CTX_STEP, 0)
        self.assertLessEqual(safe, 16384)

    def test_no_room_means_no_suggestion(self):
        patchers = self._with_model(8 * 10 ** 9)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        safe, need = theatre.suggest_safe_ctx("m1", 16384, 8 * 10 ** 9, {})
        self.assertIsNone(safe)
        self.assertEqual(need, 0)

    def test_unknown_model_is_handled(self):
        patchers = self._with_model(10 ** 9, info={})
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(theatre.suggest_safe_ctx("m1", 8192, 8 * 10 ** 9, {}),
                         (None, 0))


# ---------------------------------------------------------------- токены

class TestTokenHelpers(unittest.TestCase):

    def test_estimate_is_positive_for_a_word(self):
        self.assertGreaterEqual(theatre.estimate_tokens("привет"), 1)
        self.assertGreaterEqual(theatre.estimate_tokens("hello"), 1)
        self.assertGreaterEqual(theatre.estimate_tokens(""), 0)

    def test_estimate_grows_with_text(self):
        self.assertGreater(theatre.estimate_tokens("слово " * 50),
                           theatre.estimate_tokens("слово"))

    def test_history_is_trimmed_to_fit_context(self):
        messages = [{"role": "user", "content": "длинный текст " * 1200}
                    for _ in range(6)]
        trimmed = theatre.trim_history_by_tokens(messages, system_prompt_tokens=100)
        self.assertLess(len(trimmed), len(messages),
                        "история должна обрезаться под контекст")

    def test_short_history_is_kept_as_is(self):
        messages = [{"role": "user", "content": "ок"}]
        self.assertEqual(theatre.trim_history_by_tokens(messages, 10), messages)


# ---------------------------------------------------------------- посты

class TestPosts(unittest.TestCase):

    def setUp(self):
        self.session = make_session()
        strip_session_patch(self, self.session)

    def test_role_and_gender_are_rendered(self):
        judge = theatre.create_post("Судья", "q1", "текст", 1,
                                    role="judge", gender="female")
        self.assertEqual(judge["role_icon"], "⚖️")
        self.assertEqual(judge["role_name"], "Судья")
        self.assertEqual(judge["gender_symbol"], "♀")
        self.assertEqual(judge["gender"], "female")

    def test_content_html_is_built_from_content(self):
        post = theatre.create_post("Имя", "r1", r"Формула $x^2$ тут", 1)
        self.assertIn('<span class="math"', post["content_html"])
        self.assertIn("$x^2$", post["content"])

    def test_ids_do_not_repeat(self):
        self.session.posts = []
        first = theatre.create_post("А", "r1", "1", 1)
        self.session.posts.append(first)
        second = theatre.create_post("Б", "r1", "2", 1)
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
        self.page = theatre.HTML_TEMPLATE

    def test_every_role_produced_by_the_app_has_a_stripe(self):
        roles = {
            theatre.create_post("А", "r1", "текст", 1)["role"],
            theatre.create_post("Б", "r1", "текст", 1, role="moderator")["role"],
            theatre.create_post("В", "r1", "текст", 1, role="judge")["role"],
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
        self.page = theatre.HTML_TEMPLATE

    def test_pult_lists_the_same_parameters_as_the_server(self):
        match = self.PARAM_KEYS_RE.search(self.page)
        self.assertIsNotNone(match, "в пульте не нашёлся список PARAM_KEYS")
        listed = re.findall(r"'([^']+)'", match.group(1))
        self.assertEqual(listed, list(theatre.PER_PARTICIPANT_OPTION_KEYS),
                         "пульт и сервер разошлись в списке параметров генерации")

    def test_every_parameter_has_a_field_in_the_editor(self):
        missing = [key for key in theatre.PER_PARTICIPANT_OPTION_KEYS
                   if f"paramField('{key}'" not in self.page]
        self.assertEqual(missing, [], f"у этих параметров нет поля в пульте: {missing}")

    def test_editor_sends_parameters_from_the_one_list(self):
        """Свой список чисел внутри collectCast когда-то и обрезал пульт до пяти."""
        start = self.page.index("function collectCast()")
        body = self.page[start:self.page.index("function saveCast()", start)]
        self.assertIn("PARAM_KEYS.forEach", body)
        self.assertNotIn("'temperature'", body,
                         "список чисел в пульте должен быть один — PARAM_KEYS")


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
                    for match in self.EXTERNAL_RESOURCE_RE.finditer(theatre.HTML_TEMPLATE)]
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
            patcher = mock.patch.object(theatre, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = theatre.app.test_client()

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
        with mock.patch.object(theatre, "save_theatre_settings") as save:
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
                                json={"participants": [{}]}).get_json()
        self.assertFalse(data["success"])
        self.assertIn("error", data)

    def test_participants_post_accepts_a_valid_cast(self):
        payload = {"participants": cast_payload(self.session)}
        payload["participants"][0]["display_name"] = "Проверка Имени"
        data = self.client.post("/api/participants", json=payload).get_json()
        self.assertTrue(data["success"], data.get("error"))

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
        message = theatre.models_problem_message(
            {"ok": False, "missing": ["r1", "g1"], "error": None})
        self.assertIn("r1", message)
        self.assertIn("ollama pull r1", message)

    def test_unreachable_ollama_is_explained(self):
        message = theatre.models_problem_message(
            {"ok": False, "missing": ["r1"], "error": "нет связи"})
        self.assertIn("нет связи", message)
        self.assertIn("Ollama", message)

    def test_no_problem_means_no_message(self):
        self.assertEqual(theatre.models_problem_message(
            {"ok": True, "missing": [], "error": None}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
