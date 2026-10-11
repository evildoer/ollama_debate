"""Приложенные картинки: какого они вида и размера и какими уедут модели.

Здесь нет ни ленты, ни спектакля — только байты картинки и правила, по которым
они уезжают модели. Правила выведены из живого отказа: вендор не берёт картинку,
у которой сторона меньше десятка пикселей («The image length and width do not
meet the model restrictions» — так он ответил на проверочный пиксель 1x1, см.
cloud.PROBE_PICTURE), а картинку в тысячи пикселей и мегабайты веса модель
получает целиком в каждом запросе хода — и это оплачивается входными токенами.

Поэтому приложенное приводится к обычному размеру на отправке (см. prepare),
а не при приёме: файл на диске и в ДАМПе остаётся таким, каким его принёс
режиссёр. Приведение — своими руками, без сторонних библиотек: png разбирается
и собирается здесь (тем же приёмом, что и проверочная картинка, см.
cloud._solid_picture). jpeg, webp и gif своими руками не разобрать — их
приводит Pillow, если он рядом случился (см. deps); без него такая картинка
уезжает как есть, но не молча: молча отправить картинку, которую модель
не примет, — худший из возможных ответов (см. _foreign_note).
"""

import base64
import io
import struct
import zlib

from . import deps
from . import settings


# ============================================================
# ВЕС ФАЙЛА СЛОВАМИ
# ============================================================

# Единицы, по которым вес читается человеком: мельче килобайта — байты, дальше
# Кб, Мб, Гб
_BYTE_UNITS = ((1024 ** 3, "Гб"), (1024 ** 2, "Мб"), (1024, "Кб"))


def human_bytes(size) -> str:
    """Вес файла по значащей единице: «761 Б», «12 Кб», «5,3 Мб».

    Байты — целыми, крупное — с одной цифрой после запятой и только тогда,
    когда она что-то значит: «2 Кб» вместо «2,0 Кб». Запятая, а не точка:
    текст этот русский. А целые килобайты вниз не годятся: приложенный файл
    в 761 байт показывался как «0 Кб» — то есть «ничего не приложено»
    (см. show.save_upload).
    """
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "0 Б"
    size = max(0, size)
    for step, unit in _BYTE_UNITS:
        if size >= step:
            text = f"{size / step:.1f}".replace(".", ",")
            if text.endswith(",0"):
                text = text[:-2]
            return f"{text} {unit}"
    return f"{size} Б"


# ============================================================
# ВИД И СТОРОНЫ КАРТИНКИ — ПО ЕЁ СОДЕРЖИМОМУ
# ============================================================

_PNG_SIGN = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGN = b"\xff\xd8\xff"
_GIF_SIGNS = (b"GIF87a", b"GIF89a")


def picture_kind(raw: bytes) -> str:
    """Что это за картинка на самом деле — по её первым байтам.

    Пустая строка — не картинка (или такая, которой мы не знаем). Вид берётся
    из содержимого, а не из имени файла: браузер называет тип по расширению,
    и «photo.txt», переименованный в .png, выдавал бы себя за картинку (см.
    show.image_kind).
    """
    if raw.startswith(_PNG_SIGN):
        return "image/png"
    if raw.startswith(_JPEG_SIGN):
        return "image/jpeg"
    if raw[:6] in _GIF_SIGNS:
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def picture_marks(raw: bytes) -> dict:
    """Вид картинки и её стороны — по заголовку, без разбора пикселей.

    Стороны нужны затем, чтобы понять, станет ли размер проблемой (см.
    target_size), а разбирать ради них всю картинку незачем: у каждого вида
    они лежат в своём месте. Не разобравшийся заголовок — не отказ: стороны
    остаются нулями, и тогда решает тот, кто картинку умеет разбирать
    (см. prepare).
    """
    kind = picture_kind(raw)
    size = (0, 0)
    if kind == "image/png":
        size = _png_size(raw)
    elif kind == "image/jpeg":
        size = _jpeg_size(raw)
    elif kind == "image/gif":
        size = _gif_size(raw)
    elif kind == "image/webp":
        size = _webp_size(raw)
    return {"kind": kind, "width": size[0], "height": size[1]}


