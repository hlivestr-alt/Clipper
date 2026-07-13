# =============================================================================
#  PROYA CLIPPER — config.py
#  All tunable settings in one place. Edit this before running.
# =============================================================================

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR         = r"D:\output_clips"           # where finished clips go
WORKING_DIR        = "working"                 # temp files (transcripts, raw cuts)
YOLO_WEIGHTS       = "models/proya_best.pt"    # your trained YOLO weights
YOLO_PRETRAIN      = "yolov8n.pt"              # base model for training
DATASET_YAML       = "dataset/proya.yaml"      # YOLO dataset config
LOGO_PATH          = None                      # disable watermark for max throughput
RENDER_STYLE_VERSION = 6                       # bump when opening-hook/render styling changes

# Queue runner defaults. The PowerShell launchers and video_queue.py read these
# so routine queue tuning can live here instead of in long terminal commands.
QUEUE_INPUT_DIR = r"D:\VOD"
QUEUE_STATE_FILE = WORKING_DIR + r"\video_queue_state.json"
QUEUE_FOREVER_STATE_FILE = WORKING_DIR + r"\queue_forever_state.json"
QUEUE_CONTROL_FILE = WORKING_DIR + r"\queue_control.json"
QUEUE_START_RUN_NUMBER = 109
QUEUE_MAX_RETRIES = 2
QUEUE_MAX_INFLIGHT_VIDEOS = 1
QUEUE_FFMPEG_MAX_PARALLEL_CLIPS = 4
QUEUE_STAGE_ADMISSION_LIMIT = 3
MAX_QUEUE_SIZE = 10
QUEUE_YOLO_IN_SUBPROCESS = True
QUEUE_POLL_INTERVAL = 2.0
QUEUE_RESCAN_INTERVAL_SECONDS = 300.0
QUEUE_SCAN_INTERVAL_SECONDS = QUEUE_RESCAN_INTERVAL_SECONDS
QUEUE_STABLE_SECONDS = 60.0
QUEUE_RESTART_DELAY_SECONDS = 30
QUEUE_BETWEEN_RUNS_DELAY_SECONDS = 10
QUEUE_STUCK_THRESHOLD = 30 * 60
QUEUE_DASHBOARD_RUNNING_STALL_SECONDS = 2 * 60 * 60
QUEUE_DASHBOARD_QUEUED_STALL_SECONDS = 24 * 60 * 60

# ── Before/After Image Overlay ────────────────────────────────────────────────
# Drop your before/after result photos into this folder.
# The pipeline will randomly pick one and show it in the middle of the screen
# at the start of each clip (after the hook text fades out).
BEFORE_AFTER_DIR        = "assets/before_after"   # folder with your images
BEFORE_AFTER_ENABLED    = True                    # set False to disable globally
BEFORE_AFTER_START_T    = 0        # seconds — when image appears (after hook)
BEFORE_AFTER_START_OFFSET = 3.0    # minimum seconds before proof overlay appears
BEFORE_AFTER_DURATION   = 2.5        # seconds the image is shown
BEFORE_AFTER_OPACITY    = 1.0       # 0.0 = invisible, 1.0 = fully opaque
BEFORE_AFTER_FADE_IN    = 0.00       # seconds to fade in
BEFORE_AFTER_FADE_OUT   = 0.25       # seconds to fade out
# Label shown above the image — set to None to disable

