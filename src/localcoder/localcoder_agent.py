#!/usr/bin/env /opt/homebrew/bin/python3
"""localcoder — Claude Code-style CLI agent powered by local models."""

import os, subprocess, sys, json, urllib.request, urllib.parse, time, re, argparse, logging, signal, threading, random

from localcoder.agent_session import Session, get_latest_session_id, list_sessions
from localcoder.compaction import compress_messages as smart_compress_messages
from localcoder.safe_commands import is_safe_command, classify_command
from localcoder.mcp_client import init_mcp, get_mcp_manager

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.document import Document

from localcoder.localcoder_display import (
    ThinkingSpinner,
    show_startup_animation,
    show_tool_animation,
    tool_running_indicator,
    generating_indicator,
    context_usage_bar,
    context_usage_bar_compact,
)

console = Console()

# ── i18n ──────────────────────────────────────────────────────────────────────
UI_LANG = os.environ.get("LOCALCODER_UI_LANG", "en")

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
except Exception:
    arabic_reshaper = None
    _bidi_get_display = None

_REASONING_LOCALIZATION_CACHE = {}
_ARABIC_RUN_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]"
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
    r"\u064B-\u065F\u0670\u06D6-\u06ED\u200c\u200d"
    r"\u061F\u060C\u061B\u066A-\u066D\s]*"
)


def _shape_arabic(text):
    """Reshape Arabic text for correct terminal display.

    Default to reshaping so Arabic remains readable even when terminal RTL
    support is partial or disabled. Users can opt into native terminal shaping
    by setting LOCALCODER_NATIVE_ARABIC=1.
    """
    if not text:
        return text
    native_pref = os.environ.get("LOCALCODER_NATIVE_ARABIC")
    term_program = os.environ.get("TERM_PROGRAM", "")
    term_name = os.environ.get("TERM", "")
    terminal_supports_native = (
        term_program in ("Apple_Terminal", "iTerm.app", "WezTerm")
        or "kitty" in term_name
        or "ghostty" in term_name
    )
    if native_pref == "1" or (native_pref != "0" and terminal_supports_native):
        return f"\u2067{text}\u2069"
    if not arabic_reshaper:
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        if _bidi_get_display:
            return _bidi_get_display(reshaped)
        return reshaped
    except Exception:
        return text


def _contains_arabic(text):
    return bool(text and re.search(r"[\u0600-\u06FF]", text))


def _contains_latin(text):
    return bool(text and re.search(r"[A-Za-z]", text))


def _shape_arabic_segments(text):
    if not text or not _contains_arabic(text):
        return text

    def _repl(match):
        return _shape_arabic(match.group(0))

    return _ARABIC_RUN_RE.sub(_repl, text)


def _display_text(text):
    if not text:
        return text
    rendered = []
    for line in text.splitlines():
        if not _contains_arabic(line):
            rendered.append(line)
        elif _contains_latin(line):
            # Mixed LTR/RTL lines render badly if the whole line is force-shaped.
            rendered.append(_shape_arabic_segments(line))
        else:
            rendered.append(_shape_arabic(line))
    return "\n".join(rendered)


def _localize_reasoning_text(reasoning_text):
    return (reasoning_text or "").strip()


_IMAGE_BAD_HINTS = (
    "ai",
    "generated",
    "logo",
    "icon",
    "avatar",
    "emoji",
    "sprite",
    "sticker",
    "thumbnail",
    "thumb",
    "banner",
    "wallpaper",
    "desktop-wallpaper",
    "vector",
    "illustration",
    "drawing",
    "clipart",
    "stablediffusion",
    "stable diffusion",
    "midjourney",
    "lexica",
    "artstation",
)
_IMAGE_GOOD_HINTS = (
    "photo",
    "portrait",
    "official",
    "press",
    "getty",
    "reuters",
    "afp",
    "ap",
    "apimages",
    "wikimedia",
    "commons",
    "unsplash",
    "pexels",
    "flickr",
    "biography",
    "britannica",
    "fifa",
    "uefa",
)