def _png_size(raw: bytes) -> tuple:
    """Стороны png: они лежат в первом же куске IHDR, сразу за подписью файла."""
    if len(raw) < 24 or raw[12:16] != b"IHDR":
        return (0, 0)
    return struct.unpack(">II", raw[16:24])


# Маркеры начала кадра у jpeg (SOF0…SOF15) — кроме тех, что таблицами и прочим:
# стороны картинки лежат только в них
_JPEG_FRAME_MARKS = frozenset((0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF))


def _jpeg_size(raw: bytes) -> tuple:
    """Стороны jpeg: они лежат в первом маркере кадра, а не в начале файла.

    Идти по маркерам приходится потому, что до кадра в файле лежат вставки
    произвольной длины (EXIF, цветовой профиль, комментарий): пропустить их
    можно только по объявленной длине сегмента.
    """
    position, end = 2, len(raw)
    while position + 3 < end:
        if raw[position] != 0xFF:
            position += 1
            continue
        mark = raw[position + 1]
        if mark in (0xFF, 0x01) or 0xD0 <= mark <= 0xD9:
            # Заполнитель или маркер без длины: своего размера у него нет
            position += 2
            continue
        length = struct.unpack(">H", raw[position + 2:position + 4])[0]
        if length < 2:
            return (0, 0)
        if mark in _JPEG_FRAME_MARKS and position + 9 <= end:
            height, width = struct.unpack(">HH", raw[position + 5:position + 9])
            return (width, height)
        position += 2 + length
    return (0, 0)


def _gif_size(raw: bytes) -> tuple:
    """Стороны gif: они стоят в заголовке сразу за подписью, младшими байтами вперёд."""
    if len(raw) < 10:
        return (0, 0)
    return struct.unpack("<HH", raw[6:10])