# ── Word Correction / Brand Name Normalization ────────────────────────────────
# Whisper sometimes mishears brand/product names. This dictionary maps
# wrong transcriptions → correct versions. Case-insensitive matching.
# Add as many entries as you discover from your transcripts.
WORD_CORRECTIONS = {
    # Brand name variants
    "peroya"         : "PROYA",
    "perroya"        : "PROYA",
    "proya"          : "PROYA",    # lowercase → uppercase
    "proja"          : "PROYA",
    "proyya"         : "PROYA",
    "pro ya"         : "PROYA",
    "proiya"         : "PROYA",
    "proyah"         : "PROYA",
    "proya5x"        : "PROYA 5X",
    "proyafaifeks"   : "PROYA 5X",
    "five x"         : "5X",
    "faif eks"       : "5X",
    "Froyo"          : "PROYA",
    "proyo"          : "PROYA",
    "Froya"          : "PROYA",

    # Product names
    "sером"          : "serum",
    "Cicero"         : "serum",
    "serum vitamin"  : "serum Vitamin C",
    "vit c"          : "Vitamin C",
    "vitamin si"     : "Vitamin C",
    "moisurizer"     : "moisturizer",
    "moisturaizer"   : "moisturizer",
    "tóner"          : "toner",
    "tooner"         : "toner",
    "klenser"        : "cleanser",
    "eye krim"       : "eye cream",
    "ai krim"        : "eye cream",
    "sheetmask"      : "sheet mask",
    "sit mask"       : "sheet mask",
    "sitmes"         : "sheet mask",
    "dari  vetif"    : "derivative",
    "plan"           : "plant",
    "patipus"        : "derivatives",
    "tetamik"        : "tranexamic",
    "asid"           : "acid",
    "alparbutin"     : "alpha arbutin",
    "dari patipe"    : "derivatives",
    "bestibestiku"   : "bestie bestieku",
    "bakas"          : "bekas",
    "kelencernya"    : "cleanser", 
    "tritamic"       : "tranexamic",
    "karnoset"       : "carnosine",
    "antiglationnya" : "Anti-glycation",

    # Common Indonesian skincare terms Whisper mishears
    "skinker"        : "skincare",
    "skin ker"       : "skincare",
    "skin care"      : "skincare",
    "skin care-nya"  : "skincarenya",
    "glowing"        : "glowing",   # keep correct spellings too (force casing)
    "etal asunya"    : "etalasenya",
    "main-komen"     : "mau komen",
    "Tev"            : "tap",
    "KKA"            : "kakak",
    "welkom-welkom"  : "welcome-welcome",
    "mehol"          : "mehong",
    "sepilih"        : "spill",
    "etal-ase"       : "etalase",
    "menemuin"       : "nemenin",
    "developnya"     : "tap-tap lovenya",
    "disepilin"      : "di spill-in",
    "di skon"        : "diskon",
    "nge-sepilihnya" : "nge-spillnya",
    "Eta laksan"     : "etalase",
    "di skor"        : "diskon",
    "tanggerang"     : "Tangerang",
    "permasaan"      : "permasalahan",
    "pemenya"        : "nyampenya",
    "sepilihnya"     : "spillnya",
    "setelah senomor": "etalase nomer",
    "kesampainya"    : "sampainya",
    "memakinya"      : "packingnya",
    "cekol"          : "checkout",
    "tarini"         : "hari ini",
    "Membung"        : "mumpung",
    "lipenya"        : "livenya",
    "tevlovnya"      : "tap tap lovenya",
    "etal asenya"    : "etalasenya",
    "Etal asen"      : "etalase",
    "kulitu"         : "kulit",
    "kotaan"         : "kotoran",
    "visi"           : "fisik",
    "teratasih"      : "teratasi",
    "Meniyan"        : "mendingan",
    "de tu"          : "dia itu",
    "debua"          : "debu",
    "mainin"         : "komenin",
    "ditanyatannya"  : "ditanya tanya",
    "etal losen"     : "etalase",
    "omor"           : "nomor",
    "bebek"          : "bebep",
    "Lerma"          : "remaja",
    "permasang"      : "permasalahan",
    "pelek"          : "flek",
    "mencarakan"     : "mencerahkan",
    "targainya"      : "harganya",
    "Meningin"       : "mendingan",
    "Wartit"         : "worth it",
    "cekotin"        : "checkout",
    "sepel-sepel"    : "spill spill",
    "menganuh"       : "mengandung",
    "menyewimbangkan": "menyeimbangkan",
    "wakam-wakam"    : "welcome-welcome",
    "bermingat"      : "berminyak",
    "dehydrasi"      : "dehidrasi",
    "ibadat"         : "ibarat",
    "mencerakan"     : "mencerahkan",
    "jerout"         : "jerawat",
    "diya"           : "dia",
    "funcinya"       : "fungsinya",
    "mantulita"      : "mantul",
    "eh atas"        : "etalase",
    "atas-atas"      : "etalase",
    "kulitulah"      : "kulit",
    "kulitulah saya" : "kulit wajah",
    "meradakan"      : "meredahkan",
    "kayaknya"       : "ya kak ya",
    "melebabkan"     : "melembapkan",
    "nodah-nodah"    : "noda-noda",
    "salah se-nomor" : "etalase nomor",
    "sekali gust"    : "sekaligus",
    "kawatiang"      : "khawatir",
    "lihat laksana uang"  : "di etalase",
    "merdakan"       : "meredahkan",
    "plek"           : "flek",
    "benarbenar"     : "bener bener",
    "mencerakannya"  : "mencerahkannya",
    "seat"           : "sheet",
    "wetening"       : "whitening",
    "ethalase"       : "etalase",
    "lesson"         : "etalase",
    "eh selesai"     : "etalase",
    "atas laksan"    : "etalase",
    "benerbener"     : "bener bener",
    "meletelasin"    : "etalase",
    "kejanya"        : "wajahnya",
    "tuajanya"       : "wajahnya",
    "rebakas"        : "ada bekas",
    "keringi"        : "kering",
    "telah senomor"  : "etalase nomor",
    "black"          : "flek",
    "ceritmen"       : "treatment",
    "ethelosan"      : "etalase",
    "pembersi"       : "pembersih",
    "tablognya"      : "tap lovenya",
    "atalse"         : "etalase",
    "bekasbekas"     : "bekas bekas",
    "kongkia"        : "ongkir",
    "etalose"        : "etalase",
    "dari vatipus"   : "derivatives",
    "aldochronic"    : "Hyaluronic",
    "FLAGFLAG"       : "flek fleg",
    "denny"          : "dia ini",
    "disconok"       : "diskon",
    "deriva tipes"   : "derivatives",
    "tretamik"       : "tranexamic",
    "pandang"        : "panda",
    "derivatif"      : "derivatives",
    "trexamide"      : "tranexamic",
    "exit"           : "acid",
    "asitanya"       : "acid",
    "seritmennnya"   : "treatmentnya",

 }