def _clean_image_search_query(query):
    cleaned = query or ""
    for term in (
        "image",
        "images",
        "photo",
        "photos",
        "picture",
        "pictures",
        "wallpaper",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "real photo",
        "official photo",
        "صورة",
        "صور",
        "photo de",
        "photo d'",
    ):
        cleaned = re.sub(rf"\b{re.escape(term)}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or (query or "")


def _rewrite_photo_search_query(query):
    cleaned = _clean_image_search_query(query)
    cleaned = re.sub(
        r"^(show me|find me|give me|like|comme une?|montre(?:z)? moi|je veux|أرني|اعطني|أعطني)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^(une|un|des|la|le|les)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(de|d'|of|for|عن|ل|لك)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(portrait|headshot)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if re.match(r"^ل[\u0600-\u06FF]", cleaned):
        cleaned = cleaned[1:]
    lower = cleaned.lower()
    if any(term in lower for term in ("logo", "illustration", "vector", "wallpaper")):
        return cleaned
    if any(term in lower for term in ("photo", "photos", "صورة", "صور")):
        return f'{cleaned} real photo official press -wallpaper -portrait -headshot -ai -"stable diffusion"'
    return f'{cleaned} official press photo -wallpaper -portrait -headshot -ai -"stable diffusion"'


def _is_image_only_request(text):
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 180:
        return False
    lower = cleaned.lower()
    has_image_intent = any(
        token in lower
        for token in (
            "show me a photo",
            "show me a picture",
            "show me an image",
            "find me a photo",
            "photo of",
            "image of",
            "picture of",
            "real photo of",
            "montre moi une photo",
            "photo de",
            "image de",
            "photo réelle",
            "أرني صورة",
            "أعطني صورة",
            "صورة",
            "صور",
        )
    )
    has_build_intent = any(
        token in lower
        for token in (
            "build",
            "create",
            "make a page",
            "gallery",
            "html",
            "website",
            "site",
            "app",
            "page web",
            "crée",
            "أنشئ",
            "اصنع",
            "صفحة",
            "موقع",
        )
    )
    return has_image_intent and not has_build_intent


def _rank_image_candidate(url, title="", source=""):
    text = " ".join([url or "", title or "", source or ""]).lower()
    score = 0
    if any(bad in text for bad in _IMAGE_BAD_HINTS):
        score -= 50
    if any(good in text for good in _IMAGE_GOOD_HINTS):
        score += 20
    if re.search(r"\.(png|jpe?g|webp|gif|bmp|svg)(\?|$)", url or "", re.I):
        score += 10
    if any(
        host in (url or "").lower() for host in ("images.", "img.", "media.", "cdn.")
    ):
        score += 3
    if any(
        host in (url or "").lower()
        for host in ("pinterest.", "facebook.", "instagram.", "tiktok.")
    ):
        score -= 20
    return score


def _sort_image_candidates(candidates):
    seen = set()
    ranked = []
    for candidate in candidates or []:
        url = (candidate.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        ranked.append(
            (
                _rank_image_candidate(
                    url, candidate.get("title", ""), candidate.get("source", "")
                ),
                candidate,
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in ranked]


def _extract_image_candidates_from_text(text, source_url=""):
    found = []
    for url in re.findall(r"!\[[^\]]*\]\((https?://[^\)]+)\)", text or ""):
        found.append({"url": url, "title": "", "source": source_url})
    for url in re.findall(
        r'(https?://[^\s"\'<>()]+(?:png|jpg|jpeg|webp|gif|bmp|svg)(?:\?[^\s"\'<>()]*)?)',
        text or "",
        re.I,
    ):
        found.append({"url": url, "title": "", "source": source_url})
    return _sort_image_candidates(found)


def _resolve_whisper_model(language, cfg=None):
    cfg = cfg or {}
    whisper_home = os.path.expanduser("~/.local/share/whisper")
    env_specific = os.environ.get(
        f"LOCALCODER_WHISPER_MODEL_{(language or '').upper()}"
    )
    env_default = os.environ.get("LOCALCODER_WHISPER_MODEL")
    cfg_specific = cfg.get(f"voice_whisper_model_{language}") if language else None
    cfg_default = cfg.get("voice_whisper_model")

    candidates = []
    for item in (env_specific, cfg_specific, env_default, cfg_default):
        if item:
            candidates.append(os.path.expanduser(item))

    if language == "ar":
        candidates.extend(
            [
                os.path.join(whisper_home, "ggml-large-v3-turbo.bin"),
                os.path.join(whisper_home, "ggml-large-v3.bin"),
                os.path.join(whisper_home, "ggml-medium.bin"),
                os.path.join(whisper_home, "ggml-small.bin"),
                os.path.join(whisper_home, "ggml-base.bin"),
            ]
        )
    else:
        candidates.extend(
            [
                os.path.join(whisper_home, "ggml-medium.bin"),
                os.path.join(whisper_home, "ggml-small.bin"),
                os.path.join(whisper_home, "ggml-base.bin"),
            ]
        )

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


# Centralized translation strings
_STRINGS = {
    # ── Banner ──
    "banner_title": {"ar": "المبرمج المحلي", "fr": "LocalCoder"},
    "banner_subtitle": {
        "ar": "وكيل برمجة محلي بالذكاء الاصطناعي داخل الطرفية",
        "fr": "Agent de programmation IA local dans le terminal",
    },
    "cmd_line": {"ar": "واجهة سطر الأوامر", "fr": "Interface en ligne de commande"},
    "offline": {"ar": "✓ بدون إنترنت", "fr": "✓ hors ligne"},
    "desc_line1": {
        "ar": "اكتب واختبر وصحح الأكواد مباشرة من الطرفية.",
        "fr": "Écrivez, testez et déboguez du code depuis votre terminal.",
    },
    "desc_line2": {
        "ar": "يعمل 100% على GPU. بدون مفاتيح API. بدون سحابة. اكتب ? للمساعدة.",
        "fr": "Tourne 100% sur votre GPU. Pas de clés API. Pas de cloud. Tapez ? pour aide.",
    },
    # ── Prompt ──
    "prompt_label": {"ar": "رسالة", "fr": "message"},
    # ── Hint bar ──
    "voice": {"ar": "تسجيل", "fr": "voix"},
    "image": {"ar": "صورة", "fr": "image"},
    "stop_send": {"ar": "إيقاف + إرسال", "fr": "arrêt + envoi"},
    "stats": {"ar": "حالة", "fr": "stats"},
    "free": {"ar": "تحرير", "fr": "libérer"},
    "reason": {"ar": "تفكير", "fr": "raisonner"},
    "switch": {"ar": "تبديل", "fr": "changer"},
    # ── Voice ──
    "voice_not_avail": {
        "ar": "الصوت غير متوفر. شغّل: localcoder --setup",
        "fr": "Voix non disponible. Lancez: localcoder --setup",
    },
    "recording": {"ar": "جارٍ التسجيل...", "fr": "Enregistrement..."},
    "press_ctrlr_stop": {
        "ar": "اضغط Ctrl+R للإيقاف",
        "fr": "appuyez Ctrl+R pour arrêter",
    },
    "transcribing": {"ar": "جارٍ التفريغ...", "fr": "Transcription..."},
    "no_speech": {"ar": "لم يُكتشف كلام", "fr": "Aucune parole détectée"},
    "mic_perm_needed": {
        "ar": "يلزم إذن الميكروفون  ·  اضغط ctrl+r مرة أخرى",
        "fr": "Permission micro requise · appuyez ctrl+r à nouveau",
    },
    "voice_setup_title": {"ar": "إعداد الإدخال الصوتي", "fr": "Configuration vocale"},
    "voice_setup_sub": {"ar": "إعداد لمرة واحدة", "fr": "configuration unique"},
    "voice_select_lang": {
        "ar": "اختر لغة التحدث الرئيسية للإدخال الصوتي:",
        "fr": "Choisissez votre langue principale pour la saisie vocale:",
    },
    "voice_lang_set": {"ar": "✓ لغة الصوت: {lang}", "fr": "✓ Langue vocale: {lang}"},
    "voice_lang_change": {
        "ar": "غيّرها في أي وقت بـ /voice-lang",
        "fr": "Changez à tout moment avec /voice-lang",
    },
    # ── Session ──
    "resumed_session": {
        "ar": "✦ استئناف الجلسة ({n} رسائل)",
        "fr": "✦ Session restaurée ({n} messages)",
    },
    "no_saved_session": {
        "ar": "لا توجد جلسة محفوظة — بداية جديدة",
        "fr": "Pas de session sauvegardée — nouveau départ",
    },
    "conversation_cleared": {"ar": "تم مسح المحادثة.", "fr": "Conversation effacée."},
    # ── Approval ──
    "allow_tool": {"ar": "السماح بـ {fname}؟", "fr": "Autoriser {fname} ?"},
    "yes": {"ar": "نعم", "fr": "oui"},
    "no": {"ar": "لا", "fr": "non"},
    "always": {"ar": "دائماً", "fr": "toujours"},
    "approved": {"ar": "✓ موافق", "fr": "✓ approuvé"},
    "denied": {"ar": "✗ مرفوض", "fr": "✗ refusé"},
    "type_yna": {"ar": "اكتب y أو n أو a", "fr": "Tapez y, n, ou a"},
    # ── Thinking slider ──
    "think_off": {"ar": "إيقاف", "fr": "off"},
    "think_light": {"ar": "خفيف", "fr": "léger"},
    "think_balanced": {"ar": "تفكير", "fr": "réfléchir"},
    "think_deep": {"ar": "عميق", "fr": "profond"},
    "think_desc_off": {"ar": "بدون تفكير", "fr": "Pas de réflexion"},
    "think_desc_light": {"ar": "تفكير سريع", "fr": "Réflexion rapide"},
    "think_desc_balanced": {"ar": "متوازن", "fr": "Équilibré"},
    "think_desc_deep": {"ar": "تفكير عميق", "fr": "Réflexion profonde"},
    # ── Sandbox ──
    "unrestricted_warn": {
        "ar": "⚠ وضع غير مقيد — الحماية معطلة. وصول كامل للنظام.",
        "fr": "⚠ MODE NON RESTREINT — sandbox désactivé. Accès complet.",
    },
    "yolo_warn": {
        "ar": "وضع الموافقة التلقائية. الحماية مفعلة.",
        "fr": "Mode auto-approbation. Sandbox actif.",
    },
    # ── Exit / Cleanup ──
    "bye": {"ar": "إلى اللقاء", "fr": "au revoir"},
    "gpu_cleanup": {"ar": "تنظيف GPU", "fr": "Nettoyage GPU"},
    "keep_running": {"ar": "إبقاء التشغيل", "fr": "garder"},
    "unload_models": {"ar": "تفريغ النماذج", "fr": "décharger"},
    "stop_all": {"ar": "إيقاف الكل", "fr": "tout arrêter"},
    "keep": {"ar": "إبقاء", "fr": "garder"},
    "no_models_loaded": {
        "ar": "لا توجد نماذج محملة في GPU. إلى اللقاء!",
        "fr": "Aucun modèle chargé. Au revoir !",
    },
    "keeping_loaded": {
        "ar": "سيتم إبقاء النماذج محملة. إلى اللقاء!",
        "fr": "Modèles conservés. Au revoir !",
    },
    "models_unloaded": {
        "ar": "تم تفريغ جميع النماذج، ذاكرة GPU تم تحريرها",
        "fr": "Tous les modèles déchargés, mémoire GPU libérée",
    },
    "ollama_unloaded": {
        "ar": "تم تفريغ نماذج Ollama",
        "fr": "Modèles Ollama déchargés",
    },
    "llama_stopped": {"ar": "تم إيقاف llama-server", "fr": "llama-server arrêté"},
    # ── Slash commands ──
    "cmd_switch_model": {
        "ar": "تبديل النموذج (بحث تقريبي)",
        "fr": "Changer de modèle (recherche floue)",
    },
    "cmd_set_model": {"ar": "تحديد النموذج بالاسم", "fr": "Définir le modèle par nom"},
    "cmd_clear": {"ar": "مسح المحادثة", "fr": "Effacer la conversation"},
    "cmd_gpu": {
        "ar": "عرض ذاكرة GPU والنماذج",
        "fr": "Afficher mémoire GPU et modèles",
    },
    "cmd_clean": {
        "ar": "تحرير ذاكرة GPU (تفريغ النماذج الخاملة)",
        "fr": "Libérer mémoire GPU (décharger modèles inactifs)",
    },
    "cmd_health": {"ar": "لوحة صحة GPU الكاملة", "fr": "Tableau de bord santé GPU"},
    "cmd_resume": {
        "ar": "استعادة الجلسة الأخيرة",
        "fr": "Restaurer la dernière session",
    },
    "cmd_context": {
        "ar": "عرض استخدام الرموز",
        "fr": "Afficher utilisation des tokens",
    },
    "cmd_paste": {
        "ar": "لصق صورة من الحافظة",
        "fr": "Coller une image du presse-papiers",
    },
    "cmd_undo": {
        "ar": "التراجع عن آخر تعديل",
        "fr": "Annuler la dernière modification",
    },
    "cmd_snapshots": {"ar": "قائمة النسخ الاحتياطية", "fr": "Lister les sauvegardes"},
    "cmd_diff": {"ar": "عرض التغييرات", "fr": "Afficher les changements"},
    "cmd_cost": {
        "ar": "عرض تكلفة الرموز ($0.00)",
        "fr": "Afficher coût tokens ($0.00)",
    },
    "cmd_ask": {"ar": "السؤال قبل كل أداة", "fr": "Demander avant chaque outil"},
    "cmd_auto": {
        "ar": "موافقة تلقائية للأدوات الآمنة",
        "fr": "Auto-approuver les outils sûrs",
    },
    "cmd_bypass": {"ar": "الموافقة على كل شيء", "fr": "Tout approuver"},
    "cmd_yolo": {"ar": "مثل /bypass", "fr": "Comme /bypass"},
    "cmd_log": {"ar": "عرض سجل التصحيح", "fr": "Voir le journal de débogage"},
    "cmd_think": {
        "ar": "تبديل التفكير: لا ← خفيف ← متوسط ← عميق",
        "fr": "Basculer réflexion: non → léger → moyen → profond",
    },
    "cmd_deploy": {
        "ar": "إنشاء ونشر تطبيق React بالذكاء الاصطناعي",
        "fr": "Générer et déployer une app React IA",
    },
    "cmd_exit": {"ar": "خروج", "fr": "Quitter"},
    # ── Prompt right-side hints ──
    "hint_enter_send": {"ar": "إدخال للإرسال", "fr": "Entrée pour envoyer"},
    "hint_think": {"ar": "/think للتفكير", "fr": "/think pour réfléchir"},
    "hint_help": {"ar": "? للمساعدة", "fr": "? pour aide"},
    # ── System prompt language instruction ──
    "sys_lang_instruction": {
        "ar": "أجب دائماً باللغة العربية. استخدم العربية في الردود الظاهرة للمستخدم مع الإبقاء على الأوامر والمسارات وأسماء الملفات كما هي عند الحاجة.",
        "fr": "Réponds toujours en français. Utilise le français pour les réponses visibles tout en conservant les commandes, chemins et noms de fichiers si nécessaire.",
    },
}


def _t(key, **kwargs):
    """Get translated string by key. Falls back to English (the key itself or hardcoded)."""
    entry = _STRINGS.get(key)
    if entry:
        text = entry.get(UI_LANG, "")
        if text:
            if UI_LANG == "ar":
                text = _shape_arabic(text)
            if kwargs:
                text = text.format(**kwargs)
            return text
    # English fallback — return key as-is (caller provides English inline)
    return ""


def _ui(en, ar=None, fr=None):
    """Inline translation helper — use _t() for keyed strings, this for one-offs."""
    if UI_LANG == "ar" and ar:
        return _shape_arabic(ar)
    if UI_LANG == "fr" and fr:
        return fr
    return en


# ── Config ──
API_BASE = os.environ.get("GEMMA_API_BASE", "http://127.0.0.1:8089/v1")
MODEL = os.environ.get("GEMMA_MODEL", "gemma4-26b")
# Vision model (UI-TARS) for screenshot grounding — separate from brain model
VISION_API_BASE = os.environ.get("VISION_API_BASE", "")  # e.g. http://127.0.0.1:8090/v1
VISION_MODEL = os.environ.get("VISION_MODEL", "")  # e.g. UI-TARS-1.5-7B
CWD = os.getcwd()
REASONING_EFFORT = "medium"  # none, low, medium, high — toggle with /think


# ── Backend detection ──
def detect_backend():
    """Auto-detect backend type and model info from the API server."""
    info = {
        "backend": "unknown",
        "model_name": MODEL,
        "quant": "",
        "size": "",
        "ctx": "",
    }
    try:
        # Check if it's Ollama (has /api/tags)
        if "11434" in API_BASE:
            info["backend"] = "Ollama"
        else:
            info["backend"] = "llama.cpp"

        # Get model list from /models endpoint
        req = urllib.request.Request(
            f"{API_BASE}/models", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        models = data.get("data", [])
        if models:
            # Prefer the model matching what the user selected (MODEL),
            # otherwise fall back to the first model in the list
            m = models[0]
            mid = m.get("id", MODEL)
            for candidate in models:
                cid = candidate.get("id", "")
                if MODEL.lower().replace(":", "-").replace("_", "") in cid.lower().replace(":", "-").replace("_", ""):
                    mid = cid
                    m = candidate
                    break
            info["model_name"] = mid

            # Parse quant from model ID (e.g. "gemma-4-26B-A4B-it-UD-Q3_K_XL")
            for q in [
                "Q2_K",
                "Q3_K_S",
                "Q3_K_M",
                "Q3_K_L",
                "Q3_K_XL",
                "Q4_K_S",
                "Q4_K_M",
                "Q4_K_XL",
                "Q5_K_M",
                "Q6_K",
                "Q8_0",
                "BF16",
                "F16",
                "IQ3_S",
                "IQ4_XS",
            ]:
                if q.lower().replace("_", "") in mid.lower().replace("_", "").replace(
                    "-", ""
                ):
                    info["quant"] = q
                    break

            # Parse model size
            for s in [
                "e2b",
                "e4b",
                "26b",
                "27b",
                "31b",
                "12b",
                "8b",
                "4b",
                "2b",
                "1b",
                "70b",
            ]:
                if s in mid.lower().replace("-", ""):
                    info["size"] = s.upper()
                    break

        # Try to get context size from /props or /health
        try:
            req2 = urllib.request.Request(
                f"{API_BASE.replace('/v1', '')}/props",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req2, timeout=2) as resp2:
                props = json.loads(resp2.read())
            ctx = props.get("default_generation_settings", {}).get("n_ctx", 0)
            if ctx:
                if ctx >= 131072:
                    info["ctx"] = "128K"
                elif ctx >= 65536:
                    info["ctx"] = "64K"
                elif ctx >= 32768:
                    info["ctx"] = "32K"
                elif ctx >= 16384:
                    info["ctx"] = "16K"
                else:
                    info["ctx"] = f"{ctx // 1024}K"
        except:
            pass

    except:
        pass
    return info


BACKEND_INFO = {
    "backend": "unknown",
    "model_name": MODEL,
    "quant": "",
    "size": "",
    "ctx": "",
}

CONFIG_FILE = os.path.expanduser("~/.localcoder/config.json")


def _save_config(**kwargs):
    """Save config values to ~/.localcoder/config.json."""
    try:
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        cfg.update(kwargs)
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except:
        pass


def _load_config():
    """Load config from ~/.localcoder/config.json."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def _save_last_model(model, api_base):
    _save_config(
        model=model,
        api_base=api_base,
        backend="ollama" if "11434" in api_base else "llamacpp",
    )


def _check_permissions():
    """Check and guide user through macOS permissions on first run."""
    console.print()
    console.print(
        Panel(
            "[bold]macOS Permissions[/]  [dim]checking what Local Coder can access...[/]",
            border_style="#81b29a",
            padding=(0, 1),
        )
    )

    PERMS = [
        {
            "name": "Microphone",
            "why": "Voice input — speak prompts instead of typing (Ctrl+R)",
            "test": lambda: _test_mic(),
            "fix": "System Settings → Privacy & Security → Microphone → enable your terminal",
        },
        {
            "name": "Screen Recording",
            "why": "Computer use — let the AI see your screen and automate GUI tasks",
            "test": lambda: _test_screen(),
            "fix": "System Settings → Privacy & Security → Screen Recording → enable your terminal",
        },
        {
            "name": "Accessibility",
            "why": "System control — set wallpaper, control apps, click UI elements",
            "test": lambda: _test_accessibility(),
            "fix": "System Settings → Privacy & Security → Accessibility → enable your terminal",
        },
        {
            "name": "Automation",
            "why": "App control — automate Finder, Safari, System Events",
            "test": lambda: _test_automation(),
            "fix": "System Settings → Privacy & Security → Automation → enable your terminal",
        },
    ]

    all_granted = True
    denied = []

    for perm in PERMS:
        try:
            granted = perm["test"]()
        except:
            granted = False

        if granted:
            console.print(
                f"  [green]✓[/] [bold]{perm['name']}[/]  [dim]{perm['why']}[/]"
            )
        else:
            all_granted = False
            denied.append(perm)
            console.print(
                f"  [yellow]○[/] [bold]{perm['name']}[/]  [dim]{perm['why']}[/]"
            )

    if denied:
        console.print(f"\n  [yellow]Some permissions not granted yet.[/]")
        console.print(
            f"  [dim]Local Coder works without them, but these features will be limited:[/]\n"
        )
        for perm in denied:
            console.print(f"    [yellow]•[/] [bold]{perm['name']}[/]: {perm['why']}")
            console.print(f"      [dim]Fix: {perm['fix']}[/]")

        console.print(f"\n  [dim]Open System Settings now? (y/n)[/]")
        try:
            ans = input("  ▸ ").strip().lower()
            if ans in ("y", "yes", ""):
                subprocess.run(
                    [
                        "open",
                        "x-apple.systempreferences:com.apple.preference.security?Privacy",
                    ],
                    capture_output=True,
                )
                console.print(
                    f"  [green]Opened System Settings.[/] Grant permissions, then restart Local Coder."
                )
        except:
            pass
    else:
        console.print(f"\n  [green]✓ All permissions granted![/]")

    _save_config(permissions_checked=True)
    console.print()


def _test_mic():
    import tempfile

    tmp = tempfile.mktemp(suffix=".wav")
    try:
        r = subprocess.run(
            [
                "rec",
                "-q",
                "-r",
                "16000",
                "-c",
                "1",
                "-b",
                "16",
                tmp,
                "trim",
                "0",
                "0.3",
            ],
            capture_output=True,
            timeout=5,
        )
        return os.path.exists(tmp) and os.path.getsize(tmp) > 100
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _test_screen():
    import tempfile

    tmp = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(
            ["screencapture", "-D1", "-x", tmp], capture_output=True, timeout=5
        )
        return os.path.exists(tmp) and os.path.getsize(tmp) > 1000
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _test_accessibility():
    r = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first process',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return r.returncode == 0


def _test_automation():
    r = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get picture of desktop 1',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return r.returncode == 0


def _switch_model(new_model, new_url):
    """Switch to a new model — handles running, downloaded, and cross-backend."""
    global MODEL, API_BASE, BACKEND_INFO
    import shutil as _shutil

    # Check if this model is already running on its backend
    is_running = False
    try:
        req = urllib.request.Request(
            f"{new_url}/models", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        running_ids = [m.get("id", "").lower() for m in data.get("data", [])]
        is_running = new_model.lower() in " ".join(running_ids).lower()
    except:
        pass

    if is_running:
        # Model already running — just switch
        MODEL = new_model
        API_BASE = new_url
        BACKEND_INFO.update(detect_backend())
        _save_last_model(MODEL, API_BASE)
        console.print(
            f"  [green]✓ Switched to [bold]{MODEL}[/] on {BACKEND_INFO['backend']}[/]"
        )
        return

    # Model is downloaded but not running — need to load it
    is_ollama = "11434" in new_url

    if is_ollama:
        # Ollama model — just switch, Ollama auto-loads on first request
        MODEL = new_model
        API_BASE = new_url
        BACKEND_INFO.update(detect_backend())
        _save_last_model(MODEL, API_BASE)
        console.print(f"  [green]✓ Switched to [bold]{MODEL}[/] on Ollama[/]")
        console.print(f"  [dim]Ollama will load the model on first request[/]")
        return

    # llama.cpp downloaded model — needs server restart
    # Find the GGUF file
    all_m = discover_all_models()
    gguf_path = None
    for m in all_m:
        if m["id"] == new_model and m.get("path"):
            gguf_path = m["path"]
            break

    if not gguf_path:
        console.print(f"  [red]Cannot find GGUF file for {new_model}[/]")
        return

    console.print(f"  [yellow]Restarting llama-server with {new_model}...[/]")

    # Kill current server
    subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
    time.sleep(3)

    # Find mmproj in same directory
    model_dir = os.path.dirname(gguf_path)
    mmproj = None
    for f in os.listdir(model_dir):
        if "mmproj" in f.lower() and f.endswith(".gguf"):
            mmproj = os.path.join(model_dir, f)
            break

    # Start new server
    binary = os.path.expanduser("~/.unsloth/llama.cpp/llama-server")
    if not os.path.exists(binary):
        binary = _shutil.which("llama-server") or binary

    cmd = [
        binary,
        "-m",
        gguf_path,
        "--port",
        "8089",
        "-ngl",
        "99",
        "-c",
        "131072",
        "-np",
        "1",
        "-fa",
        "on",
        "-ctk",
        "q4_0",
        "-ctv",
        "q4_0",
        "--no-warmup",
        "--cache-ram",
        "0",
        "--jinja",
        "--reasoning-budget",
        "0",
    ]
    if mmproj:
        cmd += ["--mmproj", mmproj]
    else:
        cmd += ["--no-mmproj"]

    console.print(f"  [dim]Starting server...[/]")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for ready
    ready = False
    for i in range(60):
        try:
            req = urllib.request.Request("http://127.0.0.1:8089/health")
            with urllib.request.urlopen(req, timeout=1):
                ready = True
                break
        except:
            time.sleep(1)
        if not proc.poll() is None:
            console.print(f"  [red]Server crashed — model may not fit in GPU[/]")
            return

    if ready:
        MODEL = new_model
        API_BASE = "http://127.0.0.1:8089/v1"
        BACKEND_INFO.update(detect_backend())
        _save_last_model(MODEL, API_BASE)
        console.print(
            f"  [green]✓ Switched to [bold]{MODEL}[/] on llama.cpp ({BACKEND_INFO.get('ctx', '?')})[/]"
        )
    else:
        console.print(f"  [red]Server failed to start in 60s[/]")


def _load_last_model():
    """Load last used model from config. Also auto-detect what's actually running."""
    global MODEL, API_BASE
    # 1. Load saved config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            if cfg.get("model"):
                MODEL = cfg["model"]
            if cfg.get("api_base"):
                API_BASE = cfg["api_base"]
    except:
        pass

    # 2. Auto-detect: if llama-server is running, use whatever model it has loaded
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8089/v1/models",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        running = [m.get("id", "") for m in data.get("data", [])]
        if running:
            MODEL = running[0]
            API_BASE = "http://127.0.0.1:8089/v1"
            return
    except:
        pass

    # 3. Fallback: check Ollama
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/ps",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        loaded = [m.get("name", "") for m in data.get("models", [])]
        if loaded:
            MODEL = loaded[0]
            API_BASE = "http://127.0.0.1:11434/v1"
    except:
        pass


# ── Multi-backend discovery ──
BACKENDS = [
    {"name": "llama.cpp", "url": "http://127.0.0.1:8089/v1", "type": "llamacpp"},
    {"name": "Ollama", "url": "http://127.0.0.1:11434/v1", "type": "ollama"},
]


def discover_all_models():
    """Discover models from running backends + downloaded GGUFs."""
    all_models = []
    seen = set()

    # 1. Running models from backends
    for backend in BACKENDS:
        try:
            req = urllib.request.Request(
                f"{backend['url']}/models", headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            for m in data.get("data", []):
                mid = m.get("id", "")
                if mid:
                    all_models.append(
                        {
                            "id": mid,
                            "backend": backend["name"],
                            "url": backend["url"],
                            "status": "running",
                        }
                    )
                    seen.add(mid.lower())
        except:
            pass

    # 2. Downloaded GGUFs in HuggingFace cache (available for llama.cpp)
    import glob

    hf_cache = os.path.expanduser("~/.cache/huggingface/hub")
    for gguf in glob.glob(f"{hf_cache}/models--*/snapshots/*/*.gguf"):
        name = os.path.basename(gguf)
        if "mmproj" in name.lower():
            continue
        if name.lower() not in seen:
            size_gb = os.path.getsize(gguf) / (1024**3)
            all_models.append(
                {
                    "id": name,
                    "backend": "llama.cpp",
                    "url": "http://127.0.0.1:8089/v1",
                    "status": "downloaded",
                    "path": gguf,
                    "size_gb": round(size_gb, 1),
                }
            )
            seen.add(name.lower())

    return all_models


def select_model_interactive():
    """Interactive model selector with fuzzy search autocomplete."""
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML as PT_HTML

    models = discover_all_models()
    if not models:
        console.print(f"\n  [red]No backends found. Start llama-server or Ollama.[/]")
        return None, None

    # Display with styled backend grouping
    console.print()
    console.print(
        Panel(
            "[bold]Select Model[/]  [dim]type to search · enter to select · esc to cancel[/]",
            border_style="#81b29a",
            padding=(0, 1),
        )
    )

    by_backend = {}
    for m in models:
        by_backend.setdefault(m["backend"], []).append(m)

    for backend, mlist in by_backend.items():
        console.print(f"\n  [bold #81b29a]{backend}[/]")
        for m in mlist:
            is_current = m["id"] == MODEL and m["url"] == API_BASE
            status = m.get("status", "running")
            if is_current:
                dot = "[bold green]●[/]"
                name_style = "bold white"
                tag = " [bold green]← active[/]"
            elif status == "running":
                dot = "[green]○[/]"
                name_style = "cyan"
                tag = ""
            else:
                dot = "[dim]◌[/]"
                name_style = "dim cyan"
                size = f" ({m.get('size_gb', '?')}GB)" if m.get("size_gb") else ""
                tag = f" [dim yellow]downloaded{size}[/]"
            console.print(f"    {dot} [{name_style}]{m['id']}[/]{tag}")
    console.print()

    # Build fuzzy completer
    class ModelCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lower()
            for m in models:
                label = m["id"]
                if text in label.lower() or not text:
                    status = m.get("status", "running")
                    tag = (
                        '<style fg="ansigreen">running</style>'
                        if status == "running"
                        else '<style fg="ansiyellow">downloaded</style>'
                    )
                    yield Completion(
                        label,
                        start_position=-len(document.text_before_cursor),
                        display=PT_HTML(
                            f'<b>{label}</b> <style fg="ansigray">{m["backend"]}</style> {tag}'
                        ),
                    )

    try:
        from prompt_toolkit.shortcuts import radiolist_dialog
        from prompt_toolkit.styles import Style as PTStyle

        # Get system RAM for recommendations
        try:
            if sys.platform == "darwin":
                _ram = int(
                    subprocess.run(
                        ["sysctl", "-n", "hw.memsize"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    ).stdout.strip()
                ) // (1024**3)
            else:
                with open("/proc/meminfo") as _f:
                    for _l in _f:
                        if _l.startswith("MemTotal:"):
                            _ram = int(_l.split()[1]) // (1024 * 1024)
                            break
        except:
            _ram = 24
        metal_gb = int(_ram * 0.67)

        # Benchmark data from our tests (tok/s on M4 Pro 24GB)
        BENCHMARKS = {
            "gemma-4-26b-a4b-it-ud-q3_k_xl": {
                "tok_s": 49,
                "gpu_gb": 13.6,
                "quality": "★★★★★",
                "note": "Best overall on 24GB",
            },
            "qwen3.5-35b-a3b-ud-q2_k_xl": {
                "tok_s": 49,
                "gpu_gb": 12.0,
                "quality": "★★★★☆",
                "note": "More code detail",
            },
            "qwen3.5-4b-ud-q4_k_xl": {
                "tok_s": 50,
                "gpu_gb": 2.7,
                "quality": "★★★☆☆",
                "note": "Ultrafast, basic tasks",
            },
            "gemma-4-e4b-it-q4_k_m": {
                "tok_s": 38,
                "gpu_gb": 9.6,
                "quality": "★★★★☆",
                "note": "Audio + image",
            },
            "gemma4:e4b": {
                "tok_s": 38,
                "gpu_gb": 9.6,
                "quality": "★★★★☆",
                "note": "Audio + image",
            },
            "gemma4:e2b": {
                "tok_s": 57,
                "gpu_gb": 7.2,
                "quality": "★★☆☆☆",
                "note": "Speed demon",
            },
            "gemma4:26b": {
                "tok_s": 9,
                "gpu_gb": 16.8,
                "quality": "★★★★★",
                "note": "Slow on 24GB (swap)",
            },
            "qwen3.5:27b": {
                "tok_s": 5,
                "gpu_gb": 16.2,
                "quality": "★★★★☆",
                "note": "Dense — swap thrashing",
            },
        }

        # Build choices with recommendations
        choices = []
        for m in models:
            mid = m["id"]
            status = m.get("status", "running")
            is_current = m["id"] == MODEL and m["url"] == API_BASE

            # Look up benchmark
            bench_key = mid.lower().replace(".gguf", "")
            bench = BENCHMARKS.get(bench_key, {})
            if not bench:
                # Fuzzy match
                for bk, bv in BENCHMARKS.items():
                    if bk in bench_key or bench_key in bk:
                        bench = bv
                        break

            gpu = bench.get("gpu_gb", m.get("size_gb", 0))
            fits = gpu and gpu < metal_gb
            tok_s = bench.get("tok_s", 0)
            quality = bench.get("quality", "")
            note = bench.get("note", "")

            # Build label
            parts = []
            if is_current:
                parts.append("→ ")
            else:
                parts.append("  ")

            parts.append(mid)

            # Speed + fit indicator
            if tok_s:
                parts.append(f"  {tok_s} tok/s")
            if gpu:
                parts.append(f"  {gpu}GB")
            if quality:
                parts.append(f"  {quality}")

            # Status
            if status == "running":
                parts.append("  ✓ running")
            elif m.get("size_gb"):
                parts.append(f"  ↓ downloaded")

            # Fit warning
            if gpu and not fits:
                parts.append("  ⚠ won't fit")

            # Recommendation
            if note:
                parts.append(f"  ({note})")

            label = "".join(parts)
            choices.append((m, label))

        # Sort: running first, then by tok/s descending
        def _sort_key(item):
            m = item[0]
            bench_key = m["id"].lower().replace(".gguf", "")
            bench = BENCHMARKS.get(bench_key, {})
            if not bench:
                for bk, bv in BENCHMARKS.items():
                    if bk in bench_key or bench_key in bk:
                        bench = bv
                        break
            is_current = 0 if (m["id"] == MODEL and m["url"] == API_BASE) else 1
            is_running = 0 if m.get("status") == "running" else 1
            speed = -(bench.get("tok_s", 0))
            return (is_current, is_running, speed)

        choices.sort(key=_sort_key)

        dialog_style = PTStyle.from_dict(
            {
                "dialog": "bg:#1a1a2e",
                "dialog.body": "bg:#1a1a2e #e0e0e0",
                "dialog frame.label": "bg:#e07a5f #ffffff bold",
                "dialog shadow": "bg:#000000",
                "radiolist": "bg:#1a1a2e",
                "button": "bg:#81b29a #000000 bold",
                "button.focused": "bg:#e07a5f #ffffff bold",
            }
        )

        # Add disk space info
        disk_free = "?"
        hf_cache = "?"
        try:
            from localcoder.backends import get_disk_info

            di = get_disk_info()
            disk_free = f"{di['disk_free_gb']}GB"
            hf_cache = f"{di['hf_cache_gb']}GB"
        except Exception:
            pass

        # Add separator + trending models (live from HuggingFace)
        try:
            from localcoder.backends import (
                fetch_unsloth_top_models,
                fetch_hf_trending_models,
            )

            # Separator
            sep_entry = {"id": "__sep_trending__", "url": ""}
            choices.append(
                (
                    sep_entry,
                    "  ─── Trending (live from HuggingFace) ───────────────────",
                )
            )

            trending = fetch_unsloth_top_models(limit=6)
            for t in trending:
                if any(
                    t["label"].lower().replace("-", "")
                    in c[0]["id"].lower().replace("-", "")
                    for c in choices
                ):
                    continue
                dl = t["downloads"]
                dl_str = (
                    f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                )
                entry = {
                    "id": t["repo_id"],
                    "url": "hf_download",
                    "hf_repo": t["repo_id"],
                }
                label = f"  ★ {t['label']:<30}  {dl_str} dl  → download + install"
                choices.append((entry, label))

            # Most liked (different ranking)
            liked = fetch_hf_trending_models(limit=8, sort="likes")
            trending_repos = {t["repo_id"] for t in trending}
            liked = [l for l in liked if l["repo_id"] not in trending_repos][:4]
            if liked:
                sep2 = {"id": "__sep_liked__", "url": ""}
                choices.append(
                    (sep2, "  ─── Most liked ────────────────────────────────────────")
                )
                for lm in liked:
                    if any(
                        lm["label"].lower().replace("-", "")
                        in c[0]["id"].lower().replace("-", "")
                        for c in choices
                    ):
                        continue
                    dl = lm["downloads"]
                    dl_str = (
                        f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                    )
                    entry = {
                        "id": lm["repo_id"],
                        "url": "hf_download",
                        "hf_repo": lm["repo_id"],
                    }
                    label = f"  ♥ {lm['label']:<30}  {dl_str} dl  → download + install"
                    choices.append((entry, label))
        except Exception:
            pass

        result = radiolist_dialog(
            title="Select Model",
            text=f"RAM: {_ram}GB · GPU: ~{metal_gb}GB · Disk: {disk_free} free · Cache: {hf_cache} · ↑↓ arrows",
            values=choices,
            style=dialog_style,
        ).run()

        if result and result.get("id", "").startswith("__sep"):
            return None, None  # separator selected, ignore

        if result:
            # Handle HuggingFace download selection
            if result.get("url") == "hf_download":
                repo = result.get("hf_repo", result["id"])
                console.print(f"\n  [bold]Fetching quants for {repo}...[/]")
                try:
                    from localcoder.backends import simulate_hf_model

                    simulate_hf_model(repo)
                except Exception as e:
                    console.print(f"  [red]{e}[/]")
                return None, None  # don't switch yet — user needs to download first
            return result["id"], result["url"]
        return None, None

    except Exception:
        # Fallback to text prompt if dialog fails
        try:
            choice = pt_prompt(
                PT_HTML('<style fg="#81b29a" bold="true">  model▸ </style>'),
                completer=ModelCompleter(),
                complete_while_typing=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None, None

    if not choice or choice.lower() in ("q", "quit", "esc"):
        return None, None

    # Exact match
    for m in models:
        if choice == m["id"]:
            return m["id"], m["url"]

    # Fuzzy match
    for m in models:
        if choice.lower() in m["id"].lower():
            return m["id"], m["url"]

    console.print(f"  [red]Not found: {choice}[/]")
    return None, None


# ── Clipboard paste ──
def get_clipboard_image():
    """Get image from macOS clipboard, save to temp file, return path."""
    try:
        # Check if clipboard has image data
        r = subprocess.run(
            ["osascript", "-e", "the clipboard as «class PNGf»"],
            capture_output=True,
            timeout=3,
        )
        if r.returncode != 0:
            return None

        # Save clipboard image via Python
        tmp = os.path.join(CWD, ".localcoder-clipboard.png")
        subprocess.run(
            [
                "osascript",
                "-e",
                f'set f to open for access POSIX file "{tmp}" with write permission',
                "-e",
                "set eof f to 0",
                "-e",
                "write (the clipboard as «class PNGf») to f",
                "-e",
                "close access f",
            ],
            capture_output=True,
            timeout=5,
        )
        if os.path.isfile(tmp) and os.path.getsize(tmp) > 100:
            return tmp
    except:
        pass
    return None


# ── Tools ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return stdout+stderr. Use for: installing packages (npm install), running servers (node server.js &), testing (curl), git, and any system command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command to execute"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with content. ALWAYS use this to write code — never put code in chat messages. The path must be a filename like 'index.html', not a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to CWD (e.g. 'index.html', 'server.js')"},
                    "content": {"type": "string", "description": "Complete file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file and return its contents. Only read a file ONCE — don't re-read files you already have.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Find and replace text in an existing file. The old_text must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string", "description": "Exact text to find"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image from a text prompt using local AI (Flux). Saves the image to disk and displays it in the terminal. Call this BEFORE write_file when building pages that need images. Example: generate_image(prompt='cute cat icon, kawaii, flat design', filename='cat.png')",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Image description. Be specific: style, subject, colors. Example: 'minimalist mountain logo, blue gradient, white background'",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Output filename like 'hero.png' or 'icon-cat.png'. Must end in .png",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size WxH. Use 256x256 for icons, 512x512 for medium, 1024x1024 for large. Default: 512x512",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Quality steps. 2=fast, 4=good, 8=best. Default: 4",
                    },
                },
                "required": ["prompt", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_app",
            "description": "Open an HTML file in the browser, take a screenshot, and display it in the terminal. Use this AFTER writing an HTML file to verify it looks correct. If something looks wrong, fix it and preview again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the HTML file to preview (e.g. 'index.html')",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Use for finding info, docs, APIs. Do NOT use for finding images — use generate_image instead.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL and return its content as text.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": "Read a PDF file. Extracts text and page images.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to PDF file"},
                    "pages": {"type": "string", "description": "Page range: 'all', '1-3', '2,5'. Default: first 5"},
                },
                "required": ["path"],
            },
        },
    },
]

SNAPSHOT_DIR = os.path.join(CWD, ".localcoder-snapshots")

_last_snapshot = {}


def snapshot_file(path):
    """Save a backup before modifying an existing file. Dedupes within 30s."""
    full = os.path.join(CWD, path) if not os.path.isabs(path) else path
    if not os.path.isfile(full):
        return None
    # Don't snapshot the same file within 30 seconds
    now = time.time()
    if path in _last_snapshot and now - _last_snapshot[path] < 30:
        return None
    _last_snapshot[path] = now
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = path.replace("/", "__").replace("\\", "__")
    snap_path = os.path.join(SNAPSHOT_DIR, f"{ts}__{safe_name}")
    try:
        import shutil

        shutil.copy2(full, snap_path)
        logging.getLogger("localcoder").info(f"Snapshot: {path} → {snap_path}")
        # Clean old snapshots — keep max 20 per file
        all_snaps = sorted([s for s in os.listdir(SNAPSHOT_DIR) if safe_name in s])
        for old in all_snaps[:-20]:
            os.remove(os.path.join(SNAPSHOT_DIR, old))
        return snap_path
    except:
        return None


def list_snapshots(path=None):
    """List all snapshots, optionally filtered by filename."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return "No snapshots yet."
    snaps = sorted(os.listdir(SNAPSHOT_DIR), reverse=True)
    if path:
        safe = path.replace("/", "__").replace("\\", "__")
        snaps = [s for s in snaps if safe in s]
    if not snaps:
        return "No snapshots found."
    lines = []
    for i, s in enumerate(snaps[:15]):
        parts = s.split("__", 2)
        ts = parts[0] if parts else "?"
        fname = parts[-1] if len(parts) > 1 else s
        fp = os.path.join(SNAPSHOT_DIR, s)
        size = os.path.getsize(fp)
        lines.append(f"  [{i}] {ts} — {fname} ({size} bytes)")
    return "Snapshots:\n" + "\n".join(lines)


def restore_snapshot(index=0, path=None):
    """Restore a file from a snapshot."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return "No snapshots."
    snaps = sorted(os.listdir(SNAPSHOT_DIR), reverse=True)
    if path:
        safe = path.replace("/", "__").replace("\\", "__")
        snaps = [s for s in snaps if safe in s]
    if not snaps or index >= len(snaps):
        return "Snapshot not found."
    snap = snaps[index]
    parts = snap.split("__", 2)
    orig_name = parts[-1] if len(parts) > 1 else snap
    orig_path = orig_name.replace("__", "/")
    full = os.path.join(CWD, orig_path)
    snap_path = os.path.join(SNAPSHOT_DIR, snap)
    try:
        import shutil

        shutil.copy2(snap_path, full)
        return f"Restored {orig_path} from snapshot {parts[0]}"
    except Exception as e:
        return f"Restore failed: {e}"


def exec_tool(name, args):
    if name == "bash":
        cmd = args.get("command", "")
        # If downloading an image with curl, add browser user-agent to avoid blocks
        if "curl" in cmd and any(
            ext in cmd for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ):
            if "-A" not in cmd and "--user-agent" not in cmd and "-H" not in cmd:
                cmd = cmd.replace(
                    "curl ",
                    'curl -L -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" ',
                    1,
                )
        # Longer timeout for server starts and installs
        cmd_timeout = 120 if any(k in cmd for k in ("npm install", "pip install", "node server", "python3 -m http")) else 60
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, cwd=CWD, timeout=cmd_timeout
            )
            output = (r.stdout + r.stderr).strip()
            if not output:
                return "(no output)"
            # Tail-truncate: keep last N lines (errors are at the end)
            if len(output) > 4000:
                lines = output.splitlines()
                # Keep first 5 lines + last lines that fit in budget
                head = "\n".join(lines[:5])
                tail_lines = []
                tail_size = 0
                for line in reversed(lines[5:]):
                    if tail_size + len(line) + 1 > 3500:
                        break
                    tail_lines.insert(0, line)
                    tail_size += len(line) + 1
                return head + "\n...(truncated middle)...\n" + "\n".join(tail_lines)
            return output
        except subprocess.TimeoutExpired:
            return f"Command timed out after {cmd_timeout}s (normal for servers/long installs)."
    elif name == "write_file":
        path = args.get("path", "") or args.get("filename", "")
        content = args.get("content", "")
        # Auto-fix empty path — guess from content
        if not path or path.endswith("/"):
            if content.strip().startswith("<!DOCTYPE") or content.strip().startswith("<html"):
                path = "index.html"
            elif content.strip().startswith("{"):
                path = "data.json"
            elif "const " in content or "require(" in content:
                path = "server.js"
            else:
                path = "output.txt"
            logging.getLogger("localcoder").warning(f"write_file: empty path, auto-assigned '{path}'")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        if os.path.isdir(full):
            return f"Error: '{path}' is a directory, not a file. Provide a filename like '{path}index.html'"
        snapshot_file(path)  # backup before overwrite
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        lines = content.count("\n") + 1
        # If writing an image/binary, note the path for display
        if any(
            path.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ):
            return f"IMAGE:{full}|Written: {path} ({len(content)} bytes)"
        return f"Written: {path} ({lines} lines, {len(content)} chars)"
    elif name == "read_file":
        path = args.get("path", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        with open(full) as f:
            content = f.read()
        # Strip HTML for .html files to save context
        if path.endswith(".html") and "<html" in content[:200].lower():
            text = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:3000]
        return content[:5000]
    elif name == "edit_file":
        path = args.get("path", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        with open(full) as f:
            content = f.read()
        old = args.get("old_text", "")
        if old not in content:
            return "Error: old_text not found"
        snapshot_file(path)  # backup before edit
        new_content = content.replace(old, args.get("new_text", ""), 1)
        with open(full, "w") as f:
            f.write(new_content)
        # Show diff summary
        old_lines = content.count("\n")
        new_lines = new_content.count("\n")
        diff = new_lines - old_lines
        diff_str = f" ({'+' if diff > 0 else ''}{diff} lines)" if diff != 0 else ""
        return f"Edited: {path}{diff_str}"
    elif name == "fetch_url":
        url = args.get("url", "")
        try:
            # Auto-detect image URLs — download and display directly
            if any(
                url.lower().endswith(ext)
                for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")
            ):
                img_name = os.path.basename(url.split("?")[0])[:50] or "image.jpg"
                img_path = os.path.join(CWD, img_name)
                try:
                    dl = subprocess.run(
                        [
                            "curl",
                            "-fsSL",
                            "-A",
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                            "-o",
                            img_path,
                            url,
                        ],
                        capture_output=True,
                        timeout=15,
                    )
                    if os.path.isfile(img_path) and os.path.getsize(img_path) > 500:
                        # Validate magic bytes
                        with open(img_path, "rb") as _f:
                            hdr = _f.read(8)
                        if (
                            hdr[:2] == b"\xff\xd8"
                            or hdr[:4] == b"\x89PNG"
                            or hdr[:4] == b"GIF8"
                            or hdr[:4] == b"RIFF"
                        ):
                            show_image_inline(img_path)
                            sz = os.path.getsize(img_path) // 1024
                            return f"Image downloaded and displayed: {img_name} ({sz} KB)\nSaved to: {img_path}"
                        else:
                            os.unlink(img_path)
                            return f"Downloaded file is not a valid image (server returned HTML). Try a different URL."
                except:
                    pass

            # Use Jina Reader — reads full page, renders JS, returns markdown
            jina_url = f"https://r.jina.ai/{url}"
            req = urllib.request.Request(
                jina_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"}
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    full = resp.read().decode("utf-8", errors="replace")
                    if len(full) > 100:
                        good = _extract_image_candidates_from_text(full, source_url=url)
                        parts = [
                            f"Status: 200 (via Jina Reader) · {len(full)} chars total"
                        ]
                        # Put images FIRST so they don't get truncated
                        if good:
                            try:
                                show_image_url(
                                    good[0]["url"], max_width=50, max_height=12
                                )
                            except Exception:
                                pass
                            parts.append(
                                f"\n--- {len(good)} images found on this page ---"
                            )
                            parts.extend(candidate["url"] for candidate in good[:8])
                            parts.append("--- end images ---\n")
                        parts.append(full[:1500])
                        return "\n".join(parts)
            except:
                pass

            # Direct fallback
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read(10000).decode("utf-8", errors="replace")
                if "image" in ct:
                    return f"Status: {resp.status} · This is an image file ({ct}, {len(raw)} bytes). Use bash with 'curl -o filename.png {url}' to download it."
                if "html" in ct:
                    og = re.findall(
                        r'(?:property|name)="(?:og|twitter):image"[^>]*content="([^"]+)"',
                        raw,
                    )
                    imgs = re.findall(
                        r'https?://[^\s"\'<>]+\.(?:png|jpg|jpeg|webp|gif)', raw
                    )
                    all_imgs = _sort_image_candidates(
                        [
                            {"url": image_url, "title": "", "source": url}
                            for image_url in (og + imgs)
                        ]
                    )
                    text = re.sub(
                        r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL
                    )
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                    img_str = (
                        "\n\nImages:\n"
                        + "\n".join(candidate["url"] for candidate in all_imgs[:5])
                        if all_imgs
                        else ""
                    )
                    if all_imgs:
                        try:
                            show_image_url(
                                all_imgs[0]["url"], max_width=50, max_height=12
                            )
                        except Exception:
                            pass
                    return f"Status: {resp.status}\n{text[:1500]}{img_str}"
                return f"Status: {resp.status}\nType: {ct}\n{raw[:1500]}"
        except Exception as e:
            return f"Error: {e}"
    elif name == "web_search":
        query = args.get("query", "")
        is_image_query = any(
            w in query.lower()
            for w in [
                "image",
                "images",
                "logo",
                "photo",
                "photos",
                "screenshot",
                "picture",
                "pictures",
                "png",
                "jpg",
                "icon",
                "wallpaper",
                "unsplash",
                "official photo",
                "real photo",
                "صورة",
                "صور",
            ]
        )

        # Image search via ddgs package
        if is_image_query:
            try:
                from ddgs import DDGS

                results = list(
                    DDGS().images(
                        _rewrite_photo_search_query(query),
                        max_results=12,
                    )
                )
                candidates = _sort_image_candidates(
                    [
                        {
                            "url": r.get("image", ""),
                            "title": r.get("title", ""),
                            "source": r.get("url", ""),
                            "thumbnail": r.get("thumbnail", ""),
                        }
                        for r in results
                    ]
                )
                imgs = []
                for candidate in candidates[:6]:
                    imgs.append(
                        f"- {(candidate.get('title') or '')[:60]}\n  URL: {candidate.get('url', '')}\n  Source: {candidate.get('source', '')}"
                    )
                if imgs:
                    first_url = candidates[0].get("url", "") if candidates else ""
                    if first_url:
                        try:
                            show_image_url(first_url, max_width=50, max_height=12)
                        except Exception:
                            pass
                    return f"Image search results for '{query}':\n\n" + "\n\n".join(
                        imgs
                    )
            except ImportError:
                pass  # ddgs not installed, try unsplash fallback
            except Exception:
                pass

            # Unsplash fallback (no API key, direct source URLs)
            try:
                kw = urllib.parse.quote(
                    query.replace("image", "").replace("unsplash", "").strip()
                )
                imgs = []
                for i in range(5):
                    url = f"https://source.unsplash.com/random/800x600/?{kw}&sig={i}"
                    imgs.append(f"- Unsplash image {i + 1}\n  URL: {url}")
                return (
                    f"Unsplash images for '{query}':\n\n"
                    + "\n\n".join(imgs)
                    + "\n\nUse these URLs directly in <img src='URL'> tags."
                )
            except Exception:
                pass

        # Regular web search via ddgs package
        try:
            from ddgs import DDGS

            results = DDGS().text(query, max_results=5)
            formatted = []
            for r in results:
                formatted.append(
                    f"[{r.get('title', '')}]({r.get('href', '')})\n{r.get('body', '')[:150]}"
                )
            return (
                f"Search results for '{query}':\n\n" + "\n\n".join(formatted)
                if formatted
                else f"No results for '{query}'"
            )
        except ImportError:
            return f"Search requires 'ddgs' package. Install: pip install ddgs"
        except Exception as e:
            return f"Search error: {e}"
    elif name == "computer_use":
        action = args.get("action", "screenshot")
        import base64 as _b64

        try:
            # Execute action FIRST (before screenshot)
            action_result = ""
            # Track last opened app for screenshot focus
            if not hasattr(exec_tool, "_last_app"):
                exec_tool._last_app = "Google Chrome"

            if action == "screenshot":
                # Auto-convert repeated screenshots to scroll
                if not hasattr(exec_tool, "_screenshot_count"):
                    exec_tool._screenshot_count = 0
                exec_tool._screenshot_count += 1
                if exec_tool._screenshot_count > 1:
                    action = "scroll"  # force scroll instead of redundant screenshot
                    console.print(
                        f"  [dim yellow]Auto-scrolling instead of repeated screenshot[/]"
                    )

            if action == "screenshot" or action.startswith("scroll"):
                # Bring last app to front
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'tell application "{exec_tool._last_app}" to activate',
                    ],
                    capture_output=True,
                    timeout=3,
                )
                time.sleep(0.3)
                if action.startswith("scroll"):
                    direction = "down"
                    if "up" in action:
                        direction = "up"
                    # Click content area to ensure page has focus
                    subprocess.run(
                        ["cliclick", "c:400,500"], capture_output=True, timeout=3
                    )
                    time.sleep(0.2)
                    # Use space bar for scroll down (works in all browsers)
                    # Use shift+space for scroll up
                    # 3 space presses for a full page scroll
                    for _ in range(3):
                        if direction == "down":
                            subprocess.run(
                                [
                                    "osascript",
                                    "-e",
                                    'tell application "System Events" to keystroke space',
                                ],
                                capture_output=True,
                                timeout=3,
                            )
                        else:
                            subprocess.run(
                                [
                                    "osascript",
                                    "-e",
                                    'tell application "System Events" to keystroke space using shift down',
                                ],
                                capture_output=True,
                                timeout=3,
                            )
                        time.sleep(0.3)
                    time.sleep(0.5)
                    action_result = f"Scrolled {direction}"
                else:
                    action_result = "Screenshot taken (see below)"
            elif action.startswith("click:"):
                exec_tool._screenshot_count = 0  # reset on non-screenshot action
                coords = action.split(":", 1)[1]
                x, y = (
                    int(coords.split(",")[0].strip()),
                    int(coords.split(",")[1].strip()),
                )
                # Get screen size for coordinate conversion
                try:
                    _scr = subprocess.run(
                        [
                            "osascript",
                            "-e",
                            'tell application "Finder" to get bounds of window of desktop',
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    _parts = _scr.stdout.strip().split(", ")
                    scr_w, scr_h = int(_parts[2]), int(_parts[3])
                except:
                    scr_w, scr_h = 1512, 982  # fallback M4 Pro
                # If coordinates are already in screen space (from tars_screenshot),
                # use them directly. Otherwise convert from 0-1000 normalized grid.
                if x > 1000 or y > 1000:
                    sx, sy = x, y  # already screen coordinates from UI-TARS
                else:
                    sx = int(x * scr_w / 1000)
                    sy = int(y * scr_h / 1000)
                # Bring last app to front before clicking
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'tell application "{exec_tool._last_app}" to activate',
                    ],
                    capture_output=True,
                    timeout=3,
                )
                time.sleep(0.2)
                subprocess.run(
                    ["cliclick", f"c:{sx},{sy}"], capture_output=True, timeout=3
                )
                action_result = f"Clicked at screen ({sx},{sy}) from input ({x},{y})"
                time.sleep(0.3)
            elif action.startswith("type:"):
                text = action.split(":", 1)[1]
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'tell application "System Events" to keystroke "{text}"',
                    ],
                    capture_output=True,
                    timeout=3,
                )
                action_result = f"Typed: {text}"
                # Auto-URL-Enter: detect URL patterns and press Enter
                _url_exts = [
                    ".com",
                    ".org",
                    ".net",
                    ".io",
                    ".dev",
                    ".ai",
                    ".edu",
                    ".gov",
                ]
                if any(ext in text.lower() for ext in _url_exts):
                    time.sleep(0.2)
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            'tell application "System Events" to key code 36',
                        ],
                        capture_output=True,
                        timeout=3,
                    )
                    action_result += " (auto-pressed Enter for URL)"
                time.sleep(0.3)
            elif action.startswith("key:"):
                key = action.split(":", 1)[1].strip()
                # Normalize key names: underscore→hyphen, common aliases
                key = (
                    key.replace("_", "-")
                    .replace("escape", "esc")
                    .replace("enter", "return")
                )
                # Map key names to AppleScript key codes
                _key_map = {
                    "return": 36,
                    "esc": 53,
                    "tab": 48,
                    "delete": 51,
                    "space": 49,
                    "arrow-up": 126,
                    "arrow-down": 125,
                    "arrow-left": 123,
                    "arrow-right": 124,
                    "page-down": 121,
                    "page-up": 116,
                    "home": 115,
                    "end": 119,
                    "enter": 36,
                }
                kc = _key_map.get(key)
                if kc:
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f'tell application "System Events" to key code {kc}',
                        ],
                        capture_output=True,
                        timeout=3,
                    )
                else:
                    # Single character key
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f'tell application "System Events" to keystroke "{key}"',
                        ],
                        capture_output=True,
                        timeout=3,
                    )
                action_result = f"Pressed key: {key}"
                time.sleep(0.3)
            elif action.startswith("hotkey:"):
                keys = action.split(":", 1)[1].strip()
                # Convert cmd+a to osascript
                parts = keys.split("+")
                key_char = parts[-1]
                modifiers = [p for p in parts[:-1]]
                mod_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'tell application "System Events" to keystroke "{key_char}"{mod_str}',
                    ],
                    capture_output=True,
                    timeout=5,
                )
                action_result = f"Hotkey: {keys}"
                time.sleep(0.3)
            elif action.startswith("open:"):
                target = action.split(":", 1)[1].strip()
                if target.startswith("http") or "." in target and "/" in target:
                    # URL — open in Chrome specifically
                    url = target if target.startswith("http") else f"https://{target}"
                    subprocess.run(
                        ["open", "-a", "Google Chrome", url],
                        capture_output=True,
                        timeout=5,
                    )
                    action_result = f"Opened {url} in Chrome"
                else:
                    # App name
                    subprocess.run(
                        ["open", "-a", target], capture_output=True, timeout=5
                    )
                    action_result = f"Opened {target}"
                time.sleep(2)
                # Track and bring to front
                app_name = (
                    "Google Chrome" if ("http" in target or "." in target) else target
                )
                exec_tool._last_app = app_name
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app_name}" to activate'],
                    capture_output=True,
                    timeout=3,
                )
                time.sleep(0.5)
            elif action.startswith("wait:"):
                secs = float(action.split(":", 1)[1])
                time.sleep(min(secs, 5))
                action_result = f"Waited {secs}s"
            else:
                action_result = f"Unknown action: {action}"

            # ADK pattern: EVERY action captures current_state (screenshot)
            # Bring target app to front before screenshotting
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "{exec_tool._last_app}" to activate',
                ],
                capture_output=True,
                timeout=3,
            )
            time.sleep(0.5)

            ss_path = "/tmp/localcoder-screen.png"
            subprocess.run(
                ["screencapture", "-D1", "-x", ss_path], capture_output=True, timeout=5
            )
            subprocess.run(
                ["sips", "-Z", "1000", ss_path, "--out", ss_path],
                capture_output=True,
                timeout=5,
            )

            # Display inline
            show_image_inline(ss_path)

            # Auto-read the screenshot content (like ADK's current_state)
            # Use dedicated vision model (UI-TARS) if configured, otherwise fall back to main model
            screen_desc = ""
            _vision_api = VISION_API_BASE or API_BASE
            _vision_model = VISION_MODEL or MODEL
            try:
                img_data = _b64.b64encode(open(ss_path, "rb").read()).decode()
                vision_payload = json.dumps(
                    {
                        "model": _vision_model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Describe this screenshot for a computer-use agent. List ALL visible UI elements with their approximate (x, y) coordinates on a 0-1000 scale where (0,0) is top-left and (1000,1000) is bottom-right. Format each element as: - element_description (x, y)\nFocus on: buttons, links, text fields, search bars, tabs, menus, icons. Also briefly describe the main content visible on screen.",
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{img_data}"
                                        },
                                    },
                                ],
                            }
                        ],
                        "max_tokens": 1500,
                    }
                ).encode()
                vision_req = urllib.request.Request(
                    f"{_vision_api}/chat/completions",
                    data=vision_payload,
                    headers={"Content-Type": "application/json"},
                )
                vision_resp = urllib.request.urlopen(vision_req, timeout=90)
                vision_r = json.loads(vision_resp.read())
                vision_msg = vision_r["choices"][0]["message"]
                screen_desc = vision_msg.get("content", "") or vision_msg.get(
                    "reasoning_content", ""
                )
                # Strip reasoning preamble — find where actual data starts
                for marker in ["- ", "•", "**", "1.", "```", "{"]:
                    idx = screen_desc.find(marker)
                    if idx > 0 and idx < 300:
                        screen_desc = screen_desc[idx:]
                        break
                # Remove common preamble sentences
                for phrase in [
                    "I need to",
                    "The user wants",
                    "Let me analyze",
                    "Let me describe",
                    "I will now",
                ]:
                    if screen_desc.lstrip().startswith(phrase):
                        nl = screen_desc.find("\n\n")
                        if nl > 0:
                            screen_desc = screen_desc[nl + 2 :]
                screen_desc = screen_desc[:2500]
            except:
                screen_desc = "(could not read screen)"

            return f"{action_result}\n\n[SCREEN CONTENT — UI elements with (x,y) coordinates on 0-1000 scale]:\n{screen_desc}\n\n[NEXT ACTION]: Use action:click:x,y to click a UI element (use the coordinates above). Use action:type:text to type. Use action:scroll to see more. Use action:open:URL to navigate. Do NOT call action:screenshot again — every action already captures the screen."

        except Exception as e:
            return f"Computer use error: {e}"

    elif name == "tars_screenshot":
        # ── UI-TARS vision grounding tool ──
        # Takes screenshot, sends to UI-TARS on VISION_API_BASE, returns recommended action + coordinates
        import base64 as _b64, math as _math

        task_ctx = args.get("task_context", "complete the user's task")
        _tars_api = VISION_API_BASE or "http://127.0.0.1:8090/v1"
        _tars_model = VISION_MODEL or "UI-TARS-1.5-7B"

        try:
            # 1. Bring target app to front
            if not hasattr(exec_tool, "_last_app"):
                exec_tool._last_app = "Google Chrome"
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "{exec_tool._last_app}" to activate',
                ],
                capture_output=True,
                timeout=3,
            )
            time.sleep(0.5)

            # 2. Screenshot main display only (-D1), resize to 1000px
            ss_path = "/tmp/localcoder-tars-screen.png"
            ss_small = "/tmp/localcoder-tars-sm.png"
            subprocess.run(
                ["screencapture", "-D1", "-x", ss_path], capture_output=True, timeout=5
            )
            subprocess.run(
                ["sips", "-Z", "1000", ss_path, "--out", ss_small],
                capture_output=True,
                timeout=5,
            )

            # Display inline
            show_image_inline(ss_small)

            # 3. Compute smart_resize grid (Qwen2.5-VL vision encoder)
            try:
                _sips = subprocess.run(
                    ["sips", "-g", "pixelWidth", "-g", "pixelHeight", ss_small],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                _iw, _ih = 1000, 649
                for _l in _sips.stdout.split("\n"):
                    if "pixelWidth" in _l:
                        _iw = int(_l.split(":")[-1])
                    elif "pixelHeight" in _l:
                        _ih = int(_l.split(":")[-1])

                def _smart_resize(
                    h, w, factor=28, mn=100 * 28 * 28, mx=16384 * 28 * 28
                ):
                    hb = max(factor, round(h / factor) * factor)
                    wb = max(factor, round(w / factor) * factor)
                    if hb * wb > mx:
                        b = _math.sqrt((h * w) / mx)
                        hb = _math.floor(h / b / factor) * factor
                        wb = _math.floor(w / b / factor) * factor
                    elif hb * wb < mn:
                        b = _math.sqrt(mn / (h * w))
                        hb = _math.ceil(h * b / factor) * factor
                        wb = _math.ceil(w * b / factor) * factor
                    return hb, wb

                _model_h, _model_w = _smart_resize(_ih, _iw)
            except Exception:
                _model_w, _model_h = 1008, 644  # fallback

            # Get actual screen size
            try:
                _scr = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "Finder" to get bounds of window of desktop',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                _parts = _scr.stdout.strip().split(", ")
                _scr_w, _scr_h = int(_parts[2]), int(_parts[3])
            except Exception:
                _scr_w, _scr_h = 1512, 982

            # 4. Build action history from conversation
            _action_hist = ""
            if hasattr(exec_tool, "_tars_history"):
                if exec_tool._tars_history:
                    _action_hist = (
                        "\nPrevious actions:\n"
                        + "\n".join(
                            f"  {i + 1}. {a}"
                            for i, a in enumerate(exec_tool._tars_history[-8:])
                        )
                        + "\n"
                    )
            else:
                exec_tool._tars_history = []

            # 5. Send to UI-TARS
            _tars_system = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space
click(start_box='(x1,y1)')
left_double(start_box='(x1,y1)')
right_single(start_box='(x1,y1)')
drag(start_box='(x1,y1)', end_box='(x2,y2)')
hotkey(key='ctrl c')
type(content='xxx')
scroll(start_box='(x1,y1)', direction='down or up or right or left')
wait()
finished(content='xxx')

## Note
- Use English in Thought part.
- Write a small plan and finally summarize your next action in one sentence in Thought part.
"""
            img_b64 = _b64.b64encode(open(ss_small, "rb").read()).decode()
            tars_payload = json.dumps(
                {
                    "model": _tars_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": _tars_system
                            + f"\n## User Instruction\n{task_ctx}",
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{img_b64}"
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": f"{_action_hist}Current screenshot attached. What is the next action?",
                                },
                            ],
                        },
                    ],
                    "temperature": 0.1,
                    "max_tokens": 400,
                }
            ).encode()
            tars_req = urllib.request.Request(
                f"{_tars_api}/chat/completions",
                data=tars_payload,
                headers={"Content-Type": "application/json"},
            )
            tars_resp = urllib.request.urlopen(tars_req, timeout=120)
            tars_r = json.loads(tars_resp.read())
            tars_text = tars_r["choices"][0]["message"]["content"]
            console.print(f"  [dim cyan]UI-TARS: {tars_text[:200]}[/]")

            # 6. Parse UI-TARS response — extract Thought and Action
            import re as _re

            _thought = ""
            _action_raw = ""
            _t_match = _re.search(
                r"Thought:\s*(.+?)(?=Action:|$)", tars_text, _re.DOTALL
            )
            if _t_match:
                _thought = _t_match.group(1).strip()
            _a_match = _re.search(r"Action:\s*(.+?)(?:\n\n|$)", tars_text, _re.DOTALL)
            if _a_match:
                _action_raw = _a_match.group(1).strip()

            # Track history
            if _action_raw:
                exec_tool._tars_history.append(_action_raw)

            # 7. Convert UI-TARS coordinates to computer_use format
            _instructions = []
            if not _action_raw:
                _instructions.append(
                    "UI-TARS could not determine an action. Try scrolling or waiting."
                )
            else:
                # Parse click
                _click_m = _re.search(
                    r"click\(start_box=['\"][\(\[]([\d.]+),\s*([\d.]+)[\)\]]['\"]",
                    _action_raw,
                )
                if _click_m:
                    _mx, _my = float(_click_m.group(1)), float(_click_m.group(2))
                    _sx = int(_mx / _model_w * _scr_w)
                    _sy = int(_my / _model_h * _scr_h)
                    _instructions.append(
                        f"CLICK at screen coordinates ({_sx},{_sy}) — use: computer_use action:click:{_sx},{_sy}"
                    )

                # Parse type
                _type_m = _re.search(r"type\(content=['\"](.+?)['\"]\)", _action_raw)
                if _type_m:
                    _txt = _type_m.group(1)
                    _instructions.append(
                        f"TYPE text: {_txt} — use: computer_use action:type:{_txt}"
                    )
                    # Auto-URL-Enter detection
                    _url_exts = [
                        ".com",
                        ".org",
                        ".net",
                        ".io",
                        ".dev",
                        ".ai",
                        ".edu",
                        ".gov",
                    ]
                    if any(ext in _txt.lower() for ext in _url_exts):
                        _instructions.append(
                            "This looks like a URL — also press Enter after: computer_use action:key:return"
                        )

                # Parse hotkey
                _hk_m = _re.search(r"hotkey\(key=['\"](.+?)['\"]\)", _action_raw)
                if _hk_m:
                    _keys = _hk_m.group(1).strip()
                    # Convert "ctrl c" → "cmd+c" for macOS
                    _keys_mac = _keys.replace("ctrl ", "cmd+").replace(" ", "+")
                    _instructions.append(
                        f"HOTKEY: {_keys} — use: computer_use action:hotkey:{_keys_mac}"
                    )

                # Parse scroll
                _scr_m = _re.search(
                    r"scroll\(.*?direction=['\"](\w+)['\"]", _action_raw
                )
                if _scr_m:
                    _dir = _scr_m.group(1)
                    _instructions.append(
                        f"SCROLL {_dir} — use: computer_use action:scroll {_dir}"
                    )

                # Parse drag
                _drag_m = _re.search(
                    r"drag\(start_box=['\"][\(\[]([\d.]+),\s*([\d.]+)[\)\]]['\"],\s*end_box=['\"][\(\[]([\d.]+),\s*([\d.]+)[\)\]]['\"]",
                    _action_raw,
                )
                if _drag_m:
                    _dx1 = int(float(_drag_m.group(1)) / _model_w * _scr_w)
                    _dy1 = int(float(_drag_m.group(2)) / _model_h * _scr_h)
                    _dx2 = int(float(_drag_m.group(3)) / _model_w * _scr_w)
                    _dy2 = int(float(_drag_m.group(4)) / _model_h * _scr_h)
                    _instructions.append(
                        f"DRAG from ({_dx1},{_dy1}) to ({_dx2},{_dy2})"
                    )

                # Parse wait
                if "wait()" in _action_raw:
                    _instructions.append("WAIT — use: computer_use action:wait:3")

                # Parse finished
                _fin_m = _re.search(r"finished\(content=['\"](.+?)['\"]\)", _action_raw)
                if _fin_m:
                    _instructions.append(f"TASK COMPLETE: {_fin_m.group(1)}")

                # Parse double-click
                _dbl_m = _re.search(
                    r"left_double\(start_box=['\"][\(\[]([\d.]+),\s*([\d.]+)[\)\]]['\"]",
                    _action_raw,
                )
                if _dbl_m:
                    _mx, _my = float(_dbl_m.group(1)), float(_dbl_m.group(2))
                    _sx = int(_mx / _model_w * _scr_w)
                    _sy = int(_my / _model_h * _scr_h)
                    _instructions.append(
                        f"DOUBLE-CLICK at ({_sx},{_sy}) — use: computer_use action:click:{_sx},{_sy} (twice)"
                    )

            _result = f"[UI-TARS Vision Analysis]\n"
            _result += f"Thought: {_thought}\n"
            _result += f"Raw action: {_action_raw}\n"
            _result += f"Grid: {_model_w}x{_model_h} → Screen: {_scr_w}x{_scr_h}\n\n"
            _result += "Recommended actions:\n" + "\n".join(
                f"  → {inst}" for inst in _instructions
            )
            _result += "\n\nIMPORTANT: Use the EXACT screen coordinates above with computer_use. Do NOT call tars_screenshot again until you've executed the recommended action."

            return _result

        except Exception as e:
            import traceback

            return f"tars_screenshot error: {e}\n{traceback.format_exc()}"

    elif name == "generate_image":
        prompt_text = args.get("prompt", "")
        filename = args.get("filename", "generated-image.png")
        size = args.get("size", "512x512")
        steps = args.get("steps", 4)
        if not prompt_text:
            return "Error: prompt is required"
        if not any(filename.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp")):
            filename += ".png"
        out_path = os.path.join(CWD, filename)

        # Try local image server first (localfit at :8189)
        local_endpoint = os.environ.get("LOCALFIT_IMAGE_ENDPOINT", "http://127.0.0.1:8189")
        try:
            import base64 as _b64gen
            payload = json.dumps({"prompt": prompt_text, "size": size, "steps": steps}).encode()
            req = urllib.request.Request(
                f"{local_endpoint}/v1/images/generations",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            console.print(f"  [dim]Generating image locally ({size}, {steps} steps)…[/]")
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
            elapsed = time.time() - t0

            if "error" in result:
                return f"Error: {result['error'].get('message', 'unknown')}"

            b64 = result["data"][0]["b64_json"]
            img_bytes = _b64gen.b64decode(b64)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(img_bytes)

            show_image_inline(out_path)
            sz = len(img_bytes) // 1024
            return f"IMAGE:{out_path}|Generated: {filename} ({sz} KB, {elapsed:.1f}s)\nPrompt: {prompt_text}"

        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            # Local server not running — fall back to remote
            logging.getLogger("localcoder").info("Local image server not available, trying remote")

        # Fallback: remote Replicate endpoint
        try:
            encoded = urllib.parse.quote(prompt_text)
            gen_url = f"https://fast-flux-demo.replicate.workers.dev/api/generate-image?text={encoded}"
            console.print(f"  [dim]Generating image (remote)…[/]")
            r = subprocess.run(
                [
                    "curl", "-fsSL",
                    "-H", "User-Agent: Mozilla/5.0",
                    "-H", "Referer: https://fast-flux-demo.replicate.workers.dev/",
                    "-o", out_path,
                    gen_url,
                ],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0:
                return f"Error: image generation failed (curl rc={r.returncode}): {r.stderr.decode('utf-8', errors='replace')[:200]}"
            if not os.path.isfile(out_path) or os.path.getsize(out_path) < 500:
                return "Error: image generation returned empty or invalid response"
            with open(out_path, "rb") as _f:
                hdr = _f.read(8)
            if not (hdr[:2] == b"\xff\xd8" or hdr[:4] == b"\x89PNG" or hdr[:4] == b"RIFF" or hdr[:4] == b"GIF8"):
                os.unlink(out_path)
                return "Error: server returned non-image response. Try: localfit serve-image klein-4b"
            show_image_inline(out_path)
            sz = os.path.getsize(out_path) // 1024
            return f"IMAGE:{out_path}|Generated: {filename} ({sz} KB)\nPrompt: {prompt_text}"
        except subprocess.TimeoutExpired:
            return "Error: image generation timed out (30s). Try a simpler prompt."
        except Exception as e:
            return f"Error generating image: {e}"
    elif name == "read_pdf":
        path = args.get("path", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        if not os.path.isfile(full):
            return f"Error: PDF not found: {full}"

        pages_arg = args.get("pages", "1-5")
        tmp_dir = os.path.join(CWD, ".localcoder-pdf-tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        import shutil as _shutil

        try:
            # Get page count
            info = subprocess.run(
                ["pdfinfo", full], capture_output=True, text=True, timeout=5
            )
            total_pages = 0
            for line in info.stdout.split("\n"):
                if line.startswith("Pages:"):
                    total_pages = int(line.split(":")[1].strip())
                    break

            # Parse page range
            if pages_arg == "all":
                page_list = list(range(1, min(total_pages + 1, 21)))  # cap at 20
            elif "-" in pages_arg:
                start, end = pages_arg.split("-")
                page_list = list(range(int(start), min(int(end) + 1, total_pages + 1)))
            elif "," in pages_arg:
                page_list = [int(p) for p in pages_arg.split(",")]
            else:
                page_list = [int(pages_arg)]

            # Extract text
            text_result = subprocess.run(
                [
                    "pdftotext",
                    "-f",
                    str(page_list[0]),
                    "-l",
                    str(page_list[-1]),
                    full,
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            text_content = text_result.stdout[:3000]

            # Convert pages to images for vision
            page_images = []
            for pg in page_list[:5]:  # max 5 page images
                img_prefix = os.path.join(tmp_dir, f"page_{pg}")
                subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        str(pg),
                        "-l",
                        str(pg),
                        "-r",
                        "150",
                        "-png",
                        full,
                        img_prefix,
                    ],
                    capture_output=True,
                    timeout=10,
                )
                # pdftoppm outputs page_N-01.png
                for f in os.listdir(tmp_dir):
                    if f.startswith(f"page_{pg}") and f.endswith(".png"):
                        img_path = os.path.join(tmp_dir, f)
                        page_images.append(img_path)
                        # Display inline
                        show_image_inline(img_path)
                        break

            # Build result with image references
            result_parts = [
                f"PDF: {os.path.basename(full)} ({total_pages} pages)",
                f"Showing pages: {','.join(str(p) for p in page_list)}",
                f"\n--- TEXT CONTENT ---\n{text_content}",
            ]
            if page_images:
                result_parts.append(
                    f"\n--- {len(page_images)} page images rendered (displayed inline in terminal) ---"
                )
                # Include base64 so vision models can see the pages
                import base64 as _b64

                for img in page_images:
                    try:
                        img_b64 = _b64.b64encode(open(img, "rb").read()).decode()
                        sz_kb = os.path.getsize(img) // 1024
                        result_parts.append(
                            f"[Image: {os.path.basename(img)} ({sz_kb}KB) — base64 attached for vision]"
                        )
                    except:
                        result_parts.append(f"Page image: {img}")

            return "\n".join(result_parts)

        except Exception as e:
            return f"Error reading PDF: {e}"
        finally:
            # Cleanup old tmp files (keep last 5 mins)
            try:
                import time as _time

                for f in os.listdir(tmp_dir):
                    fp = os.path.join(tmp_dir, f)
                    if _time.time() - os.path.getmtime(fp) > 300:
                        os.unlink(fp)
            except:
                pass

    elif name == "preview_app":
        path = args.get("path", "index.html")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        if not os.path.isfile(full):
            return f"Error: file not found: {full}"

        screenshot_path = os.path.join(CWD, ".localcoder-preview.png")
        try:
            import shutil as _prev_shutil

            # Method 1: Use wkhtml if available (headless, no browser needed)
            wk = _prev_shutil.which("wkhtmltoimage")
            if wk:
                r = subprocess.run(
                    [wk, "--quality", "80", "--width", "1200", f"file://{full}", screenshot_path],
                    capture_output=True, timeout=15,
                )
                if os.path.isfile(screenshot_path) and os.path.getsize(screenshot_path) > 500:
                    show_image_inline(screenshot_path)
                    return f"Preview screenshot saved. The page renders correctly at {path}"

            # Method 2: Open in browser + use screencapture (macOS)
            subprocess.Popen(["open", full], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)  # Wait for browser to render

            # Take screenshot of the screen
            r = subprocess.run(
                ["screencapture", "-x", "-C", screenshot_path],
                capture_output=True, timeout=10,
            )
            if os.path.isfile(screenshot_path) and os.path.getsize(screenshot_path) > 500:
                show_image_inline(screenshot_path)
                sz = os.path.getsize(screenshot_path) // 1024
                return f"Preview: opened {path} in browser and captured screenshot ({sz}KB). Check the terminal image above — if something looks wrong, fix the code and preview again."

            return f"Opened {path} in browser. Could not capture screenshot automatically."
        except Exception as e:
            return f"Preview: opened {path} in browser. Screenshot failed: {e}"

    # Common E4B hallucinated tool names — redirect to real tools
    TOOL_ALIASES = {
        "generate_content": "generate_image",
        "create_image": "generate_image",
        "make_image": "generate_image",
        "image_generate": "generate_image",
        "create_file": "write_file",
        "save_file": "write_file",
        "run_command": "bash",
        "shell": "bash",
        "execute": "bash",
        "search": "web_search",
        "search_web": "web_search",
        "open_browser": "preview_app",
        "screenshot": "preview_app",
        "view_page": "preview_app",
    }
    if name in TOOL_ALIASES:
        real_name = TOOL_ALIASES[name]
        logging.getLogger("localcoder").info(f"Tool alias: {name} → {real_name}")
        return exec_tool(real_name, args)

    # MCP tool dispatch — any tool starting with mcp__
    mcp_mgr = get_mcp_manager()
    if mcp_mgr.is_mcp_tool(name):
        return mcp_mgr.call_tool(name, args)

    return f"Error: Unknown tool '{name}'. Available tools: bash, write_file, read_file, edit_file, generate_image, preview_app, web_search, fetch_url"


def estimate_tokens(text):
    return len(str(text)) // 4


def summarize_tool_result(content, fname):
    """Smart truncation based on tool type"""
    if not content or len(content) < 300:
        return content
    # Bash: keep first/last lines (errors are usually at the end)
    if fname == "bash":
        lines = content.split("\n")
        if len(lines) > 8:
            return (
                "\n".join(lines[:4])
                + f"\n... ({len(lines) - 8} lines omitted) ...\n"
                + "\n".join(lines[-4:])
            )
        return content[:600]
    # Search: keep first 3 results only
    if fname == "web_search":
        results = content.split("\n\n")
        return "\n\n".join(results[:4])[:600]
    # File reads: keep first chunk
    if fname == "read_file":
        return content[:500] + "...(truncated)" if len(content) > 500 else content
    # Everything else
    return content[:400] + "...(truncated)" if len(content) > 400 else content


def compress_messages(messages, max_tokens=12000):
    """Smart context compression with structured compaction.

    Uses LLM-based structured summarization (Goal/Discoveries/Accomplished/Files)
    instead of naive 80-char truncation. Falls back gracefully if LLM call fails.
    """
    if not messages:
        return messages

    # First pass: truncate verbose tool results
    for msg in messages[1:]:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            fname = "unknown"
            msg["content"] = summarize_tool_result(msg.get("content", ""), fname)

    total = estimate_tokens(json.dumps(messages))
    if total <= max_tokens:
        return messages

    # Delegate to structured compaction module
    return smart_compress_messages(messages, max_tokens=max_tokens,
                                   api_base=API_BASE, model=MODEL)


def chat_api(messages, spinner=None):
    """Call the LLM API with streaming.

    Streams text content live to console, accumulates tool calls.
    Returns a compatible response dict for agent_loop.
    """
    before = len(messages)
    messages = compress_messages(messages)
    tokens_est = estimate_tokens(json.dumps(messages))
    if before != len(messages):
        logging.getLogger("localcoder").info(
            f"Compressed {before} → {len(messages)} msgs (~{tokens_est} tokens)"
        )
    else:
        logging.getLogger("localcoder").debug(
            f"API call: {len(messages)} msgs, ~{tokens_est} tokens"
        )

    body = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "temperature": 1.0,
        "top_p": 0.95,
        "stream": True,
    }
    if REASONING_EFFORT != "medium":
        body["reasoning_effort"] = REASONING_EFFORT
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )

    content_parts = []
    reasoning_parts = []
    tool_calls = {}  # index → {id, function: {name, arguments}}
    finish_reason = None
    usage = {}
    timings = {}
    model_name = MODEL
    streaming_started = False
    token_count = 0
    stream_started_at = time.time()

    last_chunk_at = time.time()
    idle_timeout = 180  # seconds — finalize partial args if LLM stalls

    with urllib.request.urlopen(req, timeout=300) as resp:
        # Set socket timeout for idle detection
        try:
            resp.fp.raw._sock.settimeout(idle_timeout)
        except Exception:
            pass

        for raw_line in resp:
            last_chunk_at = time.time()
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
            finish_reason = (
                chunk.get("choices", [{}])[0].get("finish_reason") or finish_reason
            )
            model_name = chunk.get("model", model_name)

            # Usage info (llama.cpp sends it in the last chunk)
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("timings"):
                timings = chunk["timings"]

            def _kill_spinner():
                """Fully stop the spinner and clear its terminal line."""
                nonlocal spinner
                if spinner:
                    try:
                        if spinner._live is not None:
                            spinner._live.stop()
                            spinner._live = None
                    except Exception:
                        pass
                    spinner = None
                    # Clear the spinner line and move cursor
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

            # Keep reasoning buffered so we can render it as a clean card
            # instead of the old narrow ANSI box.
            reasoning_chunk = delta.get("reasoning_content", "")
            if reasoning_chunk:
                reasoning_parts.append(reasoning_chunk)
                token_count += 1

            # Stream text content live (normal style)
            text_chunk = delta.get("content", "")
            if text_chunk:
                if not streaming_started:
                    streaming_started = True
                    _kill_spinner()
                    reasoning_text = "".join(reasoning_parts).strip()
                    if reasoning_text and REASONING_EFFORT != "none":
                        console.print(
                            _render_reasoning_panel(
                                reasoning_text,
                                stream_started_at,
                            )
                        )
                        console.print()
                    sys.stdout.write("  ")
                    sys.stdout.flush()
                sys.stdout.write(text_chunk)
                sys.stdout.flush()
                content_parts.append(text_chunk)
                token_count += 1

            # Accumulate tool calls
            for tc_delta in delta.get("tool_calls", []):
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": tc_delta.get("id", f"call_{idx}"),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc_delta.get("function", {}).get("name"):
                    tool_calls[idx]["function"]["name"] = tc_delta["function"]["name"]
                if tc_delta.get("function", {}).get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += tc_delta["function"][
                        "arguments"
                    ]

    reasoning = "".join(reasoning_parts)
    if not streaming_started:
        _kill_spinner()
        if reasoning.strip() and REASONING_EFFORT != "none":
            console.print(
                _render_reasoning_panel(
                    reasoning,
                    stream_started_at,
                )
            )
            console.print()

    if streaming_started:
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Build compatible response dict
    content = "".join(content_parts)
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls.keys())]

    if not usage:
        usage = {
            "completion_tokens": token_count,
            "prompt_tokens": 0,
            "total_tokens": token_count,
        }

    tps = timings.get("predicted_per_second", 0)
    logging.getLogger("localcoder").info(
        f"Response: {usage.get('completion_tokens', 0)} tokens, {tps:.0f} tok/s, prompt={usage.get('prompt_tokens', 0)}"
    )

    return {
        "choices": [{"message": msg, "finish_reason": finish_reason}],
        "usage": usage,
        "timings": timings,
        "model": model_name,
    }


# ── Rich display ──
def show_tool_call(fname, args):
    show_tool_animation(console, fname, args)


def show_image_inline(path):
    """Display image inline in terminal — auto-detects best method"""
    if not os.path.isfile(path):
        return
    timg = "/opt/homebrew/bin/timg"
    if os.path.exists(timg):
        try:
            # Use iTerm2 protocol for best quality, fall back to half-blocks
            proto = (
                "i" if os.environ.get("TERM_PROGRAM", "").startswith("iTerm") else "h"
            )
            subprocess.run(
                [timg, "-g", "60x20", "-C", "-p", proto, path], timeout=5, cwd=CWD
            )
            console.print(f"  [dim green]📸 {os.path.basename(path)}[/]")
            return
        except:
            pass
    # Fallback: open in Preview
    subprocess.Popen(
        ["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    console.print(f"  [dim green]📸 Opened {os.path.basename(path)} in Preview[/]")


def _render_user_turn(text, note=None):
    body = Text(_display_text(text), style="bold white")
    if note:
        body = Group(
            body,
            Text(_display_text(note), style="dim green"),
        )
    title = "you"
    if UI_LANG == "ar":
        title = _display_text("أنت")
    elif UI_LANG == "fr":
        title = "vous"
    return Panel(
        body,
        title=f"[bold #94a3b8]{title}[/]",
        title_align="left",
        border_style="#4b5563",
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _render_reasoning_panel(reasoning_text, start_time, tokens=0, tps=0.0):
    cleaned = _localize_reasoning_text((reasoning_text or "").replace("\r", "").strip())
    if len(cleaned) > 1200:
        cleaned = "…\n" + cleaned[-1200:]

    lines = cleaned.splitlines() if cleaned else ["Working through the request…"]
    if len(lines) > 12:
        lines = ["…"] + lines[-11:]

    preview = Text(_display_text("\n".join(lines)), style="dim")
    stats = Text(style="dim")
    elapsed = max(0.0, time.time() - start_time)
    if elapsed < 60:
        stats.append(f"{elapsed:.0f}s")
    else:
        mins, secs = divmod(elapsed, 60)
        stats.append(f"{mins:.0f}m {secs:.0f}s")
    if tokens > 0:
        stats.append(f"  ·  {tokens} tokens")
    if tps > 0:
        stats.append(f"  ·  {tps:.0f} tok/s", style="dim cyan")

    title = "thinking"
    if UI_LANG == "ar":
        title = _display_text("تفكير")
    elif UI_LANG == "fr":
        title = "réflexion"

    return Panel(
        Group(preview, stats),
        title=f"[bold magenta]{title}[/]",
        title_align="left",
        border_style="magenta",
        box=box.ROUNDED,
        padding=(0, 1),
    )


def show_result(result, tool_name=None):
    if not result or result == "(no output)":
        return

    # ── Search results: rich formatted cards ──
    if tool_name == "web_search" and "earch results for" in result:
        _show_search_results(result)
        return

    # ── Fetch URL: show status + preview ──
    if tool_name == "fetch_url" and result.startswith("Status:"):
        _show_fetch_result(result)
        return

    # ── Bash: styled output panel ──
    if tool_name == "bash":
        lines = result.split("\n")
        output = "\n".join(lines[:12])
        if len(lines) > 12:
            output += f"\n\033[2m… {len(lines) - 12} more lines\033[0m"
        is_error = (
            result.startswith("Error")
            or "error" in result[:100].lower()
            or "Traceback" in result[:100]
        )
        border = "red" if is_error else "yellow"
        console.print(
            Panel(
                output,
                border_style=border,
                padding=(0, 1),
                title="[dim]output[/]" if not is_error else "[red]error[/]",
                title_align="left",
            )
        )
        _auto_preview_images(result)
        return

    # ── Read file: compact preview ──
    if tool_name == "read_file":
        lines = result.split("\n")
        output = "\n".join(lines[:10])
        if len(lines) > 10:
            output += f"\n… {len(lines) - 10} more lines"
        console.print(
            Panel(
                output,
                border_style="blue",
                padding=(0, 1),
                title="[dim]content[/]",
                title_align="left",
            )
        )
        return

    # ── Default: truncated panel ──
    lines = result.split("\n")
    output = "\n".join(lines[:10])
    if len(lines) > 10:
        output += f"\n… ({len(lines) - 10} more lines)"
    console.print(Panel(output, border_style="dim", padding=(0, 1)))

    # Auto-preview any image files mentioned in tool output
    _auto_preview_images(result)


def _show_search_results(result):
    """Render web search results as styled cards with clickable links."""
    # Parse header
    header_match = re.match(r"(?:Image s|S)earch results for '([^']*)':", result)
    query = header_match.group(1) if header_match else "search"

    # Split into individual results
    parts = result.split("\n\n")
    entries = [e for e in (parts[1:] if len(parts) > 1 else parts) if e.strip()]

    table = Table(
        show_header=False,
        show_edge=False,
        pad_edge=False,
        padding=(0, 1),
        expand=True,
        box=None,
    )
    table.add_column(ratio=1)

    for i, entry in enumerate(entries[:5]):
        entry = entry.strip()
        if not entry:
            continue

        # Parse markdown-style [title](url)\nsnippet
        md_match = re.match(r"\[([^\]]+)\]\(([^)]+)\)\n?(.*)", entry, re.DOTALL)
        if md_match:
            title, url, snippet = (
                md_match.group(1),
                md_match.group(2),
                md_match.group(3).strip(),
            )
        else:
            # Image result format: "- Title\n  URL: ...\n  Source: ..."
            lines = entry.split("\n")
            title = lines[0].lstrip("- ").strip()
            url = ""
            snippet = ""
            for line in lines[1:]:
                if line.strip().startswith("URL:"):
                    url = line.split("URL:", 1)[1].strip()
                elif line.strip().startswith("Source:"):
                    snippet = line.split("Source:", 1)[1].strip()

        # Build styled entry
        row = Text()
        row.append(f"  {i + 1}. ", style="bold cyan")
        row.append(title, style="bold white")
        row.append("\n")
        if url:
            # Shorten display URL
            display_url = url.replace("https://", "").replace("http://", "")
            if len(display_url) > 70:
                display_url = display_url[:67] + "..."
            row.append(f"     {display_url}", style="dim green")
            row.append("\n")
        if snippet:
            row.append(f"     {snippet[:120]}", style="dim")

        table.add_row(row)

    title_text = Text()
    title_text.append(" search ", style="bold magenta")
    title_text.append(f'"{query}"', style="bold white")
    title_text.append(f"  ({len(entries)} results)", style="dim")

    console.print(
        Panel(
            table,
            title=title_text,
            title_align="left",
            border_style="magenta",
            padding=(0, 0),
        )
    )


def _show_fetch_result(result):
    """Render fetch_url results with status and clean preview."""
    lines = result.split("\n")
    status_line = lines[0] if lines else ""

    # Extract status code
    status_match = re.search(r"Status:\s*(\d+)", status_line)
    status = status_match.group(1) if status_match else "?"
    status_style = "green" if status == "200" else "yellow"

    # Content preview
    content_lines = lines[1:]
    preview = "\n".join(content_lines[:8])
    if len(content_lines) > 8:
        preview += f"\n... ({len(content_lines) - 8} more lines)"

    title_text = Text()
    title_text.append(" fetch ", style="bold blue")
    title_text.append(f"[{status}]", style=f"bold {status_style}")

    console.print(
        Panel(
            preview,
            title=title_text,
            title_align="left",
            border_style="blue",
            padding=(0, 1),
        )
    )


# ── Piper TTS talk-back ──
_piper_bin = None
_piper_voice = None
_piper_available = False


def _init_piper():
    """Detect piper binary and Arabic voice model."""
    global _piper_bin, _piper_voice, _piper_available
    import shutil as _sh

    _piper_bin = _sh.which("piper")
    if not _piper_bin:
        # Check common install locations
        for p in [
            os.path.expanduser("~/.local/bin/piper"),
            "/opt/homebrew/bin/piper",
            os.path.expanduser("~/.local/share/piper/piper"),
        ]:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                _piper_bin = p
                break
    if not _piper_bin:
        return
    # Find voice model based on UI language
    voices_dir = os.path.expanduser("~/.local/share/piper/voices")
    lang_map = {
        "ar": ["ar_JO-kareem-medium", "ar_JO-kareem-low", "ar"],
        "fr": ["fr_FR-siwis-medium", "fr_FR-upmc-medium", "fr"],
        "en": ["en_US-lessac-medium", "en_US-amy-medium", "en"],
    }
    prefixes = lang_map.get(UI_LANG, lang_map["en"])
    for prefix in prefixes:
        for ext in [".onnx"]:
            # Check voices directory
            voice_path = os.path.join(voices_dir, f"{prefix}{ext}")
            if os.path.isfile(voice_path):
                _piper_voice = voice_path
                _piper_available = True
                return
            # Check if model name works directly (piper downloads on demand)
            # Try the model name without path
            if "/" not in prefix and not prefix.endswith(".onnx"):
                _piper_voice = prefix
                _piper_available = True
                return


try:
    _init_piper()
except Exception:
    pass


def speak_text(text, background=True):
    """Speak text using Piper TTS. Runs in background by default."""
    if not _piper_available or not text:
        return
    # Strip markdown/code blocks — only speak plain text
    plain = re.sub(r"```[\s\S]*?```", "", text)  # remove code blocks
    plain = re.sub(r"`[^`]+`", "", plain)  # remove inline code
    plain = re.sub(r"\[.*?\]", "", plain)  # remove markdown links
    plain = re.sub(r"[#*_~>]", "", plain)  # remove markdown formatting
    plain = re.sub(r"https?://\S+", "", plain)  # remove URLs
    plain = plain.strip()
    if not plain or len(plain) < 3:
        return
    # Truncate long responses — speak first ~200 chars
    if len(plain) > 200:
        # Find sentence boundary
        for sep in [". ", "。", "। ", ".\n", "\n\n"]:
            idx = plain.find(sep, 80)
            if idx > 0:
                plain = plain[: idx + 1]
                break
        else:
            plain = plain[:200]

    def _speak():
        try:
            wav_path = os.path.join(CWD, ".localcoder-tts.wav")
            # Pipe text to piper
            proc = subprocess.run(
                [_piper_bin, "--model", _piper_voice, "--output_file", wav_path],
                input=plain,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0 and os.path.isfile(wav_path):
                # Play audio (macOS: afplay, Linux: aplay)
                player = "afplay" if sys.platform == "darwin" else "aplay"
                subprocess.run(
                    [player, wav_path],
                    capture_output=True,
                    timeout=30,
                )
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass
        except Exception:
            pass

    if background:
        threading.Thread(target=_speak, daemon=True).start()
    else:
        _speak()


def show_response(text):
    """Render model response as markdown with proper formatting.
    Auto-detects image URLs and downloads+displays them inline."""
    if not text:
        return
    console.print()
    try:
        if UI_LANG == "ar" or _contains_arabic(text):
            console.print(
                Text(_display_text(text), style="white"),
                width=min(console.width - 4, 100),
            )
        else:
            md = Markdown(text, code_theme="monokai")
            console.print(md, width=min(console.width - 4, 100))
    except:
        console.print(_display_text(text))
    console.print()

    # Auto-detect and preview images from response
    _auto_preview_images(text)


def _auto_preview_images(text):
    """Detect image URLs and local file paths in text, preview them inline."""
    # 1. Image URLs — accept direct image links, markdown links, and common image hosts.
    img_urls = []
    img_urls += re.findall(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text)
    img_urls += re.findall(r"\[[^\]]+\]\((https?://[^)\s]+)\)", text)
    img_urls += re.findall(
        r"(https?://[^\s\)\]\"']+\.(?:png|jpg|jpeg|webp|gif|svg|bmp)(?:\?[^\s\)]*)?)",
        text,
    )
    img_urls += re.findall(r"(https?://[^\s\)\]\"']+)", text)

    seen = set()
    previewed = 0
    for raw_url in img_urls:
        url = raw_url.rstrip(").,]>")
        if url in seen:
            continue
        seen.add(url)
        if previewed >= 3:
            break
        try:
            if show_image_url(url):
                previewed += 1
        except Exception:
            pass

    # 2. Local file paths — detect and preview
    local_paths = re.findall(
        r"(?:^|\s)([/~][\w/.\-]+\.(?:png|jpg|jpeg|webp|gif|svg|bmp))", text
    )
    local_paths += re.findall(
        r"(?:^|\s)(\.\/[\w/.\-]+\.(?:png|jpg|jpeg|webp|gif|svg|bmp))", text
    )
    for path in local_paths[:3]:
        path = os.path.expanduser(path.strip())
        if not os.path.isabs(path):
            path = os.path.join(CWD, path)
        if os.path.isfile(path) and _is_image_file(path):
            show_image_inline(path)

    # 3. Files just created by write_file — check recent tool output
    # (handled by show_result already)


def _extract_preview_image_url(page_url, html_text):
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            return urllib.parse.urljoin(page_url, match.group(1).strip())
    return None


def _is_image_file(path):
    """Check file header to verify it's a real image."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(8)
        return (
            hdr[:2] == b"\xff\xd8"
            or hdr[:4] == b"\x89PNG"
            or hdr[:4] == b"GIF8"
            or hdr[:4] == b"RIFF"
            or hdr[4:12] in (b"ftypavif", b"ftypavis", b"ftypheic", b"ftypheif")
            or b"<svg" in open(path, "rb").read(200)
        )
    except Exception:
        return False


def show_image_url(url, max_width=50, max_height=15, _depth=0):
    """Download and display an image URL inline in terminal."""
    if _depth > 1:
        return False
    try:
        img_name = os.path.basename(url.split("?")[0])[:30] or "preview.jpg"
        img_path = os.path.join("/tmp", f"localcoder-{img_name}")
        subprocess.run(
            ["curl", "-fsSL", "-A", "Mozilla/5.0", "-o", img_path, url],
            capture_output=True,
            timeout=10,
        )
        if os.path.isfile(img_path) and os.path.getsize(img_path) > 500:
            if not _is_image_file(img_path):
                try:
                    with open(img_path, "rb") as f:
                        html = f.read(120_000).decode("utf-8", errors="ignore")
                    nested = _extract_preview_image_url(url, html)
                    if nested and nested != url:
                        return show_image_url(
                            nested,
                            max_width=max_width,
                            max_height=max_height,
                            _depth=_depth + 1,
                        )
                except Exception:
                    pass
                return False
            show_image_inline(img_path)
            return True
    except Exception:
        pass
    return False


# print_thinking is now handled by ThinkingSpinner from localcoder.localcoder_display


# ── Permissions ──
# ── Sandbox ──
class Sandbox:
    """Command-level sandbox. Default ON. Blocks destructive operations."""

    # Bash commands that are ALWAYS blocked in sandbox
    BLOCKED_CMDS = [
        "rm -rf",
        "rm -r",
        "rmdir",
        "mkfs",
        "dd if=",
        "sudo",
        "> /dev/",
        "chmod 777",
        "| sh",
        "| bash",
        "| zsh",  # pipe to shell
        "| python",
        "| perl",
        "| ruby",  # pipe to interpreter
        "eval ",
        "exec ",
        "ssh ",
        "scp ",
        "rsync ",
        "kill -9",
        "killall",
        "pkill",
        "launchctl",
        "defaults write",
        "networksetup",
        "osascript.*delete",
    ]

    # Paths that are NEVER writable in sandbox
    BLOCKED_PATHS = [
        "~/.ssh",
        "~/.aws",
        "~/.gnupg",
        "~/.config/gcloud",
        "~/.env",
        "~/.bashrc",
        "~/.zshrc",
        "~/.profile",
        "~/.bash_profile",
        "~/.netrc",
        "~/.npmrc",
        "~/.pypirc",
        "~/.docker",
        "~/.kube",
        "/etc/",
        "/usr/",
        "/System/",
        "/Library/",
        "~/.localcoder/config.json",  # protect own config
    ]

    # Bash commands allowed in sandbox (read-only operations)
    SAFE_PREFIXES = [
        "ls",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "find",
        "grep",
        "rg",
        "ag",
        "fd",
        "wc",
        "sort",
        "uniq",
        "diff",
        "file",
        "stat",
        "git status",
        "git diff",
        "git log",
        "git show",
        "git blame",
        "git branch",
        "git remote",
        "git stash list",
        "echo",
        "printf",
        "which",
        "type",
        "man",
        "python3 -c",
        "node -e",  # allow one-liner execution
        "npm list",
        "pip list",
        "pip show",
        "curl -fsSL",
        "curl -sL",
        "curl -s",  # GET requests only
        "open ",  # open files/URLs
        "timg",  # image display
    ]

    @staticmethod
    def is_bash_allowed(cmd):
        """Check if a bash command is safe in sandbox mode."""
        cmd_lower = cmd.strip().lower()

        # Block dangerous commands
        for blocked in Sandbox.BLOCKED_CMDS:
            if blocked.lower() in cmd_lower:
                return False, f"Blocked: '{blocked}' not allowed in sandbox mode"

        # Block writing to protected paths
        for path in Sandbox.BLOCKED_PATHS:
            expanded = os.path.expanduser(path)
            if expanded in cmd or path in cmd:
                return (
                    False,
                    f"Blocked: writing to '{path}' not allowed in sandbox mode",
                )

        return True, ""

    @staticmethod
    def is_path_writable(path):
        """Check if a file path is writable in sandbox mode."""
        full = os.path.expanduser(path)
        # Resolve relative paths to CWD
        if not os.path.isabs(full):
            full = os.path.join(CWD, full)
        full = os.path.abspath(full)

        # Block protected paths
        for blocked in Sandbox.BLOCKED_PATHS:
            expanded = os.path.abspath(os.path.expanduser(blocked))
            if full.startswith(expanded):
                return False, f"Blocked: '{blocked}' is protected"

        # Must be within CWD or /tmp
        if not (full.startswith(CWD) or full.startswith("/tmp")):
            return False, f"Blocked: writes only allowed in project directory or /tmp"

        return True, ""


class Permissions:
    def __init__(self, mode="auto", sandbox=True):
        self.mode = mode
        self.sandbox = sandbox
        self.approved = set()
        self._load_approved()

    SAFE = {"read_file", "read_pdf", "web_search", "fetch_url", "generate_image"}

    def _config_path(self):
        return os.path.expanduser("~/.localcoder/approved_tools.json")

    def _load_approved(self):
        """Load previously approved tools from disk."""
        try:
            with open(self._config_path()) as f:
                saved = json.load(f)
            self.approved = set(saved.get("tools", []))
        except Exception:
            pass

    def _save_approved(self):
        """Save approved tools to disk for next session."""
        try:
            os.makedirs(os.path.dirname(self._config_path()), exist_ok=True)
            with open(self._config_path(), "w") as f:
                json.dump({"tools": list(self.approved)}, f)
        except Exception:
            pass

    def check(self, fname, args=None):
        """Check if a tool call is allowed. Returns True/False."""
        # Sandbox checks (before permission check)
        if self.sandbox:
            if fname == "bash" and args:
                cmd = args.get("command", "")
                allowed, reason = Sandbox.is_bash_allowed(cmd)
                if not allowed:
                    console.print(f"  [red]🛡 {reason}[/]")
                    console.print(
                        f"  [dim]Run with --unrestricted to disable sandbox[/]"
                    )
                    return False

            if fname in ("write_file", "edit_file") and args:
                path = args.get("path", "")
                full = os.path.join(CWD, path) if not os.path.isabs(path) else path
                allowed, reason = Sandbox.is_path_writable(full)
                if not allowed:
                    console.print(f"  [red]🛡 {reason}[/]")
                    return False

            if fname == "computer_use":
                console.print(f"  [red]🛡 computer_use disabled in sandbox mode[/]")
                console.print(f"  [dim]Run with --unrestricted to enable[/]")
                return False

        # Permission modes
        if self.mode == "bypass" or fname in self.approved:
            return True
        if self.mode == "auto" and fname in self.SAFE:
            return True

        # Auto-approve safe bash commands (read-only, no side effects)
        if self.mode == "auto" and fname == "bash" and args:
            cmd = args.get("command", "")
            safe, reason = is_safe_command(cmd)
            if safe:
                return True

        console.print(
            Panel(
                f"[bold yellow]Allow [white]{fname}[/white]?[/]  [dim]y[/]es · [dim]n[/]o · [dim]a[/]lways",
                border_style="yellow",
                padding=(0, 1),
            )
        )
        # Flush any leftover input from streaming
        import termios

        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass
        try:
            ans = input("  ▸ ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans in ("a", "always"):
            self.approved.add(fname)
            self._save_approved()
            console.print(f"  [green]✓ {fname} — always approved (remembered)[/]")
            return True
        if ans in ("y", "yes"):
            console.print(f"  [green]✓ approved[/]")
            return True
        if ans == "":
            # Empty input — re-prompt, don't auto-approve
            console.print(f"  [dim]Type y, n, or a[/]")
            try:
                ans = input("  ▸ ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if ans in ("y", "yes"):
                console.print(f"  [green]✓ approved[/]")
                return True
            if ans in ("a", "always"):
                self.approved.add(fname)
                self._save_approved()
                console.print(f"  [green]✓ {fname} — always approved (remembered)[/]")
                return True
        console.print(f"  [red]✗ denied[/]")
        return False


# ── Agent loop ──
def agent_loop(messages, perms, session=None):
    total_tokens = 0
    loop_start = time.time()
    spinner = ThinkingSpinner(console)
    recent_tools = []
    self_corrected = False
    stream_idle_timeout = 180  # seconds — finalize partial args if LLM stalls
    for turn in range(10):
        spinner.start()
        spinner.update(tokens=total_tokens)
        messages_for_call = messages
        last_user_text = ""
        for _msg in reversed(messages):
            if _msg.get("role") == "user" and isinstance(_msg.get("content"), str):
                last_user_text = _msg.get("content", "")
                break
        if _is_image_only_request(last_user_text):
            messages_for_call = messages + [
                {
                    "role": "system",
                    "content": (
                        "The latest user request is image-only. "
                        "Do not inspect skills. Do not write files. Do not build HTML. "
                        "Use web_search for a REAL direct photo URL first. "
                        "If results are article pages, use fetch_url to extract lead images via Jina Reader. "
                        "Return the best direct real-photo URL and at most one fallback."
                    ),
                }
            ]
        try:
            resp = chat_api(messages_for_call, spinner=spinner)
        except urllib.error.URLError as e:
            spinner.stop()
            err_str = str(e)
            if "timed out" in err_str or "timeout" in err_str.lower():
                console.print(
                    "[bold red]  ✗ API timeout — context may be full. Auto-clearing old messages.[/]"
                )
                if len(messages) > 3:
                    messages[:] = [messages[0]] + messages[-2:]
                    continue
            elif "Connection refused" in err_str:
                console.print(
                    "[bold red]  ✗ Server not running. Start it with: localcoder --setup[/]"
                )
            else:
                console.print(f"[bold red]  ✗ Network error: {err_str[:200]}[/]")
            break
        except (json.JSONDecodeError, KeyError) as e:
            spinner.stop()
            logging.getLogger("localcoder").error(f"Malformed API response: {e}")
            console.print(f"[bold red]  ✗ Bad API response — retrying...[/]")
            if turn < 24:
                time.sleep(1)
                continue
            break
        except Exception as e:
            spinner.stop()
            console.print(f"[bold red]  ✗ {e}[/]")
            logging.getLogger("localcoder").error(f"Agent loop error: {e}", exc_info=True)
            break
        finally:
            # Ensure spinner is always cleaned up before printing
            try:
                if spinner._live is not None:
                    spinner._live.stop()
                    spinner._live = None
            except Exception:
                pass

        choice = resp["choices"][0]
        msg = choice["message"]
        usage = resp.get("usage", {})
        timings = resp.get("timings", {})
        tps = timings.get("predicted_per_second", 0)
        total_tokens += usage.get("completion_tokens", 0)

        # Use reasoning_content as content when content is empty
        # (Gemma 4 thinking mode puts answers in reasoning)
        content_text = msg.get("content", "").strip()
        reasoning_text = msg.get("reasoning_content", "").strip()
        if not content_text and reasoning_text:
            content_text = reasoning_text
        content_text = re.sub(r"<\|?channel\|?>", "", content_text).strip()
        # Text was already streamed live by chat_api — only show via
        # markdown if it came from reasoning_content fallback
        if content_text and not msg.get("content", "").strip():
            show_response(content_text)

        if not msg.get("tool_calls"):
            if content_text:
                _auto_preview_images(content_text)
            elapsed = time.time() - loop_start
            if elapsed < 60:
                t = f"{elapsed:.0f}s"
            else:
                m, s = divmod(elapsed, 60)
                t = f"{m:.0f}m {s:.0f}s"
            console.print(
                f"\n  [dim]✦ {t} · {total_tokens} tokens · {tps:.0f} tok/s[/]"
            )
            # Show context usage after completion
            ctx_str = BACKEND_INFO.get("ctx", "")
            if ctx_str:
                ctx_max = int(ctx_str.replace("K", "")) * 1024
                ctx_used = estimate_tokens(json.dumps(messages))
                context_usage_bar(console, ctx_used, ctx_max)
            # TTS talk-back — speak the response in background
            if _piper_available and content_text and UI_LANG in ("ar", "fr"):
                speak_text(content_text)
            break

        messages.append(msg)
        if session:
            session.add_message(msg)
        for tc in msg["tool_calls"]:
            fname = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                # Try to salvage partial JSON from stalled streams
                try:
                    # Common case: truncated string value — close it
                    fixed = raw_args.rstrip()
                    if fixed.count('"') % 2 == 1:
                        fixed += '"'
                    if not fixed.endswith("}"):
                        fixed += "}"
                    args = json.loads(fixed)
                    logging.getLogger("localcoder").warning(f"Salvaged partial JSON for {fname}")
                except Exception:
                    args = {}
                    logging.getLogger("localcoder").error(f"Unparseable args for {fname}: {raw_args[:200]}")

            # Loop detection — catch repeating patterns, then self-correct
            # Normalize: bash(cat file) counts as read_file(file)
            if fname == "bash" and args.get("command", "").startswith("cat "):
                tool_sig = f"read:{args['command'].split()[-1]}"
            elif fname == "read_file":
                tool_sig = f"read:{args.get('path', '')}"
            elif fname == "fetch_url":
                tool_sig = f"fetch:{args.get('url', '')[:50]}"
            else:
                tool_sig = f"{fname}:{json.dumps(args)[:60]}"
            recent_tools.append(tool_sig)
            # Track consecutive errors
            if not hasattr(agent_loop, "_error_count"):
                agent_loop._error_count = 0

            # Keep window bounded
            if len(recent_tools) > 10:
                recent_tools = recent_tools[-10:]

            if len(recent_tools) >= 3:
                last3 = recent_tools[-3:]
                is_loop = False
                if last3[0] == last3[1] == last3[2]:
                    is_loop = True
                # Catch alternating reads of same file (cat/read_file flip)
                elif len(set(last3)) <= 2 and all(s.startswith("read:") for s in last3):
                    is_loop = True
                # Catch fetch loops (same URL fetched 3 times)
                elif len(set(last3)) == 1 and last3[0].startswith("fetch:"):
                    is_loop = True

                if is_loop:
                    if not self_corrected:
                        # First loop — force model to act
                        self_corrected = True
                        console.print("  [yellow]⚠ Loop detected — redirecting...[/]")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": "LOOP DETECTED: You already have this data. STOP reading the same file. "
                                "You have all the information you need. NOW ACT:\n"
                                "1. If the user asked to build an app — START writing code with write_file\n"
                                "2. If you need more data from a URL — use web_search or fetch_url\n"
                                "3. If you need to run something — use bash\n"
                                "DO NOT read the same file again. Use write_file to create the output NOW.",
                            }
                        )
                        recent_tools.clear()
                        continue
                    else:
                        # Second loop — auto-continue, don't block on user input
                        console.print(
                            "  [yellow]⚠ Still looping — forcing action...[/]"
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": "You are stuck in a loop. STOP reading files. "
                                "You already have all the content. "
                                "START BUILDING NOW. Use write_file to create the output immediately.",
                            }
                        )
                        self_corrected = False
                        recent_tools.clear()
                        continue

            show_tool_call(fname, args)
            logging.getLogger("localcoder").info(
                f"Tool: {fname}({json.dumps(args)[:200]})"
            )

            if not perms.check(fname, args):
                logging.getLogger("localcoder").info(f"Tool denied: {fname}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "Denied by user.",
                    }
                )
                continue

            try:
                t0 = time.time()
                result = exec_tool(fname, args)
                logging.getLogger("localcoder").info(
                    f"Tool result: {fname} → {len(result)} chars in {time.time() - t0:.1f}s"
                )
            except Exception as e:
                result = f"Error: {e}"
                logging.getLogger("localcoder").error(f"Tool error: {fname} → {e}")
                console.print(f"  [bold red]✗ {e}[/]")

            show_result(result, fname)

            # Computer use results already include screen content from vision extraction
            # (built into the tool itself, ADK-style)

            # Detect repeated errors — if 3+ consecutive bash errors, tell model to web_search
            if (
                fname == "bash"
                and result
                and ("error" in result.lower() or "Error" in result)
            ):
                agent_loop._error_count += 1
                if agent_loop._error_count >= 3:
                    console.print(
                        "  [yellow]⚠ 3 consecutive errors — suggesting web search...[/]"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result[:1500],
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "You've had 3 consecutive errors with this approach. "
                            "STOP trying variations of the same command. "
                            "Use web_search to find the correct way to do this on macOS. "
                            "Search for the specific error message or task.",
                        }
                    )
                    agent_loop._error_count = 0
                    recent_tools.clear()
                    continue
            else:
                agent_loop._error_count = 0

            # Auto-show: if bash just created/downloaded an image, verify and display it
            if fname == "bash":
                now = time.time()
                candidates = []
                for fn in os.listdir(CWD):
                    if any(
                        fn.lower().endswith(e)
                        for e in (".png", ".jpg", ".jpeg", ".webp", ".gif")
                    ):
                        fp = os.path.join(CWD, fn)
                        if os.path.getmtime(fp) > now - 5:
                            candidates.append((os.path.getmtime(fp), fp, fn))
                if candidates:
                    candidates.sort(reverse=True)
                    _, fp, fn = candidates[0]
                    try:
                        with open(fp, "rb") as img_f:
                            header = img_f.read(16)
                        is_image = (
                            header[:8] == b"\x89PNG\r\n\x1a\n"
                            or header[:2] == b"\xff\xd8"
                            or header[:4] == b"GIF8"
                            or header[:4] == b"RIFF"
                        )
                        if is_image:
                            show_image_inline(fp)
                            result += "\n[IMAGE DISPLAYED INLINE IN TERMINAL]"
                        else:
                            console.print(
                                f"  [red]⚠ {fn} is not a valid image (server returned HTML). Try a different URL.[/]"
                            )
                            result += f"\n[ERROR: Downloaded file {fn} is NOT an image. The server returned HTML. Try a completely different image source — avoid wikimedia SVG thumbnails.]"
                    except Exception as img_err:
                        logging.getLogger("localcoder").error(
                            f"Image check error: {img_err}"
                        )

            # Keep more for fetch_url (has images), less for others
            max_len = (
                5000
                if fname == "read_pdf"
                else 2500
                if fname in ("fetch_url", "web_search")
                else 1500
            )

            # If result contains IMAGE:path, include base64 so the model can see it
            result_str = str(result)[:max_len]
            tool_content = result_str
            if result_str.startswith("IMAGE:") and "|" in result_str:
                img_path = result_str.split("IMAGE:", 1)[1].split("|", 1)[0]
                if os.path.isfile(img_path):
                    try:
                        import base64 as _b64m
                        img_b64 = _b64m.b64encode(open(img_path, "rb").read()).decode()
                        # Detect mime type from extension
                        ext = os.path.splitext(img_path)[1].lower()
                        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                                "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "image/png")
                        text_part = result_str.split("|", 1)[1] if "|" in result_str else result_str
                        tool_content = [
                            {"type": "text", "text": text_part},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                        ]
                    except Exception:
                        pass  # fall back to text-only

            tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_content,
                }
            messages.append(tool_msg)
            if session:
                session.add_message(tool_msg)

        tokens = usage.get("completion_tokens", 0) if usage else 0
        stats = Text()
        stats.append("  ")
        if tokens:
            stats.append(f"{tokens} tokens", style="dim")
            stats.append(" · ", style="dim")
        if tps > 0:
            stats.append(f"{tps:.0f} tok/s", style="dim cyan")
        console.print(stats)

    return total_tokens