def _webp_size(raw: bytes) -> tuple:
    """Стороны webp: у трёх его видов они лежат по-разному (VP8, VP8L, VP8X).

    Без этой малости «приложенное мельче обычного» осталось бы незамеченным
    ровно у webp — а сказать об этом надо: своей рукой такую картинку не
    увеличить (см. _foreign_note).
    """
    if len(raw) < 30:
        return (0, 0)
    kind = raw[12:16]
    if kind == b"VP8X":
        return (1 + int.from_bytes(raw[24:27], "little"),
                1 + int.from_bytes(raw[27:30], "little"))
    if kind == b"VP8L" and raw[20] == 0x2F:
        packed = int.from_bytes(raw[21:25], "little")
        return ((packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1)
    if kind == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
        # Ключевой кадр: за подписью кадра два слова по 16 бит, старшие два бита
        # в них — не размер, а признаки масштаба
        return (struct.unpack("<H", raw[26:28])[0] & 0x3FFF,
                struct.unpack("<H", raw[28:30])[0] & 0x3FFF)
    return (0, 0)


# ============================================================
# PNG СВОИМИ РУКАМИ: РАЗОБРАТЬ, ПЕРЕСОБРАТЬ, ИЗМЕНИТЬ РАЗМЕР
# ============================================================

class Unreadable(Exception):
    """Картинка, которую своими руками не разобрать: глубина, вид или оборванный кусок."""


def _png_chunks(raw: bytes):
    """Куски png по порядку: пары (вид, тело). Оборванный файл — Unreadable."""
    position, end = 8, len(raw)
    while position + 12 <= end:
        length, kind = struct.unpack(">I4s", raw[position:position + 8])
        body = raw[position + 8:position + 8 + length]
        if len(body) != length:
            raise Unreadable("кусок png оборван")
        yield kind, body
        position += 12 + length


def decode_png(raw: bytes) -> dict:
    """Разобрать png в строки пикселей: {"width", "height", "channels", "rows"}.

    Разбирается только то, что встречается в приложенных картинках: 8 бит
    на канал и не черезстрочная запись. Всё прочее (16 бит, редкие виды,
    черезстрочный png) — Unreadable: такая картинка уедет как есть, и консоль
    скажет, почему (см. prepare).
    """
    width = height = depth = color = interlace = None
    palette, transparency, pixels = None, None, bytearray()
    for kind, body in _png_chunks(raw):
        if kind == b"IHDR":
            # Ширина, высота, глубина, вид, сжатие, фильтры, чересстрочность:
            # сжатие и фильтры в png всегда одни и те же, поэтому и не читаются
            width, height, depth, color, _packing, _filters, interlace = \
                struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            pixels += body
        elif kind == b"IEND":
            break

    if width is None:
        raise Unreadable("в png нет заголовка")
    if depth != 8 or interlace != 0:
        raise Unreadable("глубина цвета или черезстрочная запись не разбираются")
    # Сколько байтов занимает один пиксель в записи этого вида
    bytes_per_pixel = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if not bytes_per_pixel or not width or not height:
        raise Unreadable("неизвестный вид png")

    # Прозрачность есть и у видов без отдельного канала прозрачности: у серого
    # и палитры её задаёт кусок tRNS
    channels = 4 if (color in (4, 6) or transparency) else 3
    rows = [_row_channels(line, color, palette, transparency, channels)
            for line in _unfilter(zlib.decompress(bytes(pixels)), height,
                                  width * bytes_per_pixel, bytes_per_pixel)]
    return {"width": width, "height": height, "channels": channels, "rows": rows}


def _unfilter(data: bytes, height: int, stride: int, bytes_per_pixel: int) -> list:
    """Снять построчные фильтры png: их пять, и вид фильтра стоит в начале строки.

    Фильтр нужен не нам, а сжатию: соседние пиксели похожи, и zlib сжимает
    разности лучше, чем сами значения. Сняв их, получаем пиксели как есть.
    """
    rows, previous, position = [], bytearray(stride), 0
    for _ in range(height):
        if position + stride + 1 > len(data):
            raise Unreadable("данных png не хватает на строку")
        method = data[position]
        line = bytearray(data[position + 1:position + 1 + stride])
        position += stride + 1
        if method == 1:                       # предыдущий пиксель в строке
            for i in range(bytes_per_pixel, stride):
                line[i] = (line[i] + line[i - bytes_per_pixel]) & 0xFF
        elif method == 2:                     # пиксель строкой выше
            # Считается в C, а не по байту: этот фильтр чаще всего и стоит
            # у больших картинок, и на снимке экрана в 4К разница в разы
            line = bytearray((a + b) & 0xFF for a, b in zip(line, previous))
        elif method == 3:                     # среднее двух предыдущих
            for i in range(stride):
                left = line[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif method == 4:                     # предсказание по трём соседям
            for i in range(stride):
                left = line[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                above_left = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                above = previous[i]
                guess = left + above - above_left
                base = (abs(guess - left), abs(guess - above), abs(guess - above_left))
                best = (left, above, above_left)[base.index(min(base))]
                line[i] = (line[i] + best) & 0xFF
        elif method != 0:
            raise Unreadable("неизвестный фильтр строки png")
        rows.append(line)
        previous = line
    return rows


def _row_channels(line: bytearray, color: int, palette, transparency, channels: int) -> bytearray:
    """Строка png — в строку каналов: RGB или RGBA, как решил decode_png.

    Прозрачность для модели не важна, но и терять её незачем: цвет картинки
    она читает по каналам, и лишний канал ничего не стоит (в отличие от
    однажды перепутанного вида строки).
    """
    out = bytearray()
    if color == 0:                            # оттенки серого
        clear = transparency[1] if transparency and len(transparency) > 1 else None
        for value in line:
            out += bytes((value, value, value))
            if channels == 4:
                out.append(0 if clear is not None and value == clear else 255)
    elif color == 2:                          # rgb
        if channels == 3:
            return bytearray(line)
        for i in range(0, len(line), 3):
            out += line[i:i + 3] + b"\xff"
    elif color == 3:                          # палитра
        for index in line:
            point = index * 3
            out += palette[point:point + 3] if palette else b"\x00\x00\x00"
            if channels == 4:
                out.append(transparency[index] if transparency and index < len(transparency)
                           else 255)
    elif color == 4:                          # серый и прозрачность
        for i in range(0, len(line), 2):
            out += bytes((line[i], line[i], line[i], line[i + 1]))
    else:                                     # rgba
        return bytearray(line)
    return out


def encode_png(width: int, height: int, rows: list, channels: int) -> bytes:
    """Собрать png из строк пикселей — тем же приёмом, что и проверочную картинку.

    Фильтры не применяются (строка начинается нулём — так записано «без
    фильтра»): картинка здесь уже уменьшена, и её вес решает размер, а не то,
    чем сжата строка.
    """
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    color = 2 if channels == 3 else 6
    body = b"".join(b"\x00" + bytes(row) for row in rows)
    return (_PNG_SIGN
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(body, 6))
            + chunk(b"IEND", b""))


# ============================================================
# ЧТО СЧИТАТЬ ОБЫЧНЫМ РАЗМЕРОМ И КАК К НЕМУ ПРИЙТИ
# ============================================================

# С какого размера картинки разбор и приведение становятся долгими: об этом
# стоит предупредить заранее (см. prepare), чтобы пауза в ходу не выглядела
# зависанием
_SLOW_PIXELS = 1000 * 1000


def target_size(width: int, height: int) -> tuple:
    """Каким сторонам быть, чтобы модель картинку приняла.

    Мельче обычного — увеличиваем, крупнее — уменьшаем, обычную не трогаем:
    пересобирать картинку, которую модель и так примет, не за что.
    """
    if width < 1 or height < 1:
        return (width, height)
    smaller, bigger = min(width, height), max(width, height)
    factor = 1.0
    if smaller < settings.PICTURE_MIN_SIDE:
        factor = settings.PICTURE_MIN_SIDE / smaller
    elif bigger > settings.PICTURE_MAX_SIDE:
        factor = settings.PICTURE_MAX_SIDE / bigger
    if factor == 1.0:
        return (width, height)
    return (max(1, round(width * factor)), max(1, round(height * factor)))


def _resample(decoded: dict, width: int, height: int) -> list:
    """Строки пикселей нужного размера.

    Уменьшение — усреднением по клетке (иначе мелкий текст со снимка экрана
    рассыпается в рябь), увеличение — ближайшим пикселем: у мелкой картинки
    промежуточных цветов нет, и выдумывать их нечего.
    """
    source_width, source_height = decoded["width"], decoded["height"]
    rows, channels = decoded["rows"], decoded["channels"]
    if (width, height) == (source_width, source_height):
        return rows
    if width * height >= source_width * source_height:
        return _nearest(rows, source_width, source_height, width, height, channels)
    return _box(rows, source_width, source_height, width, height, channels)


def _nearest(rows: list, source_width: int, source_height: int,
             width: int, height: int, channels: int) -> list:
    """Увеличение: каждый пиксель берётся у ближайшего к нему исходного."""
    out = []
    for y in range(height):
        line = rows[min(source_height - 1, y * source_height // height)]
        picked = bytearray()
        for x in range(width):
            at = min(source_width - 1, x * source_width // width) * channels
            picked += line[at:at + channels]
        out.append(picked)
    return out


def _box(rows: list, source_width: int, source_height: int,
         width: int, height: int, channels: int) -> list:
    """Уменьшение усреднением: клетка нового размера — среднее своих пикселей.

    Проход в два шага (сперва по строкам, потом по столбцам), а не клетка
    за клеткой: так каждый исходный пиксель читается один раз, а не столько
    раз, сколько клеток его накрыло.
    """
    narrowed = []
    for line in rows:
        picked = bytearray()
        for x in range(width):
            start = x * source_width // width
            stop = max(start + 1, (x + 1) * source_width // width)
            for channel in range(channels):
                values = line[start * channels + channel:stop * channels:channels]
                picked.append(sum(values) // len(values))
        narrowed.append(picked)

    out = []
    for y in range(height):
        start = y * source_height // height
        stop = max(start + 1, (y + 1) * source_height // height)
        totals = [0] * len(narrowed[start])
        for row in narrowed[start:stop]:
            for i, value in enumerate(row):
                totals[i] += value
        count = stop - start
        out.append(bytearray(value // count for value in totals))
    return out


def prepare(data_url: str) -> tuple:
    """Приложенная картинка — в том виде, какой модель точно примет.

    Возвращает пару: адрес данных (тот же самый, если менять было нечего) и
    слова о том, что сделано, — их говорит консоль. Вмешаться есть три причины:
    сторона мельче обычной (такую вендор не берёт вовсе), сторона крупнее
    обычной и вес больше обычного (такая уезжает модели в каждом запросе хода
    и оплачивается входными токенами, см. settings.PICTURE_MAX_BYTES).

    Файл на диске при этом не меняется: уменьшенное едет модели, а приложенный
    режиссёром оригинал остаётся в ленте и в ДАМПе. Что разобрать не вышло —
    уезжает как есть, и об этом сказано словами (см. _foreign_note).
    """
    text = str(data_url or "")
    _head, marker, body = text.partition(";base64,")
    if not marker:
        return data_url, ""
    try:
        raw = base64.b64decode(body, validate=True)
    except Exception:
        return data_url, ""

    marks = picture_marks(raw)
    kind, width, height = marks["kind"], marks["width"], marks["height"]
    if not kind:
        return data_url, ""
    target = target_size(width, height)
    heavy = len(raw) > settings.PICTURE_MAX_BYTES

    # Не png: своими руками такое не разобрать — нужен Pillow (см. deps)
    if kind != "image/png":
        if deps.PILLOW_AVAILABLE and (target != (width, height) or heavy):
            done = _pillow_ready(raw, kind, target)
            if done is not None:
                return done
        return data_url, _foreign_note(kind, width, height, target, heavy)

    try:
        decoded = decode_png(raw)
    except (Unreadable, zlib.error, struct.error, ValueError, IndexError) as e:
        return data_url, (f"приложенный png не разобрать своими руками ({e}) — "
                          f"уедет как есть, {human_bytes(len(raw))}")

    width, height = decoded["width"], decoded["height"]
    target = target_size(width, height)
    if target == (width, height) and not heavy:
        # Обычная картинка: не трогаем её вовсе — ни пикселя лишнего
        return data_url, ""

    # Своими руками это долго: картинку в тысячи пикселей приходится разбирать
    # байт за байтом. Сказать об этом заранее — иначе пауза в ходу выглядит
    # зависанием, а не работой (см. pictures.decode_png и _resample)
    if width * height > _SLOW_PIXELS:
        print(f"  🖼  Привожу приложенное к обычному размеру: {width}×{height}, "
              f"{human_bytes(len(raw))} — на такой картинке это займёт несколько секунд")

    # Тяжёлую картинку облегчаем, пока она не войдёт в обычный вес: она уезжает
    # в каждый запрос хода, и платится это входными токенами
    packed = b""
    while True:
        rows = _resample(decoded, target[0], target[1])
        packed = encode_png(target[0], target[1], rows, decoded["channels"])
        if (len(packed) <= settings.PICTURE_MAX_BYTES
                or min(target) <= settings.PICTURE_MIN_SIDE):
            break
        target = (max(settings.PICTURE_MIN_SIDE, target[0] * 3 // 4),
                  max(settings.PICTURE_MIN_SIDE, target[1] * 3 // 4))

    if target == (width, height) and len(packed) >= len(raw):
        # Пересобрали в тот же размер и стало не легче: прежнее лучше
        return data_url, ""
    ready = "data:image/png;base64," + base64.b64encode(packed).decode("ascii")
    return ready, (f"{width}×{height} → {target[0]}×{target[1]}, "
                   f"{human_bytes(len(raw))} → {human_bytes(len(packed))}")


def _foreign_note(kind: str, width: int, height: int,
                  target: tuple, heavy: bool) -> str:
    """Слова о картинке, которую своими руками не поправить.

    Она уезжает как есть — но не молча: если её размер станет проблемой, узнать
    об этом лучше здесь, а не отказом посреди хода. Молчание оставляем только
    там, где проблемы нет (см. prepare).
    """
    what = kind.split("/")[-1]
    size = f"{width}×{height}" if width else "размер не прочитан"
    if width and min(width, height) < settings.PICTURE_MIN_SIDE:
        return (f"{what} {size} мельче обычного — вендор такую не примет, "
                f"а увеличить её нечем: нужен Pillow (см. requirements.txt)")
    if heavy:
        return (f"{what} {size} тяжелее обычного — уменьшить её нечем, и она "
                f"будет уезжать в каждый запрос хода: нужен Pillow")
    if target != (width, height):
        return (f"{what} {size} крупнее обычного — уменьшить её нечем: нужен Pillow")
    return ""


# Какой вид картинки чем сохранять, если рядом случился Pillow
_PILLOW_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG",
                   "image/webp": "WEBP", "image/gif": "GIF"}
_PILLOW_SAVING = {"image/jpeg": {"quality": 85, "optimize": True},
                  "image/webp": {"quality": 85},
                  "image/png": {"optimize": True}}


def _pillow_ready(raw: bytes, kind: str, target: tuple):
    """Привести картинку к обычному размеру средствами Pillow (или None).

    Pillow — необязательная библиотека (см. deps): он один читает jpeg, webp
    и gif. Вид картинки сохраняется: менять вендору формат на ходу незачем.
    Качество — обычное (jpeg 85): картинка едет модели, а не в печать, и её вес
    — это входные токены каждого хода.
    """
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        frame = getattr(image, "n_frames", 1)
        wanted = target_size(*image.size)
        steps = 0
        while True:
            ready = image.resize(wanted, Image.LANCZOS) if wanted != image.size else image
            saving = ready
            if _PILLOW_FORMATS.get(kind) == "JPEG" and saving.mode not in ("RGB", "L"):
                saving = saving.convert("RGB")
            buffer = io.BytesIO()
            saving.save(buffer, format=_PILLOW_FORMATS.get(kind, "PNG"),
                        **(_PILLOW_SAVING.get(kind) or {}))
            packed = buffer.getvalue()
            if (len(packed) <= settings.PICTURE_MAX_BYTES
                    or min(wanted) <= settings.PICTURE_MIN_SIDE or steps >= 4):
                break
            wanted = (max(settings.PICTURE_MIN_SIDE, wanted[0] * 3 // 4),
                      max(settings.PICTURE_MIN_SIDE, wanted[1] * 3 // 4))
            steps += 1
    except Exception as e:
        print(f"  ⚠️  Картинку не удалось привести к обычному размеру ({e})")
        return None

    note = (f"{image.size[0]}×{image.size[1]} → {wanted[0]}×{wanted[1]}, "
            f"{human_bytes(len(raw))} → {human_bytes(len(packed))}")
    if kind == "image/gif" and frame > 1:
        # Модель читает первый кадр: и вендор, и Ollama берут картинку целиком,
        # а не картинку с движением
        note += f" (кадров было {frame} — уехал первый)"
    return "data:%s;base64,%s" % (kind, base64.b64encode(packed).decode("ascii")), note