# Apply corrections to subtitle text displayed on clips (in addition to transcript)
WORD_CORRECTION_APPLY_TO_SUBTITLES = True

# ── LM Studio ─────────────────────────────────────────────────────────────────
# LM Studio → Local Server → must be running before you start the pipeline
LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
LM_STUDIO_API_KEY  = "lm-studio"               # LM Studio accepts any non-empty string
LM_STUDIO_MOMENT_MODEL_ID = "qwen/qwen3.6-27b"
LM_STUDIO_MODEL    = LM_STUDIO_MOMENT_MODEL_ID          # match the model name shown in LM Studio
LM_STUDIO_TIMEOUT  = 360                       # seconds per request
LM_STUDIO_TEMPERATURE = 0.2                    # keep Qwen text calls stable without reverting to UI defaults
LM_STUDIO_QWEN_THINKING_ENABLED = False        # passed as enable_thinking=false for Qwen3.x chat templates
LM_STUDIO_MODEL_MANAGEMENT_ENABLED = True
LM_STUDIO_MODEL_UNLOAD_TIMEOUT = 600           # Qwen 3.6 can take minutes to fully free VRAM
LM_STUDIO_MODEL_UNLOAD_LOG_INTERVAL = 30       # log wait progress so unloads do not look frozen
MOMENT_DETECTOR_WORKERS = 2                    # limited parallel LM Studio calls

# ── Whisper ───────────────────────────────────────────────────────────────────
WHISPER_MODEL_SIZE = "large-v3-turbo"                # prioritize transcription quality on high-end GPU
WHISPER_DEVICE     = "cuda"                    # use RTX GPU
WHISPER_COMPUTE    = "float16"                 # fast GPU inference
WHISPER_BEAM_SIZE  = 5                         # broader search improves tricky words/prices
WHISPER_BEST_OF    = 5                         # keep multiple candidates before final decode
WHISPER_LANGUAGE   = "id"                      # Indonesian language code

# Word-level subtitle timing backend.
# "whisperx" is recommended for karaoke because it force-aligns words to audio.
WORD_ALIGNMENT_BACKEND      = "whisperx"
WHISPERX_DEVICE             = WHISPER_DEVICE
WHISPERX_ALIGN_MODEL        = None             # auto-pick based on language
WHISPERX_INTERPOLATE_METHOD = "nearest"
WHISPERX_MODEL_DIR          = None
WHISPERX_MAX_SEGMENT_SECONDS = 30              # cap Wav2Vec2 alignment windows to avoid CUDA OOM
WHISPERX_ALIGN_IN_SUBPROCESS = True            # protect saved raw transcript if WhisperX crashes natively
WHISPERX_FALLBACK_TO_RAW_ON_OOM = True         # keep queue moving if WhisperX still runs out of VRAM
WHISPERX_FALLBACK_TO_RAW_ON_ALIGNMENT_CRASH = True
WHISPERX_ACCEPT_RAW_FALLBACK_CACHE = True      # reuse fallback transcripts instead of retrying WhisperX forever