# ── Banner ──
def _model_label():
    bi = BACKEND_INFO
    name = f"Gemma 4 {bi['size']}" if bi["size"] else bi["model_name"]
    quant = f" {bi['quant']}" if bi["quant"] else ""
    return f"{name}{quant}"


LOGO_TEXT = [
    ("[bold #e07a5f]██╗      ██████╗  ██████╗ █████╗ ██╗     [/]",),
    ("[bold #d4725a]██║     ██╔═══██╗██╔════╝██╔══██╗██║     [/]",),
    ("[bold #c96a55]██║     ██║   ██║██║     ███████║██║     [/]",),
    ("[bold #be6250]██║     ██║   ██║██║     ██╔══██║██║     [/]",),
    ("[bold #b35a4b]███████╗╚██████╔╝╚██████╗██║  ██║███████╗[/]",),
    ("[bold #a85246]╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝[/]",),
    ("[bold #81b29a] ██████╗ ██████╗ ██████╗ ███████╗██████╗ [/]",),
    ("[bold #76a890]██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗[/]",),
    ("[bold #6b9e86]██║     ██║   ██║██║  ██║█████╗  ██████╔╝[/]",),
    ("[bold #60947c]██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗[/]",),
    ("[bold #558a72]╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║[/]",),
    ("[bold #4a8068] ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝[/]",),
]


