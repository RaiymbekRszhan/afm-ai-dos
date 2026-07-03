# -*- coding: utf-8 -*-
"""Ai-dos: связка Unreal-редактора с бэкендом на Маке.

Запускается ВНУТРИ Unreal Editor (Window -> Output Log -> вкладка Python,
или Tools -> Execute Python Script). Делает за один вызов:

    вопрос (WAV или текст) -> бэкенд Мака (/voice или /chat+/speak)
    -> answer.wav -> импорт в Content Browser -> MetaHuman Performance
    -> Process -> Export Level Sequence

Классы MetaHuman-плагина различаются между версиями UE — если что-то не
находится, запусти dump_api() и пришли вывод: подгоним имена под 5.7.

Примеры (в Python-консоли UE):
    import aidos_editor as a
    a.dump_api()                              # 1) сверить API MetaHuman
    a.ask_text("Что такое финансовый мониторинг?", "russian")
    a.ask_voice(r"C:/temp/question.wav", "russian")
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------- конфиг ----------------

BACKEND = os.environ.get("AIDOS_BACKEND", "http://192.168.23.120:8000")
TIMEOUT = 420            # TTS на CPU Мака ~2-4 мин на ответ
CONTENT_DIR = "/Game/Aidos/Answers"      # куда импортировать SoundWave
SAVE_DIR = None          # None = <Project>/Saved/Aidos (заполняется ниже)

try:
    import unreal
    SAVE_DIR = os.path.join(unreal.SystemLibrary.get_project_saved_directory(), "Aidos")
except ImportError:      # позволяет тестировать HTTP-часть вне Unreal
    unreal = None
    SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_answers")


# ---------------- HTTP к бэкенду (без сторонних библиотек) ----------------

def _multipart(fields, file_field, filename, payload, ctype="audio/wav"):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode("utf-8")
    body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
             "Content-Type: %s\r\n\r\n" % (boundary, file_field, filename, ctype)).encode("utf-8")
    body += payload + b"\r\n" + ("--%s--\r\n" % boundary).encode("utf-8")
    return body, "multipart/form-data; boundary=" + boundary


def _save_wav(audio_bytes, tag):
    os.makedirs(SAVE_DIR, exist_ok=True)
    path = os.path.join(SAVE_DIR, "answer_%s_%s.wav" % (tag, time.strftime("%H%M%S")))
    with open(path, "wb") as f:
        f.write(audio_bytes)
    return path


def backend_voice(wav_path, language="russian"):
    """Полный цикл на бэкенде: вопрос-WAV -> STT -> RAG -> TTS. Возвращает
    (путь к answer.wav, текст вопроса, текст ответа)."""
    with open(wav_path, "rb") as f:
        payload = f.read()
    body, ctype = _multipart({"language": language}, "data", "question.wav", payload)
    req = urllib.request.Request(BACKEND + "/voice", data=body,
                                 headers={"Content-Type": ctype})
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    question = urllib.parse.unquote(resp.headers.get("X-Question", ""))
    answer = urllib.parse.unquote(resp.headers.get("X-Answer", ""))
    path = _save_wav(resp.read(), language[:2])
    print("[aidos] Вопрос: %s\n[aidos] Ответ: %s\n[aidos] WAV: %s" % (question, answer, path))
    return path, question, answer


def backend_chat_speak(text, language="russian"):
    """Текстовый вопрос: /chat (RAG) -> /speak (TTS). Возвращает
    (путь к answer.wav, текст ответа)."""
    req = urllib.request.Request(
        BACKEND + "/chat", method="POST",
        data=json.dumps({"question": text, "language": language}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    answer = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))["answer"]
    print("[aidos] Ответ RAG: %s" % answer)

    req = urllib.request.Request(
        BACKEND + "/speak", method="POST",
        data=json.dumps({"text": answer, "language": language}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    path = _save_wav(urllib.request.urlopen(req, timeout=TIMEOUT).read(), language[:2])
    print("[aidos] WAV: %s" % path)
    return path, answer


# ---------------- Unreal: импорт WAV + MetaHuman Performance ----------------

def _check_not_playing():
    """Editor-скриптинг не работает во время Play — останавливаем с подсказкой."""
    try:
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        playing = bool(les and les.is_in_play_in_editor())
    except Exception:
        return
    if playing:
        raise RuntimeError("Редактор в режиме Play — останови (Esc/кнопка Stop) и повтори")


def import_wav(path):
    """Импортирует WAV в Content Browser, возвращает ассет SoundWave."""
    task = unreal.AssetImportTask()
    task.filename = path
    task.destination_path = CONTENT_DIR
    task.automated = True
    task.save = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    imported = list(task.get_editor_property("imported_object_paths") or [])
    asset_path = imported[0] if imported else \
        "%s/%s" % (CONTENT_DIR, os.path.splitext(os.path.basename(path))[0])
    sound = unreal.load_asset(asset_path)
    if not sound:
        raise RuntimeError("WAV импортирован, но ассет не загрузился: %s — если "
                           "редактор в Play-режиме, останови и запусти resume()" % asset_path)
    print("[aidos] SoundWave: %s" % asset_path)
    return sound


def resume(asset_path):
    """Продолжить с уже импортированного SoundWave (после падения/Play-режима).
    Пример: a.resume('/Game/Aidos/Answers/answer_ru_130015')"""
    _check_not_playing()
    sound = unreal.load_asset(asset_path)
    if not sound:
        raise RuntimeError("Не нашёл ассет %s" % asset_path)
    return export_sequence(process_performance(sound))


def _set_prop(obj, candidates, value):
    """Ставит editor property, пробуя имена по очереди (имена свойств могли
    смениться в 5.7). При провале печатает docstring класса — там список
    всех Editor Properties."""
    for name in candidates:
        try:
            obj.set_editor_property(name, value)
            return name
        except Exception:
            continue
    print("=== Editor Properties класса %s: ===" % type(obj).__name__)
    print(type(obj).__doc__)
    raise RuntimeError("не нашёл свойство из %s — пришли вывод выше" % (candidates,))


def process_performance(sound_wave):
    """WAV -> MetaHuman Performance (Input=Audio) -> Process (блокирующий).

    API сверен с дампом UE 5.7.4 (2026-07-02): MetaHumanPerformance,
    MetaHumanPerformanceFactoryNew, DataInputType.AUDIO, set_blocking_processing,
    start_pipeline — всё есть.
    """
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    name = "Perf_" + sound_wave.get_name()
    perf = tools.create_asset(name, CONTENT_DIR,
                              unreal.MetaHumanPerformance,
                              unreal.MetaHumanPerformanceFactoryNew())
    _set_prop(perf, ["input_type", "data_input_type"], unreal.DataInputType.AUDIO)
    _set_prop(perf, ["audio", "audio_asset", "sound_wave"], sound_wave)

    perf.set_blocking_processing(True)      # ждём завершения прямо в вызове
    if not perf.can_process():
        raise RuntimeError("Performance не готов к обработке (can_process=False) "
                           "— проверь, что WAV импортировался и назначен")
    print("[aidos] Process пошёл (редактор подвиснет на ~минуту — это норма)...")
    perf.start_pipeline()
    print("[aidos] Готово: %d кадров анимации" % perf.get_number_of_processed_frames())
    return perf


def _soft_prop(obj, candidates, value):
    """Как _set_prop, но не роняет пайплайн: warning вместо исключения."""
    for name in candidates:
        try:
            obj.set_editor_property(name, value)
            return name
        except Exception:
            continue
    print("[aidos] ⚠ у %s нет свойства из %s — запусти a.dump_export_settings() "
          "и пришли вывод" % (type(obj).__name__, candidates))
    return None


def export_sequence(perf, auto=True):
    """Экспорт Level Sequence из Performance. auto=True — без диалогов:
    сохранение в CONTENT_DIR, цель = класс эталонного актора (v03), звук+камера."""
    settings = unreal.MetaHumanPerformanceExportUtils.get_export_level_sequence_settings(perf)
    if auto:
        name = "LS_" + perf.get_name()
        dst = "%s/%s" % (CONTENT_DIR, name)
        if unreal.EditorAssetLibrary.does_asset_exist(dst):
            unreal.EditorAssetLibrary.delete_asset(dst)
        # имена свойств сверены с дампом UE 5.7.4 (2026-07-02)
        _soft_prop(settings, ["show_export_dialog"], False)
        _soft_prop(settings, ["package_path"], CONTENT_DIR)
        _soft_prop(settings, ["asset_name"], name)
        _soft_prop(settings, ["export_audio_track"], True)
        # камеру НЕ экспортируем: кадр в стриме держит вьюпорт редактора,
        # секвенция не должна перехватывать ракурс
        _soft_prop(settings, ["export_camera"], False)
        _soft_prop(settings, ["export_video_track"], False)    # у аудио-перформанса видео нет
        _soft_prop(settings, ["export_depth_track"], False)
        # НЕ запекать rigid transform: он приносил кривую позицию (46, 5.4, 87.9°)
        _soft_prop(settings, ["export_transform_track"], False)
        _soft_prop(settings, ["enable_meta_human_head_movement"], True)
        ref = _level_actor(REFERENCE_ACTOR)
        if ref:
            # свойство ждёт Blueprint-АССЕТ, не класс: /Game/.../BP_x.BP_x_C -> /Game/.../BP_x
            bp_path = ref.get_class().get_path_name().rsplit(".", 1)[0]
            bp = unreal.load_asset(bp_path)
            if bp:
                _soft_prop(settings, ["target_meta_human_class"], bp)
                print("[aidos] целевой MetaHuman: %s" % bp_path)
            else:
                print("[aidos] ⚠ не смог загрузить Blueprint %s" % bp_path)
        else:
            print("[aidos] ⚠ эталон '%s' не найден — целевой класс не задан" % REFERENCE_ACTOR)
    else:
        _set_prop(settings, ["show_export_dialog"], True)
    seq = unreal.MetaHumanPerformanceExportUtils.export_level_sequence(perf, settings)
    print("[aidos] Level Sequence: %s" % seq)
    return seq


def dump_export_settings(perf=None):
    """Печатает все свойства настроек экспорта — если _soft_prop ругался."""
    cls = unreal.MetaHumanPerformanceExportLevelSequenceSettings
    print(cls.__doc__)


# ---------------- сценарии «одной кнопкой» ----------------

def ask_voice(question_wav, language="russian"):
    """Вопрос-WAV с диска -> бэкенд -> импорт ответа -> Performance -> Sequence."""
    if unreal:
        _check_not_playing()
    path, _q, _a = backend_voice(question_wav, language)
    if unreal:
        export_sequence(process_performance(import_wav(path)))
    return path


def ask_text(question, language="russian"):
    """Текстовый вопрос -> бэкенд -> импорт ответа -> Performance -> Sequence."""
    if unreal:
        _check_not_playing()
    path, _a = backend_chat_speak(question, language)
    if unreal:
        export_sequence(process_performance(import_wav(path)))
    return path


# ---------------- сборка секвенции по шаблону прошлого разработчика ----------------
# Рабочий рецепт (проверен 2026-07-02): дубликат готовой секвенции + подмена
# клипа Face->Анимация и звука. Привязка к BP_AFM_agent_v02 и камера приезжают
# из шаблона сами. ВАЖНО: в шаблоне трек «Анимация» должен быть НЕ заглушён
# (без значка ⊘), Control Rig-треки лица скрипт вычищает сам.

# Золотой шаблон по умолчанию. Если переименуешь/переложишь секвенцию — поправь
# здесь (или на сессию: a.TEMPLATE_SEQUENCE = "/Game/...").
TEMPLATE_SEQUENCE = "/Game/LevelSequences/TPL_Aidos_v03"
FPS = 30


def export_anim(perf):
    """Экспорт лицевой Anim Sequence из обработанного Performance."""
    settings = unreal.MetaHumanPerformanceExportUtils.get_export_animation_sequence_settings(perf)
    try:
        settings.set_editor_property("show_export_dialog", False)
    except Exception:
        pass
    anim = unreal.MetaHumanPerformanceExportUtils.export_animation_sequence(perf, settings)
    print("[aidos] AnimSequence: %s" % anim.get_path_name())
    return anim


def build_from_template(anim, sound, name=None):
    """Дубликат шаблона + подмена анимации лица и звука. Возвращает секвенцию."""
    if not unreal.EditorAssetLibrary.does_asset_exist(TEMPLATE_SEQUENCE):
        raise RuntimeError("Шаблон не найден: %s — проверь путь (Copy Reference) и "
                           "задай a.TEMPLATE_SEQUENCE" % TEMPLATE_SEQUENCE)
    name = name or ("LS_" + sound.get_name())
    dst = "%s/%s" % (CONTENT_DIR, name)
    if unreal.EditorAssetLibrary.does_asset_exist(dst):
        unreal.EditorAssetLibrary.delete_asset(dst)
    seq = unreal.EditorAssetLibrary.duplicate_asset(TEMPLATE_SEQUENCE, dst)
    if not seq:
        raise RuntimeError("Не смог продублировать шаблон %s" % TEMPLATE_SEQUENCE)

    frames = int(max(anim.get_editor_property("sequence_length"),
                     sound.get_duration()) * FPS) + 5
    swapped_anim = swapped_sound = 0

    for binding in seq.get_bindings():
        for track in list(binding.get_tracks()):
            cls = track.get_class().get_name()
            if "ControlRig" in cls:            # старая лицевая запись — выкидываем
                binding.remove_track(track)
                print("[aidos] удалил %s у %s" % (cls, binding.get_name()))
            elif isinstance(track, unreal.MovieSceneSkeletalAnimationTrack) \
                    and binding.get_name().lower() == "face":
                for section in track.get_sections():
                    params = section.get_editor_property("params")
                    params.set_editor_property("animation", anim)
                    section.set_editor_property("params", params)
                    section.set_range(0, frames)
                    swapped_anim += 1

    for track in seq.get_tracks():             # мастер-треки (звук)
        if isinstance(track, unreal.MovieSceneAudioTrack):
            for section in track.get_sections():
                section.set_editor_property("sound", sound)
                section.set_range(0, frames)
                swapped_sound += 1

    seq.set_playback_start(0)
    seq.set_playback_end(frames)
    unreal.EditorAssetLibrary.save_asset(dst)
    print("[aidos] Секвенция готова: %s (анимаций заменено: %d, звуков: %d, кадров: %d)"
          % (dst, swapped_anim, swapped_sound, frames))
    if not swapped_anim or not swapped_sound:
        print("[aidos] ⚠ что-то не заменилось — запусти a.dump_template() и пришли вывод")
    unreal.LevelSequenceEditorBlueprintLibrary.open_level_sequence(seq)
    return seq


def dump_template(path=None):
    """Печатает структуру шаблона (привязки/треки/секции) — для отладки подмены."""
    seq = unreal.load_asset(path or TEMPLATE_SEQUENCE)
    for b in seq.get_bindings():
        print("binding: %r" % b.get_name())
        for t in b.get_tracks():
            print("   track: %s, секций: %d" % (t.get_class().get_name(), len(t.get_sections())))
    for t in seq.get_tracks():
        print("master track: %s, секций: %d" % (t.get_class().get_name(), len(t.get_sections())))


def ask_text_v2(question, language="russian"):
    """ПОЛНЫЙ автомат: вопрос -> бэкенд -> анимация -> секвенция по шаблону."""
    _check_not_playing()
    path, _a = backend_chat_speak(question, language)
    sound = import_wav(path)
    perf = process_performance(sound)
    return build_from_template(export_anim(perf), sound)


def ask_voice_v2(question_wav, language="russian"):
    """То же, но вопрос — WAV с диска (записанный с микрофона)."""
    _check_not_playing()
    path, _q, _a = backend_voice(question_wav, language)
    sound = import_wav(path)
    perf = process_performance(sound)
    return build_from_template(export_anim(perf), sound)


# ---------------- v3: экспорт из перформанса + авто-позиция спавненного ----------------
# Экспорт Level Sequence из перформанса надёжно приносит звук+анимацию+камеру,
# но спавнит НОВОГО аватара с нулевым transform (стоит боком в стороне).
# Лечим: копируем позу референс-актора сцены в transform-трек спавненного.

REFERENCE_ACTOR = "BP_AFM_agent_v03"   # сценический актор, чью позицию копируем
CAMERA_ACTOR = ""                      # авторазворот на камеру ВЫКЛ ("" = поза как у эталона;
                                       #  включить: a.CAMERA_ACTOR = "CineCameraActor2")
YAW_OFFSET = 0.0                       # поправка разворота в градусах, если модель смотрит боком


def _level_actor(hint):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for actor in eas.get_all_level_actors():
        if hint.lower() in actor.get_actor_label().lower() \
                or hint.lower() in actor.get_name().lower():
            return actor
    return None


def _norm(s):
    """Сравнение имён без пробелов/подчёркиваний/регистра:
    'BP AFM Agent V 03' == 'BP_AFM_agent_v03'."""
    return s.lower().replace(" ", "").replace("_", "")


def fix_spawn_transform(seq=None, binding_hint="BPAFMagent"):
    """Ставит спавненного аватара секвенции на место REFERENCE_ACTOR из сцены.

    Экспорт перформанса пишет в transform-трек КЛЮЧИ (кривая позиция приезжает
    из координат капчура), поэтому трек сносим целиком и создаём заново с
    чистой позой эталона."""
    if seq is None:
        seq = unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
    if not seq:
        raise RuntimeError("Секвенция не открыта и не передана")
    ref = _level_actor(REFERENCE_ACTOR)
    if not ref:
        print("[aidos] ⚠ не нашёл в сцене актора '%s' — позицию не поправил" % REFERENCE_ACTOR)
        return
    t = ref.get_actor_transform()
    loc, rot, scale = t.translation, t.rotation.rotator(), t.scale3d
    yaw = rot.yaw
    cam = _level_actor(CAMERA_ACTOR) if CAMERA_ACTOR else None
    if cam:                               # довернуть лицом к камере
        import math
        d = cam.get_actor_location() - loc
        yaw = math.degrees(math.atan2(d.y, d.x)) + YAW_OFFSET
        print("[aidos] разворачиваю аватара на '%s' (yaw %.1f°)"
              % (cam.get_actor_label(), yaw))
    vals = [loc.x, loc.y, loc.z, rot.roll, rot.pitch, yaw, scale.x, scale.y, scale.z]

    end = seq.get_playback_end()
    fixed, names = 0, []
    for binding in seq.get_bindings():
        names.append(binding.get_name())
        if _norm(binding_hint) not in _norm(binding.get_name()):
            continue
        for tr in list(binding.get_tracks()):          # старые ключи — в мусор
            if isinstance(tr, unreal.MovieScene3DTransformTrack):
                binding.remove_track(tr)
        tr = binding.add_track(unreal.MovieScene3DTransformTrack)
        sec = tr.add_section()
        sec.set_range(0, end)
        for ch, v in zip(sec.get_all_channels(), vals):
            try:
                ch.set_default(v)
            except Exception as e:
                print("[aidos] ⚠ канал %s: %s" % (ch.get_name(), e))
        fixed += 1
    if fixed:
        print("[aidos] позиция аватара выставлена по '%s' (привязок: %d)"
              % (ref.get_actor_label(), fixed))
    else:
        print("[aidos] ⚠ привязка аватара не найдена. Привязки секвенции: %s" % names)
    try:
        unreal.LevelSequenceEditorBlueprintLibrary.refresh_current_level_sequence()
    except Exception:
        pass


def _set_idle_visible(visible):
    """Манекен-эталон в сцене. Обычно скрыт НАВСЕГДА: между ответами в кадре
    стоит спавненный аватар открытой секвенции, манекен нужен только как
    эталон позиции/класса (работает и невидимым)."""
    ref = _level_actor(REFERENCE_ACTOR)
    if not ref:
        return
    for setter in ("set_is_temporarily_hidden_in_editor", "set_actor_hidden_in_game"):
        try:
            getattr(ref, setter)(not visible)
        except Exception:
            pass


def show_idle():
    """Вернуть манекен вручную (если ответ прервали на середине)."""
    _set_idle_visible(True)


# Артефакты нашего конвейера в CONTENT_DIR — только эти префиксы и удаляем.
# Чужое (шаблоны, AS_-анимации, ручные эксперименты) не трогаем.
_PIPE_PREFIXES = ("answer_", "last_", "Perf_answer_", "Perf_last_",
                  "LS_Perf_answer_", "LS_Perf_last_")


def _current_base():
    """База имени текущего ответа ('answer_ru_131321') по открытой секвенции."""
    try:
        seq = unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
        if seq:
            return seq.get_name().replace("LS_Perf_", "")
    except Exception:
        pass
    return None


def cleanup(keep_current=True):
    """Удаляет артефакты ПРОШЛЫХ ответов (WAV/SoundWave/Performance/секвенции),
    кроме текущей открытой. Вызывается сама после каждого нового ответа;
    руками — a.cleanup() (разгрести накопившееся). НИКОГДА не роняет пайплайн:
    любая ошибка здесь — warning, а не обрыв (иначе не дойдём до Play)."""
    removed = 0
    try:
        base = _current_base() if keep_current else None
        if keep_current and not base:
            print("[aidos] cleanup пропущен: не понял, какой ответ текущий")
            return 0
        try:    # только что закрытая секвенция ещё в памяти — без GC не удалится
            unreal.SystemLibrary.collect_garbage()
        except Exception:
            pass
        for p in list(unreal.EditorAssetLibrary.list_assets(CONTENT_DIR, recursive=False)):
            name = p.split("/")[-1].split(".")[0]
            if not name.startswith(_PIPE_PREFIXES):
                continue
            if base and base in name:
                continue                  # текущий ответ оставляем
            try:
                if unreal.EditorAssetLibrary.delete_asset(p):
                    removed += 1
            except Exception as e:
                print("[aidos] ⚠ не удалился %s: %s" % (p, e))
        try:                              # WAV-файлы прошлых ответов на диске
            for f in os.listdir(SAVE_DIR):
                if f.endswith(".wav") and not (base and base in f):
                    try:
                        os.remove(os.path.join(SAVE_DIR, f))
                    except Exception:
                        pass              # файл занят (Windows) — снесём в следующий раз
        except FileNotFoundError:
            pass
        if removed:
            print("[aidos] 🧹 убрано артефактов прошлых ответов: %d" % removed)
    except Exception as e:
        print("[aidos] ⚠ cleanup споткнулся (%s) — продолжаю без очистки" % e)
    return removed


def _build_and_play(wav_path):
    """Общий хвост пайплайна: WAV ответа -> Performance -> LS -> позиция -> Play."""
    sound = import_wav(wav_path)
    seq = export_sequence(process_performance(sound))
    fix_spawn_transform(seq)
    unreal.LevelSequenceEditorBlueprintLibrary.open_level_sequence(seq)
    _set_idle_visible(False)          # манекен скрыт: в кадре только двойник секвенции
    cleanup()                         # чистим ДО запуска: play() идёт по реальному
                                      # времени, и любой фриз после него срезает
                                      # начало ответа (перескок плейхеда вперёд)
    try:                              # плейхед строго на начало
        unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(
            int(seq.get_playback_start()))
    except Exception:
        pass
    try:
        unreal.LevelSequenceEditorBlueprintLibrary.play()
        print("[aidos] ▶ играю ответ")
    except Exception as e:
        print("[aidos] автоплей не сработал (%s) — жми Play в Sequencer" % e)
    return seq


def ask_text_v3(question, language="russian"):
    """Вопрос текстом -> бэкенд -> готовая секвенция, играет сама."""
    _check_not_playing()
    path, _a = backend_chat_speak(question, language)
    return _build_and_play(path)


def ask_voice_v3(question_wav, language="russian"):
    """То же, но вопрос — WAV с диска."""
    _check_not_playing()
    path, _q, _a = backend_voice(question_wav, language)
    return _build_and_play(path)


# ---------------- микрофон внутри Unreal (Take Recorder) ----------------
# Window -> Cinematics -> Take Recorder -> +Source -> Microphone Audio ->
# красная кнопка -> сказать вопрос -> Stop. Затем: a.ask_take("russian")

TAKES_DIR = "/Game/Cinematics/Takes"


def _latest_take_sound():
    """Последний записанный SoundWave из папки тейков."""
    paths = unreal.EditorAssetLibrary.list_assets(TAKES_DIR, recursive=True)
    sounds = []
    for p in paths:
        d = unreal.EditorAssetLibrary.find_asset_data(p)
        if d and str(d.asset_class_path.asset_name) == "SoundWave":
            sounds.append(p)
    if not sounds:
        raise RuntimeError("В %s нет аудиозаписей — сначала запиши вопрос в Take Recorder"
                           % TAKES_DIR)
    return sorted(sounds)[-1]     # имена тейков с таймстампом — последний по алфавиту


def _export_soundwave(asset_path, out_path):
    """SoundWave-ассет -> WAV-файл на диске (для отправки на бэкенд)."""
    asset = unreal.load_asset(asset_path)
    task = unreal.AssetExportTask()
    task.set_editor_property("object", asset)
    task.set_editor_property("filename", out_path)
    task.set_editor_property("automated", True)
    task.set_editor_property("prompt", False)
    if not unreal.Exporter.run_asset_export_task(task):
        raise RuntimeError("Не смог выгрузить %s в WAV" % asset_path)
    return out_path


def ask_take(language="russian"):
    """Вопрос, записанный микрофоном В UNREAL (Take Recorder) -> бэкенд -> аватар."""
    _check_not_playing()
    take = _latest_take_sound()
    print("[aidos] беру запись: %s" % take)
    os.makedirs(SAVE_DIR, exist_ok=True)
    wav = _export_soundwave(take, os.path.join(SAVE_DIR, "question.wav"))
    path, _q, _a = backend_voice(wav, language)
    return _build_and_play(path)


_LAST_ID = [None]


def play_last():
    """«Живой» режим: вопрос задаёшь ГОЛОСОМ в веб-UI (http://<Мак>:8000,
    секция 3), а тут забираешь последний ответ и аватар его проигрывает."""
    _check_not_playing()
    try:
        resp = urllib.request.urlopen(BACKEND + "/last_answer", timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[aidos] на бэкенде ещё нет ответов — сначала спроси голосом в веб-UI")
            return None
        raise
    qid = resp.headers.get("X-Id", "")
    if qid and qid == _LAST_ID[0]:
        print("[aidos] нового ответа нет (этот уже проигран) — задай вопрос в веб-UI")
        return None
    _LAST_ID[0] = qid
    question = urllib.parse.unquote(resp.headers.get("X-Question", ""))
    answer = urllib.parse.unquote(resp.headers.get("X-Answer", ""))
    print("[aidos] Вопрос: %s\n[aidos] Ответ: %s" % (question, answer))
    return _build_and_play(_save_wav(resp.read(), "last"))


# Короткие имена: основной путь — v3 (экспорт+авто-позиция). Шаблонный вариант
# остался как ask_text_v2, самый первый — ask_text_old.
ask_text_old, ask_voice_old = ask_text, ask_voice
ask_text, ask_voice = ask_text_v3, ask_voice_v3


def probe_speech_api():
    """Разведка нового API 5.7 «Speech to …» — возможно, WAV -> Level Sequence
    делается одним вызовом, без ручного Performance. Пришли вывод."""
    for cls_name in ("MetaHumanSpeechToPerformance", "MetaHumanSpeechToLevelSequenceSettings",
                     "MetaHumanSpeechProcessingSettings"):
        cls = getattr(unreal, cls_name, None)
        print("\n=== %s ===" % cls_name)
        if cls:
            print([m for m in dir(cls) if not m.startswith("_")])
            print(cls.__doc__)


# ---------------- автослежение: аватар отвечает сам, без команд ----------------

# Long-poll в ФОНОВОМ потоке: бэкенд держит запрос /last_answer/wait открытым до
# появления нового ответа — узнаём мгновенно, без частого опроса. Unreal-API из
# фонового потока звать НЕЛЬЗЯ, поэтому поток только складывает id в очередь, а
# главный поток (slate-тик) забирает её и играет.

_WATCH = {"handle": None, "queue": [], "stop": True, "thread": None}


def _watch_loop():
    import time as _time
    since = _LAST_ID[0] or ""
    while not _WATCH["stop"]:
        try:
            r = urllib.request.urlopen(
                BACKEND + "/last_answer/wait?timeout=25&since=" + since, timeout=30)
            qid = json.load(r).get("id") or ""
            if qid and qid != since:
                _WATCH["queue"].append(qid)
                since = qid
        except urllib.error.HTTPError as e:
            if e.code == 404:   # старый бэкенд без /wait — обычный опрос раз в 5 с
                _time.sleep(5)
                try:
                    r = urllib.request.urlopen(BACKEND + "/last_answer/id", timeout=3)
                    qid = json.load(r).get("id") or ""
                    if qid and qid != since:
                        _WATCH["queue"].append(qid)
                        since = qid
                except Exception:
                    pass
            else:
                _time.sleep(3)
        except Exception:
            _time.sleep(3)      # бэкенд недоступен — подождать и переподключиться


def watch(interval=None):
    """Включает автоответ: как только на бэкенде появился новый озвученный ответ
    (/voice из веб-UI) — аватар сам его проигрывает. Выключить: a.stop_watch()."""
    import threading
    stop_watch()
    _WATCH["stop"] = False
    _WATCH["queue"] = []
    _WATCH["thread"] = threading.Thread(target=_watch_loop, daemon=True)
    _WATCH["thread"].start()

    def _tick(_dt):
        if not _WATCH["queue"]:
            return
        try:  # пока играет предыдущий ответ — подождём
            if unreal.LevelSequenceEditorBlueprintLibrary.is_playing():
                return
        except Exception:
            pass
        qid = _WATCH["queue"].pop(0)
        if qid == _LAST_ID[0]:
            return
        print("[aidos] 🔔 новый ответ на бэкенде — собираю и играю")
        try:
            play_last()
        except Exception as e:
            print("[aidos] ⚠ автоответ упал: %s" % e)
            _LAST_ID[0] = qid   # не зацикливаться на битом ответе

    _WATCH["handle"] = unreal.register_slate_post_tick_callback(_tick)
    print("[aidos] 👂 слежу за ответами (long-poll, мгновенно). Стоп: a.stop_watch()")


def stop_watch():
    _WATCH["stop"] = True       # фоновый поток выйдет после текущего запроса (<30 с)
    if _WATCH.get("handle") is not None:
        unreal.unregister_slate_post_tick_callback(_WATCH["handle"])
        _WATCH["handle"] = None
        print("[aidos] слежение выключено")


# ---------------- разведка API (запустить ПЕРВЫМ, прислать вывод) ----------------

def dump_api():
    """Печатает, какие MetaHuman-классы реально есть в этой версии UE."""
    names = [n for n in dir(unreal) if "metahuman" in n.lower() or "MetaHuman" in n]
    print("=== unreal.* с MetaHuman в имени (%d): ===" % len(names))
    for n in sorted(names):
        print("  ", n)
    for cls_name in ("MetaHumanPerformance", "MetaHumanPerformanceExportUtils",
                     "DataInputType"):
        cls = getattr(unreal, cls_name, None)
        print("\n=== %s: %s ===" % (cls_name, "ЕСТЬ" if cls else "НЕТ"))
        if cls:
            members = [m for m in dir(cls) if not m.startswith("_")]
            print("   ", ", ".join(members[:60]))