# ── YOLO / Vision ─────────────────────────────────────────────────────────────
YOLO_CONF_THRESHOLD = 0.55                     # detection confidence cutoff
YOLO_FRAME_SKIP     = 24                       # scan fewer frames for faster throughput
YOLO_DEVICE         = "0"                      # use first NVIDIA GPU
YOLO_IMGSZ          = 416                      # smaller input for faster inference
YOLO_HALF           = True                     # fp16 inference on GPU
YOLO_BATCH_SIZE     = 32                       # cap YOLO inference batches to avoid RAM spikes
YOLO_SCAN_ONLY_MOMENTS   = True                # scan only candidate clip windows, not the full VOD
YOLO_SCAN_PAD_BEFORE     = 3.0                 # extra seconds before each moment when scanning
YOLO_SCAN_PAD_AFTER      = 3.0                 # extra seconds after each moment when scanning
YOLO_SCAN_RANGE_MERGE_GAP = 4.0                # merge nearby scan windows into one range

# Region of Interest — only look inside this zone (as fraction of frame size).
# Tune this to where the presenter typically holds products.
# (0,0) = top-left corner, (1,1) = bottom-right corner
ROI = {
    "x1": 0.0,   # kiri (0%)
    "y1": 0.0,   # atas (0%)
    "x2": 1.0,   # kanan (100%)
    "y2": 0.6    # 60% dari atas
}

# How long (seconds) to sustain zoom after product detected
ZOOM_DURATION  = 3.0    # total zoom window (ease-in + hold + ease-out)
ZOOM_SCALE     = 1.45   # 1.45 = 45% zoom in — tight enough to see product clearly

# ── Clip Detection ────────────────────────────────────────────────────────────
# Effective selection defaults are defined in the quality-first section below.

# ── Fonts ─────────────────────────────────────────────────────────────────────
# ImageMagick font names. Run `convert -list font | grep -i name` to see what's
# installed on your system. Install Bebas Neue, Anton, Montserrat via:
#   Windows: download TTF → right-click → Install for All Users
#   Linux:   sudo cp *.ttf /usr/local/share/fonts && sudo fc-cache -fv
#
# If a font isn't found, MoviePy falls back to the system default.
# Safe cross-platform fallback names are shown in comments.

FONT_HOOK       = "assets/fonts/Poppins-Bold.ttf"               # bold readable hook title
FONT_HOOK_FALLBACKS = ["assets/fonts/Montserrat-ExtraBold.ttf", "assets/fonts/Anton-Regular.ttf"]
FONT_LABEL      = "assets/fonts/Montserrat-SemiBold.ttf"  # or "Poppins-Medium", "Arial-Bold"
FONT_SUBTITLE   = "assets/fonts/Montserrat-ExtraBold.ttf"    # or "Arial-Bold"
FONT_PRODUCT    = "assets/fonts/PlayfairDisplay-Italic-VariableFont_wght.ttf"  # for zoom caption
SUBTITLE_FONT_RANDOMIZE = False
SUBTITLE_FONT_DIR = "assets/fonts/subtitle"

# ── Hook / Title overlay ──────────────────────────────────────────────────────
HOOK_FONTSIZE       = 150           # 110–150 range; scales with text length
HOOK_COLOR          = "white"       # "white" | "yellow" — alternate per vibe
HOOK_STROKE_COLOR   = "black"
HOOK_STROKE_W       = 5            # 4–6px thick for TikTok style
HOOK_DURATION       = 2.5          # show hook title briefly at the start
# Background is auto-height (fits text exactly + padding)

# ── End-card CTA ──────────────────────────────────────────────────────────────
CTA_ENDCARD_ENABLED = True
CTA_ENDCARD_DURATION = 2.0
CTA_ENDCARD_DEFAULT_TEXT = "CEK ETALASE SEKARANG"

# ── Subtitles ─────────────────────────────────────────────────────────────────
SUBTITLE_FONTSIZE   = 120           # 60–80 range
SUBTITLE_STROKE     = "#000000"
SUBTITLE_STROKE_W   = 3            # 2–4px
SUBTITLE_Y_POS      = 0.80        # vertical position (fraction of frame height)
SUBTITLE_SAFE_ZONE_TOP = 0.08
SUBTITLE_SAFE_ZONE_BOTTOM = 0.15