def show_banner():
    global BACKEND_INFO
    BACKEND_INFO = detect_backend()
    bi = BACKEND_INFO
    ml = _model_label()

    # GPU stats
    gpu_str = ""
    try:
        from localcoder.backends import get_metal_gpu_stats, get_swap_usage_mb, MODELS

        metal = get_metal_gpu_stats()
        swap = get_swap_usage_mb()
        gt = metal.get("total_mb", 0)
        model_size_gb = bi.get("size_gb", 0)
        if not model_size_gb:
            for mid, m in MODELS.items():
                if m.get("name", "") in ml or mid in ml.lower().replace(" ", ""):
                    model_size_gb = m["size_gb"]
                    break
        model_mb = int(model_size_gb * 1024) if model_size_gb else 0
        if gt > 0 and model_mb > 0:
            pct = min(1.0, model_mb / max(1, gt))
            bc = "green" if pct < 0.75 else "yellow" if pct < 0.9 else "red"
            gpu_str = f"[{bc}]{model_mb // 1024}/{gt // 1024}GB GPU[/{bc}]"
    except ImportError:
        pass

    from rich.text import Text as RText
    from rich.console import Group
    from rich.live import Live

    # Terminal centering — center the 50-char-wide logo box
    try:
        _tw = os.get_terminal_size().columns
    except Exception:
        _tw = 80
    _cpad = " " * max(0, (_tw - 50) // 2)

    console.print()

    gpu_icon = "[green]●[/]" if bi.get("gpu") else "[yellow]●[/]"
    status_line = f"{_cpad}{gpu_icon} [bold cyan]{ml}[/]  ·  {bi['backend']}  ·  {bi['ctx'] or '?'} context  ·  {gpu_str}  ·  [green]$0.00[/]"

    # ── Pre-designed frames (Copilot-style: each frame is a complete screen) ──
    def _frame(*lines):
        return Group(*(RText.from_markup(l) for l in lines))

    B = "#e07a5f"  # border/accent
    G = "#81b29a"  # green accent

    # Helper: build bordered logo frame with optional extras below
    def _logo_frame(reveal_cols=99, scan=False, subtitle="", extras=None):
        lines = [f"{_cpad}[{B}]┌──────────────────────────────────────────────────┐[/]"]
        for r, lt in enumerate(LOGO_TEXT):
            raw = lt[0]
            color = raw.split("]")[0] + "]"
            plain = raw.replace("[/]", "").split("]")[-1] if "]" in raw else raw
            shown = plain[:reveal_cols]
            cursor = f"[white bold]▌[/]" if scan and reveal_cols < len(plain) else ""
            rest = " " * max(0, 48 - len(shown) - (1 if cursor else 0))
            lines.append(f"{_cpad}[{B}]│[/]{color}{shown}[/]{cursor}{rest}[{B}]│[/]")
        lines.append(
            f"{_cpad}[{B}]└──────────────────────────────────────────────────┘[/]"
        )
        if subtitle:
            lines.append(subtitle)
        if extras:
            lines.extend(extras)
        return _frame(*lines)

    try:
        with Live(console=console, refresh_per_second=20, transient=True) as live:
            # Act 1: Border materializes (corners → edges → full)
            corners = [
                f"{_cpad}[{B}]┌┐[/]",
                *["" for _ in range(12)],
                f"{_cpad}[{B}]└┘[/]",
            ]
            live.update(_frame(*corners))
            time.sleep(0.07)

            for w in [12, 24, 36, 48]:
                lines = [f"{_cpad}[{B}]┌{'─' * w}{'─' * (48 - w)}┐[/]"]
                for _ in range(12):
                    lines.append(f"{_cpad}[{B}]│[/]{' ' * 48}[{B}]│[/]")
                lines.append(f"{_cpad}[{B}]└{'─' * w}{'─' * (48 - w)}┘[/]")
                live.update(_frame(*lines))
                time.sleep(0.04)

            # Act 2: Logo reveals left-to-right with typing cursor
            for col in range(0, 48, 3):
                live.update(_logo_frame(reveal_cols=col, scan=True))
                time.sleep(0.045)

            # Act 3: Full logo holds, subtitle types in
            live.update(_logo_frame(reveal_cols=99))
            time.sleep(0.12)

            subs = [
                f"{_cpad}[{B}]✦[/] [dim]{_ui('Command-line', 'واجهة سطر', 'Interface en ligne')}[/]",
                f"{_cpad}[{B}]✦[/] [dim]{_ui('Command-line interface', 'واجهة سطر الأوامر', 'Interface en ligne de commande')}[/]",
                f"{_cpad}[{B}]✦[/] [dim]{_ui('Command-line interface', 'واجهة سطر الأوامر', 'Interface en ligne de commande')}[/]  [bold {G}]{_ui('✓ offline', '✓ بدون إنترنت', '✓ hors ligne')}[/]",
            ]
            for s in subs:
                live.update(_logo_frame(reveal_cols=99, subtitle=s))
                time.sleep(0.1)

            # Act 4: Description + status appear
            desc = [
                "",
                f"{_cpad}{_ui('Write, test, and debug code right from your terminal.', 'اكتب واختبر وصحح الأكواد مباشرة من الطرفية.', 'Écrivez, testez et déboguez du code depuis votre terminal.')}",
                f"{_cpad}{_ui('Runs [bold]100% on your GPU[/]. No API keys. No cloud. Enter [bold]?[/] for help.', 'يعمل [bold]100% على GPU[/]. بدون مفاتيح API. بدون سحابة. اكتب [bold]?[/] للمساعدة.', 'Tourne [bold]100% sur votre GPU[/]. Pas de clés API. Pas de cloud. Tapez [bold]?[/] pour l\u2019aide.')}",
            ]
            live.update(_logo_frame(reveal_cols=99, subtitle=subs[-1], extras=desc))
            time.sleep(0.2)

            full_extras = desc + [
                "",
                status_line,
                f"{_cpad}[dim]{os.path.basename(CWD)}/[/]",
                "",
            ]
            live.update(
                _logo_frame(reveal_cols=99, subtitle=subs[-1], extras=full_extras)
            )
            time.sleep(0.5)

    except Exception:
        pass

    # ── Static final render (centered, all languages show ASCII art) ──
    # ASCII art logo — centered
    console.print(
        f"{_cpad}[{B}]┌──────────────────────────────────────────────────┐[/]"
    )
    for lt in LOGO_TEXT:
        raw = lt[0]
        color = raw.split("]")[0] + "]"
        plain = raw.replace("[/]", "").split("]")[-1] if "]" in raw else raw
        pad = " " * max(0, 48 - len(plain))
        console.print(f"{_cpad}[{B}]│[/]{lt[0]}{pad}[{B}]│[/]")
    console.print(
        f"{_cpad}[{B}]└──────────────────────────────────────────────────┘[/]"
    )

    if UI_LANG == "ar":
        # Arabic: large styled title + subtitle below logo
        console.print(
            f"{_cpad}[{B}]✦[/] [dim]{_t('cmd_line')}[/]  [bold {G}]{_t('offline')}[/]"
        )
        console.print()
        console.print(f"{_cpad}[bold #c084fc]  ✦  {_t('banner_title')}  ✦[/]")
        console.print(f"{_cpad}[dim]  {_t('banner_subtitle')}[/]")
        console.print()
        console.print(f"{_cpad}{_t('desc_line1')}")
        console.print(f"{_cpad}{_t('desc_line2')}")
    elif UI_LANG == "fr":
        console.print(
            f"{_cpad}[{B}]✦[/] [dim]{_t('cmd_line')}[/]  [bold {G}]{_t('offline')}[/]"
        )
        console.print()
        console.print(f"{_cpad}[bold #c084fc]  ✦  LocalCoder  ✦[/]")
        console.print(f"{_cpad}[dim]  {_t('banner_subtitle')}[/]")
        console.print()
        console.print(f"{_cpad}{_t('desc_line1')}")
        console.print(f"{_cpad}{_t('desc_line2')}")
    else:
        console.print(
            f"{_cpad}[{B}]✦[/] [dim]Command-line interface[/]  [bold {G}]✓ offline[/]"
        )
        console.print()
        console.print(f"{_cpad}Write, test, and debug code right from your terminal.")
        console.print(
            f"{_cpad}Runs [bold]100% on your GPU[/]. No API keys. No cloud. Enter [bold]?[/] for help."
        )
    console.print()
    console.print(status_line)
    console.print(f"{_cpad}[dim]{os.path.basename(CWD)}/[/]")
    console.print()


_toolbar_gpu_cache = {"text": "", "ts": 0}


def get_toolbar():
    """Bottom toolbar — model + GPU + offline. GPU stats cached (no ioreg per keystroke)."""
    bi = BACKEND_INFO
    ml = _model_label()
    ctx = bi["ctx"] or "?"

    # Cache GPU part — compute once, reuse for 60s
    gpu_part = _toolbar_gpu_cache["text"]
    if time.time() - _toolbar_gpu_cache["ts"] > 60:
        try:
            from localcoder.backends import MODELS

            # Just use model size from registry — no ioreg call
            model_gb = 0
            gt_gb = 16  # default Metal budget
            for mid, m in MODELS.items():
                if m.get("name", "") in ml or mid in ml.lower().replace(" ", ""):
                    model_gb = m["size_gb"]
                    break
            if model_gb > 0:
                gc = "ansigreen" if model_gb < gt_gb else "ansired"
                gpu_part = f' <style bg="{gc}" fg="ansiblack"> GPU {int(model_gb)}/{gt_gb}GB </style>'
                _toolbar_gpu_cache["text"] = gpu_part
                _toolbar_gpu_cache["ts"] = time.time()
        except ImportError:
            pass

    return HTML(
        f" <b>{ml}</b>"
        f' <style bg="ansigreen" fg="ansiblack"> {bi["backend"]} </style>'
        f' <style bg="ansiblue" fg="ansiwhite"> {ctx} </style>'
        f"{gpu_part}"
        f' <style bg="ansidarkgray" fg="ansiwhite"> {_ui("✓ offline", "✓ بدون إنترنت", "✓ hors ligne")} </style>'
    )


# ── Main ──
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="localcoder — local AI coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  localcoder                            interactive mode
  localcoder -p "build a react app"     one-shot mode
  localcoder -c                         continue last session
  localcoder --yolo                     auto-approve everything
  localcoder -m gemma4-e4b              use E4B model
  localcoder -m gemma4-26b --yolo -p "fix the bug"
""",
    )
    parser.add_argument("-p", "--prompt", type=str, help="Run a single task and exit")
    parser.add_argument(
        "-c",
        "--continue",
        dest="cont",
        action="store_true",
        help="Continue last session",
    )
    parser.add_argument(
        "-m", "--model", type=str, default=None, help="Model name (default: gemma4-26b)"
    )
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Auto-approve all tools (sandbox still active)",
    )
    parser.add_argument("--bypass", action="store_true", help="Same as --yolo")
    parser.add_argument(
        "--unrestricted",
        action="store_true",
        help="Disable sandbox — full system access (dangerous)",
    )
    parser.add_argument("-ar", "--arabic", action="store_true", help="Arabic UI")
    parser.add_argument("-fr", "--french", action="store_true", help="French UI")
    parser.add_argument("--ask", action="store_true", help="Ask before every tool")
    parser.add_argument(
        "--system",
        type=str,
        default=None,
        help="Custom system prompt (string or path to .txt/.md file)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Use compact system prompt (best for small models like E4B)",
    )
    parser.add_argument(
        "--api",
        type=str,
        default=None,
        help="API base URL (default: http://127.0.0.1:8089/v1)",
    )
    args = parser.parse_args(argv)

    # Override globals
    global MODEL, API_BASE, UI_LANG
    if args.model:
        MODEL = args.model
        # Auto-set API base for Ollama models if not explicitly provided
        if not args.api:
            API_BASE = "http://127.0.0.1:11434/v1"
    if args.api:
        API_BASE = args.api
    if getattr(args, "arabic", False):
        UI_LANG = "ar"
        os.environ["LOCALCODER_UI_LANG"] = "ar"
    elif getattr(args, "french", False):
        UI_LANG = "fr"
        os.environ["LOCALCODER_UI_LANG"] = "fr"

    mode = "bypass" if (args.yolo or args.bypass) else ("ask" if args.ask else "auto")
    sandbox = not args.unrestricted

    if args.unrestricted:
        console.print(
            f"  [red bold]{_ui('⚠ UNRESTRICTED MODE — sandbox disabled. Full system access.', '⚠ وضع غير مقيد — الحماية معطلة. وصول كامل للنظام.', '⚠ MODE NON RESTREINT — sandbox désactivé. Accès complet au système.')}[/]"
        )
    elif args.yolo:
        console.print(
            f"  [yellow]{_ui('Auto-approve mode. Sandbox still active (no rm -rf, no sudo, no writes outside project).', 'وضع الموافقة التلقائية. الحماية مفعلة (بدون rm -rf، بدون sudo، الكتابة داخل المشروع فقط).', 'Mode auto-approbation. Sandbox toujours actif (pas de rm -rf, sudo, ni écriture hors projet).')}[/]"
        )

    # ── First-run permission check ──
    cfg = _load_config()
    if sys.platform == "darwin" and not cfg.get("permissions_checked"):
        _check_permissions()

    # Logging — always on, auto-rotate
    log_file = os.path.join(CWD, ".localcoder.log")
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 1_000_000:
            with open(log_file, "r") as f:
                lines = f.readlines()
            with open(log_file, "w") as f:
                f.writelines(lines[-500:])
    except:
        pass
    # Only log our stuff, not library noise
    logger = logging.getLogger("localcoder")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(fh)
    # Silence noisy libraries
    logging.getLogger("markdown_it").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logger.info(f"=== Session started · model={MODEL} · cwd={CWD} · perms={mode} ===")
    perms = Permissions(mode, sandbox=sandbox)

    # ── Initialize MCP servers ──
    mcp_mgr = init_mcp()
    mcp_tools = mcp_mgr.get_tool_schemas()
    # Don't inject MCP tool schemas into TOOLS for small models (E4B) —
    # too many tools confuses them. MCP tools still work via exec_tool aliases.
    is_small_model = any(tag in MODEL.lower() for tag in ("e4b", "e2b", "4b", "2b", "small"))
    if mcp_tools and not is_small_model:
        TOOLS.extend(mcp_tools)
        mcp_names = [t["function"]["name"] for t in mcp_tools]
        logger.info(f"MCP tools added: {', '.join(mcp_names)}")
        for t in mcp_tools:
            Permissions.SAFE.add(t["function"]["name"])
    if mcp_tools:
        console.print(f"  [dim]MCP: {len(mcp_tools)} tools from {len(mcp_mgr.servers)} server(s){' (aliased)' if is_small_model else ''}[/]")

    _skills_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.agents', 'skills'))

    # ── Custom system prompt from --system flag ──
    if args.system:
        custom = args.system
        # If it's a file path, read it
        if os.path.isfile(custom) or os.path.isfile(os.path.expanduser(custom)):
            with open(os.path.expanduser(custom)) as f:
                custom = f.read()
        # Replace {CWD} placeholder
        custom = custom.replace("{CWD}", CWD).replace("{SKILLS}", _skills_dir)
        system = {"role": "system", "content": custom}
    elif args.compact or is_small_model:
        system = {
            "role": "system",
            "content": f"You are Local Coder, a coding agent. CWD: {CWD}\n"
            f"RESPOND ONLY WITH TOOL CALLS. Never put code in chat text.\n\n"
            f"TOOLS: generate_image, write_file, read_file, edit_file, bash, preview_app, web_search, fetch_url\n\n"
            f"WORKFLOW:\n"
            f"1. generate_image(prompt='detailed description', filename='name.png', size='256x256', steps=2) for each image\n"
            f"2. write_file(path='index.html', content='complete HTML') with ALL code\n"
            f"3. preview_app(path='index.html') to verify\n\n"
            f"CSS: dark bg #0a0a14, cards rgba(255,255,255,0.04) blur(20px) rounded-24px, "
            f"buttons gradient(135deg,#6366f1,#8b5cf6) rounded-14px, "
            f"titles gradient(#f97316,#22c55e) bg-clip text, system-ui font, hover transitions.\n\n"
            f"NEVER explain. NEVER plan. NEVER ask questions. Just call tools.",
        }
    else:
        # ── Full system prompt for larger models (26B, 31B) ──
        system = {
            "role": "system",
            "content": f"You are Local Coder, an autonomous AI coding agent. Working directory: {CWD}\n"
            f"Platform: macOS. Date: {time.strftime('%B %d, %Y')}.\n"
            f"You have tools. ALWAYS use tools to act. Never put code in chat — use write_file.\n\n"
            f"YOUR WORKFLOW (follow this exact order):\n"
        f"Step 1. GENERATE IMAGES FIRST — call generate_image for each image needed\n"
        f"   generate_image(prompt='cute sleeping cat icon, kawaii, pastel pink', filename='cat-sleep.png', size='256x256', steps=2)\n"
        f"   generate_image(prompt='playful dog icon, kawaii, pastel blue', filename='dog-play.png', size='256x256', steps=2)\n"
        f"Step 2. WRITE CODE — call write_file with complete HTML referencing those images\n"
        f"   write_file(path='index.html', content='<!DOCTYPE html>...<img src=\"cat-sleep.png\">...')\n"
        f"Step 3. PREVIEW — call preview_app to see the result in the browser\n"
        f"   preview_app(path='index.html')\n"
        f"Step 4. FIX if needed — if preview shows issues, call edit_file to fix, then preview_app again\n\n"
        f"IMAGES — MANDATORY:\n"
        f"- ALWAYS call generate_image to create images. NEVER use placeholder URLs or stock photo sites.\n"
        f"- Call generate_image BEFORE write_file (so the images exist when the page loads).\n"
        f"- Use size='256x256' steps=2 for icons (fast ~4s). Use size='512x512' steps=4 for hero images.\n\n"
        f"APP TEMPLATES (pre-built — ALWAYS check before building from scratch):\n"
        f"Skills directory: {_skills_dir}\n"
        f"  1. bash: ls {_skills_dir}\n"
        f"  2. Pick closest match: 'math quiz' → quiz-game, 'vocabulary' → flashcards, 'fan page' → gallery\n"
        f"  3. read_file the SKILL.md, then read_file assets/index.html\n"
        f"  4. Customize with write_file. Generate images with generate_image. Preview with preview_app.\n\n"
        f"ARCHITECTURE:\n"
        f"- Landing page / gallery / portfolio → ONE index.html file. No server.\n"
        f"- AI-powered app (chatbot, scanner) → 3 files: package.json + server.js + index.html\n"
        f"  NEVER build Express for a simple static page.\n\n"
        f"WEB APP ARCHITECTURE (ONLY for AI-powered apps that need a backend):\n\n"
        f"APP STRUCTURE: Always 3 files in the SAME directory:\n"
        f"  package.json, server.js, index.html (all inline CSS+JS)\n"
        f"  server.js must serve index.html with: app.get('/', (req,res) => res.sendFile(__dirname+'/index.html'));\n\n"
        f"SERVER.JS TEMPLATE (copy this pattern exactly):\n"
        f"  const express = require('express');\n"
        f"  const app = express();\n"
        f"  app.use(express.json({{limit:'50mb'}}));\n"
        f"  const API_BASE = process.env.LLM_API_BASE || 'http://127.0.0.1:8089/v1';\n"
        f"  const MODEL = process.env.LLM_MODEL || 'local';\n"
        f"  app.get('/', (req,res) => res.sendFile(__dirname+'/index.html'));\n"
        f"  app.post('/api/analyze', async (req,res) => {{\n"
        f"    const {{message, image}} = req.body;\n"
        f"    const userContent = image\n"
        f"      ? [{{type:'text',text:message}}, {{type:'image_url',image_url:{{url:image}}}}]\n"
        f"      : message;\n"
        f"    const r = await fetch(API_BASE+'/chat/completions', {{\n"
        f"      method:'POST', headers:{{'Content-Type':'application/json'}},\n"
        f"      body:JSON.stringify({{model:MODEL, stream:false, max_tokens:2048,\n"
        f"        messages:[{{role:'system',content:SYSTEM_PROMPT}}, {{role:'user',content:userContent}}]}}) }});\n"
        f"    const data = await r.json();\n"
        f"    res.json({{analysis: data.choices[0].message.content}}); }});\n"
        f"  app.listen(3000);\n\n"
        f"DESIGN SYSTEM (copy these EXACT values for all pages):\n"
        f"  COLORS: --bg:#0a0a14; --surface:rgba(255,255,255,0.04); --border:rgba(255,255,255,0.08); --text:#e2e8f0; --muted:#94a3b8; --accent:#8b5cf6; --accent2:#6366f1; --success:#22c55e; --warm:#f97316;\n"
        f"  FONT: font-family:system-ui,-apple-system,sans-serif; base 16px; line-height:1.6;\n"
        f"  SPACING: 0.5rem 1rem 1.5rem 2rem 3rem 4rem (use rem not px);\n"
        f"  RADIUS: buttons 14px; cards 24px; inputs 12px; pills 9999px; icons 50%;\n"
        f"  BODY: background:var(--bg); color:var(--text); min-height:100vh; padding:2rem; margin:0 auto; max-width:1200px;\n"
        f"  CARD: background:var(--surface); backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:24px; padding:2rem; transition:all 0.3s;\n"
        f"  CARD:HOVER: transform:translateY(-8px); border-color:rgba(255,255,255,0.2); box-shadow:0 20px 40px rgba(0,0,0,0.3);\n"
        f"  BUTTON: background:linear-gradient(135deg,var(--accent2),var(--accent)); color:white; font-weight:600; padding:14px 28px; border:none; border-radius:14px; cursor:pointer; transition:all 0.2s;\n"
        f"  BUTTON:HOVER: transform:translateY(-2px) scale(1.02); box-shadow:0 8px 25px rgba(139,92,246,0.4);\n"
        f"  TITLE: font-size:clamp(2rem,5vw,3.5rem); font-weight:800; background:linear-gradient(135deg,var(--warm),var(--success)); -webkit-background-clip:text; color:transparent;\n"
        f"  SUBTITLE: color:var(--muted); font-size:1.15rem; max-width:600px; margin:0 auto 2rem;\n"
        f"  GRID: display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:2rem;\n"
        f"  INPUT: background:rgba(255,255,255,0.05); border:1px solid var(--border); border-radius:12px; padding:14px 18px; color:white; font-size:1rem; width:100%; transition:border-color 0.2s;\n"
        f"  INPUT:FOCUS: border-color:var(--accent); outline:none; box-shadow:0 0 0 3px rgba(139,92,246,0.15);\n"
        f"  HERO: text-align:center; padding:4rem 1rem 3rem;\n"
        f"  NAV: display:flex; justify-content:space-between; align-items:center; padding:1rem 2rem; background:rgba(0,0,0,0.3); backdrop-filter:blur(10px); border-bottom:1px solid var(--border);\n"
        f"  FOOTER: text-align:center; padding:2rem; color:var(--muted); border-top:1px solid var(--border); margin-top:auto;\n"
        f"  BADGE: display:inline-block; padding:4px 12px; border-radius:9999px; font-size:0.8rem; background:rgba(139,92,246,0.15); color:var(--accent);\n"
        f"  ICON-CIRCLE: width:80px; height:80px; border-radius:50%; border:3px solid var(--accent); padding:8px; object-fit:cover;\n"
        f"  SECTION: padding:4rem 0; text-align:center;\n"
        f"  STATS: display:flex; justify-content:center; gap:3rem; .stat-num font-size:2.5rem font-weight:800 color:var(--accent);\n"
        f"  ANIMATION: @keyframes fadeUp{{from{{opacity:0;transform:translateY(20px)}}to{{opacity:1;transform:translateY(0)}}}} .fade-up{{animation:fadeUp 0.6s ease forwards}}\n"
        f"  GLASS-EFFECT: background:rgba(255,255,255,0.03); backdrop-filter:blur(20px) saturate(180%); -webkit-backdrop-filter:blur(20px) saturate(180%);\n\n"
        f"- IMAGE UPLOAD (must use FileReader, never fake it):\n"
        f"  let imageBase64 = null;\n"
        f"  function uploadImage() {{\n"
        f"    const input = document.createElement('input');\n"
        f"    input.type='file'; input.accept='image/*';\n"
        f"    input.onchange = e => {{\n"
        f"      const file = e.target.files[0]; if(!file) return;\n"
        f"      const reader = new FileReader();\n"
        f"      reader.onload = ev => {{ imageBase64 = ev.target.result;\n"
        f"        document.getElementById('preview').src = imageBase64;\n"
        f"        document.getElementById('preview').style.display = 'block'; }};\n"
        f"      reader.readAsDataURL(file); }};\n"
        f"    input.click(); }}\n\n"
        f"- CAMERA CAPTURE (must use getUserMedia, never fake it):\n"
        f"  async function openCamera() {{\n"
        f"    const stream = await navigator.mediaDevices.getUserMedia({{video:{{facingMode:'environment'}}}});\n"
        f"    const video = document.getElementById('camVideo');\n"
        f"    video.srcObject = stream; video.style.display='block'; video.play();\n"
        f"    document.getElementById('captureBtn').style.display='inline-block'; }}\n"
        f"  function capturePhoto() {{\n"
        f"    const video = document.getElementById('camVideo');\n"
        f"    const canvas = document.createElement('canvas');\n"
        f"    canvas.width=video.videoWidth; canvas.height=video.videoHeight;\n"
        f"    canvas.getContext('2d').drawImage(video,0,0);\n"
        f"    imageBase64 = canvas.toDataURL('image/jpeg',0.8);\n"
        f"    document.getElementById('preview').src = imageBase64;\n"
        f"    document.getElementById('preview').style.display='block';\n"
        f"    video.srcObject.getTracks().forEach(t=>t.stop()); video.style.display='none'; }}\n\n"
        f"- SEND TO API (always this pattern):\n"
        f"  async function analyze() {{\n"
        f"    const msg = document.getElementById('input').value;\n"
        f"    if(!msg && !imageBase64) return;\n"
        f"    document.getElementById('result').innerHTML = '<div class=\"loading\">Analyzing...</div>';\n"
        f"    const res = await fetch('/api/analyze', {{\n"
        f"      method:'POST', headers:{{'Content-Type':'application/json'}},\n"
        f"      body:JSON.stringify({{message:msg||'Analyze this', image:imageBase64}}) }});\n"
        f"    const data = await res.json();\n"
        f"    document.getElementById('result').innerHTML = formatMarkdown(data.analysis);\n"
        f"    imageBase64 = null; }}\n\n"
        f"- MARKDOWN RENDERER:\n"
        f"  function formatMarkdown(text) {{\n"
        f"    return text.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')\n"
        f"      .replace(/^### (.+)$/gm,'<h3>$1</h3>').replace(/^## (.+)$/gm,'<h2>$1</h2>')\n"
        f"      .replace(/^\\* (.+)$/gm,'<li>$1</li>').replace(/\\n/g,'<br>'); }}\n\n"
        f"- LOADING/SCANNING ANIMATION (always show while waiting for API):\n"
        f"  CSS: @keyframes scan {{ 0%{{transform:translateY(-100%)}} 100%{{transform:translateY(100%)}} }}\n"
        f"  .scanning {{ position:relative; overflow:hidden; }}\n"
        f"  .scanning::after {{ content:''; position:absolute; left:0; right:0; height:2px;\n"
        f"    background:linear-gradient(90deg,transparent,#22c55e,transparent); animation:scan 1.5s infinite; }}\n"
        f"  Also add a pulsing text: <div class='loading'>🔬 Scanning ingredients<span class='dots'></span></div>\n"
        f"  CSS: @keyframes dots {{ 0%{{content:''}} 33%{{content:'.'}} 66%{{content:'..'}} 100%{{content:'...'}} }}\n"
        f"  .dots::after {{ content:''; animation:dots 1.5s infinite steps(4); }}\n"
        f"  Show loading BEFORE fetch, hide AFTER response. Disable button during loading.\n\n"
        f"- NEVER fake FileReader/camera/API calls. ALWAYS use real implementations above.\n"
        f"- NEVER use SSE/streaming. Use stream:false and return full JSON.\n"
        f"- ALWAYS serve index.html from __dirname, NOT from a public/ subdirectory.\n"
        f"- ALWAYS test after building: npm install, node server.js &, curl POST to verify.\n\n"
        f"SELF-TESTING (MANDATORY for HTML/web apps):\n"
        f"After writing ANY index.html or web app, you MUST test it:\n"
        f"1. If static HTML: run 'bash: python3 -m http.server 8888 &' then 'bash: sleep 1 && curl -s http://localhost:8888/ | head -5' to verify it loads\n"
        f"2. If Express app: run 'bash: node server.js &' then 'bash: sleep 2 && curl -s http://localhost:3000/ | head -5'\n"
        f'3. Check for JS errors: run \'bash: node -e "const fs=require(\\"fs\\"); const html=fs.readFileSync(\\"index.html\\",\\"utf8\\"); const scripts=html.match(/<script[^>]*>([\\\\s\\\\S]*?)<\\\\/script>/g); scripts?.forEach(s => {{ try {{ new Function(s.replace(/<\\\\/?script[^>]*>/g,\\"\\")); }} catch(e) {{ console.error(\\"JS ERROR:\\", e.message); }} }})"\'\n'
        f"4. If ANY test fails: read the error, fix the code, test again. DO NOT STOP until tests pass.\n"
        f"5. For interactive elements (buttons, forms): verify onclick/event handlers exist in the HTML.\n"
        f"6. For SVG: verify paths are valid (use M, L, A, Z commands with real coordinates, not placeholders).\n\n"
        f"BROWSER TESTING (use computer_use to test interactively):\n"
        f"- After building an HTML app, open it: computer_use action:open:file:///path/to/index.html\n"
        f"- Take screenshot to see the result\n"
        f"- Click buttons: computer_use action:click:x,y\n"
        f"- If something looks wrong, fix the code and re-test\n\n"
        f"COMMON BUGS TO AVOID:\n"
        f'- Start screen not hiding on button click: always add onclick=\'document.getElementById("screen1").style.display="none"; document.getElementById("screen2").style.display="block"\'\n'
        f"- SVG pie/chart slices: use Math.cos(angle*Math.PI/180)*radius and Math.sin() for arc endpoints. Never approximate.\n"
        f"- Fetch to /api without server: static HTML files can't call /api. Use local JS logic or start Express.\n"
        f"- Missing CSS transitions: always add transition on hover/active states.\n\n"
        f"RULES (CRITICAL — follow strictly):\n"
        f"- NEVER explain or narrate. NEVER ask for confirmation. NEVER say 'I will' or 'Let me'. Just call tools.\n"
        f"- NEVER put code in chat messages. ALL code goes through write_file.\n"
        f"- NEVER use placeholder image URLs. ALL images go through generate_image.\n"
        f"- NEVER stop after generating images. ALWAYS continue to write_file then preview_app.\n"
        f"- For EVERY user request: call tools immediately. No planning text. No questions. Just act.\n\n"
        f"REMEMBER: You are an AGENT, not a chatbot. Your response should be TOOL CALLS, not text.",
    }

    # Add language instruction to system prompt for non-English UI
    _lang_instr = _t("sys_lang_instruction")
    if _lang_instr:
        system["content"] = _lang_instr + "\n\n" + system["content"]

    # Auto-detect running model + load saved preference (skip if user specified -m)
    if not args.model:
        _load_last_model()

    # FORCE: if user passed -m, override everything — no auto-detect can change this
    if args.model:
        MODEL = args.model
        if not args.api:
            API_BASE = "http://127.0.0.1:11434/v1"
        logger.info(f"Model forced by -m flag: {MODEL} @ {API_BASE}")

    show_banner()

    # One-time iTerm2 Arabic setup hint
    if UI_LANG == "ar":
        _cfg_hint = _load_config()
        if not _cfg_hint.get("iterm_rtl_hint_shown"):
            tp = os.environ.get("TERM_PROGRAM", "").lower()
            if tp.startswith("iterm"):
                console.print()
                console.print(
                    Panel(
                        "[bold]لأفضل عرض للعربية في iTerm2:[/]\n\n"
                        "  [cyan]1.[/] Settings → General → Experimental → [bold]Enable RTL scripts[/]\n"
                        "  [cyan]2.[/] Settings → Profiles → Text → Non-ASCII Font → [bold]Noto Sans Arabic[/]\n"
                        "  [cyan]3.[/] Settings → Profiles → Text → Font Size → [bold]14+[/]",
                        title="[bold #c084fc]إعداد الخط العربي[/]",
                        border_style="#3f3f46",
                        padding=(1, 2),
                        width=min(75, console.width - 4),
                    )
                )
                _save_config(iterm_rtl_hint_shown=True)

    # Initialize session persistence
    _session = Session(cwd=CWD, model=MODEL)
    logger.info(f"Session: {_session.session_id}")

    # One-shot mode
    if args.prompt:
        console.print(_render_user_turn(args.prompt))
        console.print()
        messages = [system, {"role": "user", "content": args.prompt}]
        _session.add_message(system)
        _session.add_message({"role": "user", "content": args.prompt})
        agent_loop(messages, perms, session=_session)
        return

    # Interactive mode
    bi = BACKEND_INFO
    cwd_short = os.path.basename(CWD)
    console.print()
    shortcuts = Text()
    shortcuts.append("  ")
    # Build shortcut pairs: (key, key_style, label, label_style)
    _shortcut_pairs = [
        (" ctrl+r ", "bold white on #3d5a80", f" {_t('voice') or 'voice'} ", "dim"),
        (
            " Enter ",
            "bold white on #6b21a8",
            f" {_t('stop_send') or 'stop + send'} ",
            "dim",
        ),
        (" ctrl+v ", "bold white on #3d5a80", f" {_t('image') or 'image'} ", "dim"),
        (" /gpu ", "bold white on #555555", f" {_t('stats') or 'stats'} ", "dim"),
        (" /clean ", "bold white on #555555", f" {_t('free') or 'free'} ", "dim"),
        (" /think ", "bold white on #555555", f" {_t('reason') or 'reason'} ", "dim"),
        (" /models ", "bold white on #555555", f" {_t('switch') or 'switch'} ", "dim"),
    ]
    for key, key_style, label, label_style in _shortcut_pairs:
        shortcuts.append(key, style=key_style)
        shortcuts.append(label, style=label_style)
    console.print(shortcuts)
    console.print()

    total_tokens = 0
    history_file = os.path.join(CWD, ".localcoder-history.json")

    # --continue: restore last session (try JSONL first, then legacy JSON)
    if args.cont:
        loaded = False
        try:
            last_id = get_latest_session_id()
            if last_id:
                prev_session, prev_cwd, prev_model = Session.load(last_id)
                messages = prev_session.get_messages_for_continuation()
                if messages:
                    # Re-wrap with current session (new ID, preserves old history)
                    for msg in messages:
                        _session.add_message(msg)
                    n = len([m for m in messages if isinstance(m, dict) and m.get("role") == "user"])
                    console.print(
                        f"  [green]{_ui(f'✦ Resumed session ({n} messages)', f'✦ استئناف الجلسة ({n} رسائل)', f'✦ Session restaurée ({n} messages)')}[/]"
                    )
                    loaded = True
        except Exception as e:
            logger.warning(f"JSONL session load failed: {e}")

        if not loaded:
            # Legacy fallback: old JSON history
            try:
                with open(history_file) as f:
                    messages = json.load(f)
                n = len([m for m in messages if isinstance(m, dict) and m.get("role") == "user"])
                console.print(
                    f"  [green]{_ui(f'✦ Resumed session ({n} messages)', f'✦ استئناف الجلسة ({n} رسائل)', f'✦ Session restaurée ({n} messages)')}[/]"
                )
                for msg in messages:
                    _session.add_message(msg)
            except Exception:
                console.print(
                    f"  [dim]{_ui('No saved session — starting fresh', 'لا توجد جلسة محفوظة — بداية جديدة', 'Pas de session sauvegardée — nouveau départ')}[/]"
                )
                messages = [system]
                _session.add_message(system)
    else:
        messages = [system]
        _session.add_message(system)

    # Clipboard image state + voice state
    _clipboard_image_path = [None]
    _voice_proc = [None]  # active recording process
    _voice_wav = [None]  # wav file path

    # Voice input setup
    _voice_available = False
    _voice_lang = "auto"
    try:
        import shutil as _shutil

        _whisper_bin = _shutil.which("whisper-cli")
        _sox_rec = _shutil.which("rec")

        # Load language preference — match UI language if not explicitly set
        _cfg = _load_config()
        _voice_lang = _cfg.get("voice_language", "auto")
        # When using Arabic/French UI, always match voice language
        if UI_LANG in ("ar", "fr"):
            _voice_lang = UI_LANG
        _whisper_model = _resolve_whisper_model(_voice_lang, _cfg)
        _voice_available = bool(
            _whisper_bin and os.path.exists(_whisper_model) and _sox_rec
        )

        # First-time voice setup — ask language
        if (
            _voice_available
            and _voice_lang == "auto"
            and not _cfg.get("voice_setup_done")
        ):
            console.print()
            console.print(
                Panel(
                    "[bold]Voice Input Setup[/]  [dim]one-time configuration[/]",
                    border_style="#81b29a",
                    padding=(0, 1),
                )
            )
            console.print(
                f"  [dim]Select your primary speaking language for voice input:[/]\n"
            )
            LANGS = [
                ("en", "English"),
                ("fr", "French"),
                ("ar", "Arabic"),
                ("es", "Spanish"),
                ("de", "German"),
                ("ja", "Japanese"),
                ("zh", "Chinese"),
                ("auto", "Auto-detect (less accurate on short phrases)"),
            ]
            for i, (code, name) in enumerate(LANGS):
                console.print(f"    [bold]{i + 1}.[/] {name} [dim]({code})[/]")
            console.print()
            try:
                ans = input("  ▸ ").strip()
                idx = int(ans) - 1 if ans.isdigit() else 0
                if 0 <= idx < len(LANGS):
                    _voice_lang = LANGS[idx][0]
                else:
                    _voice_lang = "en"
            except:
                _voice_lang = "en"
            _save_config(voice_language=_voice_lang, voice_setup_done=True)
            console.print(f"  [green]✓ Voice language: {_voice_lang}[/]")
            console.print(f"  [dim]Change anytime with /voice-lang[/]\n")

        if _voice_available:
            logger.info(
                f"Voice input available (whisper-cli + rec, lang={_voice_lang}, model={_whisper_model})"
            )
    except:
        pass

    # Voice animation state
    _voice_anim_stop = [None]  # threading.Event when animation is running
    _ctrl_c_armed_at = [0.0]

    def _voice_animation_thread(stop_event):
        """Background thread: animated waveform bars while recording."""
        bars = "▁▂▃▄▅▆▇█"
        # Gradient: cyan → green → magenta → blue
        colors = [
            "\033[36m",
            "\033[32m",
            "\033[35m",
            "\033[34m",
            "\033[36m",
            "\033[32m",
            "\033[35m",
            "\033[34m",
        ]
        label = _t("recording") or "Recording..."
        stop_hint = _t("press_ctrlr_stop") or "Ctrl+R to stop"
        start_t = time.time()
        fd = None
        try:
            fd = os.open("/dev/tty", os.O_WRONLY)
            os.write(fd, b"\n")
            n_bars = 24
            while not stop_event.is_set():
                elapsed = int(time.time() - start_t)
                mins, secs = divmod(elapsed, 60)
                time_str = f"{mins:01d}:{secs:02d}"
                # Generate waveform with smooth random levels
                wave = ""
                for i in range(n_bars):
                    ci = i % len(colors)
                    level = random.randint(0, len(bars) - 1)
                    wave += f"{colors[ci]}{bars[level]}\033[0m"
                line = f"\r  \033[1;35m●\033[0m \033[35m{label}\033[0m  {wave}  \033[2;33m{time_str}\033[0m  \033[2m{stop_hint}\033[0m\033[K"
                os.write(fd, line.encode())
                stop_event.wait(0.12)
        except Exception:
            pass
        finally:
            if fd is not None:
                try:
                    os.write(fd, b"\r\033[K")  # clear animation line
                    os.close(fd)
                except Exception:
                    pass

    # Key bindings
    kb = KeyBindings()

    @kb.add("c-r")
    def _voice_toggle(event):
        """Ctrl+R: toggle voice — start recording or stop+transcribe."""
        if not _voice_available:
            try:
                fd = os.open("/dev/tty", os.O_WRONLY)
                os.write(
                    fd,
                    b"\n  \033[33m"
                    + (
                        _t("voice_not_avail")
                        or "Voice not available. Run: localcoder --setup"
                    ).encode()
                    + b"\033[0m\n",
                )
                os.close(fd)
            except:
                pass
            return

        if _voice_proc[0] is not None:
            # STOP + TRANSCRIBE (Ctrl+R again)
            _do_voice_transcribe(event)
            return

        # START RECORDING
        try:
            _voice_wav[0] = os.path.join(CWD, ".localcoder-voice.wav")
            _voice_proc[0] = subprocess.Popen(
                [
                    _sox_rec,
                    "-q",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-b",
                    "16",
                    _voice_wav[0],
                    "trim",
                    "0",
                    "30",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Start waveform animation
            _voice_anim_stop[0] = threading.Event()
            anim = threading.Thread(
                target=_voice_animation_thread,
                args=(_voice_anim_stop[0],),
                daemon=True,
            )
            anim.start()
        except Exception as e:
            _voice_proc[0] = None
            if _voice_anim_stop[0]:
                _voice_anim_stop[0].set()
                _voice_anim_stop[0] = None
            try:
                fd = os.open("/dev/tty", os.O_WRONLY)
                os.write(fd, f"\n  \033[31mRecord error: {e}\033[0m\n".encode())
                os.close(fd)
            except:
                pass

    @kb.add("c-c")
    def _ctrl_c_clear_or_quit(event):
        """Ctrl+C once clears input, Ctrl+C twice on empty input quits."""
        buf = event.app.current_buffer
        if buf.text:
            buf.set_document(Document("", 0), bypass_readonly=True)
            _ctrl_c_armed_at[0] = 0.0
            return

        now = time.time()
        if now - _ctrl_c_armed_at[0] < 1.5:
            event.app.exit(exception=KeyboardInterrupt, style="")
            return

        _ctrl_c_armed_at[0] = now
        try:
            fd = os.open("/dev/tty", os.O_WRONLY)
            os.write(fd, b"\r\033[K  \033[2mCtrl+C again to quit\033[0m\n")
            os.close(fd)
        except Exception:
            pass

    def _do_voice_transcribe(event):
        """Stop recording and transcribe."""
        if _voice_proc[0] is None:
            return

        # Stop animation first
        if _voice_anim_stop[0]:
            _voice_anim_stop[0].set()
            _voice_anim_stop[0] = None
            time.sleep(0.15)  # let animation thread clean up

        try:
            fd = os.open("/dev/tty", os.O_WRONLY)

            # Stop recording
            try:
                _voice_proc[0].send_signal(signal.SIGINT)
                _voice_proc[0].wait(timeout=3)
            except:
                _voice_proc[0].kill()
            _voice_proc[0] = None

            os.write(
                fd,
                b"  \033[2m"
                + (_t("transcribing") or "Transcribing...").encode()
                + b"\033[0m\n",
            )

            # Transcribe with whisper (Metal GPU — only ~200MB, fits in headroom)
            # For Arabic: use beam-size 5 for much better accuracy
            whisper_cmd = [
                _whisper_bin,
                "--model",
                _whisper_model,
                "--language",
                _voice_lang,
                "--no-timestamps",
                "--threads",
                "8",
                "--file",
                _voice_wav[0],
            ]
            # Stronger decoding improves Arabic/non-English recognition.
            if _voice_lang in ("ar", "fr", "es", "de", "ja", "zh"):
                whisper_cmd.extend(
                    [
                        "--beam-size",
                        "8",
                        "--best-of",
                        "8",
                        "--temperature",
                        "0",
                        "--no-fallback",
                    ]
                )
            if _voice_lang == "ar":
                whisper_cmd.extend(
                    [
                        "--prompt",
                        "نص عربي واضح باللغة العربية الفصحى مع أسماء الأماكن والكلمات الدينية بشكل صحيح.",
                    ]
                )
            result = subprocess.run(
                whisper_cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Parse detected language
            lang = ""
            for line in result.stderr.split("\n"):
                if "auto-detected language:" in line:
                    lang = line.split("auto-detected language:")[-1].strip().split()[0]

            # Parse transcription
            lines = []
            for line in result.stdout.split("\n"):
                line = line.strip()
                if (
                    line
                    and not line.startswith("[")
                    and not line.startswith("whisper_")
                ):
                    lines.append(line)
            text = " ".join(lines).strip()
            text = text.replace("(silence)", "").replace("[BLANK_AUDIO]", "").strip()
            # Filter common whisper hallucinations for non-English
            for hallucination in [
                "(speaking in foreign language)",
                "(Speaking in foreign language)",
                "(speaks in foreign language)",
                "(foreign language)",
                "(musique)",
                "(music)",
                "[Music]",
                "(Musique)",
            ]:
                text = text.replace(hallucination, "").strip()

            if text:
                lang_tag = f" [{lang}]" if lang else ""
                os.write(
                    fd,
                    f"  \033[32m✓\033[0m \033[2m{text[:80]}{lang_tag}\033[0m\n".encode(),
                )
                event.app.current_buffer.insert_text(text)
            else:
                os.write(
                    fd,
                    b"  \033[2m"
                    + (_t("no_speech") or "No speech detected").encode()
                    + b"\033[0m\n",
                )

            os.close(fd)

            # Cleanup
            if _voice_wav[0] and os.path.exists(_voice_wav[0]):
                os.unlink(_voice_wav[0])

        except Exception as e:
            _voice_proc[0] = None
            try:
                os.write(fd, f"\n  \033[31mTranscribe error: {e}\033[0m\n".encode())
                os.close(fd)
            except:
                pass

    @kb.add("c-v")
    def _paste_image(event):
        """Ctrl+V: check clipboard for image, show preview immediately."""
        img = get_clipboard_image()
        if img:
            _clipboard_image_path[0] = img
            buf = event.app.current_buffer
            buf.insert_text("[📎 image] ")
            # Show preview immediately by writing to /dev/tty (bypasses prompt_toolkit)
            try:
                timg = "/opt/homebrew/bin/timg"
                if os.path.exists(timg):
                    tty_fd = os.open("/dev/tty", os.O_WRONLY)
                    os.write(tty_fd, b"\n")
                    spawnSync = subprocess.Popen(
                        [timg, "-g", "40x12", "-C", "-p", "i", img],
                        stdout=tty_fd,
                        stderr=tty_fd,
                    )
                    spawnSync.wait(timeout=5)
                    sz = os.path.getsize(img) // 1024
                    os.write(tty_fd, f"  📎 clipboard ({sz} KB)\n".encode())
                    os.close(tty_fd)
            except:
                pass
        else:
            # Normal paste — insert text from clipboard
            try:
                txt = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=2
                ).stdout
                if txt:
                    event.app.current_buffer.insert_text(txt)
            except:
                pass

    # Slash command autocomplete
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML as PT_HTML_CMD

    SLASH_COMMANDS = {
        "/models": "Switch model (fuzzy search)",
        "/model": "Set model by name",
        "/clear": "Clear conversation",
        "/gpu": "Show GPU memory, swap, model status",
        "/clean": "Free GPU memory (unload idle models)",
        "/health": "Full GPU health dashboard",
        "/resume": "Restore last session",
        "/context": "Show token usage",
        "/paste": "Paste clipboard image",
        "/undo": "Revert last file change",
        "/snapshots": "List file backups",
        "/diff": "Show file changes",
        "/cost": "Show token cost ($0.00)",
        "/ask": "Ask before every tool",
        "/auto": "Auto-approve safe tools",
        "/bypass": "Approve everything",
        "/yolo": "Same as /bypass",
        "/log": "View debug log",
        "/think": "Toggle reasoning: none → low → medium → high",
        "/deploy": "Generate & deploy an AI-powered React app",
        "/handoff": "Generate a focused prompt for a new session",
        "/sessions": "List recent sessions",
        "/mcp": "Show MCP servers and tools",
        "/exit": "Exit",
    }

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/"):
                for cmd, desc in SLASH_COMMANDS.items():
                    if text.lower() in cmd.lower() or cmd.startswith(text):
                        # Escape XML-invalid chars in description
                        safe_desc = (
                            desc.replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                        )
                        try:
                            display = PT_HTML_CMD(
                                f'<b>{cmd}</b> <style fg="ansigray">{safe_desc}</style>'
                            )
                        except Exception:
                            display = f"{cmd} {desc}"
                        yield Completion(
                            cmd,
                            start_position=-len(text),
                            display=display,
                        )

    from prompt_toolkit.styles import Style as PTStyle

    _prompt_style = PTStyle.from_dict(
        {
            "": "#e5e7eb",
            "prompt.label": "bold #c084fc",
            "frame.border": "#3f3f46",
            "bottom-toolbar": "bg:#0f172a #cbd5e1",
            "rprompt": "#94a3b8",
            "completion-menu": "bg:#111827 #e5e7eb",
            "completion-menu.completion.current": "bg:#1f2937 #ffffff",
        }
    )

    def _get_rprompt():
        """Right-side prompt hints — localized."""
        if UI_LANG == "ar":
            # Mixed RTL/LTR hints render poorly in many terminals. Keep this compact.
            return ""
        return _ui(
            "Enter to send  ·  /think for reasoning  ·  ? for help",
            None,
            "Entrée pour envoyer  ·  /think pour réfléchir  ·  ? pour aide",
        )

    if UI_LANG == "ar":
        _prompt_label = f"{_display_text('رسالة')} ▸ "
    else:
        _prompt_label = _ui("❯ ", None, "message ▸ ")

    session = PromptSession(
        history=FileHistory(os.path.join(CWD, ".localcoder-input-history")),
        bottom_toolbar=get_toolbar,
        key_bindings=kb,
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=_prompt_style,
        reserve_space_for_menu=0,
        erase_when_done=True,
        refresh_interval=0.08,
    )

    while True:
        _clipboard_image_path[0] = None
        try:
            task = session.prompt(
                HTML(
                    f'<style fg="ansimagenta" bg="" bold="true">{_prompt_label}</style>'
                ),
                rprompt=_get_rprompt,
                show_frame=True,
            ).strip()
        except KeyboardInterrupt:
            console.print(f"\n  [dim]{_t('bye') or 'bye'}[/]")
            break
        except EOFError:
            break

        if not task:
            continue

        if task == "/clear":
            messages = [system]
            total_tokens = 0
            console.clear()
            show_banner()
            bi = BACKEND_INFO
            ml = f"Gemma 4 {bi['size']}" if bi["size"] else MODEL
            qt = f" {bi['quant']}" if bi["quant"] else ""
            console.print(
                f"\n  [dim]model[/] [bold cyan]{ml}{qt}[/]  [dim]backend[/] [bold green]{bi['backend']}[/]  [dim]ctx[/] [bold green]{bi['ctx'] or '?'}[/]  [dim]perms[/] [bold yellow]{perms.mode}[/]"
            )
            console.print(f"  [green]Conversation cleared.[/]\n")
            continue
        if task == "/cost":
            console.print(f"  [green]$0.00 — {total_tokens} tokens[/]")
            continue
        if task.startswith("/think"):
            global REASONING_EFFORT
            import tty, termios

            levels = ["none", "low", "medium", "high"]
            icons = ["⚡", "💭", "🧠", "🔬"]
            tags = ["off", "light", "think", "deep"]
            descs = ["No thinking", "Quick reasoning", "Balanced", "Deep reasoning"]
            idx = levels.index(REASONING_EFFORT) if REASONING_EFFORT in levels else 2

            # ANSI colors
            DIM = "\033[2m"
            BOLD = "\033[1m"
            REV = "\033[7m"  # reverse video (highlight)
            RST = "\033[0m"

            def _draw(i):
                bar = f"  {icons[i]} "
                for j in range(len(levels)):
                    if j == i:
                        bar += f" {REV}{BOLD} {tags[j]} {RST} "
                    else:
                        bar += f" {DIM} {tags[j]} {RST} "
                bar += f" {DIM}{descs[i]}  ← → enter{RST}"
                sys.stdout.write(f"\r\033[K{bar}")
                sys.stdout.flush()

            _draw(idx)
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch == "\r" or ch == "\n":
                        break
                    if ch == "\x1b":
                        seq = sys.stdin.read(2)
                        if seq == "[D":  # left
                            idx = max(0, idx - 1)
                        elif seq == "[C":  # right
                            idx = min(len(levels) - 1, idx + 1)
                    elif ch == "q" or ch == "\x03":
                        break
                    _draw(idx)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

            REASONING_EFFORT = levels[idx]
            sys.stdout.write(f"\r\033[K")
            console.print(
                f"  {icons[idx]} Reasoning: [bold]{REASONING_EFFORT}[/] — {descs[idx]}"
            )
            continue
        if task == "/context":
            ctx_used = estimate_tokens(json.dumps(messages))
            ctx_str = BACKEND_INFO.get("ctx", "")
            if ctx_str:
                ctx_max = int(ctx_str.replace("K", "")) * 1024
                context_usage_bar(console, ctx_used, ctx_max)
            else:
                console.print(
                    f"  [cyan]~{ctx_used} tokens / ? ({len(messages)} msgs)[/]"
                )
            continue
        if task == "/gpu":
            try:
                from localcoder.backends import (
                    get_machine_specs,
                    get_metal_gpu_stats,
                    get_swap_usage_mb,
                    get_llama_server_config,
                    _detect_model_info,
                    get_top_memory_processes,
                )

                specs = get_machine_specs()
                metal = get_metal_gpu_stats()
                swap = get_swap_usage_mb()
                srv = get_llama_server_config()
                procs = get_top_memory_processes(min_mb=300, limit=5)

                gt = metal.get("total_mb") or specs["gpu_total_mb"]
                sc = "red" if swap > 4000 else "yellow" if swap > 1000 else "green"

                # Use model size, not ioreg alloc
                model_mb = 0
                if srv.get("running"):
                    mi_gpu = _detect_model_info(srv, None)
                    model_mb = int((mi_gpu.get("size_gb") or 0) * 1024)
                if model_mb == 0:
                    model_mb = 12 * 1024  # fallback

                gf = max(0, gt - model_mb)
                gc = "green" if model_mb < gt else "red"

                bar_w = 30
                pct = min(1.0, model_mb / max(1, gt))
                filled = int(pct * bar_w)
                bc = "green" if pct < 0.75 else "yellow" if pct < 0.9 else "red"
                bar = f"[{bc}]{'━' * filled}[/{bc}][dim]{'─' * (bar_w - filled)}[/]"

                console.print(
                    f"\n  [bold]GPU[/]  {bar}  [{gc}]{model_mb // 1024}/{gt // 1024}GB[/{gc}]  free: {gf // 1024}GB"
                )
                console.print(
                    f"  [bold]Swap[/] [{sc}]{swap // 1024}GB[/{sc}]  [bold]Pressure[/] {specs.get('mem_pressure', '?')}"
                )

                if srv.get("running"):
                    mi = _detect_model_info(srv, None)
                    ms = mi["name"] or "?"
                    if mi["quant"]:
                        ms += f" {mi['quant']}"
                    gi = "[green]GPU[/]" if srv["ngl"] >= 90 else "[red]CPU[/]"
                    console.print(
                        f"  [bold]Model[/] [cyan]{ms}[/]  {gi}  ctx {srv['n_ctx'] // 1024}K  footprint {srv.get('footprint_mb', 0)}MB"
                    )

                app_procs = [p for p in procs if p["category"] == "app"]
                if app_procs:
                    hogs = "  ".join(
                        f"{p['name']}{'×' + str(p['count']) if p.get('count', 1) > 1 else ''} {p['mb'] // 1024}G"
                        for p in app_procs[:4]
                    )
                    console.print(f"  [bold]Apps[/]  {hogs}")
                console.print()
            except ImportError:
                console.print("  [dim]Install localcoder package for GPU stats[/]")
            continue
        if task == "/clean":
            try:
                from localcoder.backends import (
                    cleanup_gpu_memory,
                    get_metal_gpu_stats,
                    get_swap_usage_mb,
                    get_top_memory_processes,
                )

                # Before
                metal_before = get_metal_gpu_stats()
                swap_before = get_swap_usage_mb()
                ga_before = metal_before.get("alloc_mb", 0)

                console.print(
                    f"\n  [yellow]Freeing GPU memory...[/]  [dim](safe — won't close your apps)[/]"
                )
                result = cleanup_gpu_memory(force=False)

                if result["ollama_unloaded"]:
                    console.print(
                        f"  [green]✓[/] Unloaded: {', '.join(result['ollama_unloaded'])}"
                    )
                else:
                    console.print(f"  [dim]No idle models to unload.[/]")

                # After
                import time as _tc

                _tc.sleep(1)
                metal_after = get_metal_gpu_stats()
                swap_after = get_swap_usage_mb()
                ga_after = metal_after.get("alloc_mb", 0)
                gt = metal_after.get("total_mb") or 16384
                freed = max(0, ga_before - ga_after)

                gc = "green" if ga_after < gt else "red"
                console.print(
                    f"  [bold]Before[/] {ga_before // 1024}GB  [bold]After[/] [{gc}]{ga_after // 1024}GB[/{gc}]  [bold]Freed[/] {freed // 1024}GB  [bold]Swap[/] {swap_after // 1024}GB"
                )

                app_procs = get_top_memory_processes(min_mb=500, limit=3)
                apps = [p for p in app_procs if p["category"] == "app"]
                if apps and ga_after > gt:
                    console.print(
                        f"  [dim]Still overloaded. Close these for more: {', '.join(p['name'] for p in apps[:3])}[/]"
                    )
                console.print()
            except ImportError:
                console.print("  [dim]Install localcoder package for cleanup[/]")
            continue
        if task == "/health":
            try:
                from localcoder.backends import print_health_dashboard

                print_health_dashboard()
            except ImportError:
                console.print(
                    "  [dim]Install localcoder package for health dashboard[/]"
                )
            continue
        if task == "/resume":
            try:
                with open(history_file) as f:
                    messages = json.load(f)
                console.print(f"  [green]Resumed {len(messages)} messages[/]")
            except:
                console.print("  [dim]No saved session[/]")
            continue
        if task in ("/ask", "/auto", "/bypass", "/yolo"):
            perms.mode = "bypass" if task == "/yolo" else task[1:]
            console.print(f"  [yellow]Permissions: {perms.mode}[/]")
            continue
        if task == "/undo" or task.startswith("/undo "):
            parts = task.split(None, 1)
            path = parts[1] if len(parts) > 1 else None
            msg = restore_snapshot(0, path)
            console.print(f"  [green]{msg}[/]")
            continue
        if task == "/snapshots" or task.startswith("/snapshots "):
            parts = task.split(None, 1)
            path = parts[1] if len(parts) > 1 else None
            console.print(list_snapshots(path))
            continue
        if task.startswith("/diff "):
            path = task.split(None, 1)[1]
            full = os.path.join(CWD, path)
            if os.path.isfile(full):
                # Diff current vs latest snapshot
                snaps = (
                    sorted(
                        [
                            s
                            for s in os.listdir(SNAPSHOT_DIR)
                            if path.replace("/", "__") in s
                        ],
                        reverse=True,
                    )
                    if os.path.isdir(SNAPSHOT_DIR)
                    else []
                )
                if snaps:
                    snap_path = os.path.join(SNAPSHOT_DIR, snaps[0])
                    try:
                        r = subprocess.run(
                            ["diff", "--color=always", "-u", snap_path, full],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if r.stdout:
                            console.print(
                                Panel(
                                    r.stdout[:3000],
                                    title=f"[bold]diff {path}[/]",
                                    border_style="yellow",
                                )
                            )
                        else:
                            console.print(f"  [dim]No changes since last snapshot[/]")
                    except:
                        console.print(f"  [red]diff failed[/]")
                else:
                    console.print(f"  [dim]No snapshots for {path}[/]")
            else:
                console.print(f"  [red]File not found: {path}[/]")
            continue
        if task in ("/models", "/model"):
            new_model, new_url = select_model_interactive()
            if new_model:
                _switch_model(new_model, new_url)
            continue
        if task.startswith("/model "):
            name = task.split(None, 1)[1]
            # Find matching model
            all_m = discover_all_models()
            matched = None
            for m in all_m:
                if name.lower() in m["id"].lower():
                    matched = m
                    break
            if matched:
                _switch_model(matched["id"], matched["url"])
            else:
                console.print(f"  [red]Model not found: {name}[/]")
            continue
        if task == "/paste":
            img = get_clipboard_image()
            if img:
                show_image_inline(img)
                console.print(
                    f"  [green]Clipboard image saved. Ask a question about it.[/]"
                )
                # Add as next user message with image reference
                task = (
                    input("  [dim]Question about image:[/] ").strip()
                    or "What is in this image?"
                )
                import base64 as b64mod

                img_b64 = b64mod.b64encode(open(img, "rb").read()).decode()
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": task},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}"
                                },
                            },
                        ],
                    }
                )
                console.print(_render_user_turn(task, note="attached clipboard image"))
                console.print()
                agent_loop(messages, perms)
                try:
                    with open(history_file, "w") as f:
                        json.dump(messages[-20:], f)
                except:
                    pass
                continue
            else:
                console.print(f"  [dim]No image in clipboard[/]")
                continue
        if task.startswith("/api"):
            parts = task.split(None, 1)
            if len(parts) == 2:
                API_BASE = parts[1]
                console.print(f"  [cyan]API: {API_BASE}[/]")
            else:
                console.print(f"  [cyan]Current: {API_BASE}[/]")
                console.print(f"  [dim]/api http://localhost:11434/v1  (Ollama)[/]")
                console.print(f"  [dim]/api http://localhost:8089/v1   (llama.cpp)[/]")
            continue
        if task == "/log":
            try:
                with open(log_file) as f:
                    lines = f.readlines()
                console.print(f"  [dim]{log_file} ({len(lines)} lines)[/]")
                for line in lines[-15:]:
                    console.print(f"  [dim]{line.rstrip()}[/]")
            except:
                console.print("  [dim]No log file[/]")
            continue
        if task.startswith("/voice-lang"):
            parts = task.split(None, 1)
            if len(parts) == 2:
                _voice_lang = parts[1].strip()
                _save_config(voice_language=_voice_lang)
                console.print(f"  [green]✓ Voice language: {_voice_lang}[/]")
            else:
                console.print(f"  [cyan]Current: {_voice_lang}[/]")
                console.print(f"  [dim]Usage: /voice-lang en|fr|ar|es|de|ja|zh|auto[/]")
            continue
        if task in ("/exit", "/quit"):
            break
        if task == "/deploy" or task.startswith("/deploy "):
            _handle_deploy(task, messages, perms, system, console)
            continue
        if task == "/handoff":
            _handle_handoff(messages, console)
            continue
        if task == "/mcp":
            mgr = get_mcp_manager()
            if not mgr.servers:
                console.print("  [dim]No MCP servers configured.[/]")
                console.print(f"  [dim]Add servers to ~/.localcoder/mcp.json[/]")
                console.print(f'  [dim]Example: {{"servers": {{"localfit-image": {{"command": "python3", "args": ["-m", "localfit.mcp_image"]}}}}}}[/]')
            else:
                for name, server in mgr.servers.items():
                    running = server.process and server.process.poll() is None
                    status = "[green]running[/]" if running else "[red]stopped[/]"
                    console.print(f"  [bold]{name}[/] {status}")
                    for tool_name, tool_def in server.tools.items():
                        desc = tool_def.get("description", "")[:60]
                        console.print(f"    [cyan]mcp__{name}__{tool_name}[/] — [dim]{desc}[/]")
            continue
        if task == "/sessions":
            sessions_list = list_sessions(limit=10)
            if sessions_list:
                for s in sessions_list:
                    console.print(
                        f"  [cyan]{s['id']}[/]  {s['messages']} msgs  "
                        f"[dim]{s['started']}  {s['model']}  {s['size_kb']}KB[/]"
                    )
            else:
                console.print("  [dim]No sessions yet[/]")
            continue

        # Handle clipboard image if pasted
        clip_img = _clipboard_image_path[0]
        if clip_img and os.path.isfile(clip_img):
            import base64 as b64mod, shutil

            # Save to a permanent file with timestamp
            ts = time.strftime("%Y%m%d_%H%M%S")
            saved_name = f".localcoder-image-{ts}.png"
            saved_path = os.path.join(CWD, saved_name)
            shutil.copy2(clip_img, saved_path)
            img_b64 = b64mod.b64encode(open(saved_path, "rb").read()).decode()
            sz_kb = os.path.getsize(saved_path) // 1024
            # Clean up the "[📎 image]" prefix from prompt
            task = task.replace("[📎 image]", "").strip() or "What is in this image?"
            console.print(
                _render_user_turn(task, note=f"attached {saved_name} ({sz_kb} KB)")
            )
            # Show image inline AFTER prompt (now we're in normal terminal mode)
            show_image_inline(saved_path)
            console.print(
                f"  [green]📎[/] [dim]{saved_name}[/] [dim green]({sz_kb} KB)[/]\n"
            )
            img_msg = {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{task}\n[Attached image: {saved_path}]",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                        },
                    ],
                }
            messages.append(img_msg)
            _session.add_message(img_msg)
        else:
            console.print(_render_user_turn(task))
            console.print()
            user_msg = {"role": "user", "content": task}
            messages.append(user_msg)
            _session.add_message(user_msg)

        try:
            tokens = agent_loop(messages, perms, session=_session)
            total_tokens += tokens
        except KeyboardInterrupt:
            console.print(f"\n  [bold yellow]⚡ Interrupted[/]")
            _session.add_event("interrupt")

        # Legacy save (for old -c continue fallback)
        try:
            safe = [m for m in messages if isinstance(m, dict)]
            with open(history_file, "w") as f:
                json.dump(safe[-20:], f)
        except:
            pass

    # ── Exit: offer memory cleanup ──
    _cleanup_on_exit()


def _handle_handoff(messages, console):
    """Generate a focused opening prompt for a new session.

    Sends the current conversation to the LLM with a structured handoff template,
    then copies the result to clipboard. Inspired by kon's /handoff command.
    """
    console.print("  [dim]Generating handoff prompt...[/]")

    # Build condensed conversation for the handoff call
    conv_parts = []
    files_seen = set()
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if role == "user" and content:
            conv_parts.append(f"User: {content[:300]}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                fname = tc.get("function", {}).get("name", "")
                try:
                    tc_args = json.loads(tc["function"].get("arguments", "{}"))
                except Exception:
                    tc_args = {}
                if fname in ("write_file", "edit_file", "read_file"):
                    files_seen.add(tc_args.get("path", ""))
            if content:
                conv_parts.append(f"Assistant: {content[:200]}")
        elif role == "tool" and content:
            conv_parts.append(f"Tool: {content[:100]}")

    conversation_text = "\n".join(conv_parts[-30:])  # Last 30 entries

    handoff_prompt = f"""Based on this conversation, generate a focused opening prompt for a NEW session.
The prompt should give the new session everything it needs to continue the work.

Use this format:
## Task
What needs to be done (1-2 sentences).

## Context
Key background (what was built, what approach was taken, what works).

## Relevant Files
{chr(10).join(f'- {f}' for f in sorted(files_seen) if f) or '(list the key files)'}

## Constraints
Any rules, preferences, or gotchas discovered.

## Next Steps
What specifically to do next (bullet list, be actionable).

Conversation:
{conversation_text[:6000]}"""

    try:
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "Generate a focused handoff prompt. Be specific and actionable."},
                {"role": "user", "content": handoff_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 1024,
            "stream": False,
        }
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{API_BASE}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            handoff = data["choices"][0]["message"]["content"].strip()

        # Copy to clipboard
        try:
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(handoff.encode())
            console.print(f"\n  [green]Handoff prompt copied to clipboard.[/]")
        except Exception:
            pass

        # Also display it
        console.print(Panel(
            Markdown(handoff),
            title="[bold cyan]Handoff Prompt[/]",
            border_style="cyan",
            padding=(1, 2),
            width=min(90, console.width - 4),
        ))
        console.print(f"  [dim]Paste this into a new session to continue.[/]\n")

    except Exception as e:
        console.print(f"  [red]Handoff failed: {e}[/]")


def _handle_deploy(task, messages, perms, system, console):
    """Build an AI app from the framework templates."""
    from rich.rule import Rule

    # Load framework apps
    framework_dir = os.path.join(os.path.dirname(__file__), "templates", "framework")
    build_module = os.path.join(framework_dir, "build.py")

    if not os.path.exists(build_module):
        console.print(f"  [red]Framework not found[/]")
        return

    # Import builder
    import importlib.util

    spec = importlib.util.spec_from_file_location("build", build_module)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    apps = builder.list_apps()
    if not apps:
        console.print(f"  [red]No app templates found[/]")
        return

    # Parse: /deploy or /deploy app-id or /deploy "description"
    parts = task.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else None

    # Direct app ID match
    if arg and any(a["id"] == arg for a in apps):
        selected = next(a for a in apps if a["id"] == arg)
    elif arg:
        # Fuzzy match or treat as custom description
        matched = [
            a
            for a in apps
            if arg.lower() in a["id"] or arg.lower() in a.get("title", "").lower()
        ]
        if matched:
            selected = matched[0]
        else:
            # Custom: use chatbot template with custom prompt
            selected = next((a for a in apps if a["id"] == "chatbot"), apps[0]).copy()
            selected["title"] = arg[:40]
            selected["subtitle"] = arg
            selected["system_prompt"] = (
                f"You are an AI expert for: {arg}. Help the user with detailed, accurate responses. Use emoji and structured formatting."
            )
    else:
        # Interactive picker
        console.print(f"\n  [bold #34d399]⚡ Deploy — AI App Framework[/]\n")
        for i, a in enumerate(apps, 1):
            inputs = ", ".join(a.get("inputs", []))
            model = a.get("model", "any")
            console.print(
                f"  [bold cyan]{i}[/]  {a['icon']}  {a['title']:<20} [dim]{inputs:<18} {model}[/]"
            )
        console.print(f"  [bold cyan]{len(apps) + 1}[/]  🛠️  Custom App")
        console.print()

        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        try:
            idx = int(choice) - 1
            if idx == len(apps):
                # Custom
                try:
                    desc = input("  Describe your app: ").strip()
                    if not desc:
                        return
                except (EOFError, KeyboardInterrupt):
                    return
                selected = next(
                    (a for a in apps if a["id"] == "chatbot"), apps[0]
                ).copy()
                selected["title"] = desc[:40]
                selected["subtitle"] = desc
                selected["system_prompt"] = (
                    f"You are an AI expert for: {desc}. Help the user. Use emoji and structured markdown."
                )
            elif 0 <= idx < len(apps):
                selected = apps[idx]
            else:
                return
        except ValueError:
            # Typed an app name
            matched = [
                a
                for a in apps
                if choice.lower() in a["id"]
                or choice.lower() in a.get("title", "").lower()
            ]
            selected = matched[0] if matched else apps[0]

    # App output directory
    default_name = selected["id"]
    try:
        app_name = input(f"  App name [{default_name}]: ").strip() or default_name
    except (EOFError, KeyboardInterrupt):
        return
    app_name = re.sub(r"[^a-z0-9-]", "-", app_name.lower())
    app_dir = os.path.join(CWD, app_name)

    console.print(f"\n  {selected['icon']}  [bold]{selected['title']}[/]")
    console.print(f"  [dim]{selected.get('subtitle', '')}[/]")
    console.print(
        f"  [dim]Inputs: {', '.join(selected.get('inputs', []))}  Model: {selected.get('model', 'any')}[/]"
    )
    console.print(Rule(style="dim"))

    # Build
    if os.path.exists(app_dir):
        try:
            ans = input(f"  {app_name}/ exists. Overwrite? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if ans not in ("y", "yes"):
            return
        import shutil

        shutil.rmtree(app_dir)

    console.print(f"  [dim]Building {app_name}...[/]")

    # Write custom config if modified
    app_config_dir = os.path.join(framework_dir, "apps", selected["id"])
    if selected.get("title") != next(
        (a["title"] for a in apps if a["id"] == selected["id"]), None
    ):
        # Custom app — write temp config
        import json as _json, tempfile

        tmp_app_dir = os.path.join(framework_dir, "apps", "_custom")
        os.makedirs(tmp_app_dir, exist_ok=True)
        with open(os.path.join(tmp_app_dir, "config.json"), "w") as f:
            _json.dump(selected, f, indent=2)
        try:
            builder.build_app("_custom", app_dir)
        finally:
            import shutil

            shutil.rmtree(tmp_app_dir, ignore_errors=True)
    else:
        builder.build_app(selected["id"], app_dir)

    file_count = sum(len(files) for _, _, files in os.walk(app_dir))
    console.print(f"  [green]✓[/] Created {file_count} files in {app_name}/")

    # npm install
    console.print(f"  [dim]Installing dependencies...[/]")
    try:
        r = subprocess.run(
            "npm install",
            shell=True,
            cwd=app_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            console.print(f"  [green]✓[/] Dependencies installed")
        else:
            console.print(f"  [yellow]npm install warnings (may still work)[/]")
    except Exception:
        console.print(f"  [yellow]npm install issue — run manually[/]")

    # Summary
    console.print(
        f"\n  [green bold]✓ {selected['icon']} {selected['title']} is ready![/]\n"
    )
    console.print(f"  [bold]Run:[/]     cd {app_name} && npm start")
    console.print(f"  [bold]Open:[/]    http://localhost:3000")
    console.print(f"\n  [bold]Switch AI provider:[/]")
    console.print(
        f"  [dim]Local:[/]    LLM_API_BASE=http://localhost:8089/v1 npm start"
    )
    console.print(
        f"  [dim]OpenAI:[/]   LLM_API_BASE=https://api.openai.com/v1 LLM_API_KEY=sk-... npm start"
    )
    console.print(
        f"  [dim]Gemini:[/]   LLM_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai LLM_API_KEY=... npm start"
    )
    console.print(
        f"  [dim]Groq:[/]     LLM_API_BASE=https://api.groq.com/openai/v1 LLM_API_KEY=... npm start"
    )

    # Start?
    console.print()
    try:
        ans = input("  Start now? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"

    if ans in ("y", "yes"):
        console.print(f"\n  [green]Starting on http://localhost:3000...[/]")
        console.print(f"  [dim]Ctrl+C to stop[/]\n")
        try:
            subprocess.run("node server.js", shell=True, cwd=app_dir)
        except KeyboardInterrupt:
            console.print(f"\n  [dim]Server stopped[/]")


def _cleanup_on_exit():
    """Ask user if they want to free GPU memory on exit."""
    console.print()

    # Check what's running
    llama_running = False
    ollama_models = []
    try:
        req = urllib.request.Request("http://127.0.0.1:8089/health")
        urllib.request.urlopen(req, timeout=1)
        llama_running = True
    except:
        pass

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/ps",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            ollama_models = [m.get("name", "") for m in data.get("models", [])]
    except:
        pass

    if not llama_running and not ollama_models:
        console.print(
            f"  [dim]{_t('no_models_loaded') or 'No models loaded in GPU. bye!'}[/]\n"
        )
        return

    # Show what's using GPU — radiolist dialog (matches /models style)
    _cleanup_title = _t("gpu_cleanup") or "GPU cleanup"
    _srv_info = ""
    if llama_running:
        _srv_info += "llama-server on :8089"
    if ollama_models:
        if _srv_info:
            _srv_info += "  ·  "
        _srv_info += f"Ollama: {', '.join(ollama_models)}"

    opt1 = _t("keep_running") or "keep running"
    opt2 = _t("unload_models") or "unload models"
    opt3 = _t("stop_all") or "stop all"

    try:
        from prompt_toolkit.shortcuts import radiolist_dialog
        from prompt_toolkit.styles import Style as PTStyle

        _cleanup_style = PTStyle.from_dict(
            {
                "dialog": "bg:#1a1a2e",
                "dialog.body": "bg:#1a1a2e #e0e0e0",
                "dialog frame.label": "bg:#e07a5f #ffffff bold",
                "dialog shadow": "bg:#000000",
                "radiolist": "bg:#1a1a2e",
                "button": "bg:#81b29a #000000 bold",
                "button.focused": "bg:#e07a5f #ffffff bold",
            }
        )

        result = radiolist_dialog(
            title=f"{_cleanup_title}  ·  {_srv_info}" if _srv_info else _cleanup_title,
            text=_ui(
                "What would you like to do?",
                "ماذا تريد أن تفعل؟",
                "Que souhaitez-vous faire ?",
            ),
            values=[
                ("1", f"  {opt1}"),
                ("2", f"  {opt2}"),
                ("3", f"  {opt3}"),
            ],
            style=_cleanup_style,
        ).run()
        ans = result or "1"
    except Exception:
        # Fallback to text input
        from rich import box as rbox

        body = (
            f"  [bold white on #1e293b] 1 [/] [#81b29a]{opt1}[/]\n"
            f"  [bold white on #1e293b] 2 [/] [#e07a5f]{opt2}[/]\n"
            f"  [bold white on #1e293b] 3 [/] [red]{opt3}[/]"
        )
        console.print(
            Panel(
                body,
                title=f"[bold #c084fc]{_cleanup_title}[/]",
                subtitle=f"[dim]{_srv_info}[/]" if _srv_info else None,
                border_style="#3f3f46",
                box=rbox.ROUNDED,
                padding=(1, 3),
                width=min(60, console.width - 4),
            )
        )
        try:
            ans = input("  ▸ ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            ans = "1"

    if ans == "2":
        # Unload Ollama models
        for m in ollama_models:
            try:
                data = json.dumps({"model": m, "keep_alive": 0}).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=5)
                console.print(f"    [green]✓[/] [dim]Unloaded {m}[/]")
            except:
                pass
        console.print(
            f"    [green]✓[/] [dim]{_t('ollama_unloaded') or 'Ollama models unloaded'}[/]"
        )

    elif ans == "3":
        # Kill llama-server
        if llama_running:
            subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
            console.print(
                f"    [green]✓[/] [dim]{_t('llama_stopped') or 'llama-server stopped'}[/]"
            )
        # Unload Ollama models
        for m in ollama_models:
            try:
                data = json.dumps({"model": m, "keep_alive": 0}).encode()
                req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=5)
            except:
                pass
        console.print(
            f"    [green]✓[/] [dim]{_t('models_unloaded') or 'All models unloaded, GPU memory freed'}[/]"
        )
    else:
        console.print(
            f"    [dim]{_t('keeping_loaded') or 'Keeping models loaded. bye!'}[/]"
        )

    console.print()


if __name__ == "__main__":
    main()