# PNG emoji overlays triggered by subtitle keywords.
# Position is randomized per subtitle chunk at render time.
EMOJI_CONFIG = {
    "fade_in": 0.2,
    "emoji_rules": [
        {
            "keywords": ["mencerahkan", "brightening", "glow", "vitamin", "cerah", "glowing"],
            "png_path": "assets/emojis/sun.png",
            "scale": 0.4,
            # "offset_x": 10,
           # "offset_y": -10,
        },
        {
            "keywords": ["jerawat", "acne", "flek", "flek hitam", "kemerahan"],
            "png_path": "assets/emojis/scared.png",
            "scale": 0.4,
        },
        {
            "keywords": ["eye", "eye cream"],
            "png_path": "assets/emojis/eye.png",
            "scale": 0.4,
        },
                {
            "keywords": ["mata", "mata panda", "panda"],
            "png_path": "assets/emojis/panda.png",
            "scale": 0.4,
        },
    ],
}

# ── Subtitle keyword highlight colours ───────────────────────────────────────
# Phrases are stored in HIGHLIGHT_PHRASES_PATH and matched case-insensitively.
# Edit highlight_phrases.json directly for curated phrase/category changes.

# Persistent registry path + category colors
HIGHLIGHT_PHRASES_PATH = "highlight_phrases.json"
HIGHLIGHT_YELLOW_COLOR = "#FFD600"
HIGHLIGHT_GREEN_COLOR = "#00C853"
HIGHLIGHT_RED_COLOR = "#FF3B30"

# ── Products ──────────────────────────────────────────────────────────────────
PRODUCT_CLASSES = {
    0: "Cleanser",
    1: "Eye Cream",
    2: "host_face",
    3: "Serum",
    4: "skin cream",
    5: "Toner",
}
BRAND_NAME = "PROYA 5X Vitamin C"

# ── Host Face Zoom ─────────────────────────────────────────────────────────────
# Add "host_face" as a class in your YOLO dataset (class index 6 or whatever
# comes next). The pipeline will zoom into the face every 4-5 words automatically.
HOST_FACE_CLASS        = "host_face"   # must match the class_name in your YOLO labels
HOST_FACE_ZOOM_ENABLED = True          # set False to disable all face zooms

# How many spoken words between each face zoom trigger (cycles through this list)
FACE_ZOOM_WORDS_TRIGGER = [4, 4, 5, 5, 4, 5]

# Zoom scale range — each face zoom picks a random value in this range
FACE_ZOOM_SCALE_MIN    = 1.25     # 1.25 = 25% zoom in (subtle)
FACE_ZOOM_SCALE_MAX    = 1.55     # 1.55 = 55% zoom in (punchy)

# Ease-in speed — lower = faster snap in, higher = floaty
FACE_ZOOM_EASE_MIN     = 0.0     # seconds (very snappy)
FACE_ZOOM_EASE_MAX     = 0.0     # seconds (smooth)

# How long to hold the face zoom before hard-cutting back
FACE_ZOOM_DUR_MIN      = 1.5      # seconds
FACE_ZOOM_DUR_MAX      = 2.5      # seconds

# Where the face lands vertically on screen after zoom (0=top, 1=bottom)
# 0.30 = face appears in the top 30% of the frame — typical TikTok talking-head
FACE_ZOOM_SCREEN_Y     = 0.10

# How far (seconds) to search around the trigger word for a YOLO face detection
FACE_ZOOM_SEARCH_WINDOW = 3.0

# Minimum gap (seconds) between consecutive face zooms
FACE_ZOOM_MIN_GAP      = 1.0

# ── Karaoke Subtitle Style ────────────────────────────────────────────────────
# Active word highlight colour (used when word is not a semantic keyword)
KARAOKE_ACTIVE_COLOR     = "#FFD600"   # TikTok yellow
KARAOKE_INACTIVE_OPACITY = 1.0       # 0.0=invisible, 1.0=same as active; try 0.3–0.5

# ── Product Zoom Caption ──────────────────────────────────────────────────────
ZOOM_CAPTION_TEXT_COLOR     = "white"      # product name
ZOOM_CAPTION_BRAND_COLOR    = "#FFD600"    # "PROYA 5X VITAMIN C" line
ZOOM_CAPTION_STROKE_COLOR   = "black"      # outline on both lines
ZOOM_CAPTION_STROKE_WIDTH   = 4
ZOOM_CAPTION_FONTSIZE       = 120    # ← product name (e.g. "SERUM") — increase for bigger
ZOOM_CAPTION_BRAND_FONTSIZE = 0    # ← brand line ("PROYA 5X VITAMIN C") — increase for bigger
ZOOM_CAPTION_Y_POS          = 0.10  # top-center caption vertical position as fraction of frame height

# ── Output Video ──────────────────────────────────────────────────────────────

# ── SFX (Sound Effects) ───────────────────────────────────────────────────────
# Drop audio files (.wav / .mp3) into the folders below.
# The pipeline picks one randomly per trigger event.
#
# Folder layout:
#   assets/sfx/
#     product_zoom/       ← plays when product zoom triggers (whoosh, ding, etc.)
#     highlight_yellow/   ← Attention / Benefits words (sparkle, chime, etc.)
#     highlight_green/    ← Results / Speed / Proof words (success, pop, etc.)
#     highlight_red/      ← Pain / Problem words (thud, bass hit, etc.)
#
# Run  python main.py --setup-sfx  to create the folders and see their status.

SFX_ENABLED            = True
SFX_DIR                = "assets/sfx"          # base folder
SFX_PRODUCT_FOLDER     = "product_zoom"
SFX_YELLOW_FOLDER      = "highlight_yellow"
SFX_GREEN_FOLDER       = "highlight_green"
SFX_RED_FOLDER         = "highlight_red"

# Volume multiplier per category (1.0 = original file volume)
SFX_VOLUME_PRODUCT     = 0.15    # product zoom whoosh
SFX_VOLUME_YELLOW      = 0.10    # attention / benefit words
SFX_VOLUME_GREEN       = 0.10    # result / proof words
SFX_VOLUME_RED         = 0.10    # pain / problem words

# Minimum seconds between SFX of the same highlight category
# (prevents rapid-fire SFX when multiple keywords appear back-to-back)
# Highlight SFX cadence in karaoke subtitle blocks.
# 2 means: trigger on a highlighted block, skip the next block, then allow again.
SFX_HIGHLIGHT_BLOCK_INTERVAL = 2

# ── BGM (Background Music) ───────────────────────────────────────────────────
# Drop music beds into assets/bgm/. If the folder is empty/missing, rendering
# continues with the original voice/SFX audio only.
BGM_ENABLED              = True
BGM_DIR                  = "assets/bgm"
BGM_VOLUME               = 0.08    # 10-15% is the sweet spot under livestream voice
BGM_DUCKING_ENABLED      = True    # lower BGM automatically when speech/SFX is present
BGM_DUCKING_THRESHOLD    = 0.03
BGM_DUCKING_RATIO        = 8.0
BGM_DUCKING_ATTACK_MS    = 50
BGM_DUCKING_RELEASE_MS   = 350

# Quality-first overrides for product-selling clips.
# Stricter values reduce random cuts, silent clips, and weak filler moments.
CHUNK_DURATION = 300
CHUNK_OVERLAP = 45
MIN_CLIP_DURATION = 25
MAX_CLIP_DURATION = 60
MIN_SCORE = 7.0
PAD_START = 0.5
PAD_END = 0.75
MIN_CLIP_WORDS = 18
MIN_SPEECH_WORDS_PER_SECOND = 0.75
MAX_CLIP_SEGMENT_GAP = 4.0
# HOOK_DURATION = 0.0
# HOST_FACE_ZOOM_ENABLED = False
DRAFT_MODE = False
OUTPUT_FPS = 30
OUTPUT_CODEC = "h264_nvenc"
OUTPUT_NVENC_PRESET = "p1" if DRAFT_MODE else "p4"
OUTPUT_PRESET = OUTPUT_NVENC_PRESET
OUTPUT_CRF = 35
OUTPUT_CQ = 35 if DRAFT_MODE else 26
OUTPUT_AUDIO_BITRATE = "96k" if DRAFT_MODE else "128k"
MAX_PARALLEL_CLIPS = 6
EDIT_LOG_EVERY_N = 25
EDIT_LOG_CLIP_PLAN = False
EDIT_LOG_CREATED_CLIPS = True
LOG_FFMPEG_FILTER_COMPLEX = False
RAW_CUT_CODEC   = "libx264"   # CPU — fast enough, no NVENC slot used
RAW_CUT_PRESET  = "ultrafast"

# Moderate dead-air compaction during Stage 4 rendering.
SILENCE_TRIM_ENABLED = True
SILENCE_TRIM_MIN_GAP = 1.2
SILENCE_TRIM_KEEP_GAP = 0.35
SILENCE_TRIM_EDGE_KEEP = 0.25
SILENCE_TRIM_MAX_REMOVAL_FRACTION = 0.45
SILENCE_TRIM_MIN_WORDS = 6

# Automated post-render clip scoring.
SCORER_ENABLED = True
SCORER_FRAME_SAMPLE_RATE = 10
SCORER_MIN_SCORE_TO_EXPORT = 0.0
SCORER_WEIGHTS = {"content": 0.466667, "quality": 0.2, "engagement": 0.333333}
SCORER_HOST_FOCUS_WEIGHT = 0.0
SCORER_APPLY_CAPS = True
SCORER_CACHE_ENABLED = True
SCORER_FORCE_RESCORE = False
SCORER_EXPORT_READY_THRESHOLD = 7.0
SCORER_REVIEW_THRESHOLD = 5.0
SCORER_AUTO_SORT_ENABLED = True
SCORER_TOP_VARIANTS_PER_CLIP = 0
SCORER_VISION_ENABLED = False
SCORER_VISION_FRAME_SAMPLE_RATE = 150
SCORER_VISION_BASE_URL = "http://localhost:1234/v1"
SCORER_VISION_API_KEY = "lm-studio"
SCORER_VISION_MODEL_ID = "qwen2.5-vl-32b-instruct"
SCORER_VISION_MODEL = SCORER_VISION_MODEL_ID
SCORER_VISION_TIMEOUT = 600
SCORER_VISION_DEBUG = False
SCORER_VISION_CONTACT_SHEET = True
SCORER_VISION_CONTACT_SHEET_MAX_FRAMES = 6
SCORER_VISION_CONTACT_SHEET_CELL_SIZE = 384
SCORER_FOCUS_DROP_OUTLIERS = True
SCORER_FOCUS_SKIP_FIRST_FRAME = True
SCORER_BATCH_FLUSH_EVERY = 5
SCORER_SIMILARITY_FRAME_SAMPLE_RATE = 30
SCORER_SIMILARITY_MAX_FRAMES = 24

# Affiliate handoff packaging. Export-ready clips from all VOD output folders
# are moved into numbered batch folders with at most 15 videos each.
EXPORT_BATCHES_ENABLED = True
EXPORT_BATCH_DIR_NAME = "export_batches"
EXPORT_BATCH_SIZE = 15
# Default layout: one export-ready variant per base clip, rotated by persisted
# VOD order and clip number into folders containing at most 15 VODs.
EXPORT_BATCH_STRATEGY = "vod_clip_variant_rotation"
EXPORT_BATCH_VARIANT_COUNT = 6
# Used only by the legacy score_round_robin_all_variants strategy.
EXPORT_PACK_ONE_VARIANT_PER_CLIP = False

# Pre-subtitle advertising compliance checks for Indonesian skincare claims.
COMPLIANCE_ENABLED = True
COMPLIANCE_AUTO_FIX = True
COMPLIANCE_BLOCK_HIGH = True
COMPLIANCE_LM_TIMEOUT = 60

# Modular raw clip library.
# Extraction is transcript-only in v1; product zoom remains a documented
# placeholder for future YOLO-backed modular rendering.
MODULE_LIBRARY_DIR = r"D:\proya_modules"
MODULE_EXTRACTION_ENABLED = False
MODULE_DURATION_STRICT = False
MODULE_HOOK_MIN_DURATION = 4.0
MODULE_HOOK_MAX_DURATION = 8.0
MODULE_MAIN_MIN_DURATION = 15.0
MODULE_MAIN_MAX_DURATION = 45.0
MODULE_CTA_MIN_DURATION = 4.0
MODULE_CTA_MAX_DURATION = 12.0
MODULE_SENTENCE_BOUNDARY_TOLERANCE = 2.0
MODULE_ASSEMBLY_ENABLED = False
MODULE_ASSEMBLY_RENDER_LIMIT = 3
MODULE_ASSEMBLY_CANDIDATE_POOL = 30
MODULE_ASSEMBLY_MAX_PER_PRODUCT = 1
MODULE_ASSEMBLY_COMPLIANCE_PREFILTER = True
MODULE_ASSEMBLY_SAFE_HOOKS_ENABLED = True
MODULE_ASSEMBLY_SAME_DATE_ONLY = True
MODULE_ASSEMBLY_VISUAL_EVENT_BONUS = 0.75
MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS = 1
MODULE_ASSEMBLY_REQUIRE_ZOOM_READY = False
MODULE_REBUILD_INDEX_BEFORE_ASSEMBLY = True
MODULE_OUTPUT_LOCK_TIMEOUT = 30.0
MODULE_EXTRACT_FFMPEG_TIMEOUT = 300
MODULE_DEDUPE_IOU_THRESHOLD = 0.5
MODULE_PRODUCT_ZOOM_ENABLED = False
# Runs YOLO during module extraction; keep opt-in so routine extraction does not contend for CUDA.
MODULE_VALIDATE_ON_EXTRACT = False
MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE = 0.55
MODULE_VISUAL_VALIDATION_SAMPLE_FPS = 1.0
MODULE_VISUAL_VALIDATION_MIN_HITS = 1
MODULE_WORD_FALLBACK_REVIEW_REQUIRED = True
MODULE_ASSEMBLY_REQUIRE_APPROVED = True
MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 2
MODULAR_ASSEMBLY_READY_MIN_HOOK = 5
MODULAR_ASSEMBLY_READY_MIN_MAIN = 3
MODULAR_ASSEMBLY_READY_MIN_CTA = 3
MODULE_PRODUCT_EVIDENCE_REQUIRED = True
MODULE_PRODUCT_EVIDENCE_CONTEXT_SECONDS = 12.0
MODULE_CLASSIFICATION_MIN_CONFIDENCE = 0.6
MODULE_CLASSIFIER_WORKERS = 1
MODULE_CANDIDATE_CACHE_ENABLED = True
MODULE_MAX_CANDIDATES_PER_ROLE = 0
MODULE_INDEX_LOCK_TIMEOUT = 30.0
MODULE_FILE_LOCK_TIMEOUT = 30.0
MODULE_INDEX_VALIDATE_MEDIA = True
MODULE_INDEX_REPROBE_MEDIA = False
MODULE_REPORT_LOAD_SIDECARS = False

# ── Variation Engine ──────────────────────────────────────────────────────────
# How many style variants to render per detected moment.
#   1  = original only (no variation, current behaviour)
#   6  = 6× output  — good starting point
#   12 = 12× output — for 8–18k/day target across multiple VODs
VARIANTS_PER_CLIP = 6

# Deterministic seed. Change to get a different style mix across runs.
VARIANT_SEED = 42

# Bake mirror/speed/grade/crop into the FFmpeg raw-cut step (recommended).
# True = fastest (pure FFmpeg, GPU-accelerated). False = MoviePy (slower).
VARIANT_FFMPEG_BAKE = True

# Optional local B-roll intro variants.
# Drop short vertical/horizontal intro videos into assets/broll_intro/.
# When files exist, a deterministic 20-40% of generated variants use B-roll
# behind the hook text instead of the before/after image.
BROLL_INTRO_ENABLED = True
BROLL_INTRO_DIR = "assets/broll_intro"
BROLL_INTRO_MIN_VARIANT_RATE = 0.50
BROLL_INTRO_MAX_VARIANT_RATE = 0.50
BROLL_INTRO_APPLY_TO_ORIGINAL = False
BROLL_INTRO_MAX_DURATION = 2.5
BROLL_INTRO_FADE_IN = 0.0
BROLL_INTRO_FADE_OUT = 0.20
BROLL_INTRO_REQUIRE_PRODUCT_MATCH = True
BROLL_INTRO_ALLOW_GENERIC_ROOT = False
BROLL_INTRO_PRODUCT_ALIASES = {
    "Cleanser": ["cleanser", "face wash", "sabun muka"],
    "Eye Cream": ["eye cream", "eyecream", "krim mata"],
    "Mask": ["mask", "masker"],
    "Serum": ["serum"],
    "Skin Cream": ["skin cream", "cream", "moisturizer", "moisturiser", "krim"],
    "Toner": ["toner"],
}

# Full-clip product B-roll visual replacement.
# Each supported product has a child folder under this root.
PRODUCT_BROLL_DIR = "assets/product_broll"
PRODUCT_BROLL_CROSSFADE_SECONDS = 0.3
PRODUCT_BROLL_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}

# Optional transitional hook pre-roll variants.
# Drop viral hook videos into assets/transitional_hooks/. When a variant uses
# Hook type "Transitional Hook", one full video is prepended before the clip.
TRANSITIONAL_HOOK_ENABLED = True
TRANSITIONAL_HOOK_DIR = "assets/transitional_hooks"
